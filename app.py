import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
import time
import sys
from flask import Flask, request, jsonify

# --- LOGGING HELPER ---
def log_print(msg):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}")
    sys.stdout.flush()

# --- CONFIG ---
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BINGX_BASE = "https://open-api.bingx.com"

app = Flask(__name__)

# --- LOKALER SPEICHER ---
pos_tracker = {}
tracker_lock = threading.Lock()
is_synced = False

# --- STRATEGIE EINSTELLUNGEN ---
TP_MODE = "AVERAGE"        # "AVERAGE" or "FIRST"
USE_SL = False
SL_PERCENT = 2.5
TP_PERCENT = 0.5
BE_DCA_LEVEL = 2
BE_PROFIT_PERCENT = 0.1

DCA_COUNT = 6
DCA_DEVIATION_PERCENT = 1.0
DCA_VOLUME_MULTIPLIER = 1.25
TRADE_SIZE = 10
LEVERAGE = 20

# --- TP/Retry Einstellungen ---
MAX_RETRIES_TP = 3
RETRY_BACKOFF = 1.5  # Sekunden, multipliziert pro Versuch

# --- API CORE ---
def api_request(method, endpoint, params=None, max_retries=3):
    url = f"{BINGX_BASE}{endpoint}"
    headers = {"X-BX-APIKEY": API_KEY} if API_KEY else {}
    params = dict(params) if params else {}
    params["timestamp"] = str(int(time.time() * 1000))
    query_string = urllib.parse.urlencode(sorted(params.items()))
    signature = hmac.new((API_SECRET or "").encode(), query_string.encode(), hashlib.sha256).hexdigest()
    full_url = f"{url}?{query_string}&signature={signature}"
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.request(method, full_url, headers=headers, timeout=15)
            if resp.status_code == 429:
                backoff = 2 ** attempt
                log_print(f"[RATE_LIMIT] {endpoint} 429, backoff {backoff}s")
                time.sleep(backoff)
                continue
            try:
                return resp.json()
            except ValueError:
                log_print(f"[ERROR] Nicht-JSON Antwort von {endpoint}: {resp.text}")
                return None
        except requests.RequestException as e:
            log_print(f"[API Fehler] {endpoint} Versuch {attempt}: {e}")
            time.sleep(1)
    return None

# --- HILFSFUNKTIONEN ---
def safe_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default

def get_executed_price_from_position(symbol, side):
    r_pos = api_request("GET", "/openApi/swap/v2/user/positions", {"symbol": symbol})
    if not r_pos or not isinstance(r_pos.get("data"), list):
        return None
    pos = next((p for p in r_pos["data"] if p.get("positionSide") == side and safe_float(p.get("positionAmt", 0)) != 0), None)
    if pos:
        return safe_float(pos.get("avgPrice"))
    return None

def get_order_fill_price_from_resp(resp):
    if not resp:
        return None
    data = resp.get("data") or resp
    for key in ("avgPrice", "filledAvgPrice", "executedPrice", "avg_price"):
        if key in data:
            return safe_float(data.get(key))
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, dict):
                for key in ("avgPrice", "filledAvgPrice", "executedPrice"):
                    if key in v:
                        return safe_float(v.get(key))
    return None

def compute_target_price(last_price, side, deviation_pct):
    if side == "LONG":
        return last_price * (1 - deviation_pct / 100.0)
    else:
        return last_price * (1 + deviation_pct / 100.0)

def round_qty_to_step(qty, step=0.000001):
    if qty <= 0:
        return 0
    return round(qty - (qty % step), 6)

# --- ORDER / OPEN ORDERS HELPERS ---
def list_open_orders(symbol):
    resp = api_request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol, "limit": 200})
    if not resp:
        log_print(f"[OPEN_ORDERS] Keine Antwort für {symbol}")
        return []
    return resp.get("data") if isinstance(resp.get("data"), list) else []

def find_tp_orders(open_orders, positionSide):
    tps = []
    for o in open_orders:
        o_type = str(o.get("type", "")).upper()
        if "TAKE_PROFIT" in o_type and o.get("positionSide") == positionSide:
            tps.append(o)
    return tps

def cancel_order_by_id(symbol, order_id):
    resp = api_request("DELETE", "/openApi/swap/v2/trade/order", {"symbol": symbol, "orderId": str(order_id)})
    log_print(f"[CANCEL] cancel order {order_id} resp={resp}")
    return resp

