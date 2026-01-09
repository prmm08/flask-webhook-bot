import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
import time  # Hinzugefügt
import json  # Hilfreich für Debug-Ausgaben

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
dca_lock = threading.Lock() # Lock hinzugefügt für Thread-Safety

# ---------------- SIGNING ----------------

def sign_bingx(params):
    query_string = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()

# ---------------- API HELPERS ----------------

# Eine verbesserte Helferfunktion für API-Anfragen
def api_request(method, endpoint, params=None, headers=None, data=None):
    url = f"{BINGX_BASE}{endpoint}"
    try:
        if method == 'GET':
            response = requests.get(url, params=params, headers=headers, timeout=10)
        elif method == 'POST':
            response = requests.post(url, params=params, headers=headers, data=data, timeout=10)
        
        response.raise_for_status() # Löst HTTPError für 4xx/5xx Fehler aus
        return response.json()
    
    except requests.exceptions.RequestException as e:
        print(f"[API ERROR] Method: {method}, URL: {url}, Error: {e}")
        if response is not None:
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
    signature = sign_bingx(params)
    # Signatur muss oft als eigener Parameter in der URL oder den Params sein, je nach API Doc
    params["signature"] = signature 
    
    r = api_request("GET", "/openApi/swap/v2/user/positions", params=params, headers={"X-BX-APIKEY": API_KEY})
    if r and "data" in r:
        return r.get("data", [])
    return []

def symbol_exists(symbol):
    r = api_request("GET", "/openApi/swap/v2/quote/price", params={"symbol": symbol})
    return r is not None and "data" in r and "price" in r["data"]

# ---------------- TP/SL ----------------

def reset_tp_sl(symbol):
    try:
        ts = str(int(time.time() * 1000))
        params = {"symbol": symbol, "timestamp": ts}
        url = (
            f"{BINGX_BASE}/openApi/swap/v2/trade/openOrders?"
            f"{urllib.parse.urlencode(sorted(params.items()))}"
            f"&signature={sign_bingx(params)}"
        )
        r = requests.get(url, headers={"X-BX-APIKEY": API_KEY}).json()
        print("[DEBUG] OpenOrders:", r)

        data = r.get("data", [])
        if not isinstance(data, list):
            return

        for order in data:
            oid = order["orderId"]
            ts2 = str(int(time.time() * 1000))
            params2 = {"orderId": oid, "symbol": symbol, "timestamp": ts2}
            url2 = (
                f"{BINGX_BASE}/openApi/swap/v2/trade/cancelOrder?"
                f"{urllib.parse.urlencode(sorted(params2.items()))}"
                f"&signature={sign_bingx(params2)}"
            )
            r2 = requests.post(url2, headers={"X-BX-APIKEY": API_KEY})
            print("[DEBUG] Cancel TP/SL:", r2.text)

    except Exception as e:
        print("[TP/SL RESET ERROR]", e)

def set_tp_sl(symbol, max_retries=5):
    # Polling-Logik: Warte, bis die Position in der API sichtbar ist
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
        time.sleep(2) # Wartezeit hinzugefügt
        retries += 1
    
    if not pos:
        print("[ERROR] Konnte Position nach Wartezeit nicht finden, TP/SL nicht gesetzt.")
        return

    side = pos["positionSide"]  # LONG oder SHORT
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
        # Verwende den API Helper für besseres Logging
        r = api_request("POST", "/openApi/swap/v2/trade/order", params=params, headers={"X-BX-APIKEY": API_KEY})
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
                
                # Lock verwenden beim Lesen/Schreiben von active_dca
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
                # print(f"[DEBUG] DCA deviation={deviation}, executed={executed}") # Stark reduziert um Logs zu entlasten

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
                    url = (
                        f"{BINGX_BASE}/openApi/swap/v2/trade/order?"
                        f"{urllib.parse.urlencode(sorted(params.items()))}"
                        f"&signature={sign_bingx(params)}"
                    )
                    r = requests.post(url, headers={"X-BX-APIKEY": API_KEY})
                    print("[DEBUG] DCA Order:", r.text)
                    
                    # Lock verwenden beim Schreiben von active_dca
                    with dca_lock:
                        d["executed"] += 1

                    reset_tp_sl(symbol)
                    set_tp_sl(symbol)

        except Exception as e:
            print("[DCA ERROR]", e)

        time.sleep(10)

# ---------------- ENTRY ----------------

def execute_trade(symbol, direction, leverage, trade_size):
    print("[DEBUG] ENTRY START", symbol, direction, leverage, trade_size)

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

    url = (
        f"{BINGX_BASE}/openApi/swap/v2/trade/order?"
        f"{urllib.parse.urlencode(sorted(params.items()))}"
        f"&signature={sign_bingx(params)}"
    )
    r = requests.post(url, headers={"X-BX-APIKEY": API_KEY})
    print("[DEBUG] Entry Response:", r.text)

    # Lock verwenden beim Schreiben von active_dca
    with dca_lock:
        active_dca[symbol] = {
            "side": direction,
            "entry": price,
            "executed": 0,
            "trade_size": trade_size
        }

    # set_tp_sl hat jetzt die interne Warte-Logik
    reset_tp_sl(symbol)
    set_tp_sl(symbol)

    print(f"[ENTRY] {symbol} {direction} ausgeführt.")

# ---------------- WEBHOOK ----------------

@app.route("/testorder", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    print("[DEBUG] Incoming:", data)

    currency = str(data.get("currency", "")).upper()
    direction = str(data.get("direction", "")).upper()

    if not currency or direction not in ("LONG", "SHORT"):
        return jsonify({"status": "ignored"}), 200

    symbol = f"{currency}-USDT"

    leverage = int(data.get("leverage", LEVERAGE))
    trade_size = float(data.get("trade_size", TRADE_SIZE))

    # Die Ausführung findet in einem separaten Thread statt
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
    # Stelle sicher, dass API Keys gesetzt sind, bevor du startest
    if not API_KEY or not API_SECRET:
        print("FEHLER: BINGX_API_KEY oder BINGX_API_SECRET Umgebungsvariablen sind nicht gesetzt.")
    else:
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
