import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
import time
import json
import logging
from math import floor
from flask import Flask, request, jsonify

# ============================================================
#   KONFIGURATION
# ============================================================
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

# Standard-Werte
LEVERAGE = 20
TRADE_SIZE = 100
TP_PERCENT = 1.0
SL_PERCENT = 40.0

# DCA Settings
DCA_DEVIATION_PERCENT = 5.0
DCA_VOLUME_MULTIPLIER = 2
DCA_MAX_STEPS = 5
DCA_TP_PERCENT = 1.2
DCA_SL_PERCENT = 10.0
DCA_SAVE_FILE = "active_dca.json"

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
app = Flask(__name__)

active_dca = {}
dca_lock = threading.Lock()
symbol_info_cache = {}
processing_symbols = set()

# ============================================================
#   API CORE
# ============================================================

def get_sign(params):
    params["timestamp"] = str(int(time.time() * 1000))
    query_string = urllib.parse.urlencode(sorted(params.items()))
    sig = hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    return f"{query_string}&signature={sig}"

def api_request(method, endpoint, payload=None):
    if payload is None: payload = {}
    query_with_sig = get_sign(payload)
    url = f"{BINGX_BASE}{endpoint}?{query_with_sig}"
    headers = {"X-BX-APIKEY": API_KEY}
    try:
        if method == "GET":
            resp = requests.get(url, headers=headers, timeout=10)
        else:
            resp = requests.post(url, headers=headers, timeout=10)
        return resp.json()
    except Exception as e:
        logging.error(f"[API ERROR] {e}")
        return None

def get_symbol_info(symbol):
    if symbol in symbol_info_cache: return symbol_info_cache[symbol]
    r = api_request("GET", "/openApi/swap/v2/quote/contracts")
    if r and "data" in r:
        for item in r["data"]:
            if item["symbol"] == symbol:
                p_p = 1 / (10 ** float(item.get("pricePrecision", 4)))
                q_p = 1 / (10 ** float(item.get("quantityPrecision", 2)))
                symbol_info_cache[symbol] = {"price_step": p_p, "qty_step": q_p}
                return symbol_info_cache[symbol]
    return {"price_step": 0.0001, "qty_step": 0.0001}

def round_step(value, step):
    if not step or step == 0: return value
    inv = 1.0 / step
    return round(floor(value * inv + 0.00000001) / inv, 8)

# ============================================================
#   PERSISTENZ
# ============================================================

def save_dca():
    with dca_lock:
        try:
            with open(DCA_SAVE_FILE, "w") as f: json.dump(active_dca, f, indent=4)
        except: pass

def load_dca():
    global active_dca
    if os.path.exists(DCA_SAVE_FILE):
        try:
            with open(DCA_SAVE_FILE, "r") as f: active_dca = json.load(f)
        except: pass

# ============================================================
#   TP/SL LOGIK (FORCE MODE)
# ============================================================

