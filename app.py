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

# Logging Setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)

# Trading Parameter
LEVERAGE = 20
TRADE_SIZE = 100
TP_PERCENT = 1.0        # Initialer TP bei BingX
SL_PERCENT = 40.0       # Initialer SL bei BingX
DCA_DEVIATION_PERCENT = 5.0 
DCA_VOLUME_MULTIPLIER = 2
DCA_MAX_STEPS = 5       # Maximale Nachkäufe
DCA_TP_PERCENT = 1.0    # TP vom Durchschnittspreis nach DCA (Watcher geführt)
DCA_SL_PERCENT = 40.0   # Not-Aus nach DCA (Watcher geführt)
FEE_BUFFER = 0.0015     # 0.15% zur Deckung der Gebühren bei Break-Even
DCA_SAVE_FILE = "active_dca.json"

# Globale Zustände
active_dca = {}
processing_symbols = set() 
dca_lock = threading.Lock()
symbol_info_cache = {}

# ============================================================
#   PERSISTENCE & API HELPERS
# ============================================================

def save_dca_data():
    with dca_lock:
        try:
            with open(DCA_SAVE_FILE, "w") as f:
                json.dump(active_dca, f, indent=4)
        except Exception as e:
            logging.error(f"Speicherfehler: {e}")

def load_dca_data():
    global active_dca
    if os.path.exists(DCA_SAVE_FILE):
        try:
            with open(DCA_SAVE_FILE, "r") as f:
                active_dca = json.load(f)
            logging.info(f"DCA Daten geladen: {len(active_dca)} Positionen.")
        except Exception as e:
            logging.error(f"Ladefehler: {e}")

def api_request(method, endpoint, params=None):
    url = f"{BINGX_BASE}{endpoint}"
    p = dict(params) if params else {}
    p["timestamp"] = str(int(time.time() * 1000))
    query_string = urllib.parse.urlencode(sorted(p.items()))
    sig = hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    full_url = f"{url}?{query_string}&signature={sig}"
    headers = {"X-BX-APIKEY": API_KEY}
    try:
        if method == "GET":
            resp = requests.get(full_url, headers=headers, timeout=10)
        else:
            resp = requests.post(full_url, headers=headers, timeout=10)
        return resp.json()
    except Exception as e:
        logging.error(f"API Error {endpoint}: {e}")
        return None

def get_symbol_precision(symbol):
    if symbol in symbol_info_cache:
        return symbol_info_cache[symbol]
    r = api_request("GET", "/openApi/swap/v2/quote/contracts")
    if r and "data" in r:
        for item in r["data"]:
            if item["symbol"] == symbol:
                p_prec = 1 / (10 ** float(item.get("pricePrecision", 4)))
                q_prec = 1 / (10 ** float(item.get("quantityPrecision", 2)))
                symbol_info_cache[symbol] = {"price_step": p_prec, "qty_step": q_prec}
                return symbol_info_cache[symbol]
    return {"price_step": 0.0001, "qty_step": 0.0001}

def round_step(value, step):
    if step == 0: return value
    inv = 1.0 / step
    return round(floor(value * inv) / inv, 10)

def get_price(symbol):
    r = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    return float(r["data"]["price"]) if r and "data" in r else None

def get_positions():
    r = api_request("GET", "/openApi/swap/v2/user/positions")
    return r.get("data", []) if r else []

# ============================================================
#   TRADING LOGIC (EXCHANGE SIDE)
# ============================================================

def set_exchange_tp_sl(symbol, side, entry, tp_p, sl_p):
    """Setzt TP/SL direkt im Orderbuch der Exchange mit Retry-Logik"""
    prec = get_symbol_precision(symbol)
    tp_raw = entry * (1 + tp_p/100 if side == "LONG" else 1 - tp_p/100)
    sl_raw = entry * (1 - sl_p/100 if side == "LONG" else 1 + sl_p/100)
    
    tp = round_step(tp_raw, prec["price_step"])
    sl = round_step(sl_raw, prec["price_step"])
    
    # Wir versuchen es bis zu 3 mal, falls die API langsam ist
    for attempt in range(3):
        time.sleep(2 * (attempt + 1)) # Steigende Wartezeit
        
        success_count = 0
        for price, otype in [(tp, "TAKE_PROFIT_MARKET"), (sl, "STOP_MARKET")]:
            res = api_request("POST", "/openApi/swap/v2/trade/order", {
                "symbol": symbol, 
                "side": "SELL" if side == "LONG" else "BUY",
                "positionSide": side, 
                "type": otype, 
                "stopPrice": str(price),
                "workingType": "MARK_PRICE", 
                "closePosition": "true"
            })
            
            if res and res.get("code") == 0:
                success_count += 1
            else:
                logging.error(f"[EXCHANGE FAIL] {otype} für {symbol}: {res.get('msg')} (Code: {res.get('code')})")

        if success_count == 2:
            logging.info(f"[EXCHANGE SUCCESS] TP/SL erfolgreich im Orderbuch für {symbol}")
            break
        elif attempt == 2:
            logging.error(f"[CRITICAL] TP/SL konnte für {symbol} nicht gesetzt werden nach 3 Versuchen!")

def execute_trade(symbol, direction, leverage, trade_size, tp_percent, sl_percent):
    if symbol in processing_symbols: return
    processing_symbols.add(symbol)
    try:
        # Check Dubletten
        pos = get_positions()
        if any(p["symbol"] == symbol and float(p["positionAmt"]) != 0 for p in pos):
            return

        price = get_price(symbol)
        if not price: return

        # Leverage & Order
        api_request("POST", "/openApi/swap/v2/trade/leverage", {"symbol": symbol, "leverage": leverage, "side": direction})
        
        prec = get_symbol_precision(symbol)
        qty = round_step(trade_size / price, prec["qty_step"])

        res = api_request("POST", "/openApi/swap/v2/trade/order", {
            "symbol": symbol, "side": "BUY" if direction == "LONG" else "SELL",
            "positionSide": direction, "type": "MARKET", "quantity": str(qty)
        })

        if res and res.get("code") == 0:
            with dca_lock:
                active_dca[symbol] = {
                    "side": direction, "entry_static": price, "entry_dynamic": price,
                    "executed": 0, "base_trade_size": trade_size
                }
                save_dca_data()
            set_exchange_tp_sl(symbol, direction, price, tp_percent, sl_percent)
    finally:
        time.sleep(2)
        processing_symbols.discard(symbol)

# ============================================================
#   WORKER (WATCHER SIDE: DCA & VIRTUAL EXIT)
# ============================================================

def monitor_worker():
    logging.info("DCA Monitor aktiv.")
    while True:
        try:
            positions = get_positions()
            active_list = {p["symbol"]: p for p in positions if float(p["positionAmt"]) != 0}

            # Cleanup
            with dca_lock:
                to_delete = [s for s in active_dca if s not in active_list]
                for s in to_delete: del active_dca[s]
                if to_delete: save_dca_data()

            for symbol in list(active_dca.keys()):
                with dca_lock:
                    d = active_dca.get(symbol)
                if not d or symbol not in active_list: continue

                side = d["side"]
                curr = get_price(symbol)
                if not curr: continue

                # --- DCA TRIGGER ---
                if d["executed"] < DCA_MAX_STEPS:
                    req_drop = DCA_DEVIATION_PERCENT * (d["executed"] + 1)
                    triggered = (side == "LONG" and curr <= d["entry_static"] * (1 - req_drop/100)) or \
                                (side == "SHORT" and curr >= d["entry_static"] * (1 + req_drop/100))
                    
                    if triggered:
                        logging.info(f"[DCA] Trigger für {symbol}. Lösche Exchange-Orders...")
                        # 1. Exchange TP/SL löschen
                        r = api_request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol})
                        for o in r.get("data", {}).get("orders", []):
                            api_request("POST", "/openApi/swap/v2/trade/cancelOrder", {"symbol": symbol, "orderId": o["orderId"]})
                        
                        # 2. Nachkaufen
                        prec = get_symbol_precision(symbol)
                        usd_amount = d["base_trade_size"] * (DCA_VOLUME_MULTIPLIER ** (d["executed"] + 1))
                        qty = round_step(usd_amount / curr, prec["qty_step"])
                        
                        api_request("POST", "/openApi/swap/v2/trade/order", {
                            "symbol": symbol, "side": "BUY" if side == "LONG" else "SELL",
                            "positionSide": side, "type": "MARKET", "quantity": str(qty)
                        })
                        
                        time.sleep(2)
                        p_now = next((p for p in get_positions() if p["symbol"] == symbol), None)
                        with dca_lock:
                            d["executed"] += 1
                            if p_now: d["entry_dynamic"] = float(p_now["avgPrice"])
                            save_dca_data()

                # --- WATCHER VIRTUAL EXIT (Nur wenn DCA aktiv war) ---
                if d["executed"] > 0:
                    exit_reason = None
                    # TP nach DCA
                    if side == "LONG" and curr >= d["entry_dynamic"] * (1 + DCA_TP_PERCENT/100):
                        exit_reason = "VIRTUAL_TP"
                    elif side == "SHORT" and curr <= d["entry_dynamic"] * (1 - DCA_TP_PERCENT/100):
                        exit_reason = "VIRTUAL_TP"
                    
                    # SL nach DCA (Not-Aus)
                    elif side == "LONG" and curr <= d["entry_dynamic"] * (1 - DCA_SL_PERCENT/100):
                        exit_reason = "VIRTUAL_SL"
                    elif side == "SHORT" and curr >= d["entry_dynamic"] * (1 + DCA_SL_PERCENT/100):
                        exit_reason = "VIRTUAL_SL"

                    if exit_reason:
                        logging.info(f"[{exit_reason}] Schließe {symbol} via Market Order.")
                        api_request("POST", "/openApi/swap/v2/trade/order", {
                            "symbol": symbol, "side": "SELL" if side == "LONG" else "BUY",
                            "positionSide": side, "type": "MARKET", "closePosition": "true"
                        })

        except Exception:
            logging.error(traceback.format_exc())
        time.sleep(5)

# ============================================================
#   FLASK ENDPOINTS
# ============================================================

@app.route("/")
@app.route("/ping")
def health():
    return "OK", 200

@app.route("/testorder", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    ticker = data.get("currency") or data.get("ticker", "")
    symbol = f"{str(ticker).upper()}-USDT"
    direction = str(data.get("direction", "")).upper()
    
    if direction in ("LONG", "SHORT"):
        threading.Thread(target=execute_trade, args=(
            symbol, direction, int(data.get("leverage", LEVERAGE)),
            float(data.get("trade_size", TRADE_SIZE)),
            float(data.get("tp_percent", TP_PERCENT)),
            float(data.get("sl_percent", SL_PERCENT))
        )).start()
    return jsonify({"status": "processing"}), 200

if __name__ == "__main__":
    load_dca_data()
    threading.Thread(target=monitor_worker, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))