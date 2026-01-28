#----------------- Working Skript WATCHER TP, SL, DCA mit BE, Telegram und Max Open Positions -------------------#

import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
import time
import json

from flask import Flask, request, jsonify
import logging

# --- CONFIG / ENV ---
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# --- DEFAULT SETTINGS ---
LEVERAGE = 20
TRADE_SIZE = 200
TP_PERCENT = 0.5
SL_PERCENT = 40

# --- DCA SETTINGS ---
DCA_INTERVAL = 5                # Sekunden zwischen DCA-Checks
DCA_COUNT = 4
DCA_DEVIATION_PERCENT = 5       # Prozent Abweichung vom entry_static für DCA-Trigger
DCA_VOLUME_MULTIPLIER = 2

# --- RISK / LIMITS ---
MAX_OPEN_POSITIONS = 20         # Maximale Anzahl gleichzeitig offener Positionen beim Exchange

active_dca = {}
dca_lock = threading.Lock()
last_dca_heartbeat = time.time()

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
app = Flask(__name__)


# --- TELEGRAM HELPERS ---
def send_telegram(message):
    """
    Sendet eine Textnachricht an den konfigurierten Telegram-Chat.
    Wenn TELEGRAM_BOT_TOKEN oder TELEGRAM_CHAT_ID nicht gesetzt sind, passiert nichts.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }
        r = requests.post(url, data=payload, timeout=5)
        r.raise_for_status()
        return True
    except Exception as e:
        print("[TELEGRAM ERROR]", e)
        return False


# --- SIGNATURE ---
def sign_bingx(params):
    if not params:
        query_string = ""
    else:
        items = sorted((k, "" if v is None else str(v)) for k, v in params.items())
        query_string = urllib.parse.urlencode(items)
    return hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()


# --- API REQUEST ---
def api_request(method, endpoint, params=None):
    url = f"{BINGX_BASE}{endpoint}"
    headers = {"X-BX-APIKEY": API_KEY}
    params = {} if params is None else dict(params)
    timeout = (5, 10)

    if method == "GET":
        try:
            params_for_sign = dict(params)
            signature = sign_bingx(params_for_sign)
            params_for_sign["signature"] = signature
            query = urllib.parse.urlencode(params_for_sign)
            response = requests.get(f"{url}?{query}", headers=headers, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print("[API ERROR GET]", e)
            return None

    if method == "POST":
        try:
            params_for_sign = dict(params)
            if "timestamp" not in params_for_sign:
                params_for_sign["timestamp"] = str(int(time.time() * 1000))
            query = urllib.parse.urlencode(sorted((k, str(v)) for k, v in params_for_sign.items()))
            signature = sign_bingx(params_for_sign)
            response = requests.post(f"{url}?{query}&signature={signature}", headers=headers, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print("[API ERROR POST]", e)
            return None


# --- HELPERS ---
def get_price(symbol):
    r = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    try:
        return float(r["data"]["price"])
    except:
        return None


def get_positions():
    ts = str(int(time.time() * 1000))
    r = api_request("GET", "/openApi/swap/v2/user/positions", {"timestamp": ts})
    return r.get("data", []) if r else []


def symbol_exists(symbol):
    r = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    return r and "data" in r and "price" in r["data"]


def set_leverage_for_symbol(symbol, leverage, position_side=None, side=None):
    ts = str(int(time.time() * 1000))
    params = {"symbol": symbol, "leverage": str(leverage), "timestamp": ts}
    if position_side:
        params["positionSide"] = position_side
    if side:
        params["side"] = side
    r = api_request("POST", "/openApi/swap/v2/trade/leverage", params)
    return bool(r)


# --- TP/SL ---
def reset_tp_sl(symbol, position_side=None):
    ts = str(int(time.time() * 1000))
    r = api_request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol, "timestamp": ts})
    orders = r.get("data", {}).get("orders", []) if r else []

    for order in orders:
        pos_side = order.get("positionSide") or order.get("position")
        if position_side and pos_side != position_side:
            continue
        oid = order.get("orderId")
        if not oid:
            continue
        api_request("POST", "/openApi/swap/v2/trade/cancelOrder",
                    {"orderId": oid, "symbol": symbol, "timestamp": str(int(time.time() * 1000))})


def set_tp_sl(symbol, desired_side=None, tp_percent=TP_PERCENT, sl_percent=SL_PERCENT, tp_price=None):
    """
    Setzt TP und SL für eine Position.
    - Wenn tp_price gesetzt ist, wird TP exakt auf diesen Preis gesetzt (BE-Logik).
    - Ansonsten wird TP aus tp_percent relativ zum Entry berechnet.
    """
    pos = None
    for _ in range(8):
        positions = get_positions()
        pos = next((p for p in positions
                    if p["symbol"] == symbol
                    and float(p.get("positionAmt", 0)) != 0
                    and (desired_side is None or p.get("positionSide") == desired_side)), None)
        if pos:
            break
        time.sleep(1)

    if not pos:
        print("[ERROR] Position nicht gefunden für TP/SL")
        return

    side = pos["positionSide"]
    entry = float(pos["avgPrice"])

    # avgPrice-Update abwarten
    for _ in range(10):
        time.sleep(0.8)
        new_pos = next((p for p in get_positions()
                        if p["symbol"] == symbol and p.get("positionSide") == side), None)
        if new_pos and abs(float(new_pos["avgPrice"]) - entry) > 0.0001:
            entry = float(new_pos["avgPrice"])
            break

    # Wenn tp_price übergeben wurde, verwende diesen als TP (BE)
    if tp_price is not None:
        tp = float(tp_price)
    else:
        tp = entry * (1 + tp_percent / 100) if side == "LONG" else entry * (1 - tp_percent / 100)

    sl = entry * (1 - sl_percent / 100) if side == "LONG" else entry * (1 + sl_percent / 100)

    reset_tp_sl(symbol, side)

    def place(price, otype):
        api_request("POST", "/openApi/swap/v2/trade/order", {
            "symbol": symbol,
            "side": "SELL" if side == "LONG" else "BUY",
            "positionSide": side,
            "type": otype,
            "stopPrice": f"{price:.6f}",
            "workingType": "MARK_PRICE",
            "closePosition": "true",
            "timestamp": str(int(time.time() * 1000))
        })

    place(tp, "TAKE_PROFIT_MARKET")
    place(sl, "STOP_MARKET")


# ============================================================
#   DCA ENGINE — STABILE VERSION MIT BE-LOGIK UND TELEGRAM
# ============================================================

def update_entry(symbol, side):
    positions = get_positions()
    pos = next((p for p in positions
                if p["symbol"] == symbol and p["positionSide"] == side), None)
    if pos:
        return float(pos["avgPrice"])
    return None


def calculate_dca_qty(base_trade_size, executed, current_price):
    multiplier = DCA_VOLUME_MULTIPLIER ** (executed + 1)
    return round((base_trade_size * multiplier) / current_price, 6)


def should_trigger_dca(side, current, entry_static, deviation_percent):
    if side == "LONG":
        return current <= entry_static * (1 - deviation_percent / 100)
    else:
        return current >= entry_static * (1 + deviation_percent / 100)


def monitor_dca():
    global last_dca_heartbeat

    while True:
        last_dca_heartbeat = time.time()

        try:
            positions = get_positions()

            for pos in positions:
                symbol = pos["symbol"]
                side = pos["positionSide"]
                amt = float(pos["positionAmt"])
                if amt == 0:
                    continue

                current_price = get_price(symbol)
                if not current_price:
                    continue

                with dca_lock:
                    if symbol not in active_dca:
                        base_value = abs(amt) * float(pos["avgPrice"])
                        active_dca[symbol] = {
                            "side": side,
                            "entry_static": float(pos["avgPrice"]),
                            "entry_dynamic": float(pos["avgPrice"]),
                            "executed": 0,
                            "base_trade_size": base_value,
                            "tp_percent": TP_PERCENT,
                            "sl_percent": SL_PERCENT
                        }

                    d = active_dca[symbol]

                if d["executed"] >= DCA_COUNT:
                    continue

                if not should_trigger_dca(side, current_price, d["entry_static"], DCA_DEVIATION_PERCENT):
                    continue

                qty = calculate_dca_qty(
                    d["base_trade_size"],
                    d["executed"],
                    current_price
                )

                # Platzieren der Market-Order
                resp = api_request("POST", "/openApi/swap/v2/trade/order", {
                    "symbol": symbol,
                    "side": "BUY" if side == "LONG" else "SELL",
                    "positionSide": side,
                    "type": "MARKET",
                    "quantity": str(qty),
                    "timestamp": str(int(time.time() * 1000))
                })

                with dca_lock:
                    d["executed"] += 1
                    new_entry = update_entry(symbol, side)
                    if new_entry:
                        d["entry_dynamic"] = new_entry

                    if d["executed"] >= 1:
                        tp_price = d["entry_dynamic"]
                    else:
                        tp_price = None

                # TP/SL neu setzen
                reset_tp_sl(symbol, side)
                if tp_price is not None:
                    print(f"[DCA] {symbol} DCA#{d['executed']} executed. Set BE TP at {tp_price:.6f}")
                    set_tp_sl(symbol, side, tp_percent=d["tp_percent"], sl_percent=d["sl_percent"], tp_price=tp_price)
                else:
                    set_tp_sl(symbol, side, tp_percent=d["tp_percent"], sl_percent=d["sl_percent"])

                # Telegram Nachricht: nur wenn Order-Platzierung versucht wurde
                try:
                    if resp is None:
                        msg = (f"⚠️ DCA Fehler für {symbol}\n"
                               f"DCA#{d['executed']} qty={qty} price={current_price:.6f}\n"
                               f"TP(BE)={'n/a' if tp_price is None else f'{tp_price:.6f}'}\n"
                               f"API response: None or error")
                    else:
                        msg = (f"✅ DCA ausgeführt für {symbol}\n"
                               f"DCA#{d['executed']} qty={qty} price={current_price:.6f}\n"
                               f"TP(BE)={'n/a' if tp_price is None else f'{tp_price:.6f}'}")
                    send_telegram(msg)
                except Exception as e:
                    print("[TELEGRAM SEND ERROR]", e)

        except Exception as e:
            print("[DCA ERROR]", e)

        time.sleep(DCA_INTERVAL)


# ============================================================
#   TP/SL WATCHER — setzt fehlende TP/SL neu
# ============================================================

def tp_sl_watcher():
    while True:
        try:
            positions = get_positions()

            print("[TP/SL WATCHER] Prüfe Positionen...")
            print(f"[WATCHER THREAD] ID={threading.get_ident()}")
            print("[TP/SL WATCHER]", time.strftime("%H:%M:%S"))

            for pos in positions:
                symbol = pos["symbol"]
                side = pos["positionSide"]
                amt = float(pos["positionAmt"])

                if amt == 0:
                    continue

                ts = str(int(time.time() * 1000))
                r = api_request("GET", "/openApi/swap/v2/trade/openOrders",
                                {"symbol": symbol, "timestamp": ts})
                orders = r.get("data", {}).get("orders", []) if r else []

                has_tp = any(o.get("type") == "TAKE_PROFIT_MARKET" and o.get("positionSide") == side for o in orders)
                has_sl = any(o.get("type") == "STOP_MARKET" and o.get("positionSide") == side for o in orders)

                print(f"[TP/SL WATCHER] {symbol} {side} TP={has_tp} SL={has_sl}")

                if not has_tp or not has_sl:
                    print(f"[TP/SL WATCHER] Setze TP/SL neu für {symbol} ({side})")
                    reset_tp_sl(symbol, side)
                    with dca_lock:
                        d = active_dca.get(symbol)
                        if d and d.get("executed", 0) >= 1:
                            set_tp_sl(symbol, side, tp_percent=d["tp_percent"], sl_percent=d["sl_percent"], tp_price=d["entry_dynamic"])
                        else:
                            set_tp_sl(symbol, side)

        except Exception as e:
            print("[TP/SL WATCHER ERROR]", e)

        time.sleep(10)


# ============================================================
#   execute_trade() MIT DCA-INTEGRATION UND MAX OPEN POSITIONS CHECK
# ============================================================

def execute_trade(symbol, direction, leverage, trade_size, tp_percent, sl_percent):
    # Prüfe Anzahl offener Positionen insgesamt
    positions = get_positions()
    if positions is None:
        print("[ERROR] Konnte Positionen nicht abrufen")
        return

    open_positions_count = sum(1 for p in positions if float(p.get("positionAmt", 0)) != 0)

    if open_positions_count >= MAX_OPEN_POSITIONS:
        msg = (f"[LIMIT] Offene Positionen {open_positions_count} >= MAX_OPEN_POSITIONS ({MAX_OPEN_POSITIONS}). "
               f"Neuer Trade für {symbol} wird nicht eröffnet.")
        print(msg)
        send_telegram(msg)
        return

    if not symbol_exists(symbol):
        print("[ERROR] Symbol existiert nicht:", symbol)
        return

    # Prüfe ob bereits eine Position für dieses Symbol+Side offen ist
    if any(p["symbol"] == symbol and p.get("positionSide") == direction and float(p["positionAmt"]) != 0 for p in positions):
        print("[SKIP] Position bereits offen:", symbol, direction)
        return

    price = get_price(symbol)
    if not price:
        print("[ERROR] Kein Preis")
        return

    if not set_leverage_for_symbol(symbol, leverage, direction, "BUY" if direction == "LONG" else "SELL"):
        print("[ERROR] Leverage Fehler")
        return

    qty = round(trade_size / price, 6)

    api_request("POST", "/openApi/swap/v2/trade/order", {
        "symbol": symbol,
        "side": "BUY" if direction == "LONG" else "SELL",
        "positionSide": direction,
        "type": "MARKET",
        "quantity": str(qty),
        "timestamp": str(int(time.time() * 1000))
    })

    with dca_lock:
        active_dca[symbol] = {
            "side": direction,
            "entry_static": price,
            "entry_dynamic": price,
            "executed": 0,
            "base_trade_size": trade_size,
            "tp_percent": tp_percent,
            "sl_percent": sl_percent
        }

    time.sleep(2)
    reset_tp_sl(symbol, direction)
    # Initial TP/SL per Prozent (erst nach DCA wird BE gesetzt)
    set_tp_sl(symbol, direction, tp_percent, sl_percent)


# ============================================================
#   FLASK + THREADS
# ============================================================

@app.route("/testorder", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}

    currency = str(data.get("currency", "")).upper()
    direction = str(data.get("direction", "")).upper()
    if not currency or direction not in ("LONG", "SHORT"):
        return jsonify({"status": "ignored"}), 200

    symbol = f"{currency}-USDT"
    leverage = int(data.get("leverage", LEVERAGE))
    trade_size = float(data.get("trade_size", TRADE_SIZE))
    tp_percent = float(data.get("tp_percent", TP_PERCENT))
    sl_percent = float(data.get("sl_percent", SL_PERCENT))

    threading.Thread(
        target=execute_trade,
        args=(symbol, direction, leverage, trade_size, tp_percent, sl_percent)
    ).start()

    return jsonify({"status": "processing"}), 200


@app.route("/ping")
def ping():
    return "pong", 200


def keep_alive():
    url = os.getenv("SELF_PING_URL")
    if not url:
        print("[KEEPALIVE] Kein SELF_PING_URL gesetzt")
        return
    while True:
        try:
            requests.get(url, timeout=5)
        except:
            pass
        time.sleep(240)


def start_dca_thread():
    while True:
        try:
            monitor_dca()
        except Exception as e:
            print("[DCA CRASH]", e)
            time.sleep(3)


def dca_watchdog():
    global last_dca_heartbeat
    while True:
        if time.time() - last_dca_heartbeat > 15:
            print("[WATCHDOG] DCA Thread hängt → Neustart")
            threading.Thread(target=start_dca_thread, daemon=True).start()
            last_dca_heartbeat = time.time()
        time.sleep(5)


if __name__ == "__main__":
    if not API_KEY or not API_SECRET:
        print("FEHLER: API Keys fehlen")
    else:
        threading.Thread(target=start_dca_thread, daemon=True).start()
        threading.Thread(target=dca_watchdog, daemon=True).start()
        threading.Thread(target=keep_alive, daemon=True).start()
        threading.Thread(target=tp_sl_watcher, daemon=True).start()

        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
