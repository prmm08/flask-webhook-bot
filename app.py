# -------- V 3.6: BINGX FUTURES - FIX SIGNATURE ERROR (100001) --------

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

logging.basicConfig(level=logging.INFO)
app = Flask(__name__)

# --- Strategie Settings ---
RSI_TIMEFRAME, RSI_PERIOD, RSI_THRESHOLD = "1m", 14, 75
EMA_TIMEFRAME, EMA_PERIOD = "5m", 50
LEVERAGE, TRADE_SIZE = 10, 10
TP_PERCENT, SL_PERCENT, BE_PERCENT = 1.0, 1.5, 0.5 

# ---------------- SIGNING & HELPERS ----------------

def send_signed_request(method, endpoint, params):
    """Verbesserte Signatur-Logik für BingX V2 API."""
    try:
        # 1. Timestamp hinzufügen falls nötig
        if method == "POST" or "user" in endpoint:
            params["timestamp"] = str(int(time.time() * 1000))
        
        # 2. Parameter alphabetisch sortieren für den Query String
        sorted_params = collections.OrderedDict(sorted(params.items()))
        query_string = urllib.parse.urlencode(sorted_params)
        
        # 3. Signatur NUR über den Query String generieren
        signature = hmac.new(
            API_SECRET.encode("utf-8"), 
            query_string.encode("utf-8"), 
            hashlib.sha256
        ).hexdigest()
        
        full_url = f"{BINGX_BASE}{endpoint}?{query_string}&signature={signature}"
        headers = {"X-BX-APIKEY": API_KEY, "Accept": "application/json"}

        logging.info(f"Request: {method} {endpoint}")
        
        if method == "POST":
            r = requests.post(full_url, headers=headers, timeout=10)
        else:
            r = requests.get(full_url, headers=headers, timeout=10)
        
        res_json = r.json()
        if res_json.get("code") != 0:
            logging.error(f"BingX Error {res_json.get('code')}: {res_json.get('msg')}")
        return res_json

    except Exception as e:
        logging.error(f"Request Exception: {e}")
        return None

import collections # Für OrderedDict

# --- Hilfsfunktionen für Daten ---
def get_price_bingx(symbol):
    r = send_signed_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    return float(r["data"]["price"]) if r and r.get("code") == 0 else None

def get_ohlcv(symbol, interval, limit):
    r = send_signed_request("GET", "/openApi/swap/v2/quote/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    return r.get("data", []) if r and r.get("code") == 0 else []

# ---------------- INDIKATOREN ----------------

def calc_rsi(closes, period):
    if len(closes) < period + 1: return 50
    gains = [max(0, closes[-i] - closes[-i-1]) for i in range(1, period + 1)]
    losses = [abs(min(0, closes[-i] - closes[-i-1])) for i in range(1, period + 1)]
    avg_gain, avg_loss = sum(gains)/period, sum(losses)/period
    if avg_loss == 0: return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calc_ema(closes, period):
    if not closes: return 0
    alpha = 2 / (period + 1)
    ema = closes[0]
    for price in closes[1:]:
        ema = (price * alpha) + (ema * (1 - alpha))
    return ema

# ---------------- TRADING LOGIC ----------------

def monitor_be(symbol, entry_price, be_trigger):
    while True:
        time.sleep(5)
        curr = get_price_bingx(symbol)
        if not curr: continue
        
        # Prüfen ob Position noch offen
        pos = send_signed_request("GET", "/openApi/swap/v2/user/positions", {"symbol": symbol})
        has_pos = any(float(p["positionAmt"]) != 0 for p in pos.get("data", [])) if pos else False
        if not has_pos: break

        if curr >= be_trigger:
            logging.info(f"BE Trigger hit for {symbol}. Moving SL to {entry_price}")
            send_signed_request("POST", "/openApi/swap/v2/trade/cancelAllOpenOrders", {"symbol": symbol})
            # Neuen SL setzen (Menge aus Position holen)
            qty = next(abs(float(p["positionAmt"])) for p in pos["data"] if float(p["positionAmt"]) != 0)
            send_signed_request("POST", "/openApi/swap/v2/trade/order", {
                "symbol": symbol, "side": "SELL", "positionSide": "LONG",
                "type": "STOP_MARKET", "quantity": str(qty), "stopPrice": f"{entry_price:.6f}",
                "workingType": "MARK_PRICE", "closePosition": "true"
            })
            break

def execute_trade_bingx(symbol):
    # RSI & EMA Checks
    c_rsi = [float(x["close"]) for x in get_ohlcv(symbol, RSI_TIMEFRAME, RSI_PERIOD+2)]
    c_ema = [float(x["close"]) for x in get_ohlcv(symbol, EMA_TIMEFRAME, EMA_PERIOD+2)]
    if not c_rsi or not c_ema: return

    rsi, ema, price = calc_rsi(c_rsi, RSI_PERIOD), calc_ema(c_ema, EMA_PERIOD), get_price_bingx(symbol)
    
    if price > ema and rsi >= RSI_THRESHOLD:
        logging.info(f"SIGNAL {symbol}: RSI={rsi:.1f}, EMA={ema:.2f}. Executing LONG.")
        qty = round(TRADE_SIZE / price, 6)
        
        # 1. Entry Order
        send_signed_request("POST", "/openApi/swap/v2/trade/order", {
            "symbol": symbol, "side": "BUY", "positionSide": "LONG",
            "type": "MARKET", "quantity": str(qty)
        })
        
        # 2. TP & SL
        time.sleep(2)
        tp, sl = price * (1 + TP_PERCENT/100), price * (1 - SL_PERCENT/100)
        send_signed_request("POST", "/openApi/swap/v2/trade/order", {
            "symbol": symbol, "side": "SELL", "positionSide": "LONG",
            "type": "TAKE_PROFIT_MARKET", "quantity": str(qty), "stopPrice": f"{tp:.6f}", "closePosition": "true"
        })
        send_signed_request("POST", "/openApi/swap/v2/trade/order", {
            "symbol": symbol, "side": "SELL", "positionSide": "LONG",
            "type": "STOP_MARKET", "quantity": str(qty), "stopPrice": f"{sl:.6f}", "closePosition": "true"
        })
        
        threading.Thread(target=monitor_be, args=(symbol, price, price*(1+BE_PERCENT/100))).start()

@app.route("/testorder", methods=["POST"])
def handle_webhook():
    data = request.get_json(silent=True) or {}
    symbol = f"{data.get('currency', '').upper()}-USDT"
    if "USDT" in symbol:
        threading.Thread(target=execute_trade_bingx, args=(symbol,)).start()
    return jsonify({"status": "received"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
