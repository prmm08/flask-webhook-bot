# executor.py
import os
import logging
from decimal import Decimal
from flask import Flask, request, jsonify
from config import WEBHOOK_PATH, WEBHOOK_HOST, WEBHOOK_PORT, DEFAULT_LEVERAGE, MIN_QTY, STEP_SIZE, LOG_LEVEL
from db import init_schema, create_position, update_position_orders
from bingx_api import place_market_order, place_limit_order, place_stop_order, fetch_ticker_price
from utils import round_qty, make_client_ref

app = Flask(__name__)
logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger("executor")

# ensure DB
try:
    init_schema()
    logger.info("DB schema ensured")
except Exception:
    logger.exception("DB init failed")

@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    data = request.get_json(silent=True)
    if not data:
        logger.warning("Webhook: no JSON payload")
        return jsonify({"status":"no_json"}), 400

    currency = data.get("currency")
    direction = data.get("direction")
    trade_usd = float(data.get("trade_usd", 0))
    leverage = int(data.get("leverage", DEFAULT_LEVERAGE))
    tp_percent = float(data.get("tp_percent", 0.0))
    sl_percent = float(data.get("sl_percent", 0.0))
    dca_count = int(data.get("dca_count", 0))
    dca_deviation_percent = float(data.get("dca_deviation_percent", 0.0))
    dca_volume_multiplier = float(data.get("dca_volume_multiplier", 1.0))

    if not currency or direction not in ("LONG","SHORT"):
        return jsonify({"status":"invalid_payload"}), 400

    side = "BUY" if direction == "LONG" else "SELL"
    symbol = currency if currency.upper().endswith("USDT") else f"{currency}USDT"

    # fetch price
    try:
        price = fetch_ticker_price(symbol)
    except Exception as e:
        logger.exception("Price fetch failed")
        return jsonify({"status":"price_error","error":str(e)}), 500

    # compute qty
    trade_qty = (Decimal(str(trade_usd)) / Decimal(str(price)))
    trade_qty = float(trade_qty)
    qty = round_qty(trade_qty, STEP_SIZE)
    if qty < MIN_QTY:
        logger.warning("Qty too small %s < %s", qty, MIN_QTY)
        return jsonify({"status":"qty_too_small","qty":qty}), 400

    client_ref = make_client_ref(data)

    # place market order
    try:
        resp = place_market_order(symbol=symbol, side=side, quantity=qty, leverage=leverage)
        # extract order id and filled price - adapt to API response
        order_id = resp.get("orderId") or resp.get("data", {}).get("orderId") or resp.get("order_id")
        entry_price = None
        if resp.get("avgPrice"):
            entry_price = float(resp.get("avgPrice"))
        elif resp.get("filledPrice"):
            entry_price = float(resp.get("filledPrice"))
        else:
            entry_price = float(price)

        pos_id = create_position(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            qty=qty,
            order_id=order_id,
            fills=resp,
            local_avg=entry_price,
            tp_percent=tp_percent,
            sl_percent=sl_percent,
            dca_count=dca_count,
            dca_deviation_percent=dca_deviation_percent,
            dca_volume_multiplier=dca_volume_multiplier
        )

        # place TP and SL orders if configured
        tp_order_id = None
        sl_order_id = None
        try:
            if tp_percent and tp_percent > 0:
                tp_price = entry_price * (1 + tp_percent/100) if side == "BUY" else entry_price * (1 - tp_percent/100)
                tp_resp = place_limit_order(symbol=symbol, side="SELL" if side=="BUY" else "BUY", price=tp_price, quantity=qty)
                tp_order_id = tp_resp.get("orderId") or tp_resp.get("data", {}).get("orderId")
            if sl_percent and sl_percent > 0:
                sl_price = entry_price * (1 - sl_percent/100) if side == "BUY" else entry_price * (1 + sl_percent/100)
                sl_resp = place_stop_order(symbol=symbol, side="SELL" if side=="BUY" else "BUY", stop_price=sl_price, quantity=qty)
                sl_order_id = sl_resp.get("orderId") or sl_resp.get("data", {}).get("orderId")
            update_position_orders(pos_id, tp_order_id, sl_order_id)
        except Exception:
            logger.exception("TP/SL placement failed; position created without TP/SL")

        logger.info("Created position id=%s symbol=%s qty=%s entry=%s order=%s", pos_id, symbol, qty, entry_price, order_id)
        return jsonify({"status":"ok","position_id":pos_id,"order_id":order_id}), 200

    except Exception as e:
        logger.exception("Order placement failed")
        return jsonify({"status":"order_error","error":str(e)}), 500

if __name__ == "__main__":
    app.run(host=WEBHOOK_HOST, port=WEBHOOK_PORT)
