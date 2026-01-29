# tpsl_worker.py
import time
import logging
import signal
from decimal import Decimal
from config import TPSL_CHECK_INTERVAL, LOG_LEVEL
from db import load_active_positions, close_position

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [TPSL] %(levelname)s %(message)s")
logger = logging.getLogger("tpsl_worker")

running = True

def graceful_shutdown(signum, frame):
    global running
    logger.info("Shutdown signal received, stopping...")
    running = False

signal.signal(signal.SIGINT, graceful_shutdown)
signal.signal(signal.SIGTERM, graceful_shutdown)

def get_market_price(symbol: str) -> Decimal:
    # Replace with real price feed
    return Decimal("30000.0") if symbol.upper() == "BTC" else Decimal("100.0")

def check_tp_sl_for_position(pos):
    try:
        symbol = pos.get("symbol")
        side = pos.get("side", "").upper()
        local_avg = Decimal(str(pos.get("local_avg", 0)))
        tp_percent = Decimal(str(pos.get("tp_percent", 0)))
        sl_percent = Decimal(str(pos.get("sl_percent", 0)))
        current_price = get_market_price(symbol)

        if local_avg == 0:
            logger.debug("Position %s has local_avg 0, skipping", pos.get("id"))
            return False

        change_pct = (current_price - local_avg) / local_avg * Decimal("100")
        if side == "SHORT":
            change_pct = -change_pct

        logger.debug("Pos %s %s: price=%s local_avg=%s change_pct=%s", pos.get("id"), symbol, current_price, local_avg, round(change_pct, 4))

        if tp_percent and change_pct >= tp_percent:
            logger.info("TP reached for pos %s -> closing", pos.get("id"))
            close_position(pos.get("id"))
            return True

        if sl_percent and change_pct <= -sl_percent:
            logger.info("SL reached for pos %s -> closing", pos.get("id"))
            close_position(pos.get("id"))
            return True

        return False
    except Exception as e:
        logger.exception("Error checking TP/SL for pos %s: %s", pos.get("id"), e)
        return False

def main_loop():
    logger.info("TPSL Worker started, interval=%ss", TPSL_CHECK_INTERVAL)
    while running:
        try:
            positions = load_active_positions()
            for pos in positions:
                check_tp_sl_for_position(pos)
            time.sleep(TPSL_CHECK_INTERVAL)
        except Exception:
            logger.exception("Unhandled error in TPSL loop")
            time.sleep(5)
    logger.info("TPSL Worker stopped")

if __name__ == "__main__":
    main_loop()
