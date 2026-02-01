import time
import os
import hmac
import hashlib
import requests
import urllib.parse
from math import floor
from datetime import datetime, timezone
from db import get_pending_trades, get_open_trades, update_trade_execution, update_dca, close_trade, fail_trade, init_db, check_trade_exists, get_conn # <--- get_conn importieren

# --- KONFIGURATION ---
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

# WICHTIG: Das Limit muss auch hier bekannt sein!
MAX_OPEN_POSITIONS = 20

# Telegram
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# DCA Settings
DCA_DEVIATION = 5.0     
DCA_MULTIPLIER = 2.0    
DCA_MAX_STEPS = 4       
DCA_FEES_BUFFER = 0.15  
DCA_COOLDOWN_SEC = 60   

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def send_telegram(message):
    if not TG_TOKEN or not TG_CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data={
            "chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML"
        }, timeout=5)
    except: pass

# --- API HELPERS ---
def get_sign(params):
    params["timestamp"] = str(int(time.time() * 1000))
    query_string = urllib.parse.urlencode(sorted(params.items()))
    sig = hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    return f"{query_string}&signature={sig}"

def api_request(method, endpoint, payload=None):
    if payload is None: payload = {}
    query_with_sig = get_sign(payload)
    url = f"{BINGX_BASE}{endpoint}?{query_with_sig}"
    headers = {"X-BX-APIKEY": API_KEY}
    try:
        if method == "GET": r = requests.get(url, headers=headers, timeout=10)
        else: r = requests.post(url, headers=headers, timeout=10)
        return r.json()
    except Exception as e:
        log(f"API Error: {e}")
        return None

def get_symbol_info(symbol):
    r = api_request("GET", "/openApi/swap/v2/quote/contracts")
    if r and "data" in r:
        for item in r["data"]:
            if item["symbol"] == symbol:
                p_p = 1 / (10 ** float(item.get("pricePrecision", 4)))
                q_p = 1 / (10 ** float(item.get("quantityPrecision", 2)))
                return {"price_step": p_p, "qty_step": q_p}
    return {"price_step": 0.0001, "qty_step": 0.0001}

def round_step(value, step):
    if not step: return value
    return round(floor(value * (1/step) + 0.00000001) / (1/step), 8)

# --- SPECIAL DB HELPER ---
def get_strict_open_count():
    """Zählt NUR Trades, die wirklich 'OPEN' sind (Geld im Markt)."""
    conn = get_conn()
    if not conn: return 999 # Sicherheits-Blocker
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as count FROM trades WHERE status = 'OPEN'")
        res = cur.fetchone()
        return res['count'] if res else 0
    except: return 999
    finally:
        conn.close()

# --- TP MANAGEMENT ---
def set_robust_tp(symbol, avg_price, total_qty, current_level, target_tp_percent):
    is_dca_active = current_level > 0
    if is_dca_active:
        percent = DCA_FEES_BUFFER
        tp_type_str = "Break-Even"
    else:
        percent = target_tp_percent
        tp_type_str = f"Target ({percent}%)"

    log(f"   [TP LOGIC] {symbol} (Level {current_level}) -> Setze {tp_type_str} TP...")
    info = get_symbol_info(symbol)
    tp_price = avg_price * (1 - percent/100)
    tp_price = round_step(tp_price, info['price_step'])
    qty_str = str(round_step(abs(total_qty), info['qty_step']))

    # Cleanup
    orders_cleared = False
    for i in range(3):
        api_request("POST", "/openApi/swap/v2/trade/cancelAllOrders", {"symbol": symbol})
        time.sleep(1) 
        check = api_request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol})
        if check and "data" in check and "orders" in check["data"] and len(check["data"]["orders"]) == 0:
            orders_cleared = True
            break
        elif not check or "data" not in check:
            orders_cleared = True
            break
            
    payload = {
        "symbol": symbol, "side": "BUY", "positionSide": "SHORT",
        "type": "TAKE_PROFIT_MARKET", 
        "stopPrice": str(tp_price), "workingType": "MARK_PRICE", 
        "quantity": qty_str, "closePosition": "true"
    }
    
    for attempt in range(3):
        res = api_request("POST", "/openApi/swap/v2/trade/order", payload)
        if res and res.get("code") == 0:
            log(f"   [TP SET] ✅ {symbol} TP @ {tp_price}")
            return
        elif res and res.get("code") == 110407:
            api_request("POST", "/openApi/swap/v2/trade/cancelAllOrders", {"symbol": symbol})
            time.sleep(2)
        else:
            break

# --- EXECUTION LOGIC ---

