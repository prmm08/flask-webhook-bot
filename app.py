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
#   KONFIGURATION & API
# ============================================================
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)

# Trading Parameter
LEVERAGE = 20
TRADE_SIZE = 100
TP_PERCENT = 1.0
SL_PERCENT = 40.0
DCA_DEVIATION_PERCENT = 5.0 
DCA_VOLUME_MULTIPLIER = 2
DCA_MAX_STEPS = 5
DCA_TP_PERCENT = 1.2
DCA_SL_PERCENT = 10.0
DCA_SAVE_FILE = "active_dca.json"

active_dca = {}
processing_symbols = set() 
dca_lock = threading.Lock()
symbol_info_cache = {}

# ============================================================
#   HELPERS
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
        logging.error(f"API Request Exception: {e}")
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
    return round(floor(value * inv) / inv, 10)

def get_price(symbol):
    r = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    return float(r["data"]["price"]) if r and "data" in r else None

# ============================================================
#   TRADING LOGIC
# ============================================================

def set_exchange_tp_sl(symbol, side, entry, tp_p, sl_p, quantity):
    """Setzt TP/SL mit Retry und korrekter Hedge-Mode Logik"""
    prec = get_symbol_precision(symbol)
    tp_price = round_step(entry * (1 + tp_p/100 if side == "LONG" else 1 - tp_p/100), prec["price_step"])
    sl_price = round_step(entry * (1 - sl_p/100 if side == "LONG" else 1 + sl_p/100), prec["price_step"])
    
    # Im Hedge Mode: Um eine LONG Position zu schließen, muss die Order SIDE "SELL" sein,
    # aber das positionSide muss "LONG" bleiben.
    order_side = "SELL" if side == "LONG" else "BUY"

    for attempt in range(3):
        time.sleep(3) # Längere Pause für API-Synchronisation
        success = 0
        for price, o_type in [(tp_price, "TAKE_PROFIT_MARKET"), (sl_price, "STOP_MARKET")]:
            payload = {
                "symbol": symbol,
                "side": order_side,
                "positionSide": side,
                "type": o_type,
                "stopPrice": str(price),
                "workingType": "MARK_PRICE",
                "quantity": str(quantity), # Manche Symbole erfordern Qty auch bei ClosePosition
                "closePosition": "true"
            }
            res = api_request("POST", "/openApi/swap/v2/trade/order", payload)
            if res and res.get("code") == 0:
                success += 1
                logging.info(f"[SUCCESS] {o_type} gesetzt @ {price}")
            else:
                logging.error(f"[FAIL] {o_type} für {symbol}: {res}")

        if success == 2: break
        logging.warning(f"Retry {attempt+1}/3 für TP/SL {symbol}...")

def execute_trade(symbol, direction, leverage, trade_size, tp_percent, sl_percent):
    if symbol in processing_symbols: return
    processing_symbols.add(symbol)
    try:
        # Check Positions
        pos_res = api_request("GET", "/openApi/swap/v2/user/positions", {"symbol": symbol})
        if pos_res and any(float(p["positionAmt"]) != 0 for p in pos_res.get("data", [])):
            logging.info(f"Position für {symbol} existiert bereits. Skip.")
            return

        price = get_price(symbol)
        if not price: return

        # 1. Leverage setzen
        api_request("POST", "/openApi/swap/v2/trade/leverage", {"symbol": symbol, "leverage": leverage, "side": direction})
        
        # 2. Market Order
        prec = get_symbol_precision(symbol)
        qty = round_step(trade_size / price, prec["qty_step"])
        
        logging.info(f"Öffne {direction} auf {symbol} mit Qty {qty}...")
        res = api_request("POST", "/openApi/swap/v2/trade/order", {
            "symbol": symbol,
            "side": "BUY" if direction == "LONG" else "SELL",
            "positionSide": direction,
            "type": "MARKET",
            "quantity": str(qty)
        })

        if res and res.get("code") == 0:
            logging.info(f"Market Order erfolgreich für {symbol}")
            with dca_lock:
                active_dca[symbol] = {
                    "side": direction, "entry_static": price, "entry_dynamic": price,
                    "executed": 0, "base_trade_size": trade_size, "qty": qty
                }
                save_dca_data()
            
            # 3. TP/SL setzen (Wichtig: qty mitgeben)
            set_exchange_tp_sl(symbol, direction, price, tp_percent, sl_percent, qty)
        else:
            logging.error(f"Market Order fehlgeschlagen: {res}")
            
    finally:
        time.sleep(2)
        processing_symbols.discard(symbol)

