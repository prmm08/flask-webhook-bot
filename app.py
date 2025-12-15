# -------- V 4.0: BINGX FUTURES (api/v3) - ALWAYS SHORT ON SIGNAL --------

import time
import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
from flask import Flask, request, jsonify

# --- API Konfiguration BingX (NEU: api.bingx.com / api/v3) ---
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://api.bingx.com"  # NEU

app = Flask(__name__)

# Globaler Status für aktive Überwachungen
active_monitors = {}


# --- SIGNATUR & REQUEST HILFSFUNKTIONEN ---

def sign_bingx(params: dict) -> str:
    """
    Erzeugt BingX-Signatur für /api/v3 Endpoints.
    Signiert wird die URL-encodete Query-String.
    """
    query = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()


def signed_get(path: str, params: dict) -> dict:
    """
    Authentifizierter GET-Request gegen BingX /api/v3.
    Fügt timestamp + signature hinzu.
    """
    ts = str(int(time.time() * 1000))
    params = dict(params) if params else {}
    params["timestamp"] = ts
    params["recvWindow"] = "5000"

    signature = sign_bingx(params)
    params["signature"] = signature

    url = f"{BINGX_BASE}{path}"
    headers = {"X-BX-APIKEY": API_KEY}

    r = requests.get(url, params=params, headers=headers, timeout=10)
    try:
        return r.json()
    except Exception:
        return {"error": "invalid_json", "raw": r.text, "status": r.status_code}


def signed_post(path: str, params: dict) -> dict:
    """
    Authentifizierter POST-Request gegen BingX /api/v3.
    Signiert Query-String (BingX: application/x-www-form-urlencoded).
    """
    ts = str(int(time.time() * 1000))
    params = dict(params) if params else {}
    params["timestamp"] = ts
    params["recvWindow"] = "5000"

    signature = sign_bingx(params)
    params["signature"] = signature

    url = f"{BINGX_BASE}{path}"
    headers = {
        "X-BX-APIKEY": API_KEY,
        "Content-Type": "application/x-www-form-urlencoded"
    }

    r = requests.post(url, data=urllib.parse.urlencode(params), headers=headers, timeout=10)
    try:
        return r.json()
    except Exception:
        return {"error": "invalid_json", "raw": r.text, "status": r.status_code}


# --- PREIS, POSITIONEN, CLOSE ---

def get_price_bingx(symbol: str):
    """
    Holt den stabilen Mark-Preis von BingX Futures.
    NEU: /api/v3/market/markPrice, Symbol z.B. BTC-USDT.
    """
    try:
        path = "/api/v3/market/markPrice"
        # Laut Deiner letzten Tests ist dieser Endpoint auth-pflichtig → signed_get
        r = signed_get(path, {"symbol": symbol})

        if "data" not in r or "markPrice" not in r["data"]:
            print(f"[ERROR PREIS] Ungültige Antwort für {symbol}: {r}")
            return None

        return float(r["data"]["markPrice"])

    except Exception as e:
        print(f"[ERROR PREIS] {symbol}: {e}")
        return None


def is_pos_open_bingx(symbol: str) -> bool:
    """
    Prüft, ob eine Futures-Position offen ist.
    NEU: /api/v3/position
    """
    try:
        path = "/api/v3/position"
        r = signed_get(path, {"symbol": symbol})

        if "data" not in r:
            print(f"[ERROR POS_OPEN] Unerwartete Antwort für {symbol}: {r}")
            return True

        data = r["data"]
        # Struktur variiert je nach API; wir gehen von Liste aus
        if isinstance(data, list):
            for p in data:
                amt = float(p.get("positionAmt", 0) or p.get("quantity", 0) or 0)
                if amt != 0:
                    return True
            return False
        else:
            # Falls Einzelobjekt
            amt = float(data.get("positionAmt", 0) or data.get("quantity", 0) or 0)
            return amt != 0

    except Exception as e:
        print(f"[ERROR POS_OPEN] {symbol}: {e}")
        # Im Zweifel lieber annehmen, dass was offen ist
        return True


def close_bingx(symbol: str):
    """
    Schließt alle Positionen für das Symbol (Market-Order in Gegenrichtung).
    Da BingX v3 kein direktes closeAll für Futures mehr bietet,
    wird hier eine Market-Order in Gegenrichtung ausgelöst (simplified).
    """
    print(f"[BINGX] Versuche Position für {symbol} zu schließen (Market).")
    try:
        # Position holen, um Richtung/Menge zu kennen
        path_pos = "/api/v3/position"
        r_pos = signed_get(path_pos, {"symbol": symbol})

        if "data" not in r_pos:
            print(f"[ERROR CLOSE] Keine Positionsdaten für {symbol}: {r_pos}")
            return

        positions = r_pos["data"]
        if isinstance(positions, dict):
            positions = [positions]

        for p in positions:
            amt = float(p.get("positionAmt", 0) or p.get("quantity", 0) or 0)
            if amt == 0:
                continue

            side = "BUY" if amt < 0 else "SELL"  # Short schließen → BUY
            qty = abs(amt)

            params = {
                "symbol": symbol,
                "side": side,
                "type": "MARKET",
                "quantity": str(qty)
            }
            path_order = "/api/v3/order"
            r_close = signed_post(path_order, params)
            print(f"[CLOSE ORDER] {symbol} → {r_close}")

    except Exception as e:
        print(f"[ERROR CLOSE] {symbol}: {e}")


# --- ORDER & MONITORING LOGIK ---

def execute_trade_bingx(symbol: str, side: str):
    """
    Platziert eine SHORT-Order basierend auf dem Signal.
    NEU: /api/v3/order, Symbol wie BTC-USDT.
    """
    print(f"[BINGX] Starte {side} Order für {symbol}")
    price = get_price_bingx(symbol)
    if not price:
        print(f"[BINGX] Abbruch, kein Preis für {symbol}.")
        return

    trade_size_usdt = 20
    leverage = 20

    tp_percent = 0.75
    sl_percent = 0.5

    qty = round(trade_size_usdt / price, 6)

    # Viele v3-APIs brauchen Leverage separat, hier vereinfachen wir:
    params = {
        "symbol": symbol,
        "side": "SELL",          # immer SELL für SHORT
        "type": "MARKET",
        "quantity": str(qty)
    }
    path_order = "/api/v3/order"
    res = signed_post(path_order, params)

    if "data" not in res:
        print(f"[ERROR ORDER] Unerwartete Antwort für {symbol}: {res}")
        return

    data = res["data"]

    try:
        entry_price = float(data.get("avgPrice") or data.get("price") or price)
    except Exception:
        entry_price = price

    tp_price = entry_price * (1 - tp_percent / 100)
    sl_price = entry_price * (1 + sl_percent / 100)

    threading.Thread(
        target=monitor_position,
        args=(symbol, entry_price, tp_price, sl_price, side),
        daemon=True
    ).start()


def monitor_position(symbol: str, entry: float, tp: float, sl: float, side: str):
    key = f"BINGX_{symbol}"
    active_monitors[key] = True
    print(f"[MONITOR] START {symbol} ({side}) | Entry: {entry:.4f} | TP: {tp:.4f} | SL: {sl:.4f}")

    try:
        spread = entry * 0.0005
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

            # EXIT TRIGGER
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
    """
    Endpunkt für Handelssignale. TRIGGERT IMMER NUR SHORT.
    Erwartet z.B.: {"currency": "ZEC"}
    daraus wird: ZEC-USDT (Futures-Symbol)
    """
    data = request.get_json(force=True, silent=True) or {}
    currency = str(data.get("currency", "")).upper()
    if not currency:
        return jsonify({"error": "no currency"}), 400

    symbol = f"{currency}-USDT"  # NEU: Bindestrich, passt zur v3-API
    print(f"\n--- SIGNAL EMPFANGEN: {currency} → BingX Symbol: {symbol} ---")

    if is_pos_open_bingx(symbol) or active_monitors.get(f"BINGX_{symbol}"):
        return jsonify({"status": "already_active", "symbol": symbol}), 200

    threading.Thread(target=execute_trade_bingx, args=(symbol, "SHORT"), daemon=True).start()
    return jsonify({"status": "order_started_short", "symbol": symbol}), 200


# --- APP START ---

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
