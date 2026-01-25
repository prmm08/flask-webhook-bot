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
def sign_bingx(params):
    items = sorted((k, "" if v is None else str(v)) for k, v in params.items())
    query_string = urllib.parse.urlencode(items)
    return hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()

def api_request(method, endpoint, params=None):
    url = f"{BINGX_BASE}{endpoint}"
    headers = {"X-BX-APIKEY": API_KEY}
    params = dict(params) if params else {}
    params["timestamp"] = str(int(time.time() * 1000))
    query_string = urllib.parse.urlencode(sorted(params.items()))
    signature = sign_bingx(params)
    full_url = f"{url}?{query_string}&signature={signature}"
    try:
        if method == "POST": resp = requests.post(full_url, headers=headers, timeout=15)
        elif method == "GET": resp = requests.get(full_url, headers=headers, timeout=15)
        elif method == "DELETE": resp = requests.delete(full_url, headers=headers, timeout=15)
        
        # Sicherstellen, dass wir JSON bekommen
        data = resp.json()
        if data.get("code") != 0:
            log_print(f"API Warnung {endpoint}: {data.get('msg')} (Code: {data.get('code')})")
        return data
    except Exception as e:
        log_print(f"Netzwerk-Fehler bei {endpoint}: {e}")
        return None

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except: pass

# --- HISTORIE ABFRAGEN (Zustand ermitteln) ---
def get_dca_history(symbol, side):
    r = api_request("GET", "/openApi/swap/v2/trade/allOrders", {"symbol": symbol, "limit": 20})
    
    # Validierung: r muss ein Dict sein und r["data"] eine Liste
    if not isinstance(r, dict) or not isinstance(r.get("data"), list):
        return 1, None, None

    # Filtern der Orders
    filled_orders = []
    for o in r["data"]:
        # Sicherstellen, dass 'o' ein Dictionary ist (verhindert den 'string indices' Fehler)
        if isinstance(o, dict) and o.get("status") == "FILLED" and o.get("positionSide") == side and o.get("type") == "MARKET":
            filled_orders.append(o)
    
    if not filled_orders:
        return 1, None, None

    level = len(filled_orders)
    try:
        last_price = float(filled_orders[0]["avgPrice"])   
        first_price = float(filled_orders[-1]["avgPrice"]) 
        return level, last_price, first_price
    except (KeyError, ValueError, IndexError):
        return 1, None, None

# --- TP / SL LOGIK ---
def set_tp_sl(symbol, side, current_level, first_price=None):
    log_print(f"Aktualisiere TP/SL für {symbol} (Level {current_level})")
    r_pos = api_request("GET", "/openApi/swap/v2/user/positions", {"symbol": symbol})
    
    if not r_pos or not isinstance(r_pos.get("data"), list): return
    
    pos = next((p for p in r_pos["data"] if p.get("positionSide") == side and float(p.get("positionAmt", 0)) != 0), None)
    if not pos: return

    avg_price = float(pos["avgPrice"])
    target_tp_pct = BE_PROFIT_PERCENT if current_level >= BE_DCA_LEVEL else TP_PERCENT

    if TP_MODE == "AVERAGE" or current_level >= BE_DCA_LEVEL:
        base_price = avg_price
    else:
        if first_price is None:
            _, _, first_price = get_dca_history(symbol, side)
        base_price = first_price if first_price else avg_price

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
    log_print(f"TP/SL aktualisiert (Ziel: {target_tp_pct}%)")

# --- MONITOR ---
def monitor_dca():
    log_print("[SYSTEM] Stateless Monitor aktiv.")
    while True:
        try:
            r_pos = api_request("GET", "/openApi/swap/v2/user/positions")
            if not r_pos or not isinstance(r_pos.get("data"), list):
                time.sleep(10)
                continue
            
            for pos in r_pos["data"]:
                if float(pos.get("positionAmt", 0)) == 0: continue
                symbol, side = pos["symbol"], pos["positionSide"]
                
                level, last_price, first_price = get_dca_history(symbol, side)
                if last_price is None: continue
                
                r_ticker = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
                if not r_ticker or "data" not in r_ticker or not isinstance(r_ticker["data"], dict):
                    continue
                
                curr_price = float(r_ticker["data"].get("price", 0))
                if curr_price == 0: continue
                
                diff = ((curr_price / last_price) - 1) * 100
                trigger = (side == "LONG" and diff <= -DCA_DEVIATION_PERCENT) or \
                          (side == "SHORT" and diff >= DCA_DEVIATION_PERCENT)

                if trigger and level < DCA_COUNT:
                    log_print(f"DCA TRIGGER {symbol} Level {level+1} | Diff: {diff:.2f}%")
                    qty = (TRADE_SIZE * (DCA_VOLUME_MULTIPLIER ** level)) / curr_price
                    
                    resp = api_request("POST", "/openApi/swap/v2/trade/order", {
                        "symbol": symbol, "side": "BUY" if side == "LONG" else "SELL",
                        "positionSide": side, "type": "MARKET", "quantity": str(round(qty, 6))
                    })
                    
                    if resp and resp.get("code") == 0:
                        if level + 1 == BE_DCA_LEVEL:
                            send_telegram(f"⚠️ DCA {BE_DCA_LEVEL} @ {symbol} erreicht. Break-Even aktiv.")
                        time.sleep(3)
                        set_tp_sl(symbol, side, level + 1, first_price)
                        
        except Exception as e:
            log_print(f"Monitor Loop Fehler: {e}")
        time.sleep(25)

# --- WEBHOOKS ---
@app.route("/ping")
@app.route("/")
def health(): return "STATLESS_BOT_ONLINE", 200

@app.route("/testorder", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    currency = str(data.get("currency", "")).upper()
    direction = str(data.get("direction", "")).upper()
    if currency and direction in ["LONG", "SHORT"]:
        threading.Thread(target=execute_initial_trade, args=(f"{currency}-USDT", direction)).start()
    return jsonify({"status": "ok"}), 200

def execute_initial_trade(symbol, direction):
    api_request("POST", "/openApi/swap/v2/trade/leverage", {"symbol": symbol, "leverage": str(LEVERAGE), "side": direction, "positionSide": direction})
    r_ticker = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    if not r_ticker or "data" not in r_ticker: return
    price = float(r_ticker["data"].get("price", 0))
    if price == 0: return
    
    qty = round(TRADE_SIZE / price, 6)
    resp = api_request("POST", "/openApi/swap/v2/trade/order", {
        "symbol": symbol, "side": "BUY" if direction == "LONG" else "SELL",
        "positionSide": direction, "type": "MARKET", "quantity": str(qty)
    })
    
    if resp and resp.get("code") == 0:
        log_print(f"Initialkauf {symbol} erfolgreich.")
        time.sleep(3)
        set_tp_sl(symbol, direction, 1, price)

if __name__ == "__main__":
    threading.Thread(target=monitor_dca, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))