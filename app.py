# -------- V 3.1: BINGX FUTURES ONLY - ALWAYS SHORT ON SIGNAL --------

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
    """Holt den stabilen Mark-Preis von BingX."""
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/markPrice"
        r = requests.get(url, params={"symbol": symbol}, timeout=10).json()
        return float(r["data"]["markPrice"])
    except Exception as e:
        print(f"[ERROR PREIS] {symbol}: {e}")
        return None

def is_pos_open_bingx(symbol):
    """Prüft, ob eine Position offen ist."""
    try:
        ts = str(int(time.time() * 1000))
        params = {"symbol": symbol, "timestamp": ts}
        params["signature"] = sign_bingx(params)
        r = requests.get(
            f"{BINGX_BASE}/openApi/swap/v2/user/positions",
            params=params,
            headers={"X-BX-APIKEY": API_KEY},
            timeout=10
        ).json()
        return any(float(p.get("positionAmt", 0)) != 0 for p in r.get("data", []))
    except Exception as e:
        print(f"[ERROR POS_OPEN] {symbol}: {e}")
        # Im Zweifel lieber nichts Neues aufmachen
        return True

def close_bingx(symbol):
    """Schließt alle Positionen für das Symbol."""
    print(f"[BINGX] Schließe Position für {symbol}")
    ts = str(int(time.time() * 1000))
    params = {"symbol": symbol, "timestamp": ts}
    params["signature"] = sign_bingx(params)
    try:
        requests.post(
            f"{BINGX_BASE}/openApi/swap/v2/trade/closeAllPositions",
            data=params,
            headers={"X-BX-APIKEY": API_KEY},
            timeout=10
        )
    except Exception as e:
        print(f"[ERROR CLOSE] {symbol}: {e}")


# --- ORDER & MONITORING LOGIK ---

def execute_trade_bingx(symbol, side):
    """Platziert eine SHORT-Order basierend auf dem Signal."""
    print(f"[BINGX] Starte {side} Order für {symbol}")
    price = get_price_bingx(symbol)
    if not price:
        return

    # Risk Management Settings
    trade_size_usdt = 20  # Positionsgröße in USDT
    leverage = 20

    # TP/SL Settings (gleich wie vorher, aber nur SHORT genutzt)
    tp_percent = 0.75
    sl_percent = 0.5

    qty = round(trade_size_usdt / price, 6)

    params = {
        "leverage": str(leverage),
        "positionSide": side,                # "SHORT"
        "quantity": str(qty),
        "side": "SELL",                      # immer SELL für SHORT
        "symbol": symbol,
        "timestamp": str(int(time.time() * 1000)),
        "type": "MARKET"
    }
    params["signature"] = sign_bingx(params)

    try:
        res = requests.post(
            f"{BINGX_BASE}/openApi/swap/v2/trade/order",
            data=params,
            headers={"X-BX-APIKEY": API_KEY},
            timeout=10
        ).json()
    except Exception as e:
        print(f"[ERROR ORDER] {symbol}: {e}")
        return

    # Exakten Fill-Preis nutzen, fallback auf aktuellen Markpreis
    entry_price = None
    try:
        entry_price = float(res.get("data", {}).get("avgPrice", price))
    except Exception:
        entry_price = price

    # TP/SL Preise für SHORT
    tp_price = entry_price * (1 - tp_percent / 100)
    sl_price = entry_price * (1 + sl_percent / 100)

    threading.Thread(
        target=monitor_position,
        args=(symbol, entry_price, tp_price, sl_price, side)
    ).start()

def monitor_position(symbol, entry, tp, sl, side):
    """Überwacht die Position im 1-Sekunden-Takt."""
    key = f"BINGX_{symbol}"
    active_monitors[key] = True
    print(f"[MONITOR] START {symbol} ({side}) | Entry: {entry:.4f} | TP: {tp:.4f} | SL: {sl:.4f}")

    try:
        # Spread-Puffer und BE-Trigger (für SHORT)
        spread = entry * 0.0005  # 0.05% Puffer
        be_trigger_short = entry * 0.98 - spread
        be_set = False

        while True:
            curr = get_price_bingx(symbol)
            if not curr:
                time.sleep(1)
                continue

            # Break-Even Logik für SHORT
            if not be_set and curr <= be_trigger_short:
                sl = entry
                be_set = True
                print(f"[BE] {symbol} aktiviert! SL auf Entry gesetzt.")

            # EXIT TRIGGER (TP oder SL/BE erreicht) - nur SHORT-Logik nötig
            if curr <= tp or curr >= sl:
                reason = "TP" if curr <= tp else "SL/BE"
                print(f"[EXIT] {symbol} Triggered durch {reason} bei Preis: {curr:.4f}")
                close_bingx(symbol)
                break

            time.sleep(1)

    except Exception as e:
        print(f"[ERROR MONITOR] {symbol}: {e}")
    finally:
        active_monitors[key] = False
        print(f"[MONITOR] END {symbol}")


# ---------------- HEALTH CHECK ----------------

@app.route("/", methods=["GET", "POST"])
def health_check():
    return jsonify({"status": "ok", "message": "Webhook erreichbar"}), 200

@app.route("/debug", methods=["GET"])
def debug_logs():
    return "Bitte Render Dashboard → Logs öffnen.", 200


# --- FLASK WEBHOOK HANDLER ---

@app.route("/testorder", methods=["POST"])
def handle_alert():
    """Endpunkt für Handelssignale. TRIGGERT IMMER NUR SHORT."""
    data = request.get_json(force=True, silent=True) or {}
    currency = str(data.get("currency", "")).upper()
    if not currency:
        return jsonify({"error": "no currency"}), 400

    symbol = f"{currency}-USDT"
    print(f"\n--- SIGNAL EMPFANGEN: {symbol} ---")

    # Verhindert doppelte Trades bei laufender oder offener Position
    if is_pos_open_bingx(symbol) or active_monitors.get(f"BINGX_{symbol}"):
        return jsonify({"status": "already_active", "symbol": symbol}), 200

    # Immer SHORT ausführen
    threading.Thread(target=execute_trade_bingx, args=(symbol, "SHORT")).start()
    return jsonify({"status": "order_started_short", "symbol": symbol}), 200


# --- APP START ---

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
