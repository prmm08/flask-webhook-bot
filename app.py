# -------- V 3.3: BINGX FUTURES - LONG ONLY (TP/SL/BE BEI BINGX) --------

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

# --- Strategie Settings (Unabhängige Perioden & Timeframes) ---
RSI_TIMEFRAME = "1m"
RSI_PERIOD = 14       # Unabhängige RSI Periode
RSI_THRESHOLD = 70    # Schwellenwert für Long-Entry

EMA_TIMEFRAME = "5m"  # Unabhängiger Timeframe für EMA
EMA_PERIOD = 50       # Unabhängige EMA Periode (Trendfilter)

LEVERAGE = 10
TRADE_SIZE = 10       # USDT pro Trade

# TP/SL/BE Prozent
TP_PERCENT, SL_PERCENT, BE_PERCENT = 2.0, 1.0, 0.5

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

def get_ohlcv(symbol, interval="1m", limit=100):
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        r = requests.get(url, params=params, timeout=10).json()
        return r.get("data", [])
    except: return []

# ---------------- INDIKATOREN ----------------

def calc_rsi(closes, period):
    if len(closes) < period + 1: return 50
    gains = [max(0, closes[-i] - closes[-i-1]) for i in range(1, period + 1)]
    losses = [abs(min(0, closes[-i] - closes[-i-1])) for i in range(1, period + 1)]
    avg_gain = sum(gains) / period or 0.0001
    avg_loss = sum(losses) / period or 0.0001
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calc_ema(closes, period):
    if len(closes) < period: return closes[-1]
    alpha = 2 / (period + 1)
    ema = closes
    for price in closes[1:]:
        ema = (price * alpha) + (ema * (1 - alpha))
    return ema

# ---------------- POSITION ACTIONS ----------------

def has_active_position(symbol):
    try:
        ts = str(int(time.time() * 1000))
        params = {"symbol": symbol, "timestamp": ts}
        params["signature"] = sign_bingx(params)
        r = requests.get(f"{BINGX_BASE}/openApi/swap/v2/user/positions", params=params, headers={"X-BX-APIKEY": API_KEY}, timeout=10).json()
        for p in r.get("data", []):
             if abs(float(p.get("positionAmt", 0))) > 0 and p.get("positionSide") == "LONG": return True
        return False
    except: return False

def set_tp_sl(symbol, qty, tp_price, sl_price):
    """ Platziert TP und SL Orders an der Börse. """
    def place_order(price, o_type):
        ts = str(int(time.time() * 1000))
        params = {
            "symbol": symbol, "side": "SELL", "positionSide": "LONG",
            "type": o_type, "quantity": str(qty), "stopPrice": "{:.6f}".format(price),
            "workingType": "MARK_PRICE", "closePosition": "true", "timestamp": ts
        }
        r = requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/order?{urllib.parse.urlencode(sorted(params.items()))}&signature={sign_bingx(params)}", headers={"X-BX-APIKEY": API_KEY}).json()
        if r.get("code") != 0:
            print(f"[ERROR] Fehler beim Setzen von {o_type}: {r.get('msg')}")

    place_order(tp_price, "TAKE_PROFIT_MARKET")
    place_order(sl_price, "STOP_MARKET")

# ---------------- MONITORING BE ----------------