def ensure_tp_exists(symbol, side, positionSide, expected_stop_price):
    open_orders = list_open_orders(symbol)
    tps = find_tp_orders(open_orders, positionSide)
    for o in tps:
        stop = safe_float(o.get("stopPrice") or o.get("price") or o.get("triggerPrice") or 0)
        if abs(stop - expected_stop_price) <= (expected_stop_price * 0.0005 + 1e-8):
            return True, o
    return False, None

# --- SYNC LOGIK ---
def sync_with_bingx():
    global is_synced
    r_pos = api_request("GET", "/openApi/swap/v2/user/positions")
    if r_pos and isinstance(r_pos.get("data"), list):
        active_api_keys = []
        with tracker_lock:
            for pos in r_pos["data"]:
                amt = safe_float(pos.get("positionAmt", 0))
                if amt == 0:
                    continue
                symbol, side = pos["symbol"], pos["positionSide"]
                key = f"{symbol}_{side}"
                active_api_keys.append(key)
                if key not in pos_tracker:
                    r_orders = api_request("GET", "/openApi/swap/v2/trade/allOrders", {"symbol": symbol, "limit": 50})
                    if r_orders and isinstance(r_orders.get("data"), list):
                        filled = [o for o in r_orders["data"] if o.get("status") == "FILLED" and o.get("positionSide") == side and o.get("type") == "MARKET"]
                        if filled:
                            first_price = safe_float(filled[-1].get("avgPrice") or filled[-1].get("price") or 0)
                            last_price = safe_float(filled[0].get("avgPrice") or filled[0].get("price") or 0)
                            pos_tracker[key] = {"level": len(filled), "last_price": last_price or safe_float(pos.get("avgPrice")), "first_price": first_price or safe_float(pos.get("avgPrice"))}
            for k in list(pos_tracker.keys()):
                if k not in active_api_keys:
                    log_print(f"[SYNC] Entferne {k} aus Tracker (nicht mehr aktiv)")
                    del pos_tracker[k]
    is_synced = True

def minute_sync_task():
    while True:
        time.sleep(60)
        try:
            sync_with_bingx()
        except Exception as e:
            log_print(f"[ERROR] minute_sync_task: {e}")

# --- ROBUSTES TP / SL SETZEN (mit Verifikation, Debug und Retry) ---
def debug_dump_open_orders(symbol):
    resp = api_request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol, "limit": 200})
    log_print(f"[DEBUG_OPEN_ORDERS] symbol={symbol} resp={resp}")
    return resp.get("data") if resp and isinstance(resp.get("data"), list) else []

def cancel_order_by_id_verbose(symbol, order_id):
    resp = api_request("DELETE", "/openApi/swap/v2/trade/order", {"symbol": symbol, "orderId": str(order_id)})
    log_print(f"[CANCEL_VERBOSE] symbol={symbol} orderId={order_id} resp={resp}")
    return resp

def set_tp_sl(symbol, side, current_level, first_price=None):
    # Hole Position
    r_pos = api_request("GET", "/openApi/swap/v2/user/positions", {"symbol": symbol})
    log_print(f"[TP/SL] user/positions resp={r_pos}")
    if not r_pos or not isinstance(r_pos.get("data"), list):
        log_print(f"[TP/SL] Keine Positionsdaten für {symbol}")
        return
    pos = next((p for p in r_pos["data"] if p.get("positionSide") == side and safe_float(p.get("positionAmt", 0)) != 0), None)
    if not pos:
        log_print(f"[TP/SL] Keine offene Position gefunden für {symbol} {side}")
        return

    avg_price = safe_float(pos.get("avgPrice"))
    target_tp_pct = BE_PROFIT_PERCENT if current_level >= BE_DCA_LEVEL else TP_PERCENT
    base_price = avg_price if (TP_MODE == "AVERAGE" or current_level >= BE_DCA_LEVEL) else (first_price or avg_price)
    tp_price = base_price * (1 + target_tp_pct / 100) if side == "LONG" else base_price * (1 - target_tp_pct / 100)
    log_print(f"[TP/SL] current_level={current_level} target_tp_pct={target_tp_pct} base_price={base_price:.8f} tp_price={tp_price:.8f}")

    # 1) Debug dump offene Orders vor Änderung
    debug_dump_open_orders(symbol)

    # 2) Lösche vorhandene TP Orders gezielt per orderId
    open_orders = debug_dump_open_orders(symbol) or []
    for o in open_orders:
        o_type = str(o.get("type", "")).upper()
        if "TAKE_PROFIT" in o_type and o.get("positionSide") == side:
            oid = o.get("orderId") or o.get("order_id") or o.get("id")
            if oid:
                cancel_order_by_id_verbose(symbol, oid)
                time.sleep(0.25)

    # 3) Versuche TP mit verschiedenen workingType falls nötig
    working_types = ["MARK_PRICE", "CONTRACT_PRICE", "LAST_PRICE"]
    for wt in working_types:
        tp_params = {
            "symbol": symbol,
            "side": "SELL" if side == "LONG" else "BUY",
            "positionSide": side,
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": f"{tp_price:.8f}",
            "workingType": wt,
            "closePosition": "true"
        }
        resp = api_request("POST", "/openApi/swap/v2/trade/order", tp_params)
        log_print(f"[TP_POST] attempt workingType={wt} params={tp_params} resp={resp}")

        # Wenn API orderId zurückgibt, nutze sie zur Verifikation
        order_id = None
        if resp and isinstance(resp, dict):
            order_id = resp.get("data", {}).get("orderId") or resp.get("orderId") or resp.get("data", {}).get("order_id") or resp.get("data", {}).get("id")
            log_print(f"[TP_POST] returned order_id={order_id}")

        # kurze Wartezeit, dann offene Orders prüfen
        time.sleep(0.8)
        open_after = debug_dump_open_orders(symbol) or []
        found = False
        for o in open_after:
            stop = safe_float(o.get("stopPrice") or o.get("price") or o.get("triggerPrice") or 0)
            oid2 = o.get("orderId") or o.get("order_id") or o.get("id")
            if abs(stop - tp_price) <= (tp_price * 0.0005 + 1e-10):
                log_print(f"[TP_VERIFY] Found TP order matching stopPrice={stop} oid={oid2} workingType={o.get('workingType')}")
                found = True
                break
            if order_id and oid2 and str(order_id) == str(oid2):
                log_print(f"[TP_VERIFY] Found TP by orderId match oid={oid2}")
                found = True
                break

        if found:
            log_print(f"[TP] TP erfolgreich gesetzt für {symbol} mit workingType={wt} stopPrice={tp_price:.8f}")
            break
        else:
            log_print(f"[TP] TP nicht gefunden nach POST mit workingType={wt}, versuche nächsten workingType")
            time.sleep(1.0)

    # 4) Falls SL gewünscht, analog setzen und verifizieren
    if USE_SL:
        sl_price = avg_price * (1 - SL_PERCENT / 100) if side == "LONG" else avg_price * (1 + SL_PERCENT / 100)
        sl_params = {
            "symbol": symbol,
            "side": "SELL" if side == "LONG" else "BUY",
            "positionSide": side,
            "type": "STOP_MARKET",
            "stopPrice": f"{sl_price:.8f}",
            "workingType": "MARK_PRICE",
            "closePosition": "true"
        }
        resp_sl = api_request("POST", "/openApi/swap/v2/trade/order", sl_params)
        log_print(f"[SL_POST] params={sl_params} resp={resp_sl}")
        time.sleep(0.6)
        debug_dump_open_orders(symbol)

# --- MONITOR (DCA Check) ---
def monitor_dca():
    while not is_synced:
        time.sleep(2)
    while True:
        try:
            with tracker_lock:
                keys = list(pos_tracker.keys())
            for key in keys:
                try:
                    symbol, side = key.split("_")
                except ValueError:
                    continue
                with tracker_lock:
                    data = pos_tracker.get(key)
                if not data:
                    continue

                r_ticker = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
                if not r_ticker or "data" not in r_ticker:
                    time.sleep(0.5)
                    continue
                curr_price = safe_float(r_ticker["data"].get("price", 0))
                if curr_price == 0:
                    continue

                last_price = safe_float(data.get("last_price", curr_price))
                target_price = compute_target_price(last_price, side, DCA_DEVIATION_PERCENT)

                trigger = (side == "LONG" and curr_price <= target_price) or (side == "SHORT" and curr_price >= target_price)
                if not trigger:
                    continue

                if data.get("level", 0) >= DCA_COUNT:
                    log_print(f"[DCA] Max level erreicht für {key}")
                    continue

                raw_qty = (TRADE_SIZE * (DCA_VOLUME_MULTIPLIER ** data["level"])) / curr_price
                qty = round_qty_to_step(raw_qty, step=0.000001)
                if qty <= 0:
                    log_print(f"[DCA] Qty zu klein für {symbol}: {raw_qty}")
                    continue

                log_print(f"[DCA] Trigger für {symbol} {side} level={data['level']} curr={curr_price:.6f} target={target_price:.6f} qty={qty}")
                resp = api_request("POST", "/openApi/swap/v2/trade/order", {
                    "symbol": symbol,
                    "side": "BUY" if side == "LONG" else "SELL",
                    "positionSide": side,
                    "type": "MARKET",
                    "quantity": str(qty)
                })

                if not resp:
                    log_print(f"[ERROR] Keine Antwort beim Platzieren DCA für {symbol}")
                    continue

                success = (resp.get("code") == 0) or (resp.get("success") is True)
                if not success:
                    log_print(f"[ERROR] DCA Order abgelehnt für {symbol}: {resp}")
                    continue

                time.sleep(1)
                executed_price = get_executed_price_from_position(symbol, side) or get_order_fill_price_from_resp(resp) or curr_price

                with tracker_lock:
                    pos_tracker[key]["level"] = data["level"] + 1
                    pos_tracker[key]["last_price"] = executed_price
                    if "first_price" not in pos_tracker[key] or not pos_tracker[key]["first_price"]:
                        pos_tracker[key]["first_price"] = executed_price

                log_print(f"[DCA] Order gefüllt für {symbol} at {executed_price:.6f} new_level={pos_tracker[key]['level']}")
                time.sleep(2)
                set_tp_sl(symbol, side, pos_tracker[key]["level"], pos_tracker[key]["first_price"])
        except Exception as e:
            log_print(f"[EXCEPTION] monitor_dca: {e}")
        time.sleep(5)

