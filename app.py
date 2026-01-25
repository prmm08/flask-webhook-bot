import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
import time
import sys
from flask import Flask, request, jsonify

# --- LOGGING HELPER ---
def log_print(msg):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}")
    sys.stdout.flush()

# --- CONFIG ---
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BINGX_BASE = "https://open-api.bingx.com"

app = Flask(__name__)

# --- GLOBAL RATE LIMIT GUARD ---
rate_limit_backoff_until = 0

# --- STRATEGIE EINSTELLUNGEN ---
TP_MODE = "FIRST_ORDER"        
USE_SL = False              
SL_PERCENT = 40.0          
TP_PERCENT = 1.0           
BE_DCA_LEVEL = 3           
BE_PROFIT_PERCENT = 0.05   

DCA_COUNT = 4          
DCA_DEVIATION_PERCENT = 5.0
DCA_VOLUME_MULTIPLIER = 2
MIN_ORDER_INTERVAL = 30
TRADE_SIZE = 20.0
LEVERAGE = 20

# --- API CORE ---
def api_request(method, endpoint, params=None):
    global rate_limit_backoff_until
    
    # Prüfen, ob wir gerade pausieren müssen wegen Rate Limit
    if time.time() < rate_limit_backoff_until:
        return None

    url = f"{BINGX_BASE}{endpoint}"
    headers = {"X-BX-APIKEY": API_KEY}
    params = dict(params) if params else {}
    params["timestamp"] = str(int(time.time() * 1000))
    
    query_string = urllib.parse.urlencode(sorted(params.items()))
    signature = hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    full_url = f"{url}?{query_string}&signature={signature}"
    
    try:
        if method == "POST": resp = requests.post(full_url, headers=headers, timeout=15)
        elif method == "GET": resp = requests.get(full_url, headers=headers, timeout=15)
        elif method == "DELETE": resp = requests.delete(full_url, headers=headers, timeout=15)
        
        data = resp.json()
        
        # RATE LIMIT HANDLING
        if data.get("code") == 100410:
            log_print("!!! RATE LIMIT ERREICHT !!! Pausiere API-Anfragen für 2 Minuten...")
            rate_limit_backoff_until = time.time() + 120 # 2 Minuten Sperre einhalten
            return None
            
        return data
    except Exception as e:
        log_print(f"Netzwerk-Fehler: {e}")
        return None

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except: pass

# --- HISTORIE ABFRAGEN ---
def get_dca_history(symbol, side):
    r = api_request("GET", "/openApi/swap/v2/trade/allOrders", {"symbol": symbol, "limit": 20})
    if not r or not isinstance(r.get("data"), list): return 1, None, None
    filled_orders = [o for o in r["data"] if isinstance(o, dict) and o.get("status") == "FILLED" and o.get("positionSide") == side and o.get("type") == "MARKET"]
    if not filled_orders: return 1, None, None
    try:
        return len(filled_orders), float(filled_orders[0]["avgPrice"]), float(filled_orders[-1]["avgPrice"])
    except: return 1, None, None

# --- TP / SL SETZEN ---
def set_tp_sl(symbol, side, current_level, first_price=None):
    log_print(f"[ACTION] Setze TP/SL für {symbol} (Level {current_level})")
    r_pos = api_request("GET", "/openApi/swap/v2/user/positions", {"symbol": symbol})
    if not r_pos or not isinstance(r_pos.get("data"), list): return
    pos = next((p for p in r_pos["data"] if p.get("positionSide") == side and float(p.get("positionAmt", 0)) != 0), None)
    if not pos: return

    avg_price = float(pos["avgPrice"])
    target_tp_pct = BE_PROFIT_PERCENT if current_level >= BE_DCA_LEVEL else TP_PERCENT
    base_price = avg_price if (TP_MODE == "AVERAGE" or current_level >= BE_DCA_LEVEL) else (first_price or avg_price)
    tp_price = base_price * (1 + target_tp_pct/100) if side == "LONG" else base_price * (1 - target_tp_pct/100)

    api_request("DELETE", "/openApi/swap/v2/trade/order", {"symbol": symbol})
    api_request("POST", "/openApi/swap/v2/trade/order", {
        "symbol": symbol, "side": "SELL" if side == "LONG" else "BUY", "positionSide": side,
        "type": "TAKE_PROFIT_MARKET", "stopPrice": f"{tp_price:.6f}", "workingType": "MARK_PRICE", "closePosition": "true"
    })
    if USE_SL:
        sl_price = avg_price * (1 - SL_PERCENT/100) if side == "LONG" else avg_price * (1 + SL_PERCENT/100)
        api_request("POST", "/openApi/swap/v2/trade/order", {
            "symbol": symbol, "side": "SELL" if side == "LONG" else "BUY", "positionSide": side,
            "type": "STOP_MARKET", "stopPrice": f"{sl_price:.6f}", "workingType": "MARK_PRICE", "closePosition": "true"
        })

# --- WATCHER (Mit Rate-Limit Schutz) ---
def tp_watcher():
    log_print("[SYSTEM] TP/SL Watcher aktiv.")
    while True:
        try:
            r_pos = api_request("GET", "/openApi/swap/v2/user/positions")
            if r_pos and isinstance(r_pos.get("data"), list):
                for pos in r_pos["data"]:
                    if float(pos.get("positionAmt", 0)) == 0: continue
                    symbol, side = pos["symbol"], pos["positionSide"]
                    
                    r_orders = api_request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol})
                    if r_orders and isinstance(r_orders.get("data"), list):
                        has_tp = any(o.get("type") in ["TAKE_PROFIT_MARKET", "TAKE_PROFIT"] for o in r_orders["data"])
                        has_sl = any(o.get("type") in ["STOP_MARKET", "STOP"] for o in r_orders["data"]) if USE_SL else True
                        
                        if not has_tp or not has_sl:
                            log_print(f"[WATCHER] Reparatur für {symbol}...")
                            level, _, first_p = get_dca_history(symbol, side)
                            set_tp_sl(symbol, side, level, first_p)
        except Exception as e:
            log_print(f"Watcher Fehler: {e}")
        
        # Falls wir gerade ein Rate Limit haben, warten wir länger
        wait_time = 30 if time.time() < rate_limit_backoff_until else 15
        time.sleep(wait_time)

# --- MONITOR (DCA) ---
def monitor_dca():
    log_print("[SYSTEM] Monitor aktiv.")
    while True:
        try:
            r_pos = api_request("GET", "/openApi/swap/v2/user/positions")
            if r_pos and isinstance(r_pos.get("data"), list):
                for pos in r_pos["data"]:
                    if float(pos.get("positionAmt", 0)) == 0: continue
                    symbol, side = pos["symbol"], pos["positionSide"]
                    level, last_price, first_price = get_dca_history(symbol, side)
                    if not last_price: continue
                    
                    r_ticker = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
                    if r_ticker and "data" in r_ticker:
                        curr_price = float(r_ticker["data"].get("price", 0))
                        diff = ((curr_price / last_price) - 1) * 100
                        trigger = (side == "LONG" and diff <= -DCA_DEVIATION_PERCENT) or (side == "SHORT" and diff >= DCA_DEVIATION_PERCENT)
                        
                        if trigger and level < DCA_COUNT:
                            qty = (TRADE_SIZE * (DCA_VOLUME_MULTIPLIER ** level)) / curr_price
                            resp = api_request("POST", "/openApi/swap/v2/trade/order", {"symbol": symbol, "side": "BUY" if side == "LONG" else "SELL", "positionSide": side, "type": "MARKET", "quantity": str(round(qty, 6))})
                            if resp and resp.get("code") == 0:
                                time.sleep(3); set_tp_sl(symbol, side, level + 1, first_price)
        except Exception as e: log_print(f"Monitor Fehler: {e}")
        time.sleep(30)

@app.route("/ping")
@app.route("/")
def health(): return "STATLESS_BOT_V1.5_ONLINE", 200

@app.route("/testorder", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    currency, direction = str(data.get("currency", "")).upper(), str(data.get("direction", "")).upper()
    if currency and direction in ["LONG", "SHORT"]:
        threading.Thread(target=execute_initial_trade, args=(f"{currency}-USDT", direction)).start()
    return jsonify({"status": "ok"}), 200

def execute_initial_trade(symbol, direction):
    api_request("POST", "/openApi/swap/v2/trade/leverage", {"symbol": symbol, "leverage": str(LEVERAGE), "side": direction, "positionSide": direction})
    r_ticker = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    if r_ticker and "data" in r_ticker:
        price = float(r_ticker["data"].get("price", 0))
        qty = round(TRADE_SIZE / price, 6)
        resp = api_request("POST", "/openApi/swap/v2/trade/order", {"symbol": symbol, "side": "BUY" if direction == "LONG" else "SELL", "positionSide": direction, "type": "MARKET", "quantity": str(qty)})
        if resp and resp.get("code") == 0:
            time.sleep(3); set_tp_sl(symbol, direction, 1, price)

if __name__ == "__main__":
    threading.Thread(target=monitor_dca, daemon=True).start()
    threading.Thread(target=tp_watcher, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))