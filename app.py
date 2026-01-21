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

# --- API ---
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
app = Flask(__name__)

# --- DEFAULT SETTINGS ---
LEVERAGE = 20
TRADE_SIZE = 50
TP_PERCENT = 1
SL_PERCENT = 40

# --- DCA SETTINGS ---
DCA_INTERVAL = 5
DCA_COUNT = 7
DCA_DEVIATION_PERCENT = 5        # Trigger in Prozent (z. B. 5)
DCA_VOLUME_MULTIPLIER = 1.5
MIN_ORDER_INTERVAL = 4.5         # minimaler Abstand zwischen Orders pro Symbol (Sekunden)
HYSTERESIS = 0.002               # 0.2% zusätzlicher Preispuffer nach Ausführung
API_ORDER_POLL_INTERVAL = 0.5    # Intervall zum Polling des Orderstatus (Sekunden)
API_ORDER_POLL_TIMEOUT = 10      # Timeout für Polling (Sekunden)

# --- NEW: Steuerung ob SL automatisch gesetzt werden soll ---
AUTO_SET_SL = False  # False = Stop Loss wird nicht automatisch gesetzt; True = wie bisher

def dca_key(symbol, side):
    return f"{symbol}:{side}"

active_dca = {}
dca_lock = threading.Lock()
last_dca_heartbeat = time.monotonic()

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
            try:
                print("[API ERROR GET]", e, "response_text=", response.text)
            except:
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
            try:
                print("[API ERROR POST]", e, "response_text=", response.text)
            except:
                print("[API ERROR POST]", e)
            return None

# --- HELPERS ---
def get_price(symbol):
    r = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    try:
        return float(r["data"]["price"])
    except Exception:
        print(f"[PRICE ERROR] Konnte Preis für {symbol} nicht lesen, response={r}")
        return None

def get_positions():
    ts = str(int(time.time() * 1000))
    r = api_request("GET", "/openApi/swap/v2/user/positions", {"timestamp": ts})
    return r.get("data", []) if r else []

def symbol_exists(symbol):
    r = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    return bool(r and "data" in r and "price" in r["data"])

def set_leverage_for_symbol(symbol, leverage, position_side=None, side=None):
    ts = str(int(time.time() * 1000))
    params = {"symbol": symbol, "leverage": str(leverage), "timestamp": ts}
    if position_side:
        params["positionSide"] = position_side
    if side:
        params["side"] = side
    r = api_request("POST", "/openApi/swap/v2/trade/leverage", params)
    if not r:
        print(f"[LEVERAGE ERROR] set_leverage_for_symbol failed for {symbol} resp={r}")
    return bool(r)

# --- TP/SL helpers with SL control ---
def reset_tp_sl(symbol, position_side=None, cancel_sl=True):
    """
    Wenn cancel_sl False, werden nur TAKE_PROFIT_MARKET Orders gelöscht.
    Wenn cancel_sl True, werden TP und SL gelöscht (wie vorher).
    """
    ts = str(int(time.time() * 1000))
    r = api_request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol, "timestamp": ts})
    orders = r.get("data", {}).get("orders", []) if r else []

    for order in orders:
        pos_side = order.get("positionSide") or order.get("position")
        if position_side and pos_side != position_side:
            continue
        otype = order.get("type")
        oid = order.get("orderId")
        if not oid:
            continue
        # Wenn cancel_sl False, überspringe STOP_MARKET (SL)
        if not cancel_sl and otype == "STOP_MARKET":
            continue
        api_request("POST", "/openApi/swap/v2/trade/cancelOrder",
                    {"orderId": oid, "symbol": symbol, "timestamp": str(int(time.time() * 1000))})

def set_tp_sl(symbol, desired_side=None, tp_percent=TP_PERCENT, sl_percent=SL_PERCENT):
    """
    Setzt TP und SL wie vorher. Wird nur aufgerufen wenn AUTO_SET_SL True ist.
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

    tp = entry * (1 + tp_percent / 100) if side == "LONG" else entry * (1 - tp_percent / 100)
    sl = entry * (1 - sl_percent / 100) if side == "LONG" else entry * (1 + sl_percent / 100)

    reset_tp_sl(symbol, side, cancel_sl=True)

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

def set_tp_only(symbol, desired_side=None, tp_percent=TP_PERCENT):
    """
    Setzt nur TAKE_PROFIT_MARKET. Löscht vorher nur TP-Orders, nicht SL.
    Wird verwendet wenn AUTO_SET_SL == False.
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
        print("[ERROR] Position nicht gefunden für TP")
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

    tp = entry * (1 + tp_percent / 100) if side == "LONG" else entry * (1 - tp_percent / 100)

    # Lösche nur TP-Orders, nicht SL
    reset_tp_sl(symbol, side, cancel_sl=False)

    api_request("POST", "/openApi/swap/v2/trade/order", {
        "symbol": symbol,
        "side": "SELL" if side == "LONG" else "BUY",
        "positionSide": side,
        "type": "TAKE_PROFIT_MARKET",
        "stopPrice": f"{tp:.6f}",
        "workingType": "MARK_PRICE",
        "closePosition": "true",
        "timestamp": str(int(time.time() * 1000))
    })

# ============================================================
#   DCA ENGINE — STABILE VERSION (mit Robustheits-Fixes)
# ============================================================

def update_entry(symbol, side):
    positions = get_positions()
    pos = next((p for p in positions
                if p["symbol"] == symbol and p["positionSide"] == side), None)
    if pos:
        try:
            return float(pos["avgPrice"])
        except:
            return None
    return None

def calculate_dca_qty(base_trade_size, executed, current_price):
    multiplier = DCA_VOLUME_MULTIPLIER ** (executed + 1)
    qty = (base_trade_size * multiplier) / current_price if current_price and current_price > 0 else 0
    qty = round(qty, 6)
    return qty

def should_trigger_dca(side, current, entry_initial, deviation_percent):
    if side == "LONG":
        return current <= entry_initial * (1 - deviation_percent / 100)
    else:
        return current >= entry_initial * (1 + deviation_percent / 100)

def poll_order_filled(symbol, order_id, timeout=API_ORDER_POLL_TIMEOUT):
    """Pollt den Orderstatus bis FILLED oder Timeout. Gibt True bei Fill, False sonst."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        ts = str(int(time.time() * 1000))
        r = api_request("GET", "/openApi/swap/v2/trade/order", {"symbol": symbol, "orderId": order_id, "timestamp": ts})
        if r:
            data = r.get("data") or {}
            status = data.get("status") or data.get("orderStatus") or data.get("state")
            if status in ("FILLED", "filled", "FILLED_PARTIAL", "FILLED_PARTIALLY"):
                return True
            if status in ("CANCELED", "CANCELLED", "REJECTED", "FAILED"):
                return False
        time.sleep(API_ORDER_POLL_INTERVAL)
    return False

def monitor_dca():
    global last_dca_heartbeat

    while True:
        last_dca_heartbeat = time.monotonic()

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

                key = dca_key(symbol, side)

                # Ensure active_dca entry exists (unter Lock)
                with dca_lock:
                    if key not in active_dca:
                        base_value = abs(amt) * float(pos["avgPrice"])
                        active_dca[key] = {
                            "symbol": symbol,
                            "side": side,
                            "entry_initial": float(pos["avgPrice"]),  # unveränderliche Referenz
                            "entry_dynamic": float(pos["avgPrice"]),  # für Anzeige/TP-SL
                            "executed": 0,
                            "base_trade_size": base_value,
                            "tp_percent": TP_PERCENT,
                            "sl_percent": SL_PERCENT,
                            "last_order_ts": 0.0,
                            "placing": False,
                            "last_order_price": None,
                            "consecutive_failures": 0
                        }
                    d = active_dca[key]

                # Logging
                next_trigger_price = (d["entry_initial"] * (1 - DCA_DEVIATION_PERCENT / 100)) if side == "LONG" else (d["entry_initial"] * (1 + DCA_DEVIATION_PERCENT / 100))
                print(f"[DCA] {key} side={side} current={current_price} entry_initial={d['entry_initial']} next_trigger={next_trigger_price:.8f} executed={d['executed']} base_trade_size={d['base_trade_size']} last_order_ts={d['last_order_ts']}")

                # Quick checks unter Lock
                with dca_lock:
                    if d["executed"] >= DCA_COUNT:
                        continue
                    if d.get("placing"):
                        continue
                    # Zeitliche Sperre
                    if time.monotonic() - d.get("last_order_ts", 0.0) < MIN_ORDER_INTERVAL:
                        continue
                    # Hysterese: wenn bereits eine Order ausgeführt wurde, erwarte zusätzliche Bewegung
                    last_price = d.get("last_order_price")
                    if last_price is not None:
                        if side == "LONG":
                            if not (current_price <= d["entry_initial"] * (1 - DCA_DEVIATION_PERCENT / 100) - HYSTERESIS * d["entry_initial"]):
                                continue
                        else:
                            if not (current_price >= d["entry_initial"] * (1 + DCA_DEVIATION_PERCENT / 100) + HYSTERESIS * d["entry_initial"]):
                                continue

                    trigger = should_trigger_dca(side, current_price, d["entry_initial"], DCA_DEVIATION_PERCENT)
                    if not trigger:
                        continue
                    d["placing"] = True

                # Berechne qty außerhalb der Sperre
                qty = calculate_dca_qty(
                    d["base_trade_size"],
                    d["executed"],
                    current_price
                )

                if qty <= 0:
                    print(f"[DCA] Berechnete qty ist 0 für {symbol}, überspringe")
                    with dca_lock:
                        d["placing"] = False
                    continue

                print(f"[DCA] ({threading.get_ident()}) Platziere DCA-Order für {symbol} qty={qty} side={'BUY' if side == 'LONG' else 'SELL'} at {time.strftime('%H:%M:%S')}")

                # API-Aufruf (außerhalb Lock)
                resp = api_request("POST", "/openApi/swap/v2/trade/order", {
                    "symbol": symbol,
                    "side": "BUY" if side == "LONG" else "SELL",
                    "positionSide": side,
                    "type": "MARKET",
                    "quantity": str(qty),
                    "timestamp": str(int(time.time() * 1000))
                })

                print("[DCA] order response:", resp)

                # Nach API-Call: handle response, Polling nach Fill falls orderId vorhanden
                order_filled = False
                order_id = None
                if resp:
                    order_id = resp.get("data", {}).get("orderId") or resp.get("orderId") or resp.get("data", {}).get("order_id")
                    success_flag = resp.get("success") is True or resp.get("code") in (0, "0", None)
                    if success_flag and order_id:
                        order_filled = poll_order_filled(symbol, order_id)
                    elif success_flag and not order_id:
                        order_filled = True
                    else:
                        order_filled = False

                # Update shared state unter Lock
                with dca_lock:
                    if order_filled:
                        d["executed"] += 1
                        d["last_order_ts"] = time.monotonic()  # setze Sperre NACH bestätigter Ausführung
                        d["last_order_price"] = current_price
                        d["consecutive_failures"] = 0
                        print(f"[DCA] Order gefüllt für {symbol} executed={d['executed']}")
                    else:
                        d["consecutive_failures"] = d.get("consecutive_failures", 0) + 1
                        backoff_multiplier = min(4.0, 1.5 ** d["consecutive_failures"])
                        d["last_order_ts"] = time.monotonic() + (MIN_ORDER_INTERVAL * (backoff_multiplier - 1))
                        print(f"[DCA] Order nicht gefüllt für {symbol}, consecutive_failures={d['consecutive_failures']}, next_try_in={d['last_order_ts'] - time.monotonic():.1f}s")
                    d["placing"] = False

                # Kurze Wartezeit, damit avgPrice/Positionen sich aktualisieren können
                time.sleep(1.5)
                new_entry = update_entry(symbol, side)
                if new_entry:
                    with dca_lock:
                        d["entry_dynamic"] = new_entry
                        print(f"[DCA] entry_dynamic updated for {symbol} -> entry_dynamic={d['entry_dynamic']}")

                # --- Hier: setze TP und optional SL abhängig von AUTO_SET_SL ---
                if AUTO_SET_SL:
                    reset_tp_sl(symbol, side, cancel_sl=True)
                    set_tp_sl(symbol, side, d["tp_percent"], d["sl_percent"])
                else:
                    # Lösche nur TP und setze nur TP
                    reset_tp_sl(symbol, side, cancel_sl=False)
                    set_tp_only(symbol, side, d["tp_percent"])

                time.sleep(0.2)

        except Exception as e:
            print("[DCA ERROR]", e)

        time.sleep(DCA_INTERVAL)

# ============================================================
#   TP/SL WATCHER — setzt fehlende TP/SL neu (respektiert AUTO_SET_SL)
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

                if not has_tp:
                    # TP immer setzen (unabhängig von AUTO_SET_SL)
                    print(f"[TP/SL WATCHER] Setze TP neu für {symbol} ({side})")
                    reset_tp_sl(symbol, side, cancel_sl=False)
                    set_tp_only(symbol, side)

                if AUTO_SET_SL and not has_sl:
                    print(f"[TP/SL WATCHER] Setze SL neu für {symbol} ({side})")
                    # Wenn AUTO_SET_SL True, setze TP+SL komplett neu
                    reset_tp_sl(symbol, side, cancel_sl=True)
                    set_tp_sl(symbol, side)

        except Exception as e:
            print("[TP/SL WATCHER ERROR]", e)

        time.sleep(10)

# ============================================================
#   execute_trade() MIT DCA-INTEGRATION
# ============================================================

def execute_trade(symbol, direction, leverage, trade_size, tp_percent, sl_percent):
    if not symbol_exists(symbol):
        print("[ERROR] Symbol existiert nicht:", symbol)
        return

    positions = get_positions()
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
    if qty <= 0:
        print("[ERROR] Berechnete qty ist 0")
        return

    resp = api_request("POST", "/openApi/swap/v2/trade/order", {
        "symbol": symbol,
        "side": "BUY" if direction == "LONG" else "SELL",
        "positionSide": direction,
        "type": "MARKET",
        "quantity": str(qty),
        "timestamp": str(int(time.time() * 1000))
    })
    print("[EXECUTE TRADE] order response:", resp)

    # Falls OrderId vorhanden, pollen wir auf Fill; ansonsten behandeln wir success-Flag
    order_filled = False
    order_id = None
    if resp:
        order_id = resp.get("data", {}).get("orderId") or resp.get("orderId") or resp.get("data", {}).get("order_id")
        success_flag = resp.get("success") is True or resp.get("code") in (0, "0", None)
        if success_flag and order_id:
            order_filled = poll_order_filled(symbol, order_id)
        elif success_flag and not order_id:
            order_filled = True
        else:
            order_filled = False

    with dca_lock:
        key = dca_key(symbol, direction)
        active_dca[key] = {
            "symbol": symbol,
            "side": direction,
            "entry_initial": price,   # unveränderliche Referenz für DCA-Trigger
            "entry_dynamic": price,
            "executed": 1 if order_filled else 0,  # falls initiale Order gefüllt, zählen wir sie
            "base_trade_size": trade_size,
            "tp_percent": tp_percent,
            "sl_percent": sl_percent,
            "last_order_ts": time.monotonic() if order_filled else time.monotonic() + MIN_ORDER_INTERVAL,
            "placing": False,
            "last_order_price": price if order_filled else None,
            "consecutive_failures": 0 if order_filled else 1
        }

    time.sleep(2)
    # Setze TP und optional SL abhängig von AUTO_SET_SL
    if AUTO_SET_SL:
        reset_tp_sl(symbol, direction, cancel_sl=True)
        set_tp_sl(symbol, direction, tp_percent, sl_percent)
    else:
        reset_tp_sl(symbol, direction, cancel_sl=False)
        set_tp_only(symbol, direction, tp_percent)

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
    # Starte monitor_dca in einem benannten Daemon-Thread, falls noch nicht vorhanden
    for t in threading.enumerate():
        if t.name == "DCA-Monitor" and t.is_alive():
            return
    t = threading.Thread(target=monitor_dca, daemon=True, name="DCA-Monitor")
    t.start()

def dca_watchdog():
    global last_dca_heartbeat
    while True:
        if time.monotonic() - last_dca_heartbeat > (DCA_INTERVAL * 3):
            print("[WATCHDOG] DCA Thread hängt oder Heartbeat alt → Prüfe/Neustart")
            start_dca_thread()
            last_dca_heartbeat = time.monotonic()
        time.sleep(5)

if __name__ == "__main__":
    if not API_KEY or not API_SECRET:
        print("FEHLER: API Keys fehlen")
    else:
        start_dca_thread()
        threading.Thread(target=dca_watchdog, daemon=True).start()
        threading.Thread(target=keep_alive, daemon=True).start()
        threading.Thread(target=tp_sl_watcher, daemon=True).start()

        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
