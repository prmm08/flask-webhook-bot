import time
from db import load_active_positions
from bingx_api import (
    get_positions, get_open_orders,
    cancel_all_tp, set_tp_sl
)


def run_tpsl_watcher():
    print("[TP/SL WORKER] gestartet...")

    while True:
        try:
            # ---------------------------------------------------------
            #   1) ACTIVE POSITIONS FROM DB
            # ---------------------------------------------------------
            positions_db = load_active_positions()
            if not positions_db:
                time.sleep(10)
                continue

            # ---------------------------------------------------------
            #   2) EXCHANGE POSITIONS
            # ---------------------------------------------------------
            exchange_positions = get_positions()
            if exchange_positions is None:
                time.sleep(10)
                continue

            for pos in positions_db:
                pos_id = pos["id"]
                symbol = pos["symbol"]
                side = pos["side"]
                local_avg = float(pos["local_avg"])
                tp_percent = float(pos["tp_percent"])
                sl_percent = float(pos["sl_percent"])
                executed = int(pos["executed"])

                # ---------------------------------------------------------
                #   3) FIND MATCHING EXCHANGE POSITION
                # ---------------------------------------------------------
                ex_pos = next(
                    (p for p in exchange_positions
                     if p["symbol"] == symbol and p["positionSide"] == side and float(p["positionAmt"]) != 0),
                    None
                )

                if not ex_pos:
                    continue

                # ---------------------------------------------------------
                #   4) CHECK OPEN ORDERS
                # ---------------------------------------------------------
                orders = get_open_orders(symbol)
                has_tp = any(o.get("type") == "TAKE_PROFIT_MARKET" and o.get("positionSide") == side for o in orders)
                has_sl = any(o.get("type") == "STOP_MARKET" and o.get("positionSide") == side for o in orders)

                if has_tp and has_sl:
                    continue  # alles ok

                print(f"[TP/SL WATCHER] {symbol} {side} → TP={has_tp} SL={has_sl} → neu setzen")

                # ---------------------------------------------------------
                #   5) CANCEL EXISTING TP
                # ---------------------------------------------------------
                cancel_all_tp(symbol, side)
                time.sleep(0.2)

                # ---------------------------------------------------------
                #   6) CALCULATE TP/SL
                # ---------------------------------------------------------
                if executed >= 1:
                    # BE‑TP
                    tp_price = local_avg
                    if side == "LONG":
                        sl_price = local_avg * (1 - sl_percent / 100)
                    else:
                        sl_price = local_avg * (1 + sl_percent / 100)
                else:
                    # Prozent‑TP
                    entry = float(ex_pos["avgPrice"])
                    if side == "LONG":
                        tp_price = entry * (1 + tp_percent / 100)
                        sl_price = entry * (1 - sl_percent / 100)
                    else:
                        tp_price = entry * (1 - tp_percent / 100)
                        sl_price = entry * (1 + sl_percent / 100)

                # ---------------------------------------------------------
                #   7) SET TP/SL
                # ---------------------------------------------------------
                set_tp_sl(symbol, side, tp_price, sl_price)

        except Exception as e:
            print("[TP/SL WORKER ERROR]", e)

        time.sleep(10)
