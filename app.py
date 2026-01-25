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

# --- LOKALER SPEICHER (Wird beim Start gefüllt) ---
# Struktur: {"SYMBOL_SIDE": {"level": 1, "last_price": 0.0, "first_price": 0.0}}
pos_tracker = {}
tracker_lock = threading.Lock()

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
TRADE_SIZE = 20.0
LEVERAGE = 20

# --- API CORE ---
def api_request(method, endpoint, params=None):
    url = f"{BINGX_BASE}{endpoint}"
    headers = {"X-BX-APIKEY": API_KEY}
    params = dict(params) if params else {}
    params["timestamp"] = str(int(time.time() * 1000))
    query_string = urllib.parse.urlencode(sorted(params.items()))
    signature = hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    full_url = f"{url}?{query_string}&signature={signature}"
    try:
        resp = requests.request(method, full_url, headers=headers, timeout=15)
        return resp.json()
    except Exception as e:
        log_print(f"API Fehler {endpoint}: {e}")
        return None

# --- INITIALER SCAN BEIM START ---
def sync_positions_with_bingx():
    log_print("[SYNC] Starte initialen Positions-Scan...")
    r_pos = api_request("GET", "/openApi/swap/v2/user/positions")
    if not r_pos or not isinstance(r_pos.get("data"), list):
        log_print("[SYNC] Keine aktiven Positionen gefunden.")
        return

    with tracker_lock:
        for pos in r_pos["data"]:
            amt = float(pos.get("positionAmt", 0))
            if amt == 0: continue
            
            symbol = pos["symbol"]
            side = pos["positionSide"]
            key = f"{symbol}_{side}"
            
            # Historie für diese Position einmalig abfragen
            r_orders = api_request("GET", "/openApi/swap/v2/trade/allOrders", {"symbol": symbol, "limit": 20})
            if r_orders and isinstance(r_orders.get("data"), list):
                filled = [o for o in r_orders["data"] if o.get("status") == "FILLED" and o.get("positionSide") == side and o.get("type") == "MARKET"]
                if filled:
                    pos_tracker[key] = {
                        "level": len(filled),
                        "last_price": float(filled[0]["avgPrice"]),
                        "first_price": float(filled[-1]["avgPrice"])
                    }
                    log_print(f"[SYNC] Gefunden: {key} auf DCA Level {len(filled)}")

# --- TP / SL SETZEN ---
def set_tp_sl(symbol, side, current_level, first_price=None):
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

# --- MONITOR (Arbeitet jetzt mit lokalem pos_tracker) ---
def monitor_dca():
    log_print("[SYSTEM] Monitor (10s) mit lokalem Tracking aktiv.")
    while True:
        try:
            # Wir prüfen nur Symbole, die wir im Tracker haben
            keys_to_check = list(pos_tracker.keys())
            if not keys_to_check:
                time.sleep(10)
                continue

            for key in keys_to_check:
                symbol, side = key.split("_")
                data = pos_tracker.get(key)
                if not data: continue

                # Aktuellen Preis holen
                r_ticker = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
                if not r_ticker or "data" not in r_ticker: continue
                curr_price = float(r_ticker["data"].get("price", 0))
                
                last_price = data["last_price"]
                level = data["level"]
                
                diff = ((curr_price / last_price) - 1) * 100
                trigger = (side == "LONG" and diff <= -DCA_DEVIATION_PERCENT) or (side == "SHORT" and diff >= DCA_DEVIATION_PERCENT)

                if trigger and level < DCA_COUNT:
                    log_print(f"[DCA] Trigger {symbol} Level {level+1} (Diff: {diff:.2f}%)")
                    qty = (TRADE_SIZE * (DCA_VOLUME_MULTIPLIER ** level)) / curr_price
                    resp = api_request("POST", "/openApi/swap/v2/trade/order", {
                        "symbol": symbol, "side": "BUY" if side == "LONG" else "SELL",
                        "positionSide": side, "type": "MARKET", "quantity": str(round(qty, 6))
                    })
                    
                    if resp and resp.get("code") == 0:
                        # Lokalen Speicher SOFORT aktualisieren
                        with tracker_lock:
                            pos_tracker[key]["level"] += 1
                            pos_tracker[key]["last_price"] = curr_price
                        
                        time.sleep(2)
                        set_tp_sl(symbol, side, level + 1, data["first_price"])
            
            # Check, ob Positionen geschlossen wurden (aus Tracker entfernen)
            # Um API zu sparen, machen wir das nur alle 30 Sek
            if int(time.time()) % 30 < 10:
                r_pos = api_request("GET", "/openApi/swap/v2/user/positions")
                if r_pos and "data" in r_pos:
                    active_keys = [f"{p['symbol']}_{p['positionSide']}" for p in r_pos["data"] if float(p.get("positionAmt", 0)) != 0]
                    with tracker_lock:
                        for k in list(pos_tracker.keys()):
                            if k not in active_keys:
                                log_print(f"[CLEANUP] Position {k} geschlossen. Entferne aus Tracker.")
                                del pos_tracker[k]

        except Exception as e:
            log_print(f"Monitor Fehler: {e}")
        time.sleep(10)

# --- WEBHOOKS ---
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
    price = float(r_ticker["data"].get("price", 0)) if r_ticker else 0
    if price == 0: return
    
    qty = round(TRADE_SIZE / price, 6)
    resp = api_request("POST", "/openApi/swap/v2/trade/order", {"symbol": symbol, "side": "BUY" if direction == "LONG" else "SELL", "positionSide": direction, "type": "MARKET", "quantity": str(qty)})
    
    if resp and resp.get("code") == 0:
        key = f"{symbol}_{direction}"
        with tracker_lock:
            pos_tracker[key] = {"level": 1, "last_price": price, "first_price": price}
        log_print(f"[INIT] {symbol} gestartet & im Tracker gespeichert.")
        time.sleep(3)
        set_tp_sl(symbol, direction, 1, price)

if __name__ == "__main__":
    # 1. Einmalige Synchronisation beim Start
    sync_positions_with_bingx()
    # 2. Monitor starten
    threading.Thread(target=monitor_dca, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))