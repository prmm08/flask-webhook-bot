from flask import Flask, request, jsonify
from db import add_pending_trade, init_db, get_open_trade_count, is_symbol_active

app = Flask(__name__)

# --- KONFIGURATION ---
MAX_OPEN_POSITIONS = 30 

init_db()

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

    # 3. DUPLIKAT-CHECK
    if is_symbol_active(symbol):
        print(f"[IGNORE] Signal für {symbol} ignoriert (bereits aktiv).", flush=True)
        return jsonify({"status": "ignored", "reason": "symbol_already_active"}), 200

    # 4. GLOBAL LIMIT CHECK
    current_count = get_open_trade_count()
    if current_count >= MAX_OPEN_POSITIONS:
        print(f"[LIMIT] Signal für {symbol} ignoriert (Max {MAX_OPEN_POSITIONS} Trades erreicht).", flush=True)
        return jsonify({"status": "ignored", "reason": "global_limit_reached"}), 200

    # 5. TRADE ERSTELLEN (Ohne SL)
    trade_data = {
        "symbol": symbol,
        "direction": direction,
        "leverage": int(data.get("leverage", 20)),
        "trade_size": float(data.get("trade_size", 100)),
        "tp_percent": float(data.get("tp_percent", 0.5)),
        # SL ist hier nicht mehr nötig, DB setzt es auf 0
    }
    
    try:
        add_pending_trade(trade_data)
        print(f"[WEBHOOK] NEUER SHORT-START (NO SL): {symbol} gespeichert.", flush=True)
        return jsonify({"status": "queued"}), 200
    except Exception as e:
        print(f"[ERROR] {e}", flush=True)
        return jsonify({"error": "db_error"}), 500

if __name__ == "__main__":
    app.run()