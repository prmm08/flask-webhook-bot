# -------- V 7.3: BINGX FUTURES - DIRECT ENTRY + MARKET-DCA + GLOBAL TP/SL + DEBUG --------

import time
import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
from flask import Flask, request, jsonify
import logging

# --- API Konfiguration ---
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
app = Flask(__name__)

# --- Strategie Settings ---
LEVERAGE = 20
TRADE_SIZE = 2000
TP_PERCENT = 1
SL_PERCENT = 50

# --- DCA Settings ---
DCA_COUNT = 3
DCA_DEVIATION_PERCENT = 5
DCA_VOLUME_MULTIPLIER = 1.5

# --- DCA Tracking ---
active_dca = {}   # {symbol: {"side": "LONG"/"SHORT", "entry": float, "executed": int}}

# ---------------- SIGNING & HELPERS ----------------

def sign_bingx(params):
    query_string = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(API_SECRET.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()

def get_price_bingx(symbol):
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/price"
        r = requests.get(url, params={"symbol": symbol}, timeout=10).json()
        print("[DEBUG] Price API Response:", r)
        return float(r["data"]["price"])
    except Exception as e:
        print("[DEBUG] Price Fetch ERROR:", e)
        return None

def get_open_positions():
    ts = str(int(time.time() * 1000))
    params = {"timestamp": ts}

    url = (
        f"{BINGX_BASE}/openApi/swap/v2/user/positions?"
        f"{urllib.parse.urlencode(sorted(params.items()))}"
        f"&signature={sign_bingx(params)}"
    )

    try:
        r = requests.get(url, headers={"X-BX-APIKEY": API_KEY}, timeout=10)
        print("[DEBUG] Positions RAW Response:", r.text)

        data = r.json()
        print("[DEBUG] Positions JSON Parsed:", data)

        # Wenn "data" fehlt oder kein Array ist → Fehler
        if "data" not in data:
            print("[DEBUG] Positions ERROR: 'data' fehlt in Response")
            return []

        if not isinstance(data["data"], list):
            print("[DEBUG] Positions ERROR: 'data' ist kein Array:", data["data"])
            return []

        return data["data"]

    except Exception as e:
        print("[DEBUG] Positions EXCEPTION:", e)
        return []


# ---------------- TP/SL HANDLING ----------------

def reset_tp_sl(symbol):
    try:
        ts = str(int(time.time() * 1000))
        params = {"symbol": symbol, "timestamp": ts}
        url = f"{BINGX_BASE}/openApi/swap/v2/trade/openOrders?{urllib.parse.urlencode(sorted(params.items()))}&signature={sign_bingx(params)}"
        r = requests.get(url, headers={"X-BX-APIKEY": API_KEY}).json()

        print("[DEBUG] OpenOrders Response:", r)

        data = r.get("data", [])

        if not isinstance(data, list):
            print(f"[TP/SL RESET] Keine offenen TP/SL Orders für {symbol}.")
            return

        for order in data:
            oid = order["orderId"]
            ts2 = str(int(time.time() * 1000))
            params2 = {"orderId": oid, "symbol": symbol, "timestamp": ts2}
            url2 = f"{BINGX_BASE}/openApi/swap/v2/trade/cancelOrder?{urllib.parse.urlencode(sorted(params2.items()))}&signature={sign_bingx(params2)}"
            r2 = requests.post(url2, headers={"X-BX-APIKEY": API_KEY})
            print("[DEBUG] Cancel TP/SL Response:", r2.text)

        if data:
            print(f"[TP/SL RESET] Alle TP/SL Orders für {symbol} gelöscht.")

    except Exception as e:
        print("[TP/SL RESET ERROR]", e)

def set_tp_sl_for_position(symbol):
    positions = get_open_positions()
    pos = next((p for p in positions if p["symbol"] == symbol and float(p.get("positionAmt", 0)) != 0), None)
    if not pos:
        print("[DEBUG] set_tp_sl_for_position: Keine Position gefunden")
        return

    side = pos["positionSide"]
    entry = float(pos["avgPrice"])
    qty = abs(float(pos["positionAmt"]))

    tp_price = entry * (1 + TP_PERCENT / 100) if side == "LONG" else entry * (1 - TP_PERCENT / 100)
    sl_price = entry * (1 - SL_PERCENT / 100) if side == "LONG" else entry * (1 + SL_PERCENT / 100)

    print(f"[DEBUG] Setting TP/SL: entry={entry}, qty={qty}, TP={tp_price}, SL={sl_price}")

    def place(price, o_type):
        ts = str(int(time.time() * 1000))
        params = {
            "symbol": symbol,
            "side": "SELL" if side == "LONG" else "BUY",
            "positionSide": side,
            "type": o_type,
            "quantity": str(qty),
            "stopPrice": "{:.6f}".format(price),
            "workingType": "MARK_PRICE",
            "closePosition": "true",
            "timestamp": ts
        }
        url = f"{BINGX_BASE}/openApi/swap/v2/trade/order?{urllib.parse.urlencode(sorted(params.items()))}&signature={sign_bingx(params)}"
        r = requests.post(url, headers={"X-BX-APIKEY": API_KEY})
        print("[DEBUG] TP/SL Order Response:", r.text)

    place(tp_price, "TAKE_PROFIT_MARKET")
    place(sl_price, "STOP_MARKET")

# ---------------- DCA MONITOR ----------------

def monitor_dca():
    while True:
        try:
            positions = get_open_positions()

            active_symbols = {p["symbol"] for p in positions if float(p.get("positionAmt", 0)) != 0}
            for sym in list(active_dca.keys()):
                if sym not in active_symbols:
                    del active_dca[sym]

            for pos in positions:
                symbol = pos["symbol"]
                side = pos["positionSide"]
                entry = float(pos["avgPrice"])
                amt = float(pos["positionAmt"])

                current = get_price_bingx(symbol)
                print("[DEBUG] DCA current price:", current)

                if not current:
                    continue

                if symbol not in active_dca:
                    active_dca[symbol] = {"side": side, "entry": entry, "executed": 0}

                d = active_dca[symbol]
                executed = d["executed"]

                deviation = abs((current - entry) / entry * 100)
                print(f"[DEBUG] DCA deviation={deviation}, executed={executed}")

                if executed >= DCA_COUNT:
                    continue

                if deviation >= (executed + 1) * DCA_DEVIATION_PERCENT:

                    base_qty = TRADE_SIZE / entry
                    qty = base_qty * (DCA_VOLUME_MULTIPLIER ** (executed + 1))

                    side_order = "BUY" if side == "LONG" else "SELL"

                    ts = str(int(time.time() * 1000))
                    params = {
                        "symbol": symbol,
                        "side": side_order,
                        "positionSide": side,
                        "type": "MARKET",
                        "quantity": str(round(qty, 6)),
                        "timestamp": ts
                    }

                    url = f"{BINGX_BASE}/openApi/swap/v2/trade/order?" \
                          f"{urllib.parse.urlencode(sorted(params.items()))}&signature={sign_bingx(params)}"
                    r = requests.post(url, headers={"X-BX-APIKEY": API_KEY})

                    print("[DEBUG] DCA Order Response:", r.text)

                    d["executed"] += 1

                    reset_tp_sl(symbol)
                    set_tp_sl_for_position(symbol)

        except Exception as e:
            print("[DCA ERROR]", e)

        time.sleep(10)

# ---------------- DIRECT ENTRY (NO FILTERS) ----------------

def execute_trade_bingx(symbol, direction):
    print("[DEBUG] execute_trade_bingx START", symbol, direction)

    positions = get_open_positions()
    print("[DEBUG] Current positions:", positions)

    if any(p['symbol'] == symbol and float(p.get('positionAmt', 0)) != 0 for p in positions):
        print(f"[SKIP] {symbol} bereits offen.")
        return

    current_price = get_price_bingx(symbol)
    print("[DEBUG] current_price:", current_price)

    if not current_price:
        print("[DEBUG] Kein Preis erhalten → Abbruch")
        return

    qty = round(TRADE_SIZE / current_price, 6)
    side = "BUY" if direction == "LONG" else "SELL"

    ts = str(int(time.time() * 1000))
    params = {
        "symbol": symbol,
        "side": side,
        "positionSide": direction,
        "type": "MARKET",
        "quantity": str(qty),
        "leverage": str(LEVERAGE),
        "timestamp": ts
    }

    url = f"{BINGX_BASE}/openApi/swap/v2/trade/order?" \
          f"{urllib.parse.urlencode(sorted(params.items()))}&signature={sign_bingx(params)}"
    r = requests.post(url, headers={"X-BX-APIKEY": API_KEY})

    print("[DEBUG] Entry Order Response:", r.text)

    active_dca[symbol] = {"side": direction, "entry": current_price, "executed": 0}

    reset_tp_sl(symbol)
    set_tp_sl_for_position(symbol)

    print(f"[ENTRY] {symbol} {direction} sofort ausgeführt.")

# ---------------- WEBHOOK ----------------

@app.route("/testorder", methods=["POST"])
def handle_signal():
    data = request.get_json(silent=True) or {}

    print("[DEBUG] Incoming JSON:", data)

    currency = str(data.get("currency", "")).upper()
    direction = str(data.get("direction", "")).upper()

    print("[DEBUG] currency:", currency, "direction:", direction)

    if not currency or direction not in ("LONG", "SHORT"):
        print("[DEBUG] INVALID SIGNAL → ignored")
        return jsonify({"status": "ignored"}), 200

    symbol = f"{currency}-USDT"

    threading.Thread(target=execute_trade_bingx, args=(symbol, direction)).start()

    return jsonify({"status": "processing", "symbol": symbol, "direction": direction}), 200

# ---------------- START ----------------

threading.Thread(target=monitor_dca, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