# --- STRIKTER WEBHOOK FILTER ---
@app.route("/testorder", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    currency = str(data.get("currency", "")).upper()
    direction = str(data.get("direction", "")).upper()

    if currency and direction in ["LONG", "SHORT"]:
        symbol = f"{currency}-USDT"

        with tracker_lock:
            existing_positions = [k for k in pos_tracker.keys() if k.startswith(symbol)]

        if existing_positions:
            log_print(f"[BLOCK] Signal ignoriert für {symbol}. Position bereits im Tracker: {existing_positions}")
            return jsonify({"status": "ignored", "message": "Symbol already active in tracker"}), 200

        threading.Thread(target=execute_initial_trade, args=(symbol, direction), daemon=True).start()
        return jsonify({"status": "executing", "symbol": symbol}), 200

    return jsonify({"status": "error", "message": "invalid data"}), 400

def execute_initial_trade(symbol, direction):
    lev_resp = api_request("POST", "/openApi/swap/v2/trade/leverage", {"symbol": symbol, "leverage": str(LEVERAGE), "side": direction, "positionSide": direction})
    if not lev_resp or not ((lev_resp.get("code") == 0) or (lev_resp.get("success") is True)):
        log_print(f"[WARN] Leverage nicht gesetzt für {symbol}: {lev_resp}")

    r_ticker = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    price = safe_float(r_ticker["data"].get("price", 0)) if r_ticker and "data" in r_ticker else 0
    if price == 0:
        log_print(f"[ERROR] Kein Tickerpreis für {symbol}")
        return

    qty = round_qty_to_step(TRADE_SIZE / price, step=0.000001)
    if qty <= 0:
        log_print(f"[ERROR] Berechnete Qty ungültig: {qty}")
        return

    resp = api_request("POST", "/openApi/swap/v2/trade/order", {
        "symbol": symbol,
        "side": "BUY" if direction == "LONG" else "SELL",
        "positionSide": direction,
        "type": "MARKET",
        "quantity": str(qty)
    })

    if not resp:
        log_print(f"[ERROR] Keine Antwort auf Initial Order {symbol}")
        return

    success = (resp.get("code") == 0) or (resp.get("success") is True)
    if not success:
        log_print(f"[ERROR] Initial Order abgelehnt für {symbol}: {resp}")
        return

    time.sleep(1)
    executed_price = get_executed_price_from_position(symbol, direction) or get_order_fill_price_from_resp(resp) or price

    with tracker_lock:
        pos_tracker[f"{symbol}_{direction}"] = {"level": 1, "last_price": executed_price, "first_price": executed_price}
    log_print(f"[SUCCESS] Erste Order für {symbol} platziert at {executed_price:.6f}.")
    time.sleep(2)
    set_tp_sl(symbol, direction, 1, executed_price)

@app.route("/ping")
@app.route("/")
def health():
    return "BOT_V1.9.1_STRICT", 200

if __name__ == "__main__":
    log_print("Starting bot (LIVE ORDERS enabled)")
    sync_with_bingx()
    threading.Thread(target=monitor_dca, daemon=True).start()
    threading.Thread(target=minute_sync_task, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
