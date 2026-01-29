#!/usr/bin/env python3
# dca_worker.py
# Führt einfache DCA‑Logik aus: prüft offene Positionen und fügt bei Bedarf zusätzliche Fills hinzu.
# Erwartet db.py mit load_active_positions, load_position, add_fill, update_last_dca_ts, enable_auto_close

import os
import time
import logging
import signal
from decimal import Decimal

from db import load_active_positions, load_position, add_fill, update_last_dca_ts

# Konfiguration
CHECK_INTERVAL = float(os.getenv("DCA_CHECK_INTERVAL", "30"))  # Sekunden
MIN_SECONDS_BETWEEN_DCA = float(os.getenv("MIN_SECONDS_BETWEEN_DCA", "3600"))  # 1 Stunde default
DCA_PERCENT_STEP = Decimal(os.getenv("DCA_PERCENT_STEP", "2.0"))  # Prozent unter local_avg, um DCA auszulösen
DCA_QTY_FACTOR = Decimal(os.getenv("DCA_QTY_FACTOR", "1.0"))  # Faktor der zusätzlichen Menge (z.B. 1.0 = gleiche Menge)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [DCA] %(levelname)s %(message)s")
logger = logging.getLogger("dca_worker")

running = True


def graceful_shutdown(signum, frame):
    global running
    logger.info("Shutdown signal received, stopping...")
    running = False


signal.signal(signal.SIGINT, graceful_shutdown)
signal.signal(signal.SIGTERM, graceful_shutdown)


# --- Platzhalter: Marktpreis abrufen (gleich wie in tpsl_worker)
def get_market_price(symbol: str) -> Decimal:
    return Decimal("30000.0") if symbol.upper() == "BTC" else Decimal("100.0")


def should_do_dca(pos):
    """
    Entscheidet, ob für pos ein DCA ausgeführt werden soll.
    Kriterien (Beispiel):
    - status == 'active'
    - seit last_dca_ts sind MIN_SECONDS_BETWEEN_DCA vergangen
    - aktueller Preis liegt mindestens DCA_PERCENT_STEP unter local_avg (für LONG)
      bzw. über local_avg + DCA_PERCENT_STEP (für SHORT)
    """
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

        price = get_market_price(symbol)
        change_pct = (price - local_avg) / local_avg * Decimal("100")
        if side == "SHORT":
            change_pct = -change_pct

        logger.debug("Pos %s: price=%s local_avg=%s change_pct=%s", pos.get("id"), price, local_avg, round(change_pct, 4))

        # DCA für LONG: price mindestens DCA_PERCENT_STEP unter local_avg
        if side == "LONG" and change_pct <= -DCA_PERCENT_STEP:
            return True
        if side == "SHORT" and change_pct >= DCA_PERCENT_STEP:
            return True

        return False
    except Exception as e:
        logger.exception("Fehler in should_do_dca für pos %s: %s", pos.get("id"), e)
        return False


def perform_dca(pos):
    """
    Führt ein DCA aus: berechnet qty anhand vorhandener fills oder einer Standardgröße,
    fügt einen Fill hinzu und updated last_dca_ts.
    """
    try:
        pos_id = pos.get("id")
        fills = pos.get("fills") or []
        if isinstance(fills, str):
            import json
            fills = json.loads(fills)

        # Bestimme Basisqty: Summe der bisherigen qty oder 1.0 als Fallback
        total_qty = sum(float(f.get("qty", 0)) for f in fills) if fills else 0.0
        base_qty = Decimal(str(total_qty)) if total_qty > 0 else Decimal("1.0")
        add_qty = (base_qty * DCA_QTY_FACTOR).quantize(Decimal("0.00000001"))

        symbol = pos.get("symbol")
        price = get_market_price(symbol)

        logger.info("DCA für Position %s: füge qty=%s zum Preis %s hinzu", pos_id, add_qty, price)
        add_fill(pos_id, float(add_qty), float(price))
        update_last_dca_ts(pos_id)
        return True
    except Exception:
        logger.exception("Fehler beim Ausführen von DCA für Position %s", pos.get("id"))
        return False


def main_loop():
    logger.info("DCA Worker gestartet, Intervall=%ss, DCA_STEP=%s%%", CHECK_INTERVAL, DCA_PERCENT_STEP)
    while running:
        try:
            positions = load_active_positions()
            if not positions:
                logger.debug("Keine aktiven Positionen.")
            for pos in positions:
                try:
                    if should_do_dca(pos):
                        perform_dca(pos)
                except Exception:
                    logger.exception("Fehler bei Position %s", pos.get("id"))
            time.sleep(CHECK_INTERVAL)
        except Exception:
            logger.exception("Unbehandelter Fehler in DCA Hauptschleife")
            time.sleep(5)

    logger.info("DCA Worker beendet.")


if __name__ == "__main__":
    main_loop()
