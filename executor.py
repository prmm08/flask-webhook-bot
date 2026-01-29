from flask import Flask, request, jsonify
import time
from config import LEVERAGE, TRADE_SIZE, TP_PERCENT, SL_PERCENT, MAX_OPEN_POSITIONS
from bingx_api import (
    get_price, get_positions, symbol_exists,
    set_leverage, place_market_order
)
from db import create_position
import threading

app = Flask(__name__)


# ---------------------------------------------------------
#   EXECUTE TRADE
# ---------------------------------------------------------
def execute_trade(symbol, direction, leverage, trade_size, tp_percent, sl_percent):
    # 1) Check open positions limit
    positions = get_positions()
    if positions is None:
        print("[EXECUTOR] Konnte Positionen nicht abrufen")
        return

    open_positions_count = sum(1 for p in positions if float(p.get("positionAmt", 0)) != 0)
    if open_positions_count >= MAX_OPEN_POSITIONS:
        print(f"[LIMIT] {open_positions_count} >= MAX_OPEN_POSITIONS → Trade abgelehnt")
        return

    # 2) Symbol check
    if not symbol_exists(symbol):
        print("[EXECUTOR] Symbol existiert nicht:", symbol)
        return

    # 3) Check if position already open
    if any(p["symbol"] == symbol and p.get("positionSide") == direction and float(p["positionAmt"]) != 0
           for p in positions):
        print("[EXECUTOR] Position bereits offen:", symbol, direction)
        return

    # 4) Price
    price = get_price(symbol)
    if not price:
        print("[EXECUTOR] Kein Preis")
        return

    # 5) Set leverage
    ok = set_leverage(symbol, leverage, direction, "BUY" if direction == "LONG" else "SELL")
    if not ok:
        print("[EXECUTOR] Leverage Fehler")
        return

    # 6) Calculate qty
    qty = round(trade_size / price, 6)

    # 7) Place market order
    resp = place_market_order(
        symbol,
        "BUY" if direction == "LONG" else "SELL",
        direction,
        qty
    )

    if resp is None:
        print("[EXECUTOR] Order fehlgeschlagen")
        return

    # 8) Save initial state in DB
    pos_id = create_position(
        symbol=symbol,
        side=direction,
        entry_price=price,
        qty=qty,
        tp_percent=tp_percent,
        sl_percent=sl_percent
    )

    print(f"[EXECUTOR] Neue Position gespeichert → ID={pos_id}, {symbol} {direction}")


# ---------------------------------------------------------
#   WEBHOOK ENDPOINT
# ---------------------------------------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}

    currency = str(data.get("currency", "")).upper()
    direction = str(data.get("direction", "")).upper()

    if not currency or direction not in ("LONG", "SHORT"):
        return jsonify({"status": "ignored"}), 200

    symbol = f"{currency}-USDT"
    leverage = int(data.get("leverage", LEVERAGE))
    trade_size = float(data.get("trade_size", TRADE_SIZE))
    tp_percent = float(data.get("tp_percent", TP_PERCENT))
    sl_percent = float(data.get("sl_percent", SL_PERCENT))

    # Run trade execution in a short-lived thread
    threading.Thread(
        target=execute_trade,
        args=(symbol, direction, leverage, trade_size, tp_percent, sl_percent)
    ).start()

    return jsonify({"status": "processing"}), 200


@app.route("/ping")
def ping():
    return "pong", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