def execute_pending_trade(trade):
    if not check_trade_exists(trade['id']): return

    symbol = trade['symbol']
    if trade['direction'] != "SHORT":
        fail_trade(trade['id'])
        return

    # --- FINALER LIMIT CHECK (Gatekeeper) ---
    # Wir prüfen JETZT (bevor wir kaufen), wie viele wirklich offen sind.
    real_open = get_strict_open_count()
    if real_open >= MAX_OPEN_POSITIONS:
        log(f"[ABORT] Limit erreicht ({real_open}/{MAX_OPEN_POSITIONS}). {symbol} wird übersprungen.")
        
        # Trade als "SKIPPED" oder "ERROR" markieren, damit er aus PENDING rausfliegt
        fail_trade(trade['id']) 
        
        # Telegram Warnung
        send_telegram(f"⛔ <b>WORKER REJECT</b>\n{symbol} cancelled.\nMax Limit ({MAX_OPEN_POSITIONS}) reached.")
        return
    # ----------------------------------------

    log(f"--- START SHORT: {symbol} ---")
    
    p_res = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    if not p_res: return
    price = float(p_res["data"]["price"])
    
    api_request("POST", "/openApi/swap/v2/trade/leverage", {"symbol": symbol, "leverage": trade['leverage'], "side": "SHORT"})
    
    info = get_symbol_info(symbol)
    qty = round_step(trade['trade_size'] / price, info['qty_step'])
    
    res = api_request("POST", "/openApi/swap/v2/trade/order", {
        "symbol": symbol, "side": "SELL", "positionSide": "SHORT", 
        "type": "MARKET", "quantity": str(qty)
    })
    
    if res and res.get("code") == 0:
        log(f"   [FILLED] Short Entry @ {price}")
        update_trade_execution(trade['id'], price, qty)
        set_robust_tp(symbol, price, qty, 0, trade['tp_percent'])
    else:
        log(f"   [FAIL] {res}")
        fail_trade(trade['id'])

def manage_open_trade(trade):
    if not check_trade_exists(trade['id']): return
    symbol = trade['symbol']

    # 1. STATUS SYNC
    pos_res = api_request("GET", "/openApi/swap/v2/user/positions", {"symbol": symbol})
    if not pos_res or "data" not in pos_res: return

    position_exists = False
    current_qty = 0.0
    avg_price = 0.0 
    
    for p in pos_res["data"]:
        if p["symbol"] == symbol and float(p["positionAmt"]) != 0:
            position_exists = True
            current_qty = abs(float(p["positionAmt"])) 
            avg_price = float(p.get("avgPrice", 0))
            break
    
    if not position_exists:
        log(f"[SYNC] {symbol} Short ist geschlossen.")
        close_trade(trade['id'])
        return

    # 2. WATCHDOG
    quote_res = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    if not quote_res: return
    curr_price = float(quote_res["data"]["price"])
    
    if trade['dca_level'] > 0 and avg_price > 0:
        target_price = avg_price * (1 - DCA_FEES_BUFFER/100)
        if curr_price <= target_price:
            res = api_request("POST", "/openApi/swap/v2/trade/order", {
                "symbol": symbol, "side": "BUY", "positionSide": "SHORT", 
                "type": "MARKET", "closePosition": "true"
            })
            if res and res.get("code") == 0: return 

    # 3. COOLDOWN
    last_update = trade.get('updated_at')
    if last_update:
        now = datetime.now(timezone.utc) if last_update.tzinfo else datetime.now()
        seconds_since = (now - last_update).total_seconds()
        if seconds_since < DCA_COOLDOWN_SEC: return 

    # 4. DCA LOGIK
    if trade['dca_level'] < DCA_MAX_STEPS:
        baseline = avg_price 
        target_trigger = baseline * (1 + DCA_DEVIATION/100)
        
        if curr_price >= target_trigger:
            log(f"[DCA TRIGGER] {symbol} Short Level {trade['dca_level']+1}")
            
            info = get_symbol_info(symbol)
            multiplier_now = DCA_MULTIPLIER ** (trade['dca_level'] + 1)
            new_size_usdt = trade['trade_size'] * multiplier_now
            new_qty = round_step(new_size_usdt / curr_price, info['qty_step'])
            
            res = api_request("POST", "/openApi/swap/v2/trade/order", {
                "symbol": symbol, "side": "SELL", "positionSide": "SHORT", 
                "type": "MARKET", "quantity": str(new_qty)
            })
            
            if res and res.get("code") == 0:
                time.sleep(3)
                
                pos_upd = api_request("GET", "/openApi/swap/v2/user/positions", {"symbol": symbol})
                new_avg = avg_price
                new_total = current_qty
                if pos_upd and pos_upd.get("data"):
                     for p in pos_upd["data"]:
                        if p["symbol"] == symbol:
                            new_avg = float(p.get("avgPrice", avg_price))
                            new_total = abs(float(p.get("positionAmt", current_qty)))

                new_level = trade['dca_level'] + 1
                update_dca(trade['id'], new_level, new_avg, new_total)
                set_robust_tp(symbol, new_avg, new_total, new_level, trade['tp_percent'])
                
                send_telegram(f"📉 <b>SHORT DCA</b> ({new_level}/{DCA_MAX_STEPS})\nSymbol: {symbol}\nAvg: {new_avg}")

if __name__ == "__main__":
    init_db()
    log(f"Worker v13 Started (Strict Limit: {MAX_OPEN_POSITIONS}).")
    while True:
        try:
            pending = get_pending_trades()
            for t in pending: execute_pending_trade(t)
            active = get_open_trades()
            for t in active: manage_open_trade(t)
        except Exception as e:
            log(f"Loop Error: {e}")
        time.sleep(3)