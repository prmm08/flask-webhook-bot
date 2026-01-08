# -------- V 7.2: BINGX FUTURES - DIRECT ENTRY + MARKET-DCA + GLOBAL TP/SL (STABLE) --------

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
TRADE_SIZE = 1000
TP_PERCENT = 1
SL_PERCENT = 50

# --- DCA Settings ---
DCA_COUNT = 3
DCA_DEVIATION_PERCENT = 5
DCA_VOLUME_MULTIPLIER = 2

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
        return float(r["data"]["price"])
    except:
        return None

def get_open_positions():
    ts = str(int(time.time() * 1000))
    params = {"timestamp": ts}
    url = f"{BINGX_BASE}/openApi/swap/v2/user/positions?{urllib.parse.urlencode(sorted(params.items()))}&signature={sign_bingx(params)}"
    try:
        r = requests.get(url, headers={"X-BX-APIKEY": API_KEY}, timeout=10).json()
        return r.get("data", [])
    except:
        return []

# ---------------- TP/SL HANDLING ----------------

def reset_tp_sl(symbol):
    try:
        ts = str(int(time.time() * 1000))
        params = {"symbol": symbol, "timestamp": ts}
        url = f"{BINGX_BASE}/openApi/swap/v2/trade/openOrders?{urllib.parse.urlencode(sorted(params.items()))}&signature={sign_bingx(params)}"
        r = requests.get(url, headers={"X-BX-APIKEY": API_KEY}).json()

        data = r.get("data", [])

        # FIX: Wenn data kein Array ist → keine offenen Orders
        if not isinstance(data, list):
            print(f"[TP/SL RESET] Keine offenen TP/SL Orders für {symbol}.")
            return

        for order in data:
            oid = order["orderId"]
            ts2 = str(int(time.time() * 1000))
            params2 = {"orderId": oid, "symbol": symbol, "timestamp": ts2}
            url2 = f"{BINGX_BASE}/openApi/swap/v2/trade/cancelOrder?{urllib.parse.urlencode(sorted(params2.items()))}&signature={sign_bingx(params2)}"
            requests.post(url2, headers={"X-BX-APIKEY": API_KEY})

        if data:
            print(f"[TP/SL RESET] Alle TP/SL Orders für {symbol} gelöscht.")

    except Exception as e:
        print("[TP/SL RESET ERROR]", e)

def set_tp_sl_for_position(symbol):
    positions = get_open_positions()
    pos = next((p for p in positions if p["symbol"] == symbol and float(p.get("positionAmt", 0)) != 0), None)
    if not pos:
        return

    side = pos["positionSide"]
    entry = float(pos["avgPrice"])
    qty = abs(float(pos["positionAmt"]))

    tp_price = entry * (1 + TP_PERCENT / 100) if side == "LONG" else entry * (1 - TP_PERCENT / 100)
    sl_price = entry * (1 - SL_PERCENT / 100) if side == "LONG" else entry * (1 + SL_PERCENT / 100)

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
        requests.post(url, headers={"X-BX-APIKEY": API_KEY})

    place(tp_price, "TAKE_PROFIT_MARKET")
    place(sl_price, "STOP_MARKET")

    print(f"[TP/SL SET] {symbol} side={side} qty={qty:.6f} TP={tp_price:.4f} SL={sl_price:.4f}")

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

                if amt == 0:
                    continue

                current = get_price_bingx(symbol)
                if not current:
                    continue

                if symbol not in active_dca:
                    active_dca[symbol] = {"side": side, "entry": entry, "executed": 0}

                d = active_dca[symbol]
                executed = d["executed"]

                if executed >= DCA_COUNT:
                    continue

                deviation = abs((current - entry) / entry * 100)

                if deviation >= (executed + 1) * DCA_DEVIATION_PERCENT:

                    base_qty = TRADE_SIZE / entry
                    qty = base_qty * (DCA_VOLUME_MULTIPLIER ** (executed + 1))  # FIXED

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

                    print(f"[DCA] {symbol} #{executed+1} qty={qty:.6f} deviation={deviation:.2f}% resp={r.text}")

                    d["executed"] += 1

                    time.sleep(0.5)
                    reset_tp_sl(symbol)
                    time.sleep(0.5)
                    set_tp_sl_for_position(symbol)

        except Exception as e:
            print("[DCA ERROR]", e)

        time.sleep(10)

# ---------------- DIRECT ENTRY (NO FILTERS) ----------------

def execute_trade_bingx(symbol, direction):
    positions = get_open_positions()
    if any(p['symbol'] == symbol and float(p.get('positionAmt', 0)) != 0 for p in positions):
        print(f"[SKIP] {symbol} bereits offen.")
        return

    current_price = get_price_bingx(symbol)
    if not current_price:
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
    requests.post(url, headers={"X-BX-APIKEY": API_KEY})

    active_dca[symbol] = {"side": direction, "entry": current_price, "executed": 0}

    time.sleep(1)
    reset_tp_sl(symbol)
    time.sleep(0.5)
    set_tp_sl_for_position(symbol)

    print(f"[ENTRY] {symbol} {direction} sofort ausgeführt.")

# ---------------- WEBHOOK ----------------

@app.route("/testorder", methods=["POST"])
def handle_signal():
    data = request.get_json(silent=True) or {}

    currency = str(data.get("currency", "")).upper()
    direction = str(data.get("direction", "")).upper()

    if not currency or direction not in ("LONG", "SHORT"):
        return jsonify({"status": "ignored"}), 200

    symbol = f"{currency}-USDT"

    threading.Thread(target=execute_trade_bingx, args=(symbol, direction)).start()

    return jsonify({"status": "processing", "symbol": symbol, "direction": direction}), 200

# ---------------- START ----------------

threading.Thread(target=monitor_dca, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
