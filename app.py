# -------- V 3.0: BINGX FUTURES ONLY - SHORT ONLY + RSI FILTER + PRÄZISERE TP/SL/BE-TRIGGER --------

import time
import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
from flask import Flask, request, jsonify

# --- API Konfiguration BingX ---
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

# --- Flask ohne Access Logs starten ---
import logging
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)

active_monitors = {}

# Cache für Tick-Sizes
TICK_SIZE_CACHE = {}

# --- RSI TIMEFRAME (wählbar: "1m", "5m", "15m") ---
RSI_TIMEFRAME = "1m"

# ---------------- SIGNING ----------------

def sign_bingx(params):
    query = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

# ---------------- PRICE (Fallback) ----------------

def get_price_bingx(symbol):
    """
    Fallback-Preis über /quote/price (nicht so präzise wie Orderbuch).
    Wird hier nur noch als Backup genutzt, Midprice kommt aus get_midprice().
    """
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/price"
        r = requests.get(url, params={"symbol": symbol}, timeout=10).json()
        return float(r["data"]["price"])
    except:
        return None

# ---------------- ORDERBUCH: BID/ASK + MIDPRICE ----------------

def get_bid_ask(symbol):
    """
    Holt Bid/Ask aus dem Orderbuch. Wird für präzisere Trigger genutzt.
    """
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/depth"
        r = requests.get(url, params={"symbol": symbol, "limit": 1}, timeout=10).json()
        data = r.get("data") or {}
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        if not bids or not asks:
            return None, None
        bid = float(bids[0][0])
        ask = float(asks[0][0])
        return bid, ask
    except:
        return None, None

def get_midprice(symbol):
    """
    Midprice = (Bid + Ask) / 2
    Wird als Entry-Preis und für Logs verwendet.
    """
    bid, ask = get_bid_ask(symbol)
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    # Fallback auf /quote/price
    return get_price_bingx(symbol)

# ---------------- TICK-SIZE ----------------

def round_to_tick(price, tick):
    if tick is None or tick <= 0:
        return price
    return round(price / tick) * tick

def get_tick_size(symbol):
    """
    Holt Tick-Size für das Symbol aus /quote/contracts und cached sie.
    Falls nicht gefunden, default 0.1 und Log.
    """
    if symbol in TICK_SIZE_CACHE:
        return TICK_SIZE_CACHE[symbol]

    tick = 0.1  # Fallback
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/contracts"
        r = requests.get(url, params={"symbol": symbol}, timeout=10).json()
        data = r.get("data", [])

        # data kann Liste oder Dict sein – beides abfangen
        item = None
        if isinstance(data, list):
            for c in data:
                if str(c.get("symbol", "")).upper() == symbol.upper():
                    item = c
                    break
        elif isinstance(data, dict):
            item = data

        if item:
            # Häufige Feldnamen: "tickSize" oder "priceStep" o.ä.
            if "tickSize" in item:
                tick = float(item["tickSize"])
            elif "priceStep" in item:
                tick = float(item["priceStep"])
    except Exception as e:
        print(f"[TICKSIZE WARN] {symbol}: Fallback Tick-Size verwendet ({tick}) – {e}")

    TICK_SIZE_CACHE[symbol] = tick
    return tick

# ---------------- OHLCV + RSI ----------------

def get_ohlcv(symbol, interval="1m", limit=100):
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        r = requests.get(url, params=params, timeout=10).json()
        return r.get("data", [])
    except:
        return []

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50

    gains = []
    losses = []

    for i in range(1, period + 1):
        diff = closes[-i] - closes[-i - 1]
        if diff > 0:
            gains.append(diff)
        else:
            losses.append(abs(diff))

    avg_gain = sum(gains) / period if gains else 0.00001
    avg_loss = sum(losses) / period if losses else 0.00001

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# ---------------- POSITION CHECK ----------------

def is_pos_open_bingx(symbol):
    try:
        ts = str(int(time.time() * 1000))
        params = {"symbol": symbol, "timestamp": ts}
        params["signature"] = sign_bingx(params)
        r = requests.get(
            f"{BINGX_BASE}/openApi/swap/v2/user/positions",
            params=params,
            headers={"X-BX-APIKEY": API_KEY},
            timeout=10
        ).json()
        return any(float(p.get("positionAmt", 0)) != 0 for p in r.get("data", []))
    except:
        return True

# ---------------- CLOSE ----------------

def close_bingx(symbol):
    ts = str(int(time.time() * 1000))
    params = {"symbol": symbol, "timestamp": ts}
    params["signature"] = sign_bingx(params)
    try:
        requests.post(
            f"{BINGX_BASE}/openApi/swap/v2/trade/closeAllPositions",
            data=params,
            headers={"X-BX-APIKEY": API_KEY},
            timeout=10
        )
    except Exception as e:
        print(f"[CLOSE ERROR] {symbol}: {e}")

# ---------------- SHORT ORDER ----------------

def execute_trade_bingx(symbol):

    # --- RSI CHECK ---
    ohlcv = get_ohlcv(symbol, RSI_TIMEFRAME, 100)
    if not ohlcv:
        print(f"[RSI ERROR] Keine Klines für {symbol}")
        return

    closes = [float(c["close"]) for c in ohlcv]
    rsi = calc_rsi(closes)

    if rsi < 80:
        print(f"[RSI BLOCK] {symbol} RSI={rsi:.1f} ({RSI_TIMEFRAME}) < 80 → Kein SHORT")
        return

    # --- Tick-Size holen ---
    tick_size = get_tick_size(symbol)

    # --- Preis (Midprice) laden ---
    price = get_midprice(symbol)
    if price is None:
        print(f"[PRICE ERROR] Kein Preis für {symbol}")
        return

    print(f"[ORDER] SHORT {symbol} | Entry≈{price:.5f} | RSI={rsi:.1f} ({RSI_TIMEFRAME}) | Tick={tick_size}")

    # --- Einstellungen ---
    trade_size_usdt = 10
    leverage = 10

    tp_percent = 0.9   # 0.9% Gewinn
    sl_percent = 0.8   # 0.8% Verlust
    be_percent = 0.4   # BE-Aktivierung bei 0.4% im Profit

    qty = round(trade_size_usdt / price, 6)

    params = {
        "leverage": str(leverage),
        "positionSide": "SHORT",
        "quantity": str(qty),
        "side": "SELL",
        "symbol": symbol,
        "timestamp": str(int(time.time() * 1000)),
        "type": "MARKET"
    }
    params["signature"] = sign_bingx(params)

    try:
        requests.post(
            f"{BINGX_BASE}/openApi/swap/v2/trade/order",
            data=params,
            headers={"X-BX-APIKEY": API_KEY},
            timeout=10
        )
    except Exception as e:
        print(f"[ORDER ERROR] {symbol}: {e}")
        return

    # Berechnungen für Monitor (SHORT!)
    entry = price

    # Ungerundete Levels
    raw_tp = entry * (1 - tp_percent / 100)
    raw_sl = entry * (1 + sl_percent / 100)
    raw_be_trigger = entry * (1 - be_percent / 100)

    # Auf Tick-Size runden
    tp = round_to_tick(raw_tp, tick_size)
    sl = round_to_tick(raw_sl, tick_size)
    be_trigger_price = round_to_tick(raw_be_trigger, tick_size)

    # BE-Level: Entry minus 0.05% (um Fees abzudecken), auch runden
    raw_be_level = entry * (1 - 0.05 / 100)
    be_level = round_to_tick(raw_be_level, tick_size)

    threading.Thread(
        target=monitor_position,
        args=(symbol, entry, tp, sl, be_trigger_price, be_level, tick_size)
    ).start()

# ---------------- MONITOR ----------------

def monitor_position(symbol, entry, tp, sl, be_trigger_price, be_level, tick_size):
    key = f"BINGX_{symbol}"
    active_monitors[key] = True

    print(
        f"[MONITOR] {symbol} SHORT | Entry≈{entry:.5f} | TP={tp:.5f} | SL={sl:.5f} | "
        f"BE-Trigger={be_trigger_price:.5f} | BE-Level={be_level:.5f} | Tick={tick_size}"
    )

    be_set = False

    try:
        while True:
            bid, ask = get_bid_ask(symbol)
            if bid is None or ask is None:
                time.sleep(0.25)
                continue

            # Midprice nur für Logs (nicht für Trigger)
            mid = (bid + ask) / 2.0

            # Break-Even Logik (SHORT: Profit, wenn Kurs fällt → Bid <= BE-Trigger)
            if not be_set and bid <= be_trigger_price:
                sl = be_level
                be_set = True
                print(
                    f"[BE-AKTIVIERT] {symbol}: SL auf BE-Level verschoben: {sl:.5f} "
                    f"(Bid={bid:.5f}, Ask={ask:.5f}, Mid={mid:.5f})"
                )

            # Exit-Bedingungen (SHORT):
            # TP: Bid <= TP (wir kaufen zum Ask zurück, aber Profit erkennbar am Bid)
            # SL/BE: Ask >= SL (wir kaufen zum Ask zurück, Verlust/BreakEven über Ask)
            if bid <= tp:
                reason = "TP"
                print(
                    f"[EXIT] {symbol} @ Mid≈{mid:.5f} (Bid={bid:.5f}, Ask={ask:.5f}) → Grund: {reason}"
                )
                close_bingx(symbol)
                break

            if ask >= sl:
                reason = "BE" if be_set else "SL"
                print(
                    f"[EXIT] {symbol} @ Mid≈{mid:.5f} (Bid={bid:.5f}, Ask={ask:.5f}) → Grund: {reason}"
                )
                close_bingx(symbol)
                break

            # Schnelleres Polling für präzisere Trigger
            time.sleep(0.25)

    finally:
        active_monitors[key] = False
        print(f"[MONITOR ENDE] {symbol}")

# ---------------- WEBHOOK ----------------

@app.route("/testorder", methods=["GET", "POST"])
def handle_alert():

    if request.method == "GET":
        return jsonify({"status": "ok"}), 200

    data = request.get_json(silent=True)

    if not data:
        return jsonify({"status": "ok"}), 200

    currency = str(data.get("currency", "")).upper()

    if not currency:
        return jsonify({"status": "ignored"}), 200

    symbol = f"{currency}-USDT"
    print(f"[SIGNAL] {symbol}")

    if is_pos_open_bingx(symbol) or active_monitors.get(f"BINGX_{symbol}"):
        return jsonify({"status": "already_active"}), 200

    threading.Thread(target=execute_trade_bingx, args=(symbol,)).start()

    return jsonify({"status": "short_started", "symbol": symbol}), 200

# ---------------- ANTI-SLEEP ----------------

def keep_alive():
    while True:
        try:
            requests.get("https://flask-webhook-bot-1.onrender.com/testorder", timeout=5)
        except:
            pass
        time.sleep(300)

threading.Thread(target=keep_alive, daemon=True).start()

# ---------------- START ----------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
