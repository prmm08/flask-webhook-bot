# -------- V 2.7: BINGX FUTURES ONLY - FINAL ROBUST COINGECKO BTC TREND FILTER ADDED --------

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
        return True

def close_bingx(symbol):
    """Schließt alle Positionen für das Symbol."""
    print(f"[BINGX] Schließe Position für {symbol}")
    ts = str(int(time.time() * 1000))
    params = {"symbol": symbol, "timestamp": ts}
    params["signature"] = sign_bingx(params)
    requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/closeAllPositions", data=params, headers={"X-BX-APIKEY": API_KEY})


# --- NEUE FUNKTION: BTC TREND ERKENNUNG VIA COINGECKO ---

def get_btc_hourly_trend():
    """Analysiert die BTC-Tendenz der letzten Stunde (LONG/SHORT/NEUTRAL) via CoinGecko."""
    # Ruft 24 Stunden Daten im 5-Minuten-Intervall ab, um die letzte Stunde zu bewerten
    url = "https://api.coingecko.com"
    
    try:
        r = requests.get(url, timeout=10).json()
        prices_data = r.get("prices", []) # prices_data ist eine Liste von [timestamp_ms, price]
        
        if len(prices_data) < 12: # Mindestens 12 Datenpunkte für 1 Stunde (bei 5m Intervallen)
            print("[TREND] Nicht genügend CoinGecko Daten für Trendanalyse.")
            return "NEUTRAL"
            
        # Timestamp vor 60 Minuten (in Millisekunden)
        one_hour_ago_ms = (time.time() - 3600) * 1000
        
        # Den ersten Datenpunkt finden, der vor oder zum Zeitpunkt vor 1 Stunde liegt
        prices_last_hour_segment = [p for p in prices_data if p[0] >= one_hour_ago_ms]

        if not prices_last_hour_segment:
            print("[TREND] Keine Preise im letzten 1-Stunden-Fenster gefunden.")
            return "NEUTRAL"
            
        # Der erste Preis im Segment ist der "Open" Preis für diese Stunde
        open_price = prices_last_hour_segment[0]
        # Der letzte Preis im Segment ist der aktuelle "Close" Preis
        close_price = prices_last_hour_segment[-1]
        
        if close_price > open_price:
            print(f"[TREND] BTC 1H Tendenz: LONG (Open: {open_price:.2f}, Close: {close_price:.2f})")
            return "LONG"
        elif close_price < open_price:
            print(f"[TREND] BTC 1H Tendenz: SHORT (Open: {open_price:.2f}, Close: {close_price:.2f})")
            return "SHORT"
        else:
            print("[TREND] BTC 1H Tendenz: NEUTRAL")
            return "NEUTRAL"
            
    except Exception as e:
        print(f"[ERROR TREND] Fehler beim Abrufen des BTC-Trends von CoinGecko: {e}")
        return "NEUTRAL"


# --- ORDER & MONITORING LOGIK (Unverändert) ---

def execute_trade_bingx(symbol, side):
    """Platziert die Order basierend auf der ermittelten Tendenz."""
    print(f"[BINGX] Starte {side} Order für {symbol}")
    price = get_price_bingx(symbol)
    if not price: return

    # Risk Management Settings
    trade_size_usdt = 20 # Positionsgröße in USDT
    leverage = 20
    
    # Passe TP/SL basierend auf der Richtung an
    if side == "LONG":
        tp_percent = 0.75
        sl_percent = 0.5
    else: # SHORT
        tp_percent = 0.75
        sl_percent = 0.5

    qty = round(trade_size_usdt / price, 6)
    
    params = {
        "leverage": str(leverage),
        "positionSide": side,
        "quantity": str(qty),
        "side": "BUY" if side == "LONG" else "SELL", # Side ist BUY/SELL, PositionSide ist LONG/SHORT
        "symbol": symbol,
        "timestamp": str(int(time.time() * 1000)),
        "type": "MARKET"
    }
    params["signature"] = sign_bingx(params)
    
    requests.post(f"{BINGX_BASE}/openApi/swap/v2/trade/order", data=params, headers={"X-BX-APIKEY": API_KEY}, timeout=10)

    # Berechne TP/SL Preise
    entry_price = price
    if side == "LONG":
        tp_price = entry_price * (1 + tp_percent / 100)
        sl_price = entry_price * (1 - sl_percent / 100)
    else: # SHORT
        tp_price = entry_price * (1 - tp_percent / 100)
        sl_price = entry_price * (1 + sl_percent / 100)
    
    # Starte den Monitoring Thread
    threading.Thread(target=monitor_position, args=(symbol, entry_price, tp_price, sl_price, side)).start()

def monitor_position(symbol, entry, tp, sl, side):
    """Überwacht die Position im 1-Sekunden-Takt."""
    key = f"BINGX_{symbol}"
    active_monitors[key] = True
    print(f"[MONITOR] START {symbol} ({side}) | Entry: {entry:.4f} | TP: {tp:.4f} | SL: {sl:.4f}")
    
    try:
        # Die Break-Even Logik ist bei Short-Positionen invers
        be_trigger_long = entry * 1.02
        be_trigger_short = entry * 0.98
        be_set = False

        while True:
            curr = get_price_bingx(symbol)
            if not curr: time.sleep(1); continue

            # Break-Even Logik (+2% Gewinn Trigger)
            if not be_set:
                if side == "LONG" and curr >= be_trigger_long:
                    sl = entry
                    be_set = True
                    print(f"[BE] {symbol} aktiviert! SL auf Entry gesetzt.")
                elif side == "SHORT" and curr <= be_trigger_short:
                    sl = entry
                    be_set = True
                    print(f"[BE] {symbol} aktiviert! SL auf Entry gesetzt.")

            # EXIT TRIGGER (TP oder SL/BE erreicht)
            if (side == "LONG" and (curr >= tp or curr <= sl)) or \
               (side == "SHORT" and (curr <= tp or curr >= sl)):
                
                if (side == "LONG" and curr >= tp) or (side == "SHORT" and curr <= tp):
                    reason = "TP"
                else:
                    reason = "SL/BE"
                    
                print(f"[EXIT] {symbol} Triggered durch {reason} bei Preis: {curr:.4f}")
                close_bingx(symbol)
                break
                
            time.sleep(1)
    except Exception as e:
        print(f"[ERROR MONITOR] {symbol}: {e}")
    finally:
        active_monitors[key] = False
        print(f"[MONITOR] END {symbol}")
        
# ---------------- HEALTH CHECK (Unverändert) ----------------

@app.route("/", methods=["GET", "POST"])
def health_check():
    return jsonify({"status": "ok", "message": "Webhook erreichbar"}), 200

# ---------------- DEBUG ROUTE (Unverändert) ----------------

@app.route("/debug", methods=["GET"])
def debug_logs():
    return "Bitte Render Dashboard → Logs öffnen.", 200
        
# --- FLASK WEBHOCK HANDLER (Unverändert in der Logik, nutzt neue Trend-Funktion) ---

@app.route("/testorder", methods=["POST"])
def handle_alert():
    """Endpunkt für Handelssignale. Fragt BTC-Trend ab und platziert LONG/SHORT."""
    data = request.get_json(force=True, silent=True) or {}
    currency = str(data.get("currency", "")).upper()
    if not currency: return jsonify({"error": "no currency"}), 400
    
    symbol = f"{currency}-USDT"
    print(f"\n--- SIGNAL EMPFANGEN: {symbol} ---")

    if is_pos_open_bingx(symbol) or active_monitors.get(f"BINGX_{symbol}"):
        return jsonify({"status": "already_active", "symbol": symbol}), 200
    
    # NEUE LOGIK HIER: BTC Tendenz abfragen
    btc_trend = get_btc_hourly_trend()
    
    if btc_trend == "LONG":
        threading.Thread(target=execute_trade_bingx, args=(symbol, "LONG",)).start()
        return jsonify({"status": "order_started_long", "symbol": symbol, "btc_trend": btc_trend}), 200
    elif btc_trend == "SHORT":
        threading.Thread(target=execute_trade_bingx, args=(symbol, "SHORT",)).start()
        return jsonify({"status": "order_started_short", "symbol": symbol, "btc_trend": btc_trend}), 200
    else:
        return jsonify({"status": "trend_neutral_no_order", "symbol": symbol, "btc_trend": btc_trend}), 200


# --- APP START (Unverändert) ---

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
