# -------- V 3.4: BINGX FUTURES - LONG ONLY + BREAK-EVEN LOGIK --------

import time
import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
from flask import Flask, request, jsonify
import logging

# --- API Konfiguration ---
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
app = Flask(__name__)

# --- Strategie Settings ---
RSI_TIMEFRAME = "1m"
RSI_PERIOD = 14
RSI_THRESHOLD = 75

EMA_TIMEFRAME = "5m"
EMA_PERIOD = 50

LEVERAGE = 10
TRADE_SIZE = 10       
TP_PERCENT, SL_PERCENT = 5.0, 1.5

# --- Break-Even Settings ---
BE_ACTIVATION_PERCENT = 0.5  # Ab 1% Gewinn wird Break-Even scharf geschaltet
active_be_positions = {}     # Speichert: { "BTC-USDT": {"entry_price": 0.0, "be_active": False} }

# ---------------- SIGNING & HELPERS ----------------

def sign_bingx(params):
    query_string = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(API_SECRET.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()

def get_price_bingx(symbol):
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/price"
        r = requests.get(url, params={"symbol": symbol}, timeout=10).json()
        return float(r["data"]["price"])
    except: return None

def get_open_positions():
    """Holt alle aktuell offenen Positionen."""
    ts = str(int(time.time() * 1000))
    params = {"timestamp": ts}
    url = f"{BINGX_BASE}/openApi/swap/v2/user/positions?{urllib.parse.urlencode(sorted(params.items()))}&signature={sign_bingx(params)}"
    try:
        r = requests.get(url, headers={"X-BX-APIKEY": API_KEY}, timeout=10).json()
        return r.get("data", [])
    except: return []

# ---------------- POSITION ACTIONS ----------------

def close_position_market(symbol):
    """Schließt die gesamte Long-Position sofort zum Marktpreis."""
    ts = str(int(time.time() * 1000))
    params = {
        "symbol": symbol, "side": "SELL", "positionSide": "LONG",
        "type": "MARKET", "quantity": "0", "timestamp": ts # quantity 0 schließt oft alles, sonst Qty holen
    }
    # Da BingX manchmal die exakte Qty braucht, ist es sicherer hier "closePosition": "true" zu nutzen
    params["closePosition"] = "true"
    
    url = f"{BINGX_BASE}/openApi/swap/v2/trade/order?{urllib.parse.urlencode(sorted(params.items()))}&signature={sign_bingx(params)}"
    requests.post(url, headers={"X-BX-APIKEY": API_KEY})
    print(f"[BREAK-EVEN] Position für {symbol} geschlossen.")

def set_tp_sl(symbol, qty, tp_price, sl_price):
    def place_order(price, o_type):
        ts = str(int(time.time() * 1000))
        params = {
            "symbol": symbol, "side": "SELL", "positionSide": "LONG",
            "type": o_type, "quantity": str(qty), "stopPrice": "{:.6f}".format(price),
            "workingType": "MARK_PRICE", "closePosition": "true", "timestamp": ts
        }
        requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/order?{urllib.parse.urlencode(sorted(params.items()))}&signature={sign_bingx(params)}", headers={"X-BX-APIKEY": API_KEY})
    
    place_order(tp_price, "TAKE_PROFIT_MARKET")
    place_order(sl_price, "STOP_MARKET")

# ---------------- BREAK-EVEN MONITOR ----------------

def monitor_break_even():
    """Hintergrund-Thread zur Überwachung der Break-Even-Bedingung."""
    while True:
        try:
            positions = get_open_positions()
            for pos in positions:
                symbol = pos['symbol']
                long_qty = float(pos.get('avgPrice', 0))
                if float(pos['longQty']) > 0:
                    entry_price = float(pos['avgPrice'])
                    current_price = get_price_bingx(symbol)
                    if not current_price: continue

                    # Gewinn in Prozent berechnen
                    profit_pct = (current_price - entry_price) / entry_price * 100

                    # 1. BE Aktivieren wenn Preis Ziel erreicht
                    if profit_pct >= BE_ACTIVATION_PERCENT:
                        if symbol not in active_be_positions or not active_be_positions[symbol]['be_active']:
                            active_be_positions[symbol] = {"entry_price": entry_price, "be_active": True}
                            print(f"[BE-MODUS] Aktiviert für {symbol} (Gewinn > {BE_ACTIVATION_PERCENT}%)")

                    # 2. BE Auslösen wenn Preis zum Einstieg zurückfällt
                    if symbol in active_be_positions and active_be_positions[symbol]['be_active']:
                        if current_price <= entry_price:
                            print(f"[BE-TRIGGER] Preis zurück bei Entry ({current_price}). Schließe {symbol}...")
                            close_position_market(symbol)
                            del active_be_positions[symbol]
                else:
                    # Falls Position durch TP/SL geschlossen wurde, aus BE-Liste löschen
                    if symbol in active_be_positions:
                        del active_be_positions[symbol]
                        
        except Exception as e:
            print(f"Error in BE Monitor: {e}")
        time.sleep(5) # Alle 5 Sekunden prüfen

# ---------------- EXECUTION LOGIC ----------------

def execute_trade_bingx(symbol):
    # (OHLCV Abruf und Indikatoren-Logik wie in V3.1)
    # ... [Inhalt der execute_trade_bingx aus deinem Original] ...
    # Wenn Order ausgeführt wird, wird die Position automatisch vom monitor_break_even erkannt.
    pass # Hier den Rest deines Codes einfügen

# ---------------- START ----------------

@app.route("/testorder", methods=["POST", "GET"])
def handle_alert():
    if request.method == "GET": return jsonify({"status": "ok"}), 200
    data = request.get_json(silent=True) or {}
    currency = str(data.get("currency", "")).upper()
    if not currency: return jsonify({"status": "ignored"}), 200
    symbol = f"{currency}-USDT"
    
    threading.Thread(target=execute_trade_bingx, args=(symbol,)).start()
    return jsonify({"status": "processing", "symbol": symbol}), 200

if __name__ == "__main__":
    # Startet den Überwachungs-Thread für Break-Even
    threading.Thread(target=monitor_break_even, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
