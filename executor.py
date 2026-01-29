#!/usr/bin/env python3
# executor.py
# Flask webhook that places orders (BingX integration if available) and stores positions in DB.

import logging
import time
from decimal import Decimal
from flask import Flask, request, jsonify

from config import (
    WEBHOOK_HOST, WEBHOOK_PORT, WEBHOOK_PATH, LOG_LEVEL,
    DEFAULT_TP_PERCENT, DEFAULT_SL_PERCENT, DEFAULT_TRADE_QTY,
    API_KEY, API_SECRET, BINGX_BASE, LEVERAGE, DEFAULT_TRADE_USD,
    DCA_COUNT, DCA_DEVIATION_PERCENT, DCA_VOLUME_MULTIPLIER
)
from db import create_position

# Optional BingX integration: try to import place_market_order from bingx.py
try:
    from bingx import place_market_order  # type: ignore
    _HAS_BINGX = True
except Exception:
    _HAS_BINGX = False

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [EXECUTOR] %(levelname)s %(message)s")
logger = logging.getLogger("executor")

app = Flask(__name__)


def get_market_price(symbol: str) -> float:
    """
    Placeholder price feed. Replace with real price API or exchange quote.
    Return price as float (USD per unit).
    """
    if not symbol:
        return 1.0
    s = symbol.upper()
    if s in ("BTC", "BTCUSDT", "BTCUSD"):
        return 30000.0
    if s in ("ETH", "ETHUSDT", "ETHUSD"):
        return 2000.0
    return 100.0


def usd_to_qty(usd_amount: float, price: float, leverage: int = 1) -> float:
    """
    Convert USD amount to asset quantity.
    For leveraged futures: notional = usd_amount * leverage.
    qty = notional / price
    """
    if price <= 0:
        raise ValueError("Invalid market price for conversion")
    notional = usd_amount * max(1, int(leverage))
    qty = notional / price
    return float(qty)


def _mock_place_order(symbol: str, side: str, qty: float, leverage: int):
    """
    Fallback mock order placement. Returns dict with entry_price and order_id.
    """
    logger.debug("Mock order: %s %s qty=%s lev=%s", symbol, side, qty, leverage)
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

    # Determine trade_qty: explicit trade_qty > trade_usd > DEFAULT_TRADE_USD
    trade_qty = None
    if data.get("trade_qty") is not None:
        try:
            trade_qty = float(data.get("trade_qty"))
        except Exception:
            logger.warning("Invalid trade_qty in payload, ignoring field")

    if trade_qty is None:
        usd_amount = None
        if data.get("trade_usd") is not None:
            try:
                usd_amount = float(data.get("trade_usd"))
            except Exception:
                logger.warning("Invalid trade_usd in payload, falling back to default")
        if usd_amount is None:
            usd_amount = float(data.get("trade_usd") or DEFAULT_TRADE_USD)

        price = get_market_price(currency)
        leverage = int(data.get("leverage") or LEVERAGE or 1)
        try:
            trade_qty = usd_to_qty(usd_amount, price, leverage)
        except Exception as e:
            logger.exception("Failed to compute trade_qty from USD: %s", e)
            return jsonify({"status": "error", "message": "price conversion failed"}), 500

    # Parse tp/sl and leverage
    tp_percent = Decimal(str(data.get("tp_percent") or DEFAULT_TP_PERCENT))
    sl_percent = Decimal(str(data.get("sl_percent") or DEFAULT_SL_PERCENT))
    leverage = int(data.get("leverage") or LEVERAGE or 1)

    # Per-position DCA overrides (optional)
    dca_count = int(data.get("dca_count") or DCA_COUNT)
    dca_dev = float(data.get("dca_deviation_percent") or float(DCA_DEVIATION_PERCENT))
    dca_mult = float(data.get("dca_volume_multiplier") or float(DCA_VOLUME_MULTIPLIER))

    # Map direction to exchange side (adjust if your exchange expects different strings)
    side = "BUY" if direction == "LONG" else "SELL"

    # Place order: prefer real BingX integration if available, otherwise mock
    try:
        if _HAS_BINGX:
            # symbol mapping: ensure correct market symbol (e.g., "BTCUSDT")
            market_symbol = currency if currency.upper().endswith("USDT") else f"{currency}USDT"
            resp = place_market_order(symbol=market_symbol, side=side, qty=trade_qty, leverage=leverage, contract_type="linear")
            # Extract entry price and order id from response (adjust keys to actual API)
            entry_price = float(resp.get("filledPrice") or resp.get("avgPrice") or resp.get("data", {}).get("price", 0.0) or 0.0)
            order_id = resp.get("orderId") or resp.get("data", {}).get("orderId") or None
        else:
            resp = _mock_place_order(currency, side, trade_qty, leverage)
            entry_price = float(resp.get("entry_price", 0.0))
            order_id = resp.get("order_id")
    except Exception as e:
        logger.exception("Order placement failed: %s", e)
        return jsonify({"status": "error", "message": "order placement failed"}), 500

    # Persist position in DB with per-position DCA/TP/SL values
    try:
        pos_id = create_position(
            symbol=currency,
            side=direction,
            entry_price=entry_price,
            qty=float(trade_qty),
            tp_percent=float(tp_percent),
            sl_percent=float(sl_percent),
            dca_count=dca_count,
            dca_deviation_percent=dca_dev,
            dca_volume_multiplier=dca_mult
        )
        logger.info("Created position id=%s symbol=%s side=%s qty=%s entry_price=%s order_id=%s",
                    pos_id, currency, direction, trade_qty, entry_price, order_id)
        return jsonify({"status": "processing", "position_id": pos_id, "order_id": order_id}), 200
    except Exception as e:
        logger.exception("Error creating position in DB: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    app.run(host=WEBHOOK_HOST, port=WEBHOOK_PORT)
