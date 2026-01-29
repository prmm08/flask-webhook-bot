#!/usr/bin/env python3
# tpsl_worker.py
# Überwacht offene Positionen und führt einfache TP/SL‑Logik aus.
# Erwartet, dass db.py die Funktionen load_active_positions, load_position, close_position, update_executed etc. bereitstellt.

import os
import time
import logging
import signal
import sys
from decimal import Decimal

from db import load_active_positions, load_position, close_position, update_executed

# Konfiguration
CHECK_INTERVAL = float(os.getenv("TPSL_CHECK_INTERVAL", "5"))  # Sekunden
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [TPSL] %(levelname)s %(message)s")
logger = logging.getLogger("tpsl_worker")

running = True


def graceful_shutdown(signum, frame):
    global running
    logger.info("Shutdown signal received, stopping...")
    running = False


signal.signal(signal.SIGINT, graceful_shutdown)
signal.signal(signal.SIGTERM, graceful_shutdown)


# --- Platzhalter: Marktpreis abrufen
# Ersetze diese Funktion durch eine echte Marktdatenabfrage (API, Websocket, Exchange SDK).
def get_market_price(symbol: str) -> Decimal:
    """
    Liefert den aktuellen Marktpreis für das gegebene Symbol.
    Aktuell stub: muss durch echte Implementierung ersetzt werden.
    """
    # Beispiel: feste Testwerte oder einfache Simulation
    # In Produktion: REST/Websocket-Call zu Exchange/Price-Feed
    return Decimal("30000.0") if symbol.upper() == "BTC" else Decimal("100.0")


def check_tp_sl_for_position(pos):
    """
    pos: dict mit Feldern wie symbol, side, local_avg, tp_percent, sl_percent, fills, executed, status
    Returns: True wenn Aktion ausgeführt wurde (z.B. close_position)
    """
    try:
        symbol = pos.get("symbol")
        side = pos.get("side", "").upper()
        local_avg = Decimal(str(pos.get("local_avg", 0)))
        tp_percent = Decimal(str(pos.get("tp_percent", 0)))
        sl_percent = Decimal(str(pos.get("sl_percent", 0)))
        current_price = get_market_price(symbol)

        if local_avg == 0:
            logger.debug("Position %s hat local_avg 0, überspringe", pos.get("id"))
            return False

        # Gewinn/Verlust in Prozent relativ zum local_avg
        change_pct = (current_price - local_avg) / local_avg * Decimal("100")
        if side == "SHORT":
            change_pct = -change_pct  # invert für Short

        logger.debug("Pos %s %s: price=%s local_avg=%s change_pct=%s", pos.get("id"), symbol, current_price, local_avg, round(change_pct, 4))

        # Take profit
        if tp_percent and change_pct >= tp_percent:
            logger.info("TP erreicht für Position %s (%s): change=%s%% >= tp=%s%% -> schließen", pos.get("id"), symbol, round(change_pct, 4), tp_percent)
            close_position(pos.get("id"))
            return True

        # Stop loss
        if sl_percent and change_pct <= -sl_percent:
            logger.info("SL erreicht für Position %s (%s): change=%s%% <= -sl=%s%% -> schließen", pos.get("id"), symbol, round(change_pct, 4), sl_percent)
            close_position(pos.get("id"))
            return True

        return False
    except Exception as e:
        logger.exception("Fehler beim Prüfen von TP/SL für Position %s: %s", pos.get("id"), e)
        return False


def main_loop():
    logger.info("TPSL Worker gestartet, Intervall=%ss", CHECK_INTERVAL)
    while running:
        try:
            positions = load_active_positions()
            if not positions:
                logger.debug("Keine aktiven Positionen gefunden.")
            for pos in positions:
                # pos ist dict (db.py liefert dict_row)
                acted = check_tp_sl_for_position(pos)
                if acted:
                    # optional: update executed oder andere Nacharbeiten
                    logger.debug("Aktion für Position %s ausgeführt.", pos.get("id"))
            time.sleep(CHECK_INTERVAL)
        except Exception:
            logger.exception("Unbehandelter Fehler in Hauptschleife")
            time.sleep(5)

    logger.info("TPSL Worker beendet.")


if __name__ == "__main__":
    main_loop()
