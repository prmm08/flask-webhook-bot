from flask import Flask, request, jsonify
from db import add_pending_trade, init_db
import logging

app = Flask(__name__)

# Einmaliges Setup der DB beim Start
init_db()

@app.route("/", methods=["GET"])
@app.route("/ping", methods=["GET"])
def ping():
    return "OK", 200

@app.route("/testorder", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    
    # Intelligente Ticker-Erkennung
    raw = data.get("ticker") or data.get("currency") or data.get("pair") or data.get("symbol")
    if not raw:
        return jsonify({"error": "No ticker found"}), 400
    
    symbol = str(raw).upper().replace("USDT", "").replace("-", "") + "-USDT"
    
    # Trade Objekt bauen
    trade_data = {
        "symbol": symbol,
        "direction": str(data.get("direction", "LONG")).upper(),
        "leverage": int(data.get("leverage", 20)),
        "trade_size": float(data.get("trade_size", 100)),
        "tp_percent": float(data.get("tp_percent", 1.0)),
        "sl_percent": float(data.get("sl_percent", 40.0))
    }
    
    # Ab in die DB damit
    try:
        add_pending_trade(trade_data)
        print(f"[WEBHOOK] Signal gespeichert: {symbol}", flush=True)
        return jsonify({"status": "queued", "msg": "Trade is safe in DB"}), 200
    except Exception as e:
        print(f"[WEBHOOK ERROR] {e}", flush=True)
        return jsonify({"error": "Database error"}), 500

if __name__ == "__main__":
    app.run()