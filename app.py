import time
import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
import base64
import uuid
import json
from flask import Flask, request, jsonify

# --- API Konfiguration ---
BINGX_API_KEY = os.getenv("BINGX_API_KEY")
BINGX_API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

KUCOIN_API_KEY = os.getenv("KUCOIN_API_KEY")
KUCOIN_API_SECRET = os.getenv("KUCOIN_API_SECRET")
KUCOIN_API_PASSPHRASE = os.getenv("KUCOIN_API_PASSPHRASE")
KUCOIN_BASE = "https://api-futures.kucoin.com"

app = Flask(__name__)

# Tracking Speicher
active_monitors = {}
cooldowns = {}
COOLDOWN_SECONDS = 2 * 60 * 60

# --- Hilfsfunktionen ---

def dynamic_round(price, value):
    decimals = 2 if price > 1000 else 4 if price > 1 else 6
    return round(value, decimals)

def kucoin_headers(method, endpoint, body=""):
    """Erzeugt die notwendigen Header für KuCoin (Auth)."""
    now = str(int(time.time() * 1000))
    sig_str = f"{now}{method.upper()}{endpoint}{body}"
    sig = base64.b64encode(hmac.new(KUCOIN_API_SECRET.encode(), sig_str.encode(), hashlib.sha256).digest()).decode()
    pass_sig = base64.b64encode(hmac.new(KUCOIN_API_SECRET.encode(), KUCOIN_API_PASSPHRASE.encode(), hashlib.sha256).digest()).decode()
    return {
        "KC-API-KEY": KUCOIN_API_KEY,
        "KC-API-SIGN": sig,
        "KC-API-TIMESTAMP": now,
        "KC-API-PASSPHRASE": pass_sig,
        "KC-API-KEY-VERSION": "2",
        "Content-Type": "application/json"
    }

def get_price_generic(exchange, symbol):
    """Holt den aktuellen Preis von der jeweiligen Börse."""
    try:
        if exchange == "BINGX":
            url = f"{BINGX_BASE}/openApi/swap/v2/quote/price"
            r = requests.get(url, params={"symbol": symbol}, timeout=10).json()
            return float(r["data"]["price"])
        
        elif exchange == "KUCOIN":
            endpoint = f"/api/v1/ticker?symbol={symbol}"
            r = requests.get(KUCOIN_BASE + endpoint, headers=kucoin_headers("GET", endpoint), timeout=10).json()
            if "data" in r and "price" in r["data"]:
                return float(r["data"]["price"])
            else:
                print(f"[KUCOIN] Preisfehler: {r}")
                return None
    except Exception as e:
        print(f"[ERROR] Preisabfrage {exchange} fehlgeschlagen: {e}")
        return None

# --- BINGX LOGIK ---

def sign_bingx(params):
    query = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(BINGX_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

def is_pos_open_bingx(symbol):
    url = f"{BINGX_BASE}/openApi/swap/v2/user/positions"
    params = {"symbol": symbol, "timestamp": str(int(time.time() * 1000))}
    params["signature"] = sign_bingx(params)
    try:
        r = requests.get(url, params=params, headers={"X-BX-APIKEY": BINGX_API_KEY}, timeout=10).json()
        return any(float(p.get("positionAmt", 0)) != 0 for p in r.get("data", []))
    except Exception as e:
        print(f"[ERROR BINGX Check] {e}")
        return True 

def execute_long_bingx(symbol):
    price = get_price_generic("BINGX", symbol)
    if not price: return
    
    qty = round(20 / price, 6) # 20 USDT Einsatz
    url = f"{BINGX_BASE}/openApi/swap/v2/trade/order"
    params = {
        "leverage": "20", "positionSide": "LONG", "quantity": str(qty),
        "side": "BUY", "symbol": symbol, "timestamp": str(int(time.time() * 1000)), "type": "MARKET"
    }
    params["signature"] = sign_bingx(params)
    resp = requests.post(url, data=params, headers={"X-BX-APIKEY": BINGX_API_KEY}, timeout=10).json()
    print(f"[BINGX] Order Antwort: {resp}")
    
    tp, sl = dynamic_round(price, price * 1.04), dynamic_round(price, price * 0.98)
    threading.Thread(target=monitor_generic, args=(symbol, price, tp, sl, "BINGX")).start()
    cooldowns[f"BINGX_{symbol}"] = time.time()

# --- KUCOIN LOGIK ---

def is_pos_open_kucoin(symbol):
    endpoint = f"/api/v1/position?symbol={symbol}"
    try:
        r = requests.get(KUCOIN_BASE + endpoint, headers=kucoin_headers("GET", endpoint), timeout=10).json()
        if "data" in r:
            return float(r["data"].get("currentQty", 0)) != 0
        print(f"[KUCOIN] Check Fehlgeschlagen: {r}")
        return True
    except Exception as e:
        print(f"[ERROR KUCOIN Check] {e}")
        return True

def execute_long_kucoin(symbol):
    price = get_price_generic("KUCOIN", symbol)
    if not price: return
    
    endpoint = "/api/v1/orders"
    payload = {
        "clientOid": str(uuid.uuid4()), 
        "side": "buy", 
        "symbol": symbol, 
        "type": "market", 
        "leverage": "20", 
        "size": "1" # WICHTIG: Prüfen Sie Kontraktgröße!
    }
    body = json.dumps(payload)
    resp = requests.post(KUCOIN_BASE + endpoint, data=body, headers=kucoin_headers("POST", endpoint, body), timeout=10).json()
    print(f"[KUCOIN] Order Antwort: {resp}")
    
    tp, sl = round(price * 1.04, 4), round(price * 0.98, 4)
    threading.Thread(target=monitor_generic, args=(symbol, price, tp, sl, "KUCOIN")).start()
    cooldowns[f"KUCOIN_{symbol}"] = time.time()

# --- MONITORING (PLATTFORMÜBERGREIFEND) ---

def monitor_generic(symbol, entry, tp, sl, exchange):
    key = f"{exchange}_{symbol}"
    active_monitors[key] = True
    be_set = False
    print(f"[INFO] {exchange} Monitoring gestartet für {symbol}. TP: {tp}, SL: {sl}")
    try:
        while True:
            curr = get_price_generic(exchange, symbol)
            if not curr: 
                time.sleep(5)
                continue

            # Break-Even Logik (+2%)
            if not be_set and curr >= entry * 1.02:
                sl, be_set = entry, True
                print(f"[{exchange}] {symbol} Break-Even auf {sl} gesetzt.")

            # Exit Bedingungen
            if curr >= tp:
                print(f"[{exchange}] {symbol} TP erreicht!")
                break
            if curr <= sl:
                print(f"[{exchange}] {symbol} SL erreicht!")
                break
            
            time.sleep(5)
    finally: 
        active_monitors[key] = False

# --- WEBHOOK HANDLER ---

@app.route("/testorder", methods=["POST"])
def handle_alert():
    try:
        data = request.get_json(force=True, silent=True) or {}
        currency = str(data.get("currency", "")).upper()
        if not currency:
            return jsonify({"status": "error", "message": "Keine Waehrung angegeben"}), 400

        symbol_bx = f"{currency}-USDT"
        symbol_kc = f"{currency}USDTM"
        
        print(f"\n--- Neues Signal: {currency} ---")

        # --- BINGX PROZESS ---
        bx_key = f"BINGX_{symbol_bx}"
        if not active_monitors.get(bx_key) and not is_pos_open_bingx(symbol_bx):
            last_bx = cooldowns.get(bx_key, 0)
            if time.time() - last_bx > COOLDOWN_SECONDS:
                threading.Thread(target=execute_long_bingx, args=(symbol_bx,)).start()
            else:
                print(f"[LOG] BingX {currency} im Cooldown.")
        else:
            print(f"[LOG] BingX {currency} bereits aktiv.")

        # --- KUCOIN PROZESS ---
        kc_key = f"KUCOIN_{symbol_kc}"
        if not active_monitors.get(kc_key) and not is_pos_open_kucoin(symbol_kc):
            last_kc = cooldowns.get(kc_key, 0)
            if time.time() - last_kc > COOLDOWN_SECONDS:
                threading.Thread(target=execute_long_kucoin, args=(symbol_kc,)).start()
            else:
                print(f"[LOG] KuCoin {currency} im Cooldown.")
        else:
            print(f"[LOG] KuCoin {currency} bereits aktiv oder Check fehlgeschlagen.")

        return jsonify({"status": "processed", "currency": currency}), 200

    except Exception as e:
        print(f"[FATAL ERROR] {e}")
        return jsonify({"status": "error", "message": str(e)}), 400

# Health-Check für Render
@app.route("/", methods=["GET"])
def home():
    return "Bot is running", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
