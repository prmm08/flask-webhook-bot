from flask import Flask, request, jsonify
from db import add_pending_trade, init_db, get_open_trade_count, is_symbol_active
import os
import requests  # <--- WICHTIG: Das brauchen wir zum Senden

app = Flask(__name__)

# --- KONFIGURATION ---
MAX_OPEN_POSITIONS = 20  # Dein Limit

# Telegram Config (Muss auch hier in app.py stehen!)
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

init_db()

# --- HELPER: TELEGRAM SENDEN ---
def send_telegram_warning(symbol, current, max_limit):
    """Sendet eine Warnung, wenn das Limit voll ist"""
    if not TG_TOKEN or not TG_CHAT_ID: return
    try:
        msg = (
            f"⛔ <b>SIGNAL REJECTED</b>\n"
            f"Symbol: {symbol}\n"
            f"Reason: Portfolio Full\n"
            f"Status: {current}/{max_limit} Slots used."
        )
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        data = {
            "chat_id": TG_CHAT_ID, 
            "text": msg, 
            "parse_mode": "HTML"
        }
        requests.post(url, data=data, timeout=3)
    except Exception as e:
        print(f"[TG ERROR] {e}")

@app.route("/", methods=["GET"])
@app.route("/ping", methods=["GET"])
def ping(): return "OK", 200

@app.route("/testorder", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    
    # 1. PARSING
    raw = data.get("ticker") or data.get("currency") or data.get("pair") or data.get("symbol")
    if not raw: return jsonify({"error": "No ticker"}), 400
    
    symbol = str(raw).upper().replace("USDT", "").replace("-", "") + "-USDT"
    
    # 2. SHORT-ONLY FILTER
    direction_raw = str(data.get("direction", "")).upper()
    if direction_raw not in ["SHORT", "SELL"]:
        print(f"[FILTER] Ignoriere Signal für {symbol}. Grund: Richtung ist {direction_raw} (Nur SHORT erlaubt).", flush=True)
        return jsonify({"status": "ignored", "reason": "only_shorts_allowed"}), 200
    
    direction = "SHORT"

    # 3. DUPLIKAT-CHECK (Läuft der Coin schon?)
    if is_symbol_active(symbol):
        print(f"[IGNORE] Signal für {symbol} ignoriert (bereits aktiv).", flush=True)
        return jsonify({"status": "ignored", "reason": "symbol_already_active"}), 200

    # 4. GLOBAL LIMIT CHECK (HIER PASSIERT ES!)
    current_count = get_open_trade_count()
    
    # Wenn wir 3 oder mehr haben...
    if current_count >= MAX_OPEN_POSITIONS:
        print(f"[LIMIT] Signal für {symbol} blockiert. Limit {MAX_OPEN_POSITIONS} erreicht.", flush=True)
        
        # ---> SENDE TELEGRAM NACHRICHT <---
        send_telegram_warning(symbol, current_count, MAX_OPEN_POSITIONS)
        
        return jsonify({"status": "ignored", "reason": "global_limit_reached"}), 200

    # 5. TRADE ERSTELLEN
    trade_data = {
        "symbol": symbol,
        "direction": direction,
        "leverage": int(data.get("leverage", 20)),
        "trade_size": float(data.get("trade_size", 200)),
        "tp_percent": float(data.get("tp_percent", 0.5)),
        # SL ist 0
    }
    
    try:
        add_pending_trade(trade_data)
        print(f"[WEBHOOK] NEUER SHORT-START: {symbol} gespeichert.", flush=True)
        return jsonify({"status": "queued"}), 200
    except Exception as e:
        print(f"[ERROR] {e}", flush=True)
        return jsonify({"error": "db_error"}), 500

if __name__ == "__main__":
    app.run()