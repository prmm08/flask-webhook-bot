import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
import time
import json
import logging
import traceback
from math import floor
from flask import Flask, request, jsonify

# ============================================================
#   KONFIGURATION
# ============================================================
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)

# Standard-Werte (werden durch Webhook überschrieben falls mitgesendet)
LEVERAGE = 20
TRADE_SIZE = 100
TP_PERCENT = 1.0
SL_PERCENT = 40.0
DCA_DEVIATION_PERCENT = 5.0 
DCA_VOLUME_MULTIPLIER = 2
DCA_MAX_STEPS = 5
DCA_TP_PERCENT = 1.0
DCA_SL_PERCENT = 40.0
DCA_SAVE_FILE = "active_dca.json"

active_dca = {}
processing_symbols = set() 
dca_lock = threading.Lock()
symbol_info_cache = {}

# ============================================================
#   PERSISTENCE & HELPERS
# ============================================================

def save_dca_data():
    with dca_lock:
        try:
            with open(DCA_SAVE_FILE, "w") as f:
                json.dump(active_dca, f, indent=4)
        except Exception as e: logging.error(f"Save Error: {e}")

def load_dca_data():
    global active_dca
    if os.path.exists(DCA_SAVE_FILE):
        try:
            with open(DCA_SAVE_FILE, "r") as f:
                active_dca = json.load(f)
            logging.info(f"DCA Daten geladen: {len(active_dca)} Positionen.")
        except Exception as e: logging.error(f"Load Error: {e}")

def api_request(method, endpoint, params=None):
    url = f"{BINGX_BASE}{endpoint}"
    p = dict(params) if params else {}
    p["timestamp"] = str(int(time.time() * 1000))
    query_string = urllib.parse.urlencode(sorted(p.items()))
    sig = hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    full_url = f"{url}?{query_string}&signature={sig}"
    headers = {"X-BX-APIKEY": API_KEY}
    try:
        resp = requests.get(full_url, headers=headers, timeout=10) if method == "GET" else requests.post(full_url, headers=headers, timeout=10)
        return resp.json()
    except Exception as e:
        logging.error(f"API Error: {e}")
        return None

def get_symbol_precision(symbol):
    if symbol in symbol_info_cache: return symbol_info_cache[symbol]
    r = api_request("GET", "/openApi/swap/v2/quote/contracts")
    if r and "data" in r:
        for item in r["data"]:
            if item["symbol"] == symbol:
                p_p = 1 / (10 ** float(item.get("pricePrecision", 4)))
                q_p = 1 / (10 ** float(item.get("quantityPrecision", 2)))
                symbol_info_cache[symbol] = {"price_step": p_p, "qty_step": q_p}
                return symbol_info_cache[symbol]
    return {"price_step": 0.0001, "qty_step": 0.0001}

def round_step(value, step):
    if not step or step == 0: return value
    inv = 1.0 / step
    # Kleiner Offset gegen Floating Point Fehler
    return round(floor(value * inv + 0.0000000001) / inv, 10)

def get_price(symbol):
    r = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    return float(r["data"]["price"]) if r and "data" in r else None

# ============================================================
#   TRADING LOGIC
# ============================================================

def set_exchange_tp_sl(symbol, side, entry, tp_p, sl_p, quantity):
    """Setzt TP/SL optimiert für Hedge Mode & Altcoins"""
    logging.info(f"[WAIT] 5s Pause für Positions-Sync {symbol}...")
    time.sleep(5)

    prec = get_symbol_precision(symbol)
    tp_price = round_step(entry * (1 + tp_p/100 if side == "LONG" else 1 - tp_p/100), prec["price_step"])
    sl_price = round_step(entry * (1 - sl_p/100 if side == "LONG" else 1 + sl_p/100), prec["price_step"])
    order_side = "SELL" if side == "LONG" else "BUY"

    for o_type, price in [("TAKE_PROFIT_MARKET", tp_price), ("STOP_MARKET", sl_price)]:
        # Wir senden jetzt direkt die 'sichere' Kombination: closePosition + quantity
        payload = {
            "symbol": symbol,
            "side": order_side,
            "positionSide": side,
            "type": o_type,
            "stopPrice": str(price),
            "workingType": "MARK_PRICE",
            "quantity": str(quantity),
            "closePosition": "true"
        }
        
        res = api_request("POST", "/openApi/swap/v2/trade/order", payload)
        
        if res and res.get("code") == 0:
            logging.info(f"[SUCCESS] {o_type} für {symbol} gesetzt bei {price}")
        else:
            # Letzter Rettungsversuch ohne closePosition falls die API meckert
            payload.pop("closePosition")
            res = api_request("POST", "/openApi/swap/v2/trade/order", payload)
            if res and res.get("code") == 0:
                logging.info(f"[SUCCESS] {o_type} via Methode B gesetzt.")
            else:
                logging.error(f"[FATAL] TP/SL fehlgeschlagen für {symbol}: {res}")

def execute_trade(symbol, direction, leverage, trade_size, tp_percent, sl_percent):
    if symbol in processing_symbols: return
    processing_symbols.add(symbol)
    try:
        # Check Positions
        pos_data = api_request("GET", "/openApi/swap/v2/user/positions", {"symbol": symbol})
        if pos_data and any(float(p["positionAmt"]) != 0 for p in pos_data.get("data", [])):
            return

        price = get_price(symbol)
        if not price: return

        # Leverage & Order
        api_request("POST", "/openApi/swap/v2/trade/leverage", {"symbol": symbol, "leverage": leverage, "side": direction})
        
        prec = get_symbol_precision(symbol)
        qty = round_step(trade_size / price, prec["qty_step"])
        
        logging.info(f"[EXEC] {direction} {symbol} | Qty: {qty}")
        res = api_request("POST", "/openApi/swap/v2/trade/order", {
            "symbol": symbol, "side": "BUY" if direction == "LONG" else "SELL",
            "positionSide": direction, "type": "MARKET", "quantity": str(qty)
        })

        if res and res.get("code") == 0:
            with dca_lock:
                active_dca[symbol] = {
                    "side": direction, "entry_static": price, "entry_dynamic": price,
                    "executed": 0, "base_trade_size": trade_size, "qty": qty
                }
                save_dca_data()
            # TP/SL in Hintergrund-Thread
            threading.Thread(target=set_exchange_tp_sl, args=(symbol, direction, price, tp_percent, sl_percent, qty)).start()
    finally:
        time.sleep(2)
        processing_symbols.discard(symbol)

# ============================================================
#   WATCHER & FLASK
# ============================================================

def monitor_worker():
    while True:
        try:
            pos_res = api_request("GET", "/openApi/swap/v2/user/positions")
            active_list = {p["symbol"]: p for p in pos_res.get("data", []) if float(p["positionAmt"]) != 0} if pos_res else {}

            with dca_lock:
                to_delete = [s for s in active_dca if s not in active_list]
                for s in to_delete: del active_dca[s]
                if to_delete: save_dca_data()

            for symbol in list(active_dca.keys()):
                d = active_dca[symbol]
                curr = get_price(symbol)
                if not curr: continue

                # DCA Logik
                if d["executed"] < DCA_MAX_STEPS:
                    req_drop = DCA_DEVIATION_PERCENT * (d["executed"] + 1)
                    triggered = (d["side"] == "LONG" and curr <= d["entry_static"] * (1 - req_drop/100)) or \
                                (d["side"] == "SHORT" and curr >= d["entry_static"] * (1 + req_drop/100))
                    
                    if triggered:
                        logging.info(f"[DCA] Trigger {symbol}. Lösche Exchange-Orders...")
                        ords = api_request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol})
                        if ords and ords.get("data"):
                            for o in ords["data"].get("orders", []):
                                api_request("POST", "/openApi/swap/v2/trade/cancelOrder", {"symbol": symbol, "orderId": o["orderId"]})
                        
                        prec = get_symbol_precision(symbol)
                        new_qty = round_step((d["base_trade_size"] * (DCA_VOLUME_MULTIPLIER ** (d["executed"]+1))) / curr, prec["qty_step"])
                        api_request("POST", "/openApi/swap/v2/trade/order", {
                            "symbol": symbol, "side": "BUY" if d["side"] == "LONG" else "SELL",
                            "positionSide": d["side"], "type": "MARKET", "quantity": str(new_qty)
                        })
                        time.sleep(3)
                        p_now = api_request("GET", "/openApi/swap/v2/user/positions", {"symbol": symbol})
                        if p_now and p_now.get("data"):
                            with dca_lock:
                                d["executed"] += 1
                                d["entry_dynamic"] = float(p_now["data"][0]["avgPrice"])
                                save_dca_data()

                # Virtual Exit (Nur nach DCA)
                if d["executed"] > 0:
                    exit_reason = None
                    if d["side"] == "LONG":
                        if curr >= d["entry_dynamic"] * (1 + DCA_TP_PERCENT/100): exit_reason = "VIRTUAL_TP"
                        elif curr <= d["entry_dynamic"] * (1 - DCA_SL_PERCENT/100): exit_reason = "VIRTUAL_SL"
                    else:
                        if curr <= d["entry_dynamic"] * (1 - DCA_TP_PERCENT/100): exit_reason = "VIRTUAL_TP"
                        elif curr >= d["entry_dynamic"] * (1 + DCA_SL_PERCENT/100): exit_reason = "VIRTUAL_SL"

                    if exit_reason:
                        logging.info(f"[{exit_reason}] Schließe {symbol} via Market.")
                        api_request("POST", "/openApi/swap/v2/trade/order", {
                            "symbol": symbol, "side": "SELL" if d["side"] == "LONG" else "BUY",
                            "positionSide": d["side"], "type": "MARKET", "closePosition": "true"
                        })
        except: pass
        time.sleep(10)

@app.route("/")
@app.route("/ping")
def health(): return "OK", 200

@app.route("/testorder", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    t = data.get("currency") or data.get("ticker", "BTC")
    symbol = f"{str(t).upper()}-USDT"
    threading.Thread(target=execute_trade, args=(
        symbol, str(data.get("direction", "")).upper(),
        int(data.get("leverage", LEVERAGE)), float(data.get("trade_size", TRADE_SIZE)),
        float(data.get("tp_percent", TP_PERCENT)), float(data.get("sl_percent", SL_PERCENT))
    )).start()
    return jsonify({"status": "received"}), 200

if __name__ == "__main__":
    load_dca_data()
    threading.Thread(target=monitor_worker, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))