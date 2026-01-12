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
    if not params:
        query_string = ""
    else:
        items = sorted((k, "" if v is None else str(v)) for k, v in params.items())
        query_string = urllib.parse.urlencode(items)
    return hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()

# ---------------- API HELPERS ----------------

def api_request(method, endpoint, params=None):
    url = f"{BINGX_BASE}{endpoint}"
    headers = {"X-BX-APIKEY": API_KEY}
    params = {} if params is None else dict(params)

    if method == "GET":
        try:
            params_for_sign = dict(params)
            signature = sign_bingx(params_for_sign)
            params_with_sig = dict(params_for_sign)
            params_with_sig["signature"] = signature
            query = urllib.parse.urlencode(params_with_sig)
            signed_url = f"{url}?{query}" if query else url

            response = requests.get(signed_url, headers=headers, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"[API ERROR] GET {url} → {e}")
            try:
                print("[BODY]", response.text)
            except Exception:
                pass
            return None

    elif method == "POST":
        try:
            params_for_sign = dict(params)
            if "timestamp" not in params_for_sign:
                params_for_sign["timestamp"] = str(int(time.time() * 1000))

            query = urllib.parse.urlencode(sorted((k, "" if v is None else str(v)) for k, v in params_for_sign.items()))
            signature = sign_bingx(params_for_sign)
            signed_url = f"{url}?{query}&signature={signature}" if query else f"{url}?signature={signature}"

            response = requests.post(signed_url, headers=headers, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"[API ERROR] POST {url} → {e}")
            try:
                print("[BODY]", response.text)
            except Exception:
                pass
            return None
    else:
        raise ValueError("Unsupported HTTP method")

def get_price(symbol):
    r = api_request("GET", "/openApi/swap/v2/quote/price", params={"symbol": symbol})
    if r and "data" in r and "price" in r["data"]:
        try:
            return float(r["data"]["price"])
        except Exception:
            return None
    return None

def get_positions():
    ts = str(int(time.time() * 1000))
    params = {"timestamp": ts}
    r = api_request("GET", "/openApi/swap/v2/user/positions", params=params)
    if r and "data" in r:
        return r.get("data", [])
    return []

def symbol_exists(symbol):
    r = api_request("GET", "/openApi/swap/v2/quote/price", params={"symbol": symbol})
    return r is not None and "data" in r and "price" in r["data"]

# ---------------- Leverage ----------------

def set_leverage_for_symbol(symbol, leverage, position_side=None, side=None):
    ts = str(int(time.time() * 1000))
    params = {
        "symbol": symbol,
        "leverage": str(leverage),
        "timestamp": ts
    }
    if position_side:
        params["positionSide"] = position_side
    if side:
        params["side"] = side

    r = api_request("POST", "/openApi/swap/v2/trade/leverage", params=params)
    if r:
        print("[DEBUG] Set Leverage Response:", json.dumps(r))
        return True
    else:
        print("[ERROR] Failed to set leverage for", symbol)
        return False

# ---------------- TP SL ----------------

def reset_tp_sl(symbol, position_side=None):
    ts = str(int(time.time() * 1000))
    params = {"symbol": symbol, "timestamp": ts}
    r = api_request("GET", "/openApi/swap/v2/trade/openOrders", params=params)
    orders = r.get("data", {}).get("orders", []) if r else []

    for order in orders:
        order_pos_side = order.get("positionSide") or order.get("position_side") or order.get("position")
        if position_side and order_pos_side and order_pos_side != position_side:
            continue

        oid = order.get("orderId") or order.get("order_id")
        if not oid:
            continue
        ts2 = str(int(time.time() * 1000))
        params2 = {"orderId": oid, "symbol": symbol, "timestamp": ts2}
        r2 = api_request("POST", "/openApi/swap/v2/trade/cancelOrder", params=params2)
        if r2:
            print("[DEBUG] Cancel TP/SL:", json.dumps(r2))

def set_tp_sl(symbol, desired_side=None, tp_percent=TP_PERCENT, sl_percent=SL_PERCENT, max_retries=8):
    pos = None
    retries = 0
    while retries < max_retries:
        positions = get_positions()
        if desired_side:
            pos = next(
                (p for p in positions if p["symbol"] == symbol and p.get("positionSide") == desired_side and float(p.get("positionAmt", 0)) != 0),
                None
            )
        else:
            pos = next(
                (p for p in positions if p["symbol"] == symbol and float(p.get("positionAmt", 0)) != 0),
                None
            )
        if pos:
            break
        print(f"[DEBUG] Position ({desired_side}) noch nicht sichtbar, warte 1s... Versuch {retries + 1}/{max_retries}")
        time.sleep(1)
        retries += 1

    if not pos:
        print(f"[ERROR] Konnte Position {symbol} {desired_side} nicht finden.")
        return

    side = pos.get("positionSide", "LONG")
    entry = float(pos.get("avgPrice", 0))

    # Warte bis BingX avgPrice aktualisiert hat
    old_entry = entry
    for i in range(10):
        time.sleep(0.8)
        new_positions = get_positions()
        new_pos = next((p for p in new_positions if p["symbol"] == symbol and p.get("positionSide") == side), None)
        if not new_pos:
            continue
        new_entry = float(new_pos.get("avgPrice", 0))
        if abs(new_entry - old_entry) > 0.0001:
            old_entry = new_entry
            break

    entry = old_entry

    tp = entry * (1 + tp_percent / 100) if side == "LONG" else entry * (1 - tp_percent / 100)
    sl = entry * (1 - sl_percent / 100) if side == "LONG" else entry * (1 + sl_percent / 100)

    print(f"[DEBUG] Setting TP/SL for {symbol} {side}: entry={entry}, TP={tp:.6f}, SL={sl:.6f}")

    reset_tp_sl(symbol, position_side=side)

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
                symbol = pos.get("symbol")
                side = pos.get("positionSide")
                entry = float(pos.get("avgPrice", 0))
                amt = float(pos.get("positionAmt", 0))

                if not symbol or amt == 0 or entry == 0:
                    continue

                current = get_price(symbol)
                if not current:
                    continue

                with dca_lock:
                    if symbol not in active_dca:
                        try:
                            base_trade_value = abs(amt) * entry
                            if base_trade_value <= 0:
                                raise ValueError("base_trade_value 0")
                            active_dca[symbol] = {
                                "side": side,
                                "entry": entry,
                                "executed": 0,
                                "trade_size": base_trade_value,
                                "tp_percent": TP_PERCENT,
                                "sl_percent": SL_PERCENT
                            }
                        except Exception:
                            active_dca[symbol] = {
                                "side": side,
                                "entry": entry,
                                "executed": 0,
                                "trade_size": TRADE_SIZE,
                                "tp_percent": TP_PERCENT,
                                "sl_percent": SL_PERCENT
                            }

                    d = active_dca[symbol]
                    executed = d["executed"]

                deviation = abs((current - entry) / entry * 100) if entry != 0 else 0
                if executed >= DCA_COUNT:
                    continue

                if deviation >= (executed + 1) * DCA_DEVIATION_PERCENT:
                    base_qty = d["trade_size"] / entry
                    qty = base_qty * (DCA_VOLUME_MULTIPLIER ** (executed + 1))
                    qty_rounded = round(qty, 6)
                    ts = str(int(time.time() * 1000))
                    params = {
                        "symbol": symbol,
                        "side": "BUY" if side == "LONG" else "SELL",
                        "positionSide": side,
                        "type": "MARKET",
                        "quantity": str(qty_rounded),
                        "timestamp": ts
                    }

                    r = api_request("POST", "/openApi/swap/v2/trade/order", params=params)
                    if r:
                        print("[DEBUG] DCA Order:", json.dumps(r))
                    else:
                        print("[ERROR] Failed to place DCA order.")

                    with dca_lock:
                        d["executed"] += 1

                    reset_tp_sl(symbol, position_side=side)
                    set_tp_sl(
                        symbol,
                        desired_side=side,
                        tp_percent=d["tp_percent"],
                        sl_percent=d["sl_percent"]
                    )

        except Exception as e:
            print("[DCA ERROR]", e)

        time.sleep(10)

# ---------------- ENTRY ----------------

def execute_trade(symbol, direction, leverage, trade_size, tp_percent, sl_percent):
    print(f"[DEBUG] ENTRY START {symbol} {direction} {leverage} {trade_size}")

    if not symbol_exists(symbol):
        print(f"[ERROR] Symbol {symbol} existiert NICHT auf BingX Futures.")
        return

    positions = get_positions()
    if any(p["symbol"] == symbol and p.get("positionSide") == direction and float(p.get("positionAmt", 0)) != 0 for p in positions):
        print(f"[SKIP] {symbol} {direction} bereits offen.")
        return

    price = get_price(symbol)
    if not price:
        print("[ERROR] Kein Preis → Abbruch")
        return

    leverage_side = "BUY" if direction == "LONG" else "SELL"
    if not set_leverage_for_symbol(symbol, leverage, position_side=direction, side=leverage_side):
        print("[ERROR] Leverage konnte nicht gesetzt werden. Abbruch.")
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
        "timestamp": ts
    }

    r = api_request("POST", "/openApi/swap/v2/trade/order", params=params)

    if not r:
        print("[ERROR] Failed to place Entry order.")
        return

    with dca_lock:
        active_dca[symbol] = {
            "side": direction,
            "entry": price,
            "executed": 0,
            "trade_size": trade_size,
            "tp_percent": tp_percent,
            "sl_percent": sl_percent
        }

    time.sleep(1.5)

    reset_tp_sl(symbol, position_side=direction)
    set_tp_sl(symbol, desired_side=direction, tp_percent=tp_percent, sl_percent=sl_percent)

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

    tp_percent = float(data.get("tp_percent", TP_PERCENT))
    sl_percent = float(data.get("sl_percent", SL_PERCENT))

    threading.Thread(
        target=execute_trade,
        args=(symbol, direction, leverage, trade_size, tp_percent, sl_percent)
    ).start()

    return jsonify({
        "status": "processing",
        "symbol": symbol,
        "direction": direction,
        "leverage": leverage,
        "trade_size": trade_size,
        "tp_percent": tp_percent,
        "sl_percent": sl_percent
    }), 200

# ---------------- START ----------------

threading.Thread(target=monitor_dca, daemon=True).start()

if __name__ == "__main__":
    if not API_KEY or not API_SECRET:
        print("FEHLER: BINGX_API_KEY oder BINGX_API_SECRET fehlen.")
    else:
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