def monitor_be(symbol, entry_price, be_trigger_price):
    be_active = False
    print(f"[MONITOR BE] Warte auf BE-Trigger ({be_trigger_price}) für {symbol}.")
    
    while has_active_position(symbol):
        curr = get_price_bingx(symbol)
        if not curr: time.sleep(2); continue

        if not be_active and curr >= be_trigger_price:
            be_active = True
            print(f"[BE STATUS] {symbol} Profit erreicht. Aktualisiere SL auf Entry Price ({entry_price}).")
            
            # --- Hier findet das Nachziehen statt ---
            # 1. Alle ausstehenden Stop-Orders stornieren (nur SL, TP bleibt)
            ts_cancel = str(int(time.time() * 1000))
            params_cancel = {"symbol": symbol, "timestamp": ts_cancel}
            requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/cancelAllOpenOrders?{urllib.parse.urlencode(sorted(params_cancel.items()))}&signature={sign_bingx(params_cancel)}", headers={"X-BX-APIKEY": API_KEY})

            time.sleep(1)

            # 2. Neue SL-Order am Entry Price platzieren
            # Die Menge muss hier leider statisch sein oder erneut abgefragt werden, 
            # da BingX keine einfache "Modify SL" Funktion bietet. Wir nutzen TRADE_SIZE
            # für die Schätzung der ursprünglichen Menge (ungenau, aber notwendig).

            # Bessere Methode: Positionsdaten abfragen um genaue Menge zu erhalten
            qty = 0
            ts_pos = str(int(time.time() * 1000))
            params_pos = {"symbol": symbol, "timestamp": ts_pos}
            params_pos["signature"] = sign_bingx(params_pos)
            r_pos = requests.get(f"{BINGX_BASE}/openApi/swap/v2/user/positions", params=params_pos, headers={"X-BX-APIKEY": API_KEY}, timeout=10).json()
            for p in r_pos.get("data", []):
                 if abs(float(p.get("positionAmt", 0))) > 0 and p.get("positionSide") == "LONG": 
                     qty = float(p.get("positionAmt"))
                     break
            
            if qty > 0:
                ts_new_sl = str(int(time.time() * 1000))
                params_new_sl = {
                    "symbol": symbol, "side": "SELL", "positionSide": "LONG",
                    "type": "STOP_MARKET", "quantity": str(qty), "stopPrice": "{:.6f}".format(entry_price),
                    "workingType": "MARK_PRICE", "closePosition": "true", "timestamp": ts_new_sl
                }
                requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/order?{urllib.parse.urlencode(sorted(params_new_sl.items()))}&signature={sign_bingx(params_new_sl)}", headers={"X-BX-APIKEY": API_KEY})
                print(f"[BE SUCCESS] Neuer SL bei Entry Price {entry_price} gesetzt.")

            break # Monitoring-Thread kann beendet werden, da SL nun bei BE liegt und TP aktiv ist.
        
        time.sleep(3)

# ---------------- EXECUTION LOGIC ----------------

def execute_trade_bingx(symbol):
    
    if has_active_position(symbol):
        print(f"[SKIP] Position für {symbol} existiert bereits. Warte auf Exit/BE.")
        return
            
    ohlcv_rsi = get_ohlcv(symbol, RSI_TIMEFRAME, limit=RSI_PERIOD + 1)
    if not ohlcv_rsi: return
    closes_rsi = [float(c["close"]) for c in ohlcv_rsi]
    rsi = calc_rsi(closes_rsi, RSI_PERIOD)
    
    ohlcv_ema = get_ohlcv(symbol, EMA_TIMEFRAME, limit=EMA_PERIOD)
    if not ohlcv_ema: return
    closes_ema = [float(c["close"]) for c in ohlcv_ema]
    ema = calc_ema(closes_ema, EMA_PERIOD)
    
    current_price = get_price_bingx(symbol)
    if not current_price: return
    
    is_uptrend = current_price > ema
    is_overbought = rsi >= RSI_THRESHOLD
    
    if is_uptrend and is_overbought:
        qty = round(TRADE_SIZE / current_price, 6)
        ts = str(int(time.time() * 1000))
        
        print(f"[ENTRY LONG] {symbol} @ {current_price} | RSI({RSI_TIMEFRAME}:{RSI_PERIOD}): {rsi:.1f} | EMA({EMA_TIMEFRAME}:{EMA_PERIOD}): {ema:.2f}")
        
        entry_params = {
            "symbol": symbol, "side": "BUY", "positionSide": "LONG", 
            "type": "MARKET", "quantity": str(qty), "leverage": str(LEVERAGE), "timestamp": ts
        }
        
        requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/order?{urllib.parse.urlencode(sorted(entry_params.items()))}&signature={sign_bingx(entry_params)}", headers={"X-BX-APIKEY": API_KEY})
        
        tp = current_price * (1 + TP_PERCENT / 100)
        sl = current_price * (1 - SL_PERCENT / 100)
        be_trigger = current_price * (1 + BE_PERCENT / 100)

        time.sleep(1) # Kurze Pause, damit Entry sicher durch ist
        set_tp_sl(symbol, qty, tp, sl) # TP und SL Orders an Börse senden
        
        # Starte den BE Monitoring Thread, der nur den SL nachzieht
        threading.Thread(target=monitor_be, args=(symbol, current_price, be_trigger)).start()
        
    else:
        reason = f"Trend ({EMA_TIMEFRAME}:{EMA_PERIOD}) negativ" if not is_uptrend else f"RSI ({RSI_TIMEFRAME}:{RSI_PERIOD}) {rsi:.1f} < {RSI_THRESHOLD}"
        print(f"[SKIP] {symbol}: {reason}")

# ---------------- WEBHOOK ----------------

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
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