# ============================================================
#   MONITOR & FLASK (HEALTH CHECK INKLUSIVE)
# ============================================================

def monitor_worker():
    while True:
        try:
            # Sync mit API
            pos_data = api_request("GET", "/openApi/swap/v2/user/positions")
            active_on_exchange = {p["symbol"]: p for p in pos_data.get("data", []) if float(p["positionAmt"]) != 0} if pos_data else {}

            with dca_lock:
                to_delete = [s for s in active_dca if s not in active_on_exchange]
                for s in to_delete: del active_dca[s]
                if to_delete: save_dca_data()

            for symbol in list(active_dca.keys()):
                d = active_dca[symbol]
                curr = get_price(symbol)
                if not curr: continue

                # DCA Logik
                if d["executed"] < DCA_MAX_STEPS:
                    req_change = DCA_DEVIATION_PERCENT * (d["executed"] + 1)
                    triggered = (d["side"] == "LONG" and curr <= d["entry_static"] * (1 - req_change/100)) or \
                                (d["side"] == "SHORT" and curr >= d["entry_static"] * (1 + req_change/100))
                    
                    if triggered:
                        logging.info(f"DCA Trigger {symbol}. Level {d['executed']+1}")
                        # Cancel Exchange Orders
                        ords = api_request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol})
                        if ords and ords.get("data"):
                            for o in ords["data"].get("orders", []):
                                api_request("POST", "/openApi/swap/v2/trade/cancelOrder", {"symbol": symbol, "orderId": o["orderId"]})
                        
                        # Nachkauf
                        prec = get_symbol_precision(symbol)
                        new_qty = round_step((d["base_trade_size"] * (DCA_VOLUME_MULTIPLIER ** (d["executed"]+1))) / curr, prec["qty_step"])
                        api_request("POST", "/openApi/swap/v2/trade/order", {
                            "symbol": symbol, "side": "BUY" if d["side"] == "LONG" else "SELL",
                            "positionSide": d["side"], "type": "MARKET", "quantity": str(new_qty)
                        })
                        
                        time.sleep(3)
                        # Update Durchschnittspreis
                        p_res = api_request("GET", "/openApi/swap/v2/user/positions", {"symbol": symbol})
                        if p_res and p_res.get("data"):
                            with dca_lock:
                                d["executed"] += 1
                                d["entry_dynamic"] = float(p_res["data"][0]["avgPrice"])
                                save_dca_data()

                # Virtual Exit Logik (Nach DCA)
                if d["executed"] > 0:
                    exit_reason = None
                    if d["side"] == "LONG":
                        if curr >= d["entry_dynamic"] * (1 + DCA_TP_PERCENT/100): exit_reason = "VIRTUAL_TP"
                        elif curr <= d["entry_dynamic"] * (1 - DCA_SL_PERCENT/100): exit_reason = "VIRTUAL_SL"
                    else:
                        if curr <= d["entry_dynamic"] * (1 - DCA_TP_PERCENT/100): exit_reason = "VIRTUAL_TP"
                        elif curr >= d["entry_dynamic"] * (1 + DCA_SL_PERCENT/100): exit_reason = "VIRTUAL_SL"

                    if exit_reason:
                        api_request("POST", "/openApi/swap/v2/trade/order", {
                            "symbol": symbol, "side": "SELL" if d["side"] == "LONG" else "BUY",
                            "positionSide": d["side"], "type": "MARKET", "closePosition": "true"
                        })
        except: logging.error(traceback.format_exc())
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
    return jsonify({"status": "sent"}), 200

if __name__ == "__main__":
    load_dca_data()
    threading.Thread(target=monitor_worker, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))