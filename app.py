import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
import time  
import json  

from flask import Flask, request, jsonify
import logging

# --- API ---
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
app = Flask(__name__)

# --- DEFAULT SETTINGS ---
LEVERAGE = 20
TRADE_SIZE = 1250
TP_PERCENT = 1
SL_PERCENT = 20

DCA_COUNT = 3
DCA_DEVIATION_PERCENT = 5
DCA_VOLUME_MULTIPLIER = 2

active_dca = {}
dca_lock = threading.Lock()

# ---------------- SIGNING ----------------

def sign_bingx(params):
    # Generiert die Signatur basierend auf den URL-kodierten Parametern
    query_string = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()

# ---------------- API HELPERS (NEU & Korrigiert) ----------------

def api_request(method, endpoint, params=None, headers=None):
    url = f"{BINGX_BASE}{endpoint}"
    
    # NEU: Generiere die Signatur hier und füge sie den Headern hinzu, falls API Key vorhanden
    request_headers = headers.copy() if headers else {}
    if API_KEY:
        request_headers["X-BX-APIKEY"] = API_KEY
        if params and method == 'POST':
             # Signatur für POST-Anfragen basierend auf den Parametern generieren
             request_headers["X-BX-SIGNATURE"] = sign_bingx(params)

    try:
        if method == 'GET':
            # Bei GET die Signatur als Query-Parameter hinzufügen
            if params and "signature" not in params:
                 params["signature"] = sign_bingx(params)

            response = requests.get(url, params=params, headers=request_headers, timeout=10)
        
        elif method == 'POST':
            # Bei POST die Parameter als URL-kodierten Body senden (Standardschema von BingX)
            response = requests.post(url, data=urllib.parse.urlencode(params), headers=request_headers, timeout=10)
        
        response.raise_for_status() 
        return response.json()
    
    except requests.exceptions.RequestException as e:
        print(f"[API ERROR] Method: {method}, URL: {url}, Error: {e}")
        if hasattr(response, 'text'):
            print(f"[API ERROR] Response Body: {response.text}")
        return None

def get_price(symbol):
    r = api_request("GET", "/openApi/swap/v2/quote/price", params={"symbol": symbol})
    if r and "data" in r and "price" in r["data"]:
        return float(r["data"]["price"])
    return None

def get_positions():
    ts = str(int(time.time() * 1000))
    params = {"timestamp": ts}
    # get_positions nutzt api_request("GET"), das die Signatur automatisch hinzufügt
    r = api_request("GET", "/openApi/swap/v2/user/positions", params=params)
    if r and "data" in r:
        return r.get("data", [])
    return []

def symbol_exists(symbol):
    r = api_request("GET", "/openApi/swap/v2/quote/price", params={"symbol": symbol})
    return r is not None and "data" in r and "price" in r["data"]

# ---------------- TP/SL ----------------

def reset_tp_sl(symbol):
    # Diese Funktion muss auch api_request nutzen, um Signature Null Fehler zu vermeiden
    ts = str(int(time.time() * 1000))
    params = {"symbol": symbol, "timestamp": ts}
    
    # API Helfer für GET nutzen, der Signatur hinzufügt
    r = api_request("GET", "/openApi/swap/v2/trade/openOrders", params=params)

    data = r.get("data", {}).get("orders", []) if r else []

    for order in data:
        oid = order["orderId"]
        ts2 = str(int(time.time() * 1000))
        params2 = {"orderId": oid, "symbol": symbol, "timestamp": ts2}
        
        # API Helfer für POST nutzen (Cancel order ist POST)
        r2 = api_request("POST", "/openApi/swap/v2/trade/cancelOrder", params=params2)
        if r2:
            print("[DEBUG] Cancel TP/SL:", json.dumps(r2))


def set_tp_sl(symbol, max_retries=5):
    pos = None
    retries = 0
    while retries < max_retries:
        positions = get_positions()
        pos = next(
            (p for p in positions if p["symbol"] == symbol and float(p["positionAmt"]) != 0),
            None
        )
        if pos:
            break
        print(f"[DEBUG] Position noch nicht sichtbar, warte 2s... Versuch {retries + 1}/{max_retries}")
        time.sleep(2) 
        retries += 1
    
    if not pos:
        print("[ERROR] Konnte Position nach Wartezeit nicht finden, TP/SL nicht gesetzt.")
        return

    side = pos["positionSide"]
    entry = float(pos["avgPrice"])
    qty = abs(float(pos["positionAmt"]))

    tp = entry * (1 + TP_PERCENT / 100) if side == "LONG" else entry * (1 - TP_PERCENT / 100)
    sl = entry * (1 - SL_PERCENT / 100) if side == "LONG" else entry * (1 + SL_PERCENT / 100)

    print(f"[DEBUG] Setting TP/SL: entry={entry}, qty={qty}, TP={tp:.6f}, SL={sl:.6f}")

    def place(price, otype):
        ts = str(int(time.time() * 1000))
        params = {
            "symbol": symbol,
            "side": "SELL" if side == "LONG" else "BUY",
            "positionSide": side,
            "type": otype,
            "stopPrice": f"{price:.6f}",
            "workingType": "MARK_PRICE",
            "closePosition": "true",
            "timestamp": ts
        }
        # Verwende den API Helper für POST. Die Signatur wird automatisch hinzugefügt.
        r = api_request("POST", "/openApi/swap/v2/trade/order", params=params)
        if r:
            print(f"[DEBUG] {otype} Response:", json.dumps(r))
        else:
            print(f"[ERROR] Failed to place {otype} order.")

    place(tp, "TAKE_PROFIT_MARKET")
    place(sl, "STOP_MARKET")


# ---------------- DCA ----------------

def monitor_dca():
    while True:
        try:
            positions = get_positions()

            for pos in positions:
                symbol = pos["symbol"]
                side = pos["positionSide"]
                entry = float(pos["avgPrice"])
                amt = float(pos["positionAmt"])

                if amt == 0:
                    continue

                current = get_price(symbol)
                if not current:
                    continue
                
                with dca_lock:
                    if symbol not in active_dca:
                        active_dca[symbol] = {
                            "side": side,
                            "entry": entry,
                            "executed": 0,
                            "trade_size": TRADE_SIZE
                        }

                    d = active_dca[symbol]
                    executed = d["executed"]

                deviation = abs((current - entry) / entry * 100)

                if executed >= DCA_COUNT:
                    continue

                if deviation >= (executed + 1) * DCA_DEVIATION_PERCENT:
                    base_qty = d["trade_size"] / entry
                    qty = base_qty * (DCA_VOLUME_MULTIPLIER ** (executed + 1))

                    ts = str(int(time.time() * 1000))
                    params = {
                        "symbol": symbol,
                        "side": "BUY" if side == "LONG" else "SELL", 
                        "positionSide": side,
                        "type": "MARKET",
                        "quantity": str(round(qty, 6)),
                        "timestamp": ts
                    }
                    
                    # Verwende api_request Helfer für POST. Signatur wird automatisch hinzugefügt.
                    r = api_request("POST", "/openApi/swap/v2/trade/order", params=params)
                    if r:
                        print("[DEBUG] DCA Order:", json.dumps(r))
                    else:
                        print("[ERROR] Failed to place DCA order.")
                    
                    with dca_lock:
                        d["executed"] += 1

                    reset_tp_sl(symbol)
                    set_tp_sl(symbol)

        except Exception as e:
            print("[DCA ERROR]", e)

        time.sleep(10)

# ---------------- ENTRY ----------------

def execute_trade(symbol, direction, leverage, trade_size):
    print(f"[DEBUG] ENTRY START {symbol} {direction} {leverage} {trade_size}")

    if not symbol_exists(symbol):
        print(f"[ERROR] Symbol {symbol} existiert NICHT auf BingX Futures.")
        return

    positions = get_positions()
    if any(p["symbol"] == symbol and p["positionSide"] == direction and float(p["positionAmt"]) != 0 for p in positions):
        print(f"[SKIP] {symbol} {direction} bereits offen.")
        return

    price = get_price(symbol)
    print("[DEBUG] price:", price)

    if not price:
        print("[ERROR] Kein Preis → Abbruch")
        return

    qty = round(trade_size / price, 6)
    side = "BUY" if direction == "LONG" else "SELL"

    ts = str(int(time.time() * 1000))
    params = {
        "symbol": symbol,
        "side": side,
        "positionSide": direction,
        "type": "MARKET",
        "quantity": str(qty),
        "leverage": str(leverage),
        "timestamp": ts
    }

    print("[DEBUG] ORDER PARAMS:", params)

    # Verwende api_request Helfer für POST. Signatur wird automatisch hinzugefügt.
    r = api_request("POST", "/openApi/swap/v2/trade/order", params=params)
    
    if r:
        print("[DEBUG] Entry Response:", json.dumps(r))
    else:
        print("[ERROR] Failed to place Entry order.")
        return 

    with dca_lock:
        active_dca[symbol] = {
            "side": direction,
            "entry": price,
            "executed": 0,
            "trade_size": trade_size
        }

    reset_tp_sl(symbol)
    set_tp_sl(symbol)

    print(f"[ENTRY] {symbol} {direction} ausgeführt.")

# ---------------- WEBHOOK ----------------

@app.route("/testorder", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    print("[DEBUG] Incoming:", data)
    # ... (Rest der Webhook Logik) ...
    currency = str(data.get("currency", "")).upper()
    direction = str(data.get("direction", "")).upper()

    if not currency or direction not in ("LONG", "SHORT"):
        return jsonify({"status": "ignored"}), 200

    symbol = f"{currency}-USDT"

    leverage = int(data.get("leverage", LEVERAGE))
    trade_size = float(data.get("trade_size", TRADE_SIZE))

    threading.Thread(
        target=execute_trade,
        args=(symbol, direction, leverage, trade_size)
    ).start()

    return jsonify({
        "status": "processing",
        "symbol": symbol,
        "direction": direction,
        "leverage": leverage,
        "trade_size": trade_size
    }), 200

# ---------------- START ----------------

threading.Thread(target=monitor_dca, daemon=True).start()

if __name__ == "__main__":
    if not API_KEY or not API_SECRET:
        print("FEHLER: BINGX_API_KEY oder BINGX_API_SECRET Umgebungsvariablen sind nicht gesetzt.")
    else:
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
