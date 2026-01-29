import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
import time
import json
import logging
from flask import Flask, request, jsonify

# --- API ---
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
app = Flask(__name__)

# --- DEFAULT SETTINGS ---
LEVERAGE = 20
TRADE_SIZE = 100
TP_PERCENT = 1
SL_PERCENT = 40

# --- DCA SETTINGS ---
DCA_INTERVAL = 5
DCA_COUNT = 4
DCA_DEVIATION_PERCENT = 5.0  # 1% Marktbewegung
DCA_VOLUME_MULTIPLIER = 2
DCA_SAVE_FILE = "active_dca.json"

active_dca = {}
dca_lock = threading.Lock()

# ============================================================
#   PERSISTENCE & API HELPERS
# ============================================================

def save_dca_data():
    with dca_lock:
        try:
            with open(DCA_SAVE_FILE, "w") as f:
                json.dump(active_dca, f)
        except Exception as e: print("[SAVE ERROR]", e)

def load_dca_data():
    global active_dca
    if os.path.exists(DCA_SAVE_FILE):
        try:
            with open(DCA_SAVE_FILE, "r") as f:
                active_dca = json.load(f)
        except Exception as e: print("[LOAD ERROR]", e)

def sign_bingx(params):
    query_string = urllib.parse.urlencode(sorted((k, str(v)) for k, v in params.items()))
    return hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()

def api_request(method, endpoint, params=None):
    url = f"{BINGX_BASE}{endpoint}"
    headers = {"X-BX-APIKEY": API_KEY}
    params = dict(params) if params else {}
    if "timestamp" not in params:
        params["timestamp"] = str(int(time.time() * 1000))
    
    sig = sign_bingx(params)
    if method == "GET":
        r = requests.get(f"{url}?{urllib.parse.urlencode(params)}&signature={sig}", headers=headers)
    else:
        r = requests.post(f"{url}?{urllib.parse.urlencode(params)}&signature={sig}", headers=headers)
    return r.json()

def get_price(symbol):
    r = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    return float(r["data"]["price"]) if r and "data" in r else None

def get_positions():
    r = api_request("GET", "/openApi/swap/v2/user/positions")
    return r.get("data", []) if r else []

# ============================================================
#   TP / SL LOGIC (EXCHANGE SIDE)
# ============================================================

def reset_tp_sl(symbol, side):
    r = api_request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol})
    orders = r.get("data", {}).get("orders", []) if r else []
    for o in orders:
        if o.get("positionSide") == side and o.get("type") in ("TAKE_PROFIT_MARKET", "STOP_MARKET"):
            api_request("POST", "/openApi/swap/v2/trade/cancelOrder", {"symbol": symbol, "orderId": o["orderId"]})

def set_exchange_tp_sl(symbol, side, entry, tp_p, sl_p):
    tp = entry * (1 + tp_p/100) if side == "LONG" else entry * (1 - tp_p/100)
    sl = entry * (1 - sl_p/100) if side == "LONG" else entry * (1 + sl_p/100)
    
    for price, otype in [(tp, "TAKE_PROFIT_MARKET"), (sl, "STOP_MARKET")]:
        api_request("POST", "/openApi/swap/v2/trade/order", {
            "symbol": symbol, "side": "SELL" if side == "LONG" else "BUY",
            "positionSide": side, "type": otype, "stopPrice": f"{price:.6f}",
            "workingType": "MARK_PRICE", "closePosition": "true"
        })

# ============================================================
#   WATCHER LOGIC (DCA & BREAK EVEN)
# ============================================================

