from flask import Flask, request, jsonify
from db import add_pending_trade, init_db, get_open_trade_count

app = Flask(__name__)

# --- KONFIGURATION ---
MAX_OPEN_POSITIONS = 3  # Dein Limit

# DB beim Start prüfen
init_db()

@app.route("/", methods=["GET"])
@app.route("/ping", methods=["GET"])
def ping(): return "OK", 200

@app.route("/testorder", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    
    # 1. LIMIT PRÜFUNG (Vor dem Speichern)
    current_count = get_open_trade_count()
    if current_count >= MAX_OPEN_POSITIONS:
        print(f"[LIMIT] Ignoriere Signal. {current_count}/{MAX_OPEN_POSITIONS} Slots belegt.", flush=True)
        # Wir geben 'success' zurück, damit TradingView nicht meckert, aber tun nichts.
        return jsonify({"status": "ignored", "reason": "limit_reached"}), 200

    # 2. PARSING
    raw = data.get("ticker") or data.get("currency") or data.get("pair") or data.get("symbol")
    if not raw: return jsonify({"error": "No ticker"}), 400
    
    symbol = str(raw).upper().replace("USDT", "").replace("-", "") + "-USDT"
    
    trade_data = {
        "symbol": symbol,
        "direction": str(data.get("direction", "LONG")).upper(),
        "leverage": int(data.get("leverage", 20)),
        "trade_size": float(data.get("trade_size", 100)),
        "tp_percent": float(data.get("tp_percent", 1.0)),
        "sl_percent": float(data.get("sl_percent", 40.0))
    }
    
    try:
        add_pending_trade(trade_data)
        print(f"[WEBHOOK] Signal gespeichert: {symbol}", flush=True)
        return jsonify({"status": "queued"}), 200
    except Exception as e:
        print(f"[ERROR] {e}", flush=True)
        return jsonify({"error": "db_error"}), 500

if __name__ == "__main__":
    app.run()