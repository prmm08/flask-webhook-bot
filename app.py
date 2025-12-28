# -------- V 5.2 LONG + DCA: BINGX FUTURES --------

import time, hmac, hashlib, requests, os, urllib.parse, threading, logging
from flask import Flask, request, jsonify

# --- API Konfiguration ---
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"
APP_URL = os.getenv("APP_URL", "http://localhost:5000")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)

# --- Strategie Settings ---
LEVERAGE = 10
TRADE_SIZE = 10
TP_PERCENT = 20          # Beispiel: +20%
SL_PERCENT_HARD = 5      # Beispiel: -5%

# --- DCA Settings ---
DCA_STEP_PERCENT = 5.0   # -5% vom letzten DCA-Preis → neuer LONG
DCA_MAX_COUNT = 3        # max. 3 Nachkäufe

# DCA-State pro Symbol
dca_states = {}

# ---------------- SIGNING & HELPERS ----------------

def sign_bingx(params):
    query_string = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(API_SECRET.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()

def get_price_bingx(symbol):
    try:
        url = f"{BINGX_BASE}/openApi/swap/v2/quote/price"
        r = requests.get(url, params={"symbol": symbol}, timeout=10).json()
        return float(r["data"]["price"])
    except:
        return None

# ---------------- POSITION ACTIONS ----------------

def close_position_market(symbol):
    ts = str(int(time.time() * 1000))
    params = {
        "symbol": symbol, "side": "SELL", "positionSide": "LONG",
        "type": "MARKET", "closePosition": "true", "timestamp": ts
    }
    requests.post(
        f"{BINGX_BASE}/openApi/swap/v2/trade/order?"
        f"{urllib.parse.urlencode(sorted(params.items()))}"
        f"&signature={sign_bingx(params)}",
        headers={"X-BX-APIKEY": API_KEY}
    )
    logging.info(f"[CLOSE] {symbol} LONG Position geschlossen.")

    if symbol in dca_states:
        del dca_states[symbol]

def place_sl_once(symbol, qty, sl_price):
    """
    Fixer SL, wird nur einmal gesetzt.
    """
    def place(price):
        ts = str(int(time.time() * 1000))
        params = {
            "symbol": symbol, "side": "SELL", "positionSide": "LONG",
            "type": "STOP_MARKET", "quantity": str(qty),
            "stopPrice": "{:.6f}".format(price),
            "workingType": "MARK_PRICE",
            "closePosition": "true",
            "timestamp": ts
        }
        return requests.post(
            f"{BINGX_BASE}/openApi/swap/v2/trade/order?"
            f"{urllib.parse.urlencode(sorted(params.items()))}"
            f"&signature={sign_bingx(params)}",
            headers={"X-BX-APIKEY": API_KEY}
        ).json()

    for i in range(5):
        r = place(sl_price)
        if r.get("code") == 0:
            logging.info(f"[SL] Fixer SL gesetzt bei {sl_price:.6f}")
            return
        logging.warning(f"[SL] Retry {i+1}/5: {r.get('msg')}")
        time.sleep(2)

def set_tp_dynamic(symbol, qty, avg_entry):
    """
    TP wird nach jedem DCA neu gesetzt.
    """
    tp_price = avg_entry * (1 + TP_PERCENT / 100)

    def place(price):
        ts = str(int(time.time() * 1000))
        params = {
            "symbol": symbol, "side": "SELL", "positionSide": "LONG",
            "type": "TAKE_PROFIT_MARKET", "quantity": str(qty),
            "stopPrice": "{:.6f}".format(price),
            "workingType": "MARK_PRICE",
            "closePosition": "true",
            "timestamp": ts
        }
        return requests.post(
            f"{BINGX_BASE}/openApi/swap/v2/trade/order?"
            f"{urllib.parse.urlencode(sorted(params.items()))}"
            f"&signature={sign_bingx(params)}",
            headers={"X-BX-APIKEY": API_KEY}
        ).json()

    for i in range(5):
        r = place(tp_price)
        if r.get("code") == 0:
            logging.info(f"[TP] TP gesetzt bei {tp_price:.6f}")
            return
        logging.warning(f"[TP] Retry {i+1}/5: {r.get('msg')}")
        time.sleep(2)

# ---------------- DCA MONITOR ----------------

def monitor_dca(symbol):
    logging.info(f"[DCA] Monitor gestartet für {symbol}")

    while True:
        try:
            state = dca_states.get(symbol)
            if not state or not state["active"]:
                break

            current_price = get_price_bingx(symbol)
            if not current_price:
                time.sleep(5)
                continue

            initial_entry = state["initial_entry"]
            last_dca_price = state["last_dca_price"]
            total_qty = state["total_qty"]
            base_qty = state["base_qty"]
            dca_count = state["dca_count"]
            sl_price = state["sl_price"]

            # --- HARTE NOTBREMSE ---
            if current_price <= sl_price:
                logging.warning(f"[STOP] {symbol} Preis unter SL. Schließe Position.")
                close_position_market(symbol)
                break

            # --- DCA TRIGGER ---
            if dca_count < DCA_MAX_COUNT and current_price <= last_dca_price * 0.95:
                logging.info(f"[DCA] Trigger für {symbol}: -5% erreicht. DCA #{dca_count+1}")

                ts = str(int(time.time() * 1000))
                params = {
                    "symbol": symbol, "side": "BUY", "positionSide": "LONG",
                    "type": "MARKET", "quantity": str(base_qty),
                    "leverage": str(LEVERAGE), "timestamp": ts
                }
                r = requests.post(
                    f"{BINGX_BASE}/openApi/swap/v2/trade/order?"
                    f"{urllib.parse.urlencode(sorted(params.items()))}"
                    f"&signature={sign_bingx(params)}",
                    headers={"X-BX-APIKEY": API_KEY}
                ).json()

                if r.get("code") == 0:
                    new_total = total_qty + base_qty
                    new_avg = (state["avg_entry"] * total_qty + current_price * base_qty) / new_total

                    state["avg_entry"] = new_avg
                    state["last_dca_price"] = current_price
                    state["total_qty"] = new_total
                    state["dca_count"] += 1
                    dca_states[symbol] = state

                    logging.info(f"[DCA] Neuer avg={new_avg:.6f}, qty={new_total}")

                    time.sleep(2)
                    set_tp_dynamic(symbol, new_total, new_avg)

            time.sleep(5)

        except Exception as e:
            logging.error(f"[DCA] Fehler: {e}")
            time.sleep(5)

# ---------------- EXECUTION LOGIC ----------------

def execute_trade_bingx(symbol):
    current_price = get_price_bingx(symbol)
    if not current_price:
        return

    qty = round(TRADE_SIZE / current_price, 6)
    ts = str(int(time.time() * 1000))

    entry_params = {
        "symbol": symbol, "side": "BUY", "positionSide": "LONG",
        "type": "MARKET", "quantity": str(qty),
        "leverage": str(LEVERAGE), "timestamp": ts
    }

    res = requests.post(
        f"{BINGX_BASE}/openApi/swap/v2/trade/order?"
        f"{urllib.parse.urlencode(sorted(entry_params.items()))}"
        f"&signature={sign_bingx(entry_params)}",
        headers={"X-BX-APIKEY": API_KEY}
    ).json()

    if res.get("code") != 0:
        logging.error(f"[ERROR] Entry Error: {res.get('msg')}")
        return

    logging.info(f"[ORDER] LONG Entry {symbol} @ {current_price}")

    # -------------------------
    # ✔️ KORRIGIERTER SL-BLOCK
    # -------------------------
    sl_price = initial_entry * (1 - SL_PERCENT_HARD / 100)  # z.B. -5%
    # -------------------------

    dca_states[symbol] = {
        "initial_entry": initial_entry,
        "avg_entry": initial_entry,
        "last_dca_price": initial_entry,
        "total_qty": qty,
        "base_qty": qty,
        "dca_count": 0,
        "sl_price": sl_price,
        "active": True
    }

    time.sleep(2)
    place_sl_once(symbol, qty, sl_price)

    time.sleep(2)
    set_tp_dynamic(symbol, qty, initial_entry)

    threading.Thread(target=monitor_dca, args=(symbol,), daemon=True).start()

# ---------------- WEBHOOK ----------------

@app.route("/testorder", methods=["POST", "GET"])
def handle_alert():
    if request.method == "GET":
        return jsonify({"status": "active"}), 200

    data = request.get_json(silent=True) or {}
    currency = str(data.get("currency", "")).upper()

    if not currency:
        return jsonify({"status": "no_currency"}), 200

    symbol = f"{currency}-USDT"
    threading.Thread(target=execute_trade_bingx, args=(symbol,)).start()

    return jsonify({"status": "processing", "symbol": symbol}), 200

# ---------------- MAIN ----------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
