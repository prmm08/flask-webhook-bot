# -------- V 4.9: BINGX FUTURES - EMA LIST FIX & WORKINGTYPE UPDATE --------

import time, hmac, hashlib, requests, os, urllib.parse, threading, json, logging
from flask import Flask, request, jsonify

# --- API Konfiguration ---
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"
APP_URL = os.getenv("APP_URL", "http://localhost:5000") 

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR) 

app = Flask(__name__)

# --- Strategie Settings ---
RSI_TIMEFRAME, RSI_PERIOD, RSI_THRESHOLD = "1m", 14, 75
EMA_TIMEFRAME, EMA_PERIOD_SHORT, EMA_PERIOD_LONG = "3m", 50, 200
LEVERAGE, TRADE_SIZE = 10, 10
TP_PERCENT, SL_PERCENT = 1.5, 1.5

# --- Break-Even Settings ---
BE_ACTIVATION_PERCENT = 1.0
active_be_positions = {}

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

def get_open_positions():
    ts = str(int(time.time() * 1000))
    params = {"timestamp": ts}
    url = f"{BINGX_BASE}/openApi/swap/v2/user/positions?{urllib.parse.urlencode(sorted(params.items()))}&signature={sign_bingx(params)}"
    try:
        r = requests.get(url, headers={"X-BX-APIKEY": API_KEY}, timeout=10).json()
        return r.get("data", [])
    except: return []

# ---------------- INDIKATOREN (FIXED) ----------------

def calc_rsi(closes, period):
    if len(closes) < period + 1: return 50
    gains = [max(0, closes[-i] - closes[-i-1]) for i in range(1, period + 1)]
    losses = [abs(min(0, closes[-i] - closes[-i-1])) for i in range(1, period + 1)]
    avg_gain = sum(gains) / period or 0.0001
    avg_loss = sum(losses) / period or 0.0001
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calc_ema(closes, period):
    """Berechnet den EMA korrekt (Fix: Zugriff auf Index 0)."""
    if not closes or len(closes) < 1: return 0
    if len(closes) < period: return float(closes[-1])
    
    alpha = 2 / (period + 1)
    # FIX: Startwert ist der erste Wert der Liste, nicht die Liste selbst
    ema = float(closes[0]) 
    for price in closes[1:]:
        ema = (float(price) * alpha) + (ema * (1 - alpha))
    return ema

# ---------------- POSITION ACTIONS ----------------

def close_position_market(symbol):
    ts = str(int(time.time() * 1000))
    params = {
        "symbol": symbol, "side": "SELL", "positionSide": "LONG",
        "type": "MARKET", "closePosition": "true", "timestamp": ts
    }
    requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/order?{urllib.parse.urlencode(sorted(params.items()))}&signature={sign_bingx(params)}", headers={"X-BX-APIKEY": API_KEY})
    logging.info(f"[BREAK-EVEN] {symbol} Position geschlossen.")

def set_tp_sl(symbol, qty, tp_price, sl_price):
    # FIX: workingType basierend auf API-Error auf MARK_PRICE gesetzt
    def place_order_robust(price, o_type, w_type="MARK_PRICE"):
        for i in range(5): 
            ts = str(int(time.time() * 1000))
            params = {
                "symbol": symbol, "side": "SELL", "positionSide": "LONG",
                "type": o_type, "quantity": str(qty), "stopPrice": "{:.6f}".format(price),
                "workingType": w_type, 
                "closePosition": "true", "timestamp": ts
            }
            response = requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/order?{urllib.parse.urlencode(sorted(params.items()))}&signature={sign_bingx(params)}", headers={"X-BX-APIKEY": API_KEY}).json()
            if response.get("code") == 0:
                logging.info(f"SUCCESS: {o_type} für {symbol} platziert.")
                return True
            logging.warning(f"RETRY {i+1}/5: {o_type} für {symbol} fehlgeschlagen: {response.get('msg')}")
            time.sleep(2)
        return False

    logging.info(f"Versuche TP/SL für {symbol} zu setzen...")
    place_order_robust(tp_price, "TAKE_PROFIT_MARKET")
    place_order_robust(sl_price, "STOP_MARKET")

# ---------------- MONITOREN ----------------

def monitor_break_even():
    while True:
        try:
            positions = get_open_positions()
            active_long_symbols = [p['symbol'] for p in positions if p.get('positionSide') == 'LONG' and float(p.get('positionAmt', 0)) > 0]
            for sym in list(active_be_positions.keys()):
                if sym not in active_long_symbols: del active_be_positions[sym]

            for pos in positions:
                if pos.get('positionSide') == 'LONG' and float(pos.get('positionAmt', 0)) > 0:
                    symbol = pos['symbol']
                    entry_price = float(pos['avgPrice'])
                    current_price = get_price_bingx(symbol)
                    if not current_price: continue
                    profit_pct = (current_price - entry_price) / entry_price * 100
                    if profit_pct >= BE_ACTIVATION_PERCENT and symbol not in active_be_positions:
                        active_be_positions[symbol] = True
                        logging.info(f"[BE-READY] {symbol} erreicht {BE_ACTIVATION_PERCENT}% Gewinn.")
                    if active_be_positions.get(symbol) and current_price <= entry_price:
                        close_position_market(symbol)
                        if symbol in active_be_positions: del active_be_positions[symbol]
        except: pass
        time.sleep(10)

def keep_alive_monitor():
    while True:
        time.sleep(60) # 1 Minute
        try:
            requests.get(f"{APP_URL}/testorder", timeout=10)
        except: pass

# ---------------- EXECUTION LOGIC ----------------

def execute_trade_bingx(symbol):
    ohlcv_rsi = get_ohlcv(symbol, RSI_TIMEFRAME, limit=RSI_PERIOD + 1)
    ohlcv_ema_short = get_ohlcv(symbol, EMA_TIMEFRAME, limit=EMA_PERIOD_SHORT)
    ohlcv_ema_long = get_ohlcv(symbol, EMA_TIMEFRAME, limit=EMA_PERIOD_LONG)
    
    if not ohlcv_rsi or not ohlcv_ema_short or not ohlcv_ema_long:
        logging.error(f"Datenfehler für {symbol}")
        return
    
    rsi = calc_rsi([float(c["close"]) for c in ohlcv_rsi], RSI_PERIOD)
    ema_short = calc_ema([float(c["close"]) for c in ohlcv_ema_short], EMA_PERIOD_SHORT)
    ema_long = calc_ema([float(c["close"]) for c in ohlcv_ema_long], EMA_PERIOD_LONG)
    current_price = get_price_bingx(symbol)
    
    if current_price:
        # Logik: RSI >= 75 UND Preis > EMA200 UND EMA200 > EMA50
        if rsi >= RSI_THRESHOLD and current_price > ema_long and ema_long > ema_short:
            qty = round(TRADE_SIZE / current_price, 6)
            ts = str(int(time.time() * 1000))
            entry_params = {
                "symbol": symbol, "side": "SELL", "positionSide": "SHORT", 
                "type": "MARKET", "quantity": str(qty), "leverage": str(LEVERAGE), "timestamp": ts
            }
            res = requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/order?{urllib.parse.urlencode(sorted(entry_params.items()))}&signature={sign_bingx(entry_params)}", headers={"X-BX-APIKEY": API_KEY}).json()
            
            if res.get("code") == 0:
                logging.info(f"[ORDER] {symbol} Entry bei {current_price}")
                time.sleep(2)
                set_tp_sl(symbol, qty, current_price*(1+TP_PERCENT/100), current_price*(1-SL_PERCENT/100))
            else:
                logging.error(f"Entry Error: {res.get('msg')}")
        else:
            logging.info(f"[SKIP] {symbol} Filter nicht erfüllt.")

# ---------------- WEBHOOK ----------------

@app.route("/testorder", methods=["POST", "GET"])
def handle_alert():
    if request.method == "GET": return jsonify({"status": "active"}), 200
    data = request.get_json(silent=True) or {}
    currency = str(data.get("currency", "")).upper()
    if not currency: return jsonify({"status": "no_currency"}), 200
    symbol = f"{currency}-USDT"
    threading.Thread(target=execute_trade_bingx, args=(symbol,)).start()
    return jsonify({"status": "processing", "symbol": symbol}), 200

if __name__ == "__main__":
    threading.Thread(target=monitor_break_even, daemon=True).start()
    threading.Thread(target=keep_alive_monitor, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