def set_exchange_tp_sl(symbol, side, entry, tp_p, sl_p, quantity):
    """
    Versucht aktiv die Position zu finden. 
    Wenn nicht gefunden -> Trotzdem setzen (Blind Fire).
    """
    logging.info(f"[TP/SL WORKER] Gestartet für {symbol}. Warte 5s Initial...")
    time.sleep(5)

    # 1. VERSUCH: Position finden (Polling)
    position_found = False
    for i in range(10): # 10 Versuche a 3 Sekunden = 30 Sekunden
        try:
            pos_res = api_request("GET", "/openApi/swap/v2/user/positions", {"symbol": symbol})
            # DEBUG LOGGING: Damit wir sehen was BingX antwortet
            logging.info(f"[DEBUG POS CHECK] Antwort {i}: {json.dumps(pos_res)}")
            
            if pos_res and "data" in pos_res:
                for p in pos_res["data"]:
                    # Prüfen ob Symbol stimmt und Position NICHT 0 ist
                    if p["symbol"] == symbol and abs(float(p["positionAmt"])) > 0:
                        logging.info(f"[POS FOUND] Position bestätigt: {p['positionAmt']}")
                        position_found = True
                        break
            if position_found: break
        except Exception as e:
            logging.error(f"[CHECK ERROR] {e}")
        
        time.sleep(3)

    if not position_found:
        logging.warning(f"[WARN] Position {symbol} nach 35s nicht in API sichtbar! Aktiviere FORCE MODE...")
        # Wir machen trotzdem weiter, vielleicht "lügt" der Position-Endpoint, aber die Order geht durch.

    # 2. ORDERS SETZEN
    info = get_symbol_info(symbol)
    tp_price = round_step(entry * (1 + tp_p/100 if side == "LONG" else 1 - tp_p/100), info["price_step"])
    sl_price = round_step(entry * (1 - sl_p/100 if side == "LONG" else 1 + sl_p/100), info["price_step"])
    order_side = "SELL" if side == "LONG" else "BUY"
    str_qty = str(round_step(quantity, info["qty_step"]))

    orders = [("TAKE_PROFIT_MARKET", tp_price), ("STOP_MARKET", sl_price)]

    for o_type, price in orders:
        payload = {
            "symbol": symbol,
            "side": order_side,
            "positionSide": side,
            "type": o_type,
            "stopPrice": str(price),
            "workingType": "MARK_PRICE",
            "quantity": str_qty,
            "closePosition": "true"
        }
        
        logging.info(f"[SENDING] {o_type} Payload: {payload}")
        
        # 3 Versuche beim Senden
        for attempt in range(3):
            res = api_request("POST", "/openApi/swap/v2/trade/order", payload)
            logging.info(f"[BINGX RESPONSE] {res}") # WICHTIG: Wir loggen die Antwort
            
            if res and res.get("code") == 0:
                logging.info(f"[SUCCESS] {o_type} gesetzt.")
                break
            else:
                logging.warning(f"[RETRY] Fehler bei {o_type}. Warte 2s...")
                time.sleep(2)

# ============================================================
#   TRADE EXECUTION
# ============================================================

def execute_trade(symbol, direction, leverage, trade_size, tp_p, sl_p):
    if symbol in processing_symbols: return
    processing_symbols.add(symbol)
    
    try:
        # Preis holen
        price_res = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
        if not price_res or "data" not in price_res: return
        price = float(price_res["data"]["price"])

        # Hebel
        api_request("POST", "/openApi/swap/v2/trade/leverage", {"symbol": symbol, "leverage": leverage, "side": direction})
        
        # Order
        info = get_symbol_info(symbol)
        qty = round_step(trade_size / price, info["qty_step"])
        
        logging.info(f"[EXEC] {direction} {symbol} @ {price} Qty:{qty}")
        res = api_request("POST", "/openApi/swap/v2/trade/order", {
            "symbol": symbol, "side": "BUY" if direction == "LONG" else "SELL",
            "positionSide": direction, "type": "MARKET", "quantity": str(qty)
        })

        if res and res.get("code") == 0:
            with dca_lock:
                active_dca[symbol] = {
                    "side": direction, "entry_static": price, "entry_dynamic": price,
                    "executed": 0, "base_trade_size": trade_size, "qty": qty
                }
                save_dca()
            # Thread starten
            threading.Thread(target=set_exchange_tp_sl, args=(symbol, direction, price, tp_p, sl_p, qty)).start()
        else:
            logging.error(f"[FAIL] Order abgelehnt: {res}")

    except Exception as e:
        logging.error(f"[CRASH] {e}")
    finally:
        time.sleep(2)
        processing_symbols.discard(symbol)

# ============================================================
#   WATCHER & WEBHOOK
# ============================================================

