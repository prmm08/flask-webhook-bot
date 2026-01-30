import time
import os
import hmac
import hashlib
import requests
import urllib.parse
from math import floor
# Importiere unsere DB Funktionen
from db import get_pending_trades, get_open_trades, update_trade_execution, update_dca, close_trade, fail_trade, init_db

# --- BINGX CONFIG ---
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

# --- DCA SETTINGS ---
DCA_DEVIATION = 5.0
DCA_MULTIPLIER = 2.0
DCA_MAX_STEPS = 5
DCA_EXIT_TP = 1.2  # Virtual TP
DCA_EXIT_SL = 40.0 # Virtual SL

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

# --- API HELPER ---
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

# --- TRADING ACTIONS ---

def place_tp_sl(symbol, side, entry, tp_p, sl_p, quantity):
    """
    Die 'Force'-Methode mit quantity+closePosition
    """
    log(f"   [TP/SL] Warte 5s auf Sync für {symbol}...")
    time.sleep(5) 
    
    info = get_symbol_info(symbol)
    tp_price = round_step(entry * (1 + tp_p/100 if side == "LONG" else 1 - tp_p/100), info['price_step'])
    sl_price = round_step(entry * (1 - sl_p/100 if side == "LONG" else 1 + sl_p/100), info['price_step'])
    
    close_side = "SELL" if side == "LONG" else "BUY"
    str_qty = str(round_step(quantity, info['qty_step']))
    
    orders = [("TAKE_PROFIT_MARKET", tp_price), ("STOP_MARKET", sl_price)]
    
    for o_type, price in orders:
        payload = {
            "symbol": symbol, "side": close_side, "positionSide": side,
            "type": o_type, "stopPrice": str(price), "workingType": "MARK_PRICE",
            "quantity": str_qty, "closePosition": "true"
        }
        # Retry Loop
        for i in range(3):
            res = api_request("POST", "/openApi/swap/v2/trade/order", payload)
            if res and res.get("code") == 0:
                log(f"   [OK] {o_type} gesetzt.")
                break
            time.sleep(1)

def execute_pending_trade(trade):
    symbol = trade['symbol']
    log(f"--- FÜHRE TRADE AUS: {symbol} ---")
    
    # 1. Preis
    p_res = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    if not p_res or "data" not in p_res: return
    price = float(p_res["data"]["price"])
    
    # 2. Leverage
    api_request("POST", "/openApi/swap/v2/trade/leverage", {"symbol": symbol, "leverage": trade['leverage'], "side": trade['direction']})
    
    # 3. Order
    info = get_symbol_info(symbol)
    qty = round_step(trade['trade_size'] / price, info['qty_step'])
    
    res = api_request("POST", "/openApi/swap/v2/trade/order", {
        "symbol": symbol, "side": "BUY" if trade['direction'] == "LONG" else "SELL",
        "positionSide": trade['direction'], "type": "MARKET", "quantity": str(qty)
    })
    
    if res and res.get("code") == 0:
        log(f"   [FILLED] Entry @ {price}")
        # DB Update: Pending -> Open
        update_trade_execution(trade['id'], price, qty)
        
        # TP/SL setzen
        place_tp_sl(symbol, trade['direction'], price, trade['tp_percent'], trade['sl_percent'], qty)
    else:
        log(f"   [FAIL] {res}")
        fail_trade(trade['id'])

def manage_open_trade(trade):
    symbol = trade['symbol']
    
    # Aktuellen Preis holen
    p_res = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    if not p_res: return
    curr = float(p_res["data"]["price"])
    
    # DCA Logik
    if trade['dca_level'] < DCA_MAX_STEPS:
        entry = trade['entry_price'] # Basis für DCA Stufen
        req_drop = DCA_DEVIATION * (trade['dca_level'] + 1)
        
        triggered = False
        if trade['direction'] == "LONG" and curr <= entry * (1 - req_drop/100): triggered = True
        if trade['direction'] == "SHORT" and curr >= entry * (1 + req_drop/100): triggered = True
        
        if triggered:
            log(f"[DCA TRIGGER] {symbol} Level {trade['dca_level']+1}")
            
            # Alte Orders löschen
            ords = api_request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol})
            for o in ords.get("data", {}).get("orders", []):
                api_request("POST", "/openApi/swap/v2/trade/cancelOrder", {"symbol": symbol, "orderId": o["orderId"]})
            
            # Nachkaufen
            info = get_symbol_info(symbol)
            new_size = trade['trade_size'] * (DCA_MULTIPLIER ** (trade['dca_level'] + 1))
            new_qty = round_step(new_size / curr, info['qty_step'])
            
            res = api_request("POST", "/openApi/swap/v2/trade/order", {
                "symbol": symbol, "side": "BUY" if trade['direction'] == "LONG" else "SELL",
                "positionSide": trade['direction'], "type": "MARKET", "quantity": str(new_qty)
            })
            
            if res and res.get("code") == 0:
                # Neuen Durchschnitt holen
                time.sleep(2)
                pos = api_request("GET", "/openApi/swap/v2/user/positions", {"symbol": symbol})
                if pos and pos.get("data"):
                    avg_p = float(pos["data"][0]["avgPrice"])
                    total_qty = float(pos["data"][0]["positionAmt"]) # Achtung: Short kann hier negativ sein
                    
                    update_dca(trade['id'], trade['dca_level'] + 1, avg_p, abs(total_qty))
                    log(f"   [DCA OK] Neuer Avg: {avg_p}")

    # Virtual Exit Check (Nur wenn DCA aktiv war)
    if trade['dca_level'] > 0:
        avg = trade['avg_price']
        exit_reason = None
        
        if trade['direction'] == "LONG":
            if curr >= avg * (1 + DCA_EXIT_TP/100): exit_reason = "TP"
            elif curr <= avg * (1 - DCA_EXIT_SL/100): exit_reason = "SL"
        else:
            if curr <= avg * (1 - DCA_EXIT_TP/100): exit_reason = "TP"
            elif curr >= avg * (1 + DCA_EXIT_SL/100): exit_reason = "SL"
            
        if exit_reason:
            log(f"[VIRTUAL EXIT] {symbol} via {exit_reason}")
            res = api_request("POST", "/openApi/swap/v2/trade/order", {
                "symbol": symbol, "side": "SELL" if trade['direction'] == "LONG" else "BUY",
                "positionSide": trade['direction'], "type": "MARKET", "closePosition": "true"
            })
            if res and res.get("code") == 0:
                close_trade(trade['id'])

# --- WORKER LOOP ---
if __name__ == "__main__":
    log("--- WORKER GESTARTET ---")
    init_db() # Stellt sicher, dass DB Tabelle existiert
    
    while True:
        try:
            # 1. Neue Trades abarbeiten
            pending = get_pending_trades()
            if pending:
                log(f"Gefunden: {len(pending)} neue Signale.")
                for t in pending:
                    execute_pending_trade(t)
            
            # 2. Laufende Trades managen (DCA)
            active = get_open_trades()
            for t in active:
                manage_open_trade(t)
                
        except Exception as e:
            log(f"Loop Fehler: {e}")
        
        time.sleep(3) # Kurze Pause