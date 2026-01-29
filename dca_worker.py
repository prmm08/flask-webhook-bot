import time
from config import (
    DCA_COUNT, DCA_DEVIATION_PERCENT, DCA_VOLUME_MULTIPLIER,
    AUTO_CLOSE_FROM_DCA, AUTO_CLOSE_BUFFER
)
from db import (
    load_active_positions, add_fill, update_executed,
    update_last_dca_ts, enable_auto_close, close_position
)
from bingx_api import (
    get_price, place_market_order, cancel_all_tp,
    close_position_market, set_tp_sl
)


# ---------------------------------------------------------
#   DCA QTY CALCULATION
# ---------------------------------------------------------
def calculate_dca_qty(base_trade_size, executed, current_price):
    multiplier = DCA_VOLUME_MULTIPLIER ** (executed + 1)
    return round((base_trade_size * multiplier) / current_price, 6)


# ---------------------------------------------------------
#   MAIN LOOP
# ---------------------------------------------------------
def run_dca_engine():
    print("[DCA WORKER] gestartet...")

    while True:
        try:
            positions = load_active_positions()

            for pos in positions:
                pos_id = pos["id"]
                symbol = pos["symbol"]
                side = pos["side"]
                entry_static = float(pos["entry_static"])
                executed = int(pos["executed"])
                local_avg = float(pos["local_avg"])
                tp_percent = float(pos["tp_percent"])
                sl_percent = float(pos["sl_percent"])
                last_ts = float(pos["last_dca_ts"])
                auto_close_enabled = pos["auto_close_enabled"]

                # ---------------------------------------------------------
                #   1) PRICE
                # ---------------------------------------------------------
                current_price = get_price(symbol)
                if current_price is None:
                    continue

                # ---------------------------------------------------------
                #   2) AUTO CLOSE CHECK
                # ---------------------------------------------------------
                if auto_close_enabled:
                    if side == "LONG":
                        threshold = local_avg * (1 + AUTO_CLOSE_BUFFER)
                        reached = current_price >= threshold
                    else:
                        threshold = local_avg * (1 - AUTO_CLOSE_BUFFER)
                        reached = current_price <= threshold

                    if reached:
                        print(f"[AUTO CLOSE] {symbol} erreicht BE {local_avg:.6f}")

                        cancel_all_tp(symbol, side)
                        time.sleep(0.2)

                        resp = close_position_market(symbol, side)
                        if resp:
                            close_position(pos_id)
                            print(f"[AUTO CLOSE] {symbol} geschlossen")
                        continue

                # ---------------------------------------------------------
                #   3) DCA LIMIT
                # ---------------------------------------------------------
                if executed >= DCA_COUNT:
                    continue

                # ---------------------------------------------------------
                #   4) DCA COOLDOWN
                # ---------------------------------------------------------
                if time.time() - last_ts < 2:
                    continue

                # ---------------------------------------------------------
                #   5) DCA TRIGGER
                # ---------------------------------------------------------
                if side == "LONG":
                    trigger = current_price <= entry_static * (1 - DCA_DEVIATION_PERCENT / 100)
                else:
                    trigger = current_price >= entry_static * (1 + DCA_DEVIATION_PERCENT / 100)

                if not trigger:
                    continue

                # ---------------------------------------------------------
                #   6) DCA QTY
                # ---------------------------------------------------------
                base_trade_size = float(pos["fills"][0]["qty"]) * float(pos["fills"][0]["price"])
                qty = calculate_dca_qty(base_trade_size, executed, current_price)

                print(f"[DCA] {symbol} → DCA#{executed + 1} qty={qty} price={current_price}")

                # ---------------------------------------------------------
                #   7) PLACE MARKET ORDER
                # ---------------------------------------------------------
                resp = place_market_order(
                    symbol,
                    "BUY" if side == "LONG" else "SELL",
                    side,
                    qty
                )

                if resp is None:
                    print(f"[DCA ERROR] Order fehlgeschlagen für {symbol}")
                    continue

                # ---------------------------------------------------------
                #   8) UPDATE LOCAL FILLS
                # ---------------------------------------------------------
                add_fill(pos_id, qty, current_price)

                # ---------------------------------------------------------
                #   9) UPDATE EXECUTED COUNT
                # ---------------------------------------------------------
                update_executed(pos_id, executed + 1)

                # ---------------------------------------------------------
                #   10) UPDATE LAST TS
                # ---------------------------------------------------------
                update_last_dca_ts(pos_id)

                # ---------------------------------------------------------
                #   11) ENABLE AUTO CLOSE
                # ---------------------------------------------------------
                if executed + 1 >= AUTO_CLOSE_FROM_DCA:
                    enable_auto_close(pos_id)

                # ---------------------------------------------------------
                #   12) SET TP/SL BASED ON LOCAL AVG
                # ---------------------------------------------------------
                new_local_avg = float(load_active_positions()[0]["local_avg"])

                if side == "LONG":
                    tp_price = new_local_avg
                    sl_price = new_local_avg * (1 - sl_percent / 100)
                else:
                    tp_price = new_local_avg
                    sl_price = new_local_avg * (1 + sl_percent / 100)

                cancel_all_tp(symbol, side)
                time.sleep(0.2)

                set_tp_sl(symbol, side, tp_price, sl_price)

        except Exception as e:
            print("[DCA WORKER ERROR]", e)

        time.sleep(1)
