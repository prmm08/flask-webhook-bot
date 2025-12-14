# -------- VER 4.0: BINGX FUTURES ONLY - LONG/ISOLATED/AUTO-CLOSE --------

import time
import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
from flask import Flask, request, jsonify

# --- API Konfiguration BingX ---
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

app = Flask(__name__)

# Globaler Status für aktive Überwachungen
active_monitors = {}

# --- HILFSFUNKTIONEN ---

def sign_bingx(params):
    """Erzeugt die BingX Signatur."""
    query = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

def get_price_bingx(symbol):
    """Holt den aktuellen Preis von BingX."""
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/price"
        r = requests.get(url, params={"symbol": symbol}, timeout=10).json()
        return float(r["data"]["price"])
    except Exception as e:
        print(f"[ERROR PREIS] {symbol}: {e}")
        return None

def is_pos_open_bingx(symbol):
    """Prüft, ob eine Position offen ist."""
    try:
        ts = str(int(time.time() * 1000))
        params = {"symbol": symbol, "timestamp": ts}
        params["signature"] = sign_bingx(params)
        r = requests.get(f"{BINGX_BASE}/openApi/swap/v2/user/positions", params=params, headers={"X-BX-APIKEY": API_KEY}, timeout=10).json()
        return any(float(p.get("positionAmt", 0)) != 0 for p in r.get("data", []))
    except:
        # Bei API-Fehler sicherheitshalber True zurückgeben, um Doppel-Orders zu vermeiden
        return True

def close_bingx(symbol):
    """Schließt alle Positionen für das Symbol."""
    print(f"[BINGX] Schließe Position für {symbol}")
    ts = str(int(time.time() * 1000))
    params = {"symbol": symbol, "timestamp": ts}
    params["signature"] = sign_bingx(params)
    requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/closeAllPositions", data=params, headers={"X-BX-APIKEY": API_KEY})

# --- ORDER & MONITORING LOGIK ---

def execute_long_bingx(symbol):
    """Platziert die Long Order."""
    print(f"[BINGX] Starte Long Order für {symbol}")
    price = get_price_bingx(symbol)
    if not price: return

    # Risk Management Settings
    trade_size_usdt = 20 # Positionsgröße in USDT
    leverage = 20
    tp_percent = 0.5
    sl_percent = 0.5
    
    qty = round(trade_size_usdt / price, 6)
    ts = str(int(time.time() * 1000))
    
    params = {
        "leverage": str(leverage),
        "positionSide": "LONG", # LONG Position
        "quantity": str(qty),
        "side": "BUY", # Kaufen für Long
        "symbol": symbol,
        "timestamp": ts,
        "type": "MARKET"
    }
    params["signature"] = sign_bingx(params)
    
    resp = requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/order", data=params, headers={"X-BX-APIKEY": API_KEY}, timeout=10).json()
    print(f"[BINGX] Order Antwort: {resp}")

    # Berechne TP/SL Preise
    entry_price = price
    tp_price = entry_price * (1 + tp_percent / 100)
    sl_price = entry_price * (1 - sl_percent / 100)
    
    # Starte den Monitoring Thread
    threading.Thread(target=monitor_position, args=(symbol, entry_price, tp_price, sl_price)).start()

def monitor_position(symbol, entry, tp, sl):
    """Überwacht die Position im 1-Sekunden-Takt."""
    key = f"BINGX_{symbol}"
    active_monitors[key] = True
    print(f"[MONITOR] START {symbol} | Entry: {entry:.4f} | TP: {tp:.4f} | SL: {sl:.4f}")
    
    try:
        be_set = False
        while True:
            curr = get_price_bingx(symbol)
            if not curr:
                time.sleep(1)
                continue

            # Break-Even Logik (+2% Gewinn)
            if not be_set and curr >= entry * 1.02:
                sl = entry # Setze SL auf Entry Preis
                be_set = True
                print(f"[BE] {symbol} aktiviert! SL auf Entry gesetzt.")

            # EXIT TRIGGER (TP oder SL/BE erreicht)
            if curr >= tp or curr <= sl:
                reason = "TP" if curr >= tp else "SL/BE"
                print(f"[EXIT] {symbol} Triggered durch {reason} bei Preis: {curr:.4f}")
                close_bingx(symbol)
                break
                
            time.sleep(1)
    except Exception as e:
        print(f"[ERROR MONITOR] {symbol}: {e}")
    finally:
        active_monitors[key] = False
        print(f"[MONITOR] END {symbol}")

# --- FLASK WEBHOOK HANDLER ---

@app.route("/testorder", methods=["POST"])
def handle_alert():
    data = request.get_json(force=True, silent=True) or {}
    currency = str(data.get("currency", "")).upper()
    if not currency: return jsonify({"error": "no currency"}), 400
    
    symbol = f"{currency}-USDT"
    print(f"\n--- SIGNAL EMPFANGEN: {symbol} ---")

    # Prüfe ob bereits aktiv oder in Überwachung
    if not is_pos_open_bingx(symbol) and not active_monitors.get(f"BINGX_{symbol}"):
        threading.Thread(target=execute_long_bingx, args=(symbol,)).start()
        return jsonify({"status": "order_started", "symbol": symbol}), 200
    else:
        return jsonify({"status": "already_active", "symbol": symbol}), 200


@app.route("/")
def health():
    # Stellt sicher, dass Render den Service als "Online" erkennt
    return "Bot Online", 200

if __name__ == "__main__":
    # Bindet an den Port, den Render vorschreibt (standardmäßig 10000)
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