def monitor_worker():
    while True:
        try:
            pos_res = api_request("GET", "/openApi/swap/v2/user/positions")
            if not pos_res: 
                time.sleep(10)
                continue
            
            active_list = {p["symbol"]: p for p in pos_res.get("data", []) if float(p["positionAmt"]) != 0}

            with dca_lock:
                to_delete = [s for s in active_dca if s not in active_list]
                for s in to_delete: del active_dca[s]
                if to_delete: save_dca()

            for symbol in list(active_dca.keys()):
                d = active_dca[symbol]
                curr_res = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
                if not curr_res: continue
                curr = float(curr_res["data"]["price"])

                if d["executed"] < DCA_MAX_STEPS:
                    req_drop = DCA_DEVIATION_PERCENT * (d["executed"] + 1)
                    triggered = False
                    if d["side"] == "LONG" and curr <= d["entry_static"] * (1 - req_drop/100): triggered = True
                    if d["side"] == "SHORT" and curr >= d["entry_static"] * (1 + req_drop/100): triggered = True

                    if triggered:
                        logging.info(f"[DCA TRIGGER] {symbol}")
                        ords = api_request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol})
                        for o in ords.get("data", {}).get("orders", []):
                            api_request("POST", "/openApi/swap/v2/trade/cancelOrder", {"symbol": symbol, "orderId": o["orderId"]})
                        
                        info = get_symbol_info(symbol)
                        new_qty = round_step((d["base_trade_size"] * (DCA_VOLUME_MULTIPLIER ** (d["executed"] + 1))) / curr, info["qty_step"])
                        api_request("POST", "/openApi/swap/v2/trade/order", {
                            "symbol": symbol, "side": "BUY" if d["side"] == "LONG" else "SELL",
                            "positionSide": d["side"], "type": "MARKET", "quantity": str(new_qty)
                        })
                        time.sleep(2)
                        p_now = api_request("GET", "/openApi/swap/v2/user/positions", {"symbol": symbol})
                        if p_now and p_now.get("data"):
                            with dca_lock:
                                d["executed"] += 1
                                d["entry_dynamic"] = float(p_now["data"][0]["avgPrice"])
                                save_dca()

                if d["executed"] > 0:
                    exit_reason = None
                    if d["side"] == "LONG":
                        if curr >= d["entry_dynamic"] * (1 + DCA_TP_PERCENT/100): exit_reason = "TP"
                        elif curr <= d["entry_dynamic"] * (1 - DCA_SL_PERCENT/100): exit_reason = "SL"
                    else:
                        if curr <= d["entry_dynamic"] * (1 - DCA_TP_PERCENT/100): exit_reason = "TP"
                        elif curr >= d["entry_dynamic"] * (1 + DCA_SL_PERCENT/100): exit_reason = "SL"

                    if exit_reason:
                        logging.info(f"[EXIT] {symbol} wegen {exit_reason}")
                        api_request("POST", "/openApi/swap/v2/trade/order", {
                            "symbol": symbol, "side": "SELL" if d["side"] == "LONG" else "BUY",
                            "positionSide": d["side"], "type": "MARKET", "closePosition": "true"
                        })
        except: pass
        time.sleep(5)

@app.route("/ping")
def ping(): return "OK", 200

@app.route("/testorder", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    raw = data.get("ticker") or data.get("currency") or data.get("pair") or data.get("symbol")
    if not raw: return jsonify({"error": "No ticker"}), 400
    
    symbol = str(raw).upper().replace("USDT", "").replace("-", "") + "-USDT"
    threading.Thread(target=execute_trade, args=(
        symbol, str(data.get("direction", "LONG")).upper(),
        int(data.get("leverage", LEVERAGE)), float(data.get("trade_size", TRADE_SIZE)),
        float(data.get("tp_percent", TP_PERCENT)), float(data.get("sl_percent", SL_PERCENT))
    )).start()
    return jsonify({"status": "processing"}), 200

if __name__ == "__main__":
    load_dca()
    threading.Thread(target=monitor_worker, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))