# -------- VER 1.9: Auto Orders LONG - Mit Positions-Check (No Double Entry) --------

import time
import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
from flask import Flask, request, jsonify

API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

app = Flask(__name__)

# Globaler Status für aktive Überwachungen
active_monitors = {}
cooldowns = {}
COOLDOWN_SECONDS = 2 * 60 * 60

def sign_params(params):
    query = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

def get_open_interest(symbol):
    binance_symbol = symbol.replace("-", "")
    url = "fapi.binance.com"
    params = {"symbol": binance_symbol, "period": "5m", "limit": 1}
    try:
        r = requests.get(url, params=params, timeout=10)
        resp = r.json()
        return float(resp[0]["sumOpenInterest"]) if resp else None
    except:
        return None

def is_position_open(symbol):
    """Prüft via API, ob aktuell eine Position für dieses Symbol offen ist."""
    url = f"{BINGX_BASE}/openApi/swap/v2/user/positions"
    headers = {"X-BX-APIKEY": API_KEY}
    params = {"symbol": symbol, "timestamp": str(int(time.time() * 1000))}
    params["signature"] = sign_params(params)
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10).json()
        positions = resp.get("data", [])
        for pos in positions:
            # Wenn die Position-Size ungleich 0 ist, ist sie offen
            if float(pos.get("positionAmt", 0)) != 0:
                return True
        return False
    except Exception as e:
        print(f"Fehler beim Positions-Check: {e}")
        return True # Im Zweifel True, um Doppel-Orders zu vermeiden

def monitor_oi_for_long(symbol, oi_at_signal, window_minutes=15, interval=30):
    """Wartet auf OI-Anstieg, bricht aber ab, wenn Position bereits existiert."""
    print(f"[OI-Monitor] Starte... {symbol}")
    deadline = time.time() + window_minutes * 60

    while time.time() < deadline:
        # Sicherheitscheck: Falls Position bereits offen (z.B. durch anderes Signal oder manuell)
        if is_position_open(symbol):
            print(f"[OI-Monitor] {symbol} bereits offen. Breche Monitoring ab.")
            active_monitors[symbol] = False
            return False

        try:
            current_oi = get_open_interest(symbol)
            if current_oi and current_oi > oi_at_signal:
                print(f"[OI-Monitor] Trigger! OI gestiegen für {symbol}")
                execute_long_order(symbol)
                return True
        except Exception as e:
            print(f"Fehler: {e}")
        time.sleep(interval)

    active_monitors[symbol] = False
    return False

def get_price(symbol):
    url = f"{BINGX_BASE}/openApi/swap/v2/quote/price"
    r = requests.get(url, params={"symbol": symbol}, timeout=10)
    return float(r.json()["data"]["price"])

def close_all_positions(symbol):
    url = f"{BINGX_BASE}/openApi/swap/v2/trade/closeAllPositions"
    headers = {"X-BX-APIKEY": API_KEY, "Content-Type": "application/x-www-form-urlencoded"}
    params = {"symbol": symbol, "timestamp": str(int(time.time() * 1000))}
    params["signature"] = sign_params(params)
    return requests.post(url, data=params, headers=headers, timeout=10).json()

def dynamic_round(price, value):
    decimals = 2 if price > 1000 else 4 if price > 1 else 6
    return round(value, decimals)

def monitor_position(symbol, entry_price, tp_price, sl_price):
    active_monitors[symbol] = True
    try:
        be_set = False
        while True:
            current = get_price(symbol)
            if not be_set and current >= entry_price * 1.02:
                sl_price = entry_price
                be_set = True
                print(f"BE aktiv für {symbol}")

            if current >= tp_price or current <= sl_price:
                close_all_positions(symbol)
                print(f"Position {symbol} geschlossen.")
                break
            time.sleep(2)
    finally:
        active_monitors[symbol] = False

def execute_long_order(symbol):
    price = get_price(symbol)
    qty = round(20 / price, 6) # 20 USDT Size
    
    headers = {"X-BX-APIKEY": API_KEY, "Content-Type": "application/x-www-form-urlencoded"}
    url_order = f"{BINGX_BASE}/openApi/swap/v2/trade/order"

    params = {
        "leverage": "20",
        "positionSide": "LONG",
        "quantity": str(qty),
        "side": "BUY",
        "symbol": symbol,
        "timestamp": str(int(time.time() * 1000)),
        "type": "MARKET"
    }
    params["signature"] = sign_params(params)
    resp = requests.post(url_order, data=params, headers=headers, timeout=10)

    tp_price = dynamic_round(price, price * 1.04)
    sl_price = dynamic_round(price, price * 0.98)

    threading.Thread(target=monitor_position, args=(symbol, price, tp_price, sl_price)).start()
    cooldowns[symbol] = time.time()
    return resp.json()

@app.route("/testorder", methods=["POST"])
def handle_alert():
    try:
        data = request.get_json(force=True, silent=True) or {}
        currency = str(data.get("currency", "")).upper()
        if not currency: return jsonify({"status": "ignored"}), 200
        
        symbol = f"{currency}-USDT"

        # 1. Check: Läuft bereits ein Monitoring oder eine Position für dieses Symbol?
        if active_monitors.get(symbol, False):
            print(f"[Signal] Ignoriert: {symbol} wird bereits überwacht/ist offen.")
            return jsonify({"status": "blocked", "reason": "already_active"}), 200

        # 2. Check: Besteht eine echte Position auf BingX?
        if is_position_open(symbol):
            print(f"[Signal] Ignoriert: Position für {symbol} bereits auf BingX offen.")
            active_monitors[symbol] = True # Status synchronisieren
            return jsonify({"status": "blocked", "reason": "position_exists"}), 200

        # 3. Cooldown Check
        if time.time() - cooldowns.get(symbol, 0) < COOLDOWN_SECONDS:
            return jsonify({"status": "cooldown"}), 200

        oi_at_signal = get_open_interest(symbol)
        if oi_at_signal is None: return jsonify({"status": "error"}), 200

        # Monitoring starten
        active_monitors[symbol] = True
        threading.Thread(target=monitor_oi_for_long, args=(symbol, oi_at_signal)).start()

        return jsonify({"status": "monitoring_started", "symbol": symbol}), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
