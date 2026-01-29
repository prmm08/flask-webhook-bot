# dca_worker.py
import time
import logging
from config import DCA_CHECK_INTERVAL
from db import get_open_positions, append_dca
from bingx_api import fetch_ticker_price, place_market_order
from utils import round_qty

logger = logging.getLogger("dca_worker")
logger.setLevel(logging.INFO)

def run_loop():
    while True:
        try:
            positions = get_open_positions()
            for pos in positions:
                # DCA only if configured
                if pos["dca_count"] <= 0:
                    continue
                symbol = pos["symbol"]
                base_side = pos["side"]  # BUY means LONG
                current_price = fetch_ticker_price(symbol)
                entry = float(pos["entry_price"])
                deviation = float(pos["dca_deviation_percent"])
                # compute threshold
                if base_side == "BUY":
                    threshold_price = entry * (1 - deviation/100)
                    need_dca = current_price <= threshold_price
                else:
                    threshold_price = entry * (1 + deviation/100)
                    need_dca = current_price >= threshold_price

                if need_dca:
                    # compute DCA qty: previous qty * multiplier
                    existing_qty = float(pos["qty"])
                    multiplier = float(pos["dca_volume_multiplier"])
                    add_qty = existing_qty * multiplier
                    # round
                    add_qty = round_qty(add_qty, 0.000001)
                    if add_qty <= 0:
                        continue
                    # place market order in same direction
                    side = "BUY" if base_side == "BUY" else "SELL"
                    try:
                        resp = place_market_order(symbol=symbol, side=side, quantity=add_qty)
                        # compute new average price (simplified)
                        filled_price = resp.get("avgPrice") or resp.get("filledPrice") or current_price
                        new_avg = (existing_qty * entry + add_qty * float(filled_price)) / (existing_qty + add_qty)
                        append_dca(pos["id"], add_qty, new_avg)
                        logger.info("DCA executed for pos %s added_qty=%s new_avg=%s", pos["id"], add_qty, new_avg)
                    except Exception:
                        logger.exception("DCA order failed for pos %s", pos["id"])
        except Exception:
            logger.exception("DCA worker loop error")
        time.sleep(DCA_CHECK_INTERVAL)

if __name__ == "__main__":
    run_loop()