def monitor_worker():
    while True:
        try:
            positions = get_positions()
            pos_dict = {p["symbol"]: p for p in positions if float(p["positionAmt"]) != 0}

            # Cleanup local data
            with dca_lock:
                to_delete = [s for s in active_dca if s not in pos_dict]
                for s in to_delete: del active_dca[s]
                if to_delete: save_dca_data()

            for symbol, pos in pos_dict.items():
                side = pos["positionSide"]
                current_price = get_price(symbol)
                if not current_price: continue

                with dca_lock:
                    if symbol not in active_dca: continue
                    d = active_dca[symbol]

                # --- FALL A: DCA TRIGGER ---
                triggered = (side == "LONG" and current_price <= d["entry_static"] * (1 - DCA_DEVIATION_PERCENT/100)) or \
                            (side == "SHORT" and current_price >= d["entry_static"] * (1 + DCA_DEVIATION_PERCENT/100))
                
                if triggered and d["executed"] < DCA_COUNT:
                    print(f"[DCA] Kaufe nach für {symbol}")
                    qty = round((d["base_trade_size"] * (DCA_VOLUME_MULTIPLIER ** (d["executed"] + 1))) / current_price, 6)
                    api_request("POST", "/openApi/swap/v2/trade/order", {
                        "symbol": symbol, "side": "BUY" if side == "LONG" else "SELL",
                        "positionSide": side, "type": "MARKET", "quantity": str(qty)
                    })
                    # Nach DCA: Exchange TP/SL löschen (Bot übernimmt ab jetzt BE-Exit)
                    reset_tp_sl(symbol, side)
                    with dca_lock:
                        d["executed"] += 1
                        time.sleep(1)
                        new_p = next((p for p in get_positions() if p["symbol"] == symbol and p["positionSide"] == side), None)
                        if new_p: d["entry_dynamic"] = float(new_p["avgPrice"])
                        save_dca_data()

                # --- FALL B: BREAK EVEN EXIT (Nur wenn DCA bereits erfolgte) ---
                if d["executed"] > 0:
                    be_reached = (side == "LONG" and current_price >= d["entry_dynamic"]) or \
                                 (side == "SHORT" and current_price <= d["entry_dynamic"])
                    if be_reached:
                        print(f"[BE EXIT] Schließe {symbol} am Break-Even.")
                        api_request("POST", "/openApi/swap/v2/trade/order", {
                            "symbol": symbol, "side": "SELL" if side == "LONG" else "BUY",
                            "positionSide": side, "type": "MARKET", "closePosition": "true"
                        })

        except Exception as e: print("[WORKER ERROR]", e)
        time.sleep(DCA_INTERVAL)

# ============================================================
#   EXECUTION & FLASK
# ============================================================

def execute_trade(symbol, direction, leverage, trade_size, tp_percent, sl_percent):
    # Check if already open
    if any(p["symbol"] == symbol and p["positionSide"] == direction for p in get_positions()):
        return

    price = get_price(symbol)
    if not price or not set_leverage_for_symbol(symbol, leverage, direction): return
    
    qty = round(trade_size / price, 6)
    api_request("POST", "/openApi/swap/v2/trade/order", {
        "symbol": symbol, "side": "BUY" if direction == "LONG" else "SELL",
        "positionSide": direction, "type": "MARKET", "quantity": str(qty)
    })
    
    # 1. Sofort TP/SL am Exchange setzen (Sicherheit für ersten Trade)
    time.sleep(1)
    set_exchange_tp_sl(symbol, direction, price, tp_percent, sl_percent)

    # 2. In Watcher-Liste aufnehmen
    with dca_lock:
        active_dca[symbol] = {
            "side": direction, "entry_static": price, "entry_dynamic": price,
            "executed": 0, "base_trade_size": trade_size
        }
        save_dca_data()

def set_leverage_for_symbol(symbol, leverage, side):
    r = api_request("POST", "/openApi/swap/v2/trade/leverage", {"symbol": symbol, "leverage": leverage, "side": "BUY" if side=="LONG" else "SELL"})
    return r.get("code") == 0

@app.route("/testorder", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    symbol = f"{str(data.get('currency', '')).upper()}-USDT"
    direction = str(data.get("direction", "")).upper()
    
    if direction in ("LONG", "SHORT"):
        # Korrektur der Klammern:
        threading.Thread(
            target=execute_trade, 
            args=(
                symbol, 
                direction, 
                int(data.get("leverage", LEVERAGE)), 
                float(data.get("trade_size", TRADE_SIZE)), 
                float(data.get("tp_percent", TP_PERCENT)), 
                float(data.get("sl_percent", SL_PERCENT))
            )
        ).start()
        
    return jsonify({"status": "processing"}), 200

if __name__ == "__main__":
    if not API_KEY or not API_SECRET:
        print("FEHLER: API Keys fehlen in den Environment Variables!")
    else:
        load_dca_data()
        # Der Worker übernimmt jetzt DCA und Break-Even Monitoring
        threading.Thread(target=monitor_worker, daemon=True).start()
        
        print("Bot gestartet. Warte auf Webhooks...")
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
