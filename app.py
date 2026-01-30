from flask import Flask, request, jsonify
# WICHTIG: is_symbol_active importieren
from db import add_pending_trade, init_db, get_open_trade_count, is_symbol_active

app = Flask(__name__)

# --- KONFIGURATION ---
MAX_OPEN_POSITIONS = 20 

init_db()

@app.route("/", methods=["GET"])
@app.route("/ping", methods=["GET"])
def ping(): return "OK", 200

@app.route("/testorder", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    
    # 1. PARSING (Zuerst Symbol klären)
    raw = data.get("ticker") or data.get("currency") or data.get("pair") or data.get("symbol")
    if not raw: return jsonify({"error": "No ticker"}), 400
    
    symbol = str(raw).upper().replace("USDT", "").replace("-", "") + "-USDT"

    # 2. DUPLIKAT-CHECK (DAS IST NEU & WICHTIG!)
    # Läuft dieser Coin schon?
    if is_symbol_active(symbol):
        print(f"[IGNORE] Signal für {symbol} ignoriert (bereits aktiv).", flush=True)
        # Wir sagen TradingView "alles ok" (200), damit kein Alarm-Fehler kommt, 
        # aber wir speichern NICHTS.
        return jsonify({"status": "ignored", "reason": "symbol_already_active"}), 200

    # 3. GLOBAL LIMIT CHECK
    # Ist noch Platz im Portfolio allgemein?
    current_count = get_open_trade_count()
    if current_count >= MAX_OPEN_POSITIONS:
        print(f"[LIMIT] Signal für {symbol} ignoriert (Max {MAX_OPEN_POSITIONS} Trades erreicht).", flush=True)
        return jsonify({"status": "ignored", "reason": "global_limit_reached"}), 200

    # 4. TRADE ERSTELLEN
    trade_data = {
        "symbol": symbol,
        "direction": str(data.get("direction", "LONG")).upper(),
        "leverage": int(data.get("leverage", 20)),
        "trade_size": float(data.get("trade_size", 200)),
        "tp_percent": float(data.get("tp_percent", 0.5)),
        "sl_percent": float(data.get("sl_percent", 20.0))
    }
    
    try:
        add_pending_trade(trade_data)
        print(f"[WEBHOOK] NEUER START: {symbol} gespeichert.", flush=True)
        return jsonify({"status": "queued"}), 200
    except Exception as e:
        print(f"[ERROR] {e}", flush=True)
        return jsonify({"error": "db_error"}), 500

if __name__ == "__main__":
    app.run()