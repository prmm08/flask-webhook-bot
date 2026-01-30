import time
import os
import hmac
import hashlib
import requests
import urllib.parse
from math import floor
from db import get_pending_trades, get_open_trades, update_trade_execution, update_dca, close_trade, fail_trade, init_db

# --- KONFIGURATION ---
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

# Telegram
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# DCA Einstellungen
DCA_DEVIATION = 5.0     # 5% Abstand vom Durchschnittspreis
DCA_MULTIPLIER = 2.0    # Volumen verdoppeln (Punkt 4)
DCA_MAX_STEPS = 4
DCA_FEES_BUFFER = 0.15  # Break Even + 0.15% (um Gebühren zu decken)

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def send_telegram(message):
    # Punkt 5: Diese Funktion wird nur noch bei DCA aufgerufen
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

# --- NEU: FUNKTION UM TP ZU UPDATE (PUNKT 3) ---
def update_tp_to_breakeven(symbol, direction, avg_price, total_qty):
    """
    Löscht alle Orders und setzt TP auf (AvgPrice + Fees).
    """
    log(f"   [TP UPDATE] Setze TP auf Break-Even für {symbol}...")
    
    # 1. Alle alten Orders löschen (alter TP/SL muss weg)
    ords = api_request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol})
    for o in ords.get("data", {}).get("orders", []):
        api_request("POST", "/openApi/swap/v2/trade/cancelOrder", {"symbol": symbol, "orderId": o["orderId"]})
    
    time.sleep(1)
    
    # 2. Neuen TP berechnen (Break Even + kleine Gebühr)
    info = get_symbol_info(symbol)
    if direction == "LONG":
        tp_price = avg_price * (1 + DCA_FEES_BUFFER/100)
    else:
        tp_price = avg_price * (1 - DCA_FEES_BUFFER/100)
        
    tp_price = round_step(tp_price, info['price_step'])
    
    # 3. TP Order senden (RedcuseOnly / closePosition nicht zwingend bei TP/SL Order Typen, aber sicherer)
    close_side = "SELL" if direction == "LONG" else "BUY"
    
    # Wir nutzen TAKE_PROFIT_MARKET für die ganze Position
    # Achtung: quantity muss String sein. total_qty muss positiv sein.
    payload = {
        "symbol": symbol,
        "side": close_side,
        "positionSide": direction,
        "type": "TAKE_PROFIT_MARKET",
        "stopPrice": str(tp_price),
        "workingType": "MARK_PRICE",
        "quantity": str(round_step(abs(total_qty), info['qty_step'])),
        "closePosition": "true" # Schließt alles, wenn Preis erreicht
    }
    
    api_request("POST", "/openApi/swap/v2/trade/order", payload)
    log(f"   [TP SET] Neuer TP @ {tp_price} (BE)")

# --- TRADING LOGIC ---

def place_initial_tp_sl(symbol, side, entry, tp_p, sl_p, quantity):
    # Die normale erste TP/SL Setzung (wie gehabt)
    time.sleep(5)
    info = get_symbol_info(symbol)
    tp_price = round_step(entry * (1 + tp_p/100 if side == "LONG" else 1 - tp_p/100), info['price_step'])
    sl_price = round_step(entry * (1 - sl_p/100 if side == "LONG" else 1 + sl_p/100), info['price_step'])
    close_side = "SELL" if side == "LONG" else "BUY"
    str_qty = str(round_step(quantity, info['qty_step']))
    
    for o_type, price in [("TAKE_PROFIT_MARKET", tp_price), ("STOP_MARKET", sl_price)]:
        payload = {
            "symbol": symbol, "side": close_side, "positionSide": side,
            "type": o_type, "stopPrice": str(price), "workingType": "MARK_PRICE",
            "quantity": str_qty, "closePosition": "true"
        }
        api_request("POST", "/openApi/swap/v2/trade/order", payload)

def execute_pending_trade(trade):
    symbol = trade['symbol']
    # Limit check ist jetzt in app.py, hier führen wir einfach aus
    log(f"--- START TRADE: {symbol} ---")
    
    p_res = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    if not p_res: return
    price = float(p_res["data"]["price"])
    
    api_request("POST", "/openApi/swap/v2/trade/leverage", {"symbol": symbol, "leverage": trade['leverage'], "side": trade['direction']})
    
    info = get_symbol_info(symbol)
    qty = round_step(trade['trade_size'] / price, info['qty_step'])
    
    res = api_request("POST", "/openApi/swap/v2/trade/order", {
        "symbol": symbol, "side": "BUY" if trade['direction'] == "LONG" else "SELL",
        "positionSide": trade['direction'], "type": "MARKET", "quantity": str(qty)
    })
    
    if res and res.get("code") == 0:
        log(f"   [FILLED] Entry @ {price}")
        update_trade_execution(trade['id'], price, qty)
        place_initial_tp_sl(symbol, trade['direction'], price, trade['tp_percent'], trade['sl_percent'], qty)
        # KEIN TELEGRAM HIER (Punkt 5)
    else:
        log(f"   [FAIL] {res}")
        fail_trade(trade['id'])

def manage_open_trade(trade):
    symbol = trade['symbol']
    
    # 1. ECHTE DATEN VON BINGX HOLEN
    pos_res = api_request("GET", "/openApi/swap/v2/user/positions", {"symbol": symbol})
    if not pos_res or "data" not in pos_res: return

    position_exists = False
    current_qty = 0.0
    avg_price = 0.0 # Das ist der Schlüssel für Punkt 1 (Abstand) und 3 (BE)
    
    for p in pos_res["data"]:
        if p["symbol"] == symbol and float(p["positionAmt"]) != 0:
            position_exists = True
            current_qty = float(p["positionAmt"])
            avg_price = float(p.get("avgPrice", 0))
            break
    
    if not position_exists:
        log(f"[SYNC] {symbol} geschlossen.")
        close_trade(trade['id'])
        return

    # 2. DCA LOGIK (PUNKT 1 & 4)
    quote_res = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    if not quote_res: return
    curr_price = float(quote_res["data"]["price"])
    
    if trade['dca_level'] < DCA_MAX_STEPS:
        # PUNKT 1: Abstand basiert auf dem Avg Price (nicht Entry Price)
        # Wenn Avg 100 und Dev 5% -> Kauf bei 95.
        # Wenn neuer Avg 97.5 -> Kauf bei 92.625 (97.5 * 0.95)
        baseline = avg_price 
        req_drop = DCA_DEVIATION # Fixe 5% vom aktuellen Durchschnitt
        
        triggered = False
        if trade['direction'] == "LONG" and curr_price <= baseline * (1 - req_drop/100): triggered = True
        if trade['direction'] == "SHORT" and curr_price >= baseline * (1 + req_drop/100): triggered = True
        
        if triggered:
            log(f"[DCA TRIGGER] {symbol} Level {trade['dca_level']+1}")
            
            # PUNKT 4: Multiplier korrekt berechnen (Base Size * 2^(Level+1))
            # Level 0 (Start) -> Level 1 (DCA1) = Base * 2^1 = 200
            # Level 1 (DCA1)  -> Level 2 (DCA2) = Base * 2^2 = 400
            info = get_symbol_info(symbol)
            new_size_usdt = trade['trade_size'] * (DCA_MULTIPLIER ** (trade['dca_level'] + 1))
            new_qty = round_step(new_size_usdt / curr_price, info['qty_step'])
            
            res = api_request("POST", "/openApi/swap/v2/trade/order", {
                "symbol": symbol, "side": "BUY" if trade['direction'] == "LONG" else "SELL",
                "positionSide": trade['direction'], "type": "MARKET", "quantity": str(new_qty)
            })
            
            if res and res.get("code") == 0:
                # Warten bis Position geupdated ist
                time.sleep(3)
                
                # Neuen Durchschnitt holen für TP Berechnung
                pos_upd = api_request("GET", "/openApi/swap/v2/user/positions", {"symbol": symbol})
                new_avg = avg_price # Fallback
                new_total = current_qty
                if pos_upd and pos_upd.get("data"):
                     for p in pos_upd["data"]:
                        if p["symbol"] == symbol:
                            new_avg = float(p.get("avgPrice", avg_price))
                            new_total = float(p.get("positionAmt", current_qty))

                # DB Update
                update_dca(trade['id'], trade['dca_level'] + 1, new_avg, abs(new_total))
                
                # PUNKT 3: TP auf Break Even setzen
                update_tp_to_breakeven(symbol, trade['direction'], new_avg, new_total)
                
                # PUNKT 5: Telegram Nachricht
                send_telegram(f"📉 <b>DCA EXECUTION</b> ({trade['dca_level']+1})\nSymbol: {symbol}\nNew Avg: {new_avg}\nNext TP: Break-Even")

# --- MAIN LOOP ---
if __name__ == "__main__":
    init_db()
    log("Worker v2 gestartet (DCA Fixes + BE Logic)")
    
    while True:
        try:
            pending = get_pending_trades()
            for t in pending: execute_pending_trade(t)
            
            active = get_open_trades()
            for t in active: manage_open_trade(t)
        except Exception as e:
            log(f"Loop Error: {e}")
        time.sleep(3)