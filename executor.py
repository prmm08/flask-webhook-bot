# executor.py
import logging
import time
from decimal import Decimal
from flask import Flask, request, jsonify

from config import (
    WEBHOOK_HOST, WEBHOOK_PORT, WEBHOOK_PATH, LOG_LEVEL,
    DEFAULT_TP_PERCENT, DEFAULT_SL_PERCENT, DEFAULT_TRADE_QTY,
    API_KEY, API_SECRET, BINGX_BASE, LEVERAGE, DEFAULT_TRADE_USD
)
from db import create_position

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [EXECUTOR] %(levelname)s %(message)s")
logger = logging.getLogger("executor")

app = Flask(__name__)

def get_market_price(symbol: str) -> float:
    """
    Placeholder for price feed. Replace with real API call to exchange or price oracle.
    Return price as float (USD per unit).
    """
    # Mock values for testing
    if not symbol:
        return 1.0
    s = symbol.upper()
    if s in ("BTC", "BTCUSDT", "BTCUSD"):
        return 30000.0
    if s in ("ETH", "ETHUSDT", "ETHUSD"):
        return 2000.0
    # fallback
    return 100.0

def usd_to_qty(usd_amount: float, price: float, leverage: int = 1) -> float:
    """
    Convert USD amount to asset quantity.
    For futures with leverage: notional = usd_amount * leverage.
    qty = notional / price
    """
    if price <= 0:
        raise ValueError("Invalid market price for conversion")
    notional = usd_amount * max(1, int(leverage))
    qty = notional / price
    return float(qty)

def _mock_place_order(symbol, side, qty, leverage):
    """
    Minimal placeholder for placing an order on BingX.
    Returns a dict with 'entry_price' and 'order_id'.
    """
    logger.debug("Placing mock order: %s %s qty=%s lev=%s", symbol, side, qty, leverage)
    entry_price = get_market_price(symbol)
    return {"entry_price": float(entry_price), "order_id": f"mock-{int(time.time())}"}

@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200

@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    if not data:
        logger.warning("Webhook: no JSON payload")
        return jsonify({"status": "ignored"}), 200

    currency = data.get("currency")
    direction = (data.get("direction") or "").upper()
    if not currency or direction not in ("LONG", "SHORT"):
        logger.info("Webhook ignored: missing currency or invalid direction")
        return jsonify({"status": "ignored"}), 200

    # priority: explicit trade_qty > trade_usd > DEFAULT_TRADE_USD/DEFAULT_TRADE_QTY
    trade_qty = None
    if data.get("trade_qty") is not None:
        try:
            trade_qty = float(data.get("trade_qty"))
        except Exception:
            logger.warning("Invalid trade_qty in payload, ignoring field")

    if trade_qty is None:
        # determine USD amount to use
        usd_amount = None
        if data.get("trade_usd") is not None:
            try:
                usd_amount = float(data.get("trade_usd"))
            except Exception:
                logger.warning("Invalid trade_usd in payload, falling back to default")
        if usd_amount is None:
            usd_amount = float(data.get("trade_usd") or DEFAULT_TRADE_USD)

        # get market price and leverage
        price = get_market_price(currency)
        leverage = int(data.get("leverage") or LEVERAGE or 1)
        try:
            trade_qty = usd_to_qty(usd_amount, price, leverage)
        except Exception as e:
            logger.exception("Failed to compute trade_qty from USD: %s", e)
            return jsonify({"status": "error", "message": "price conversion failed"}), 500

    # tp/sl parsing
    tp_percent = Decimal(str(data.get("tp_percent") or DEFAULT_TP_PERCENT))
    sl_percent = Decimal(str(data.get("sl_percent") or DEFAULT_SL_PERCENT))
    leverage = int(data.get("leverage") or LEVERAGE)

    try:
        # Place order (mock). Replace with real order placement if desired.
        order = _mock_place_order(currency, direction, trade_qty, leverage)
        entry_price = float(order.get("entry_price", 0.0))

        pos_id = create_position(
            symbol=currency,
            side=direction,
            entry_price=entry_price,
            qty=float(trade_qty),
            tp_percent=float(tp_percent),
            sl_percent=float(sl_percent)
        )
        logger.info("Created position id=%s symbol=%s side=%s qty=%s entry_price=%s", pos_id, currency, direction, trade_qty, entry_price)
        return jsonify({"status": "processing", "position_id": pos_id}), 200
    except Exception as e:
        logger.exception("Error creating position: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    app.run(host=WEBHOOK_HOST, port=WEBHOOK_PORT)
