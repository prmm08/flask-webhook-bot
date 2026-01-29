# tpsl_worker.py
import time
import logging
from config import TPSL_CHECK_INTERVAL
from db import get_open_positions, set_position_status
from bingx_api import fetch_ticker_price
from bingx_api import place_market_order
logger = logging.getLogger("tpsl_worker")
logger.setLevel(logging.INFO)

def run_loop():
    while True:
        try:
            positions = get_open_positions()
            for pos in positions:
                symbol = pos["symbol"]
                side = pos["side"]
                tp_percent = float(pos.get("tp_percent") or 0)
                sl_percent = float(pos.get("sl_percent") or 0)
                entry = float(pos["entry_price"])
                qty = float(pos["qty"])
                current = fetch_ticker_price(symbol)
                # check TP
                if tp_percent > 0:
                    tp_price = entry * (1 + tp_percent/100) if side == "BUY" else entry * (1 - tp_percent/100)
                    if (side == "BUY" and current >= tp_price) or (side == "SELL" and current <= tp_price):
                        # close position market
                        try:
                            close_side = "SELL" if side == "BUY" else "BUY"
                            place_market_order(symbol=symbol, side=close_side, quantity=qty)
                            set_position_status(pos["id"], "closed")
                            logger.info("TP hit pos %s closed at %s", pos["id"], current)
                        except Exception:
                            logger.exception("Failed to close on TP for pos %s", pos["id"])
                        continue
                # check SL
                if sl_percent > 0:
                    sl_price = entry * (1 - sl_percent/100) if side == "BUY" else entry * (1 + sl_percent/100)
                    if (side == "BUY" and current <= sl_price) or (side == "SELL" and current >= sl_price):
                        try:
                            close_side = "SELL" if side == "BUY" else "BUY"
                            place_market_order(symbol=symbol, side=close_side, quantity=qty)
                            set_position_status(pos["id"], "closed")
                            logger.info("SL hit pos %s closed at %s", pos["id"], current)
                        except Exception:
                            logger.exception("Failed to close on SL for pos %s", pos["id"])
        except Exception:
            logger.exception("TPSL worker loop error")
        time.sleep(TPSL_CHECK_INTERVAL)

if __name__ == "__main__":
    run_loop()
