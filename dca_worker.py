# dca_worker.py
import time
import logging
import signal
from decimal import Decimal
from config import (
    DCA_CHECK_INTERVAL, MIN_SECONDS_BETWEEN_DCA,
    DCA_DEVIATION_PERCENT, DCA_VOLUME_MULTIPLIER, LOG_LEVEL
)
from db import load_active_positions, add_fill, update_last_dca_ts

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [DCA] %(levelname)s %(message)s")
logger = logging.getLogger("dca_worker")

running = True

def graceful_shutdown(signum, frame):
    global running
    logger.info("Shutdown signal received, stopping...")
    running = False

signal.signal(signal.SIGINT, graceful_shutdown)
signal.signal(signal.SIGTERM, graceful_shutdown)

def get_market_price(symbol: str) -> Decimal:
    return Decimal("30000.0") if symbol and symbol.upper().startswith("BTC") else Decimal("100.0")

def should_do_dca(pos):
    try:
        if pos.get("status") != "active":
            return False
        last_ts = float(pos.get("last_dca_ts") or 0)
        now = time.time()
        if now - last_ts < MIN_SECONDS_BETWEEN_DCA:
            return False

        symbol = pos.get("symbol")
        side = pos.get("side", "").upper()
        local_avg = Decimal(str(pos.get("local_avg", 0)))
        if local_avg == 0:
            return False

        # per-position override or global default
        dca_dev = Decimal(str(pos.get("dca_deviation_percent") or DCA_DEVIATION_PERCENT))
        price = get_market_price(symbol)
        change_pct = (price - local_avg) / local_avg * Decimal("100")
        if side == "SHORT":
            change_pct = -change_pct

        logger.debug("Pos %s: price=%s local_avg=%s change_pct=%s dca_dev=%s", pos.get("id"), price, local_avg, round(change_pct,4), dca_dev)

        if side == "LONG" and change_pct <= -dca_dev:
            return True
        if side == "SHORT" and change_pct >= dca_dev:
            return True
        return False
    except Exception as e:
        logger.exception("Error in should_do_dca for pos %s: %s", pos.get("id"), e)
        return False

def perform_dca(pos):
    try:
        pos_id = pos.get("id")
        fills = pos.get("fills") or []
        if isinstance(fills, str):
            import json
            fills = json.loads(fills)
        total_qty = sum(float(f.get("qty", 0)) for f in fills) if fills else 0.0
        base_qty = Decimal(str(total_qty)) if total_qty > 0 else Decimal("1.0")

        # per-position override or global default
        dca_mult = Decimal(str(pos.get("dca_volume_multiplier") or DCA_VOLUME_MULTIPLIER))
        add_qty = (base_qty * dca_mult).quantize(Decimal("0.00000001"))

        symbol = pos.get("symbol")
        price = get_market_price(symbol)
        logger.info("DCA for pos %s: adding qty=%s at price=%s", pos_id, add_qty, price)
        add_fill(pos_id, float(add_qty), float(price))
        update_last_dca_ts(pos_id)
        return True
    except Exception:
        logger.exception("Error performing DCA for pos %s", pos.get("id"))
        return False

def main_loop():
    logger.info("DCA Worker started, interval=%ss, deviation=%s%%", DCA_CHECK_INTERVAL, DCA_DEVIATION_PERCENT)
    while running:
        try:
            positions = load_active_positions()
            for pos in positions:
                if should_do_dca(pos):
                    perform_dca(pos)
            time.sleep(DCA_CHECK_INTERVAL)
        except Exception:
            logger.exception("Unhandled error in DCA loop")
            time.sleep(5)
    logger.info("DCA Worker stopped")

if __name__ == "__main__":
    main_loop()
