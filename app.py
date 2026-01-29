import hmac
import hashlib
import requests
import os
import urllib.parse
import threading
import time
import json
import logging
import traceback
from math import floor
from flask import Flask, request, jsonify

# --- API ---
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

# Logging verbessern
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
app = Flask(__name__)

# --- SETTINGS ---
LEVERAGE = 20
TRADE_SIZE = 100
TP_PERCENT = 1.0
SL_PERCENT = 40.0
DCA_DEVIATION_PERCENT = 5.0 
DCA_VOLUME_MULTIPLIER = 2
DCA_SAVE_FILE = "active_dca.json"
FEE_BUFFER = 0.0015  # 0.15% Buffer für Break-Even Exit (Gebühren decken)

active_dca = {}
processing_symbols = set() 
dca_lock = threading.Lock()
symbol_info_cache = {} # Cache für Präzisionen

# ============================================================
#   HELPERS & PERSISTENCE
# ============================================================

def save_dca_data():
    with dca_lock:
        try:
            with open(DCA_SAVE_FILE, "w") as f:
                json.dump(active_dca, f, indent=4)
        except Exception as e:
            logging.error(f"Fehler beim Speichern: {e}")

def load_dca_data():
    global active_dca
    if os.path.exists(DCA_SAVE_FILE):
        try:
            with open(DCA_SAVE_FILE, "r") as f:
                active_dca = json.load(f)
            logging.info(f"DCA Daten geladen: {len(active_dca)} aktive Positionen.")
        except Exception as e:
            logging.error(f"Fehler beim Laden: {e}")

def api_request(method, endpoint, params=None):
    url = f"{BINGX_BASE}{endpoint}"
    p = dict(params) if params else {}
    p["timestamp"] = str(int(time.time() * 1000))
    
    query_string = urllib.parse.urlencode(sorted(p.items()))
    sig = hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    full_url = f"{url}?{query_string}&signature={sig}"
    
    headers = {"X-BX-APIKEY": API_KEY}
    try:
        if method == "GET":
            resp = requests.get(full_url, headers=headers, timeout=10)
        else:
            resp = requests.post(full_url, headers=headers, timeout=10)
        
        resp.raise_for_status() # Löst Fehler bei 4xx/5xx aus
        return resp.json()
    except Exception as e:
        logging.error(f"[API ERROR] {endpoint}: {e}")
        return None

def get_symbol_precision(symbol):
    """Holt tickSize (Preis) und stepSize (Menge) von der API"""
    if symbol in symbol_info_cache:
        return symbol_info_cache[symbol]
    
    # API Request für Contract Info
    r = api_request("GET", "/openApi/swap/v2/quote/contracts")
    if r and "data" in r:
        for item in r["data"]:
            if item["symbol"] == symbol:
                # Fallback Werte falls API 0 liefert
                tick_size = float(item.get("pricePrecision", 0)) # BingX gibt oft int zurück (z.B. 5 decimal places)
                step_size = float(item.get("quantityPrecision", 0))
                
                # BingX spezifisch: Manchmal ist es Decimal Places, manchmal Value
                # Wir konvertieren "Anzahl Dezimalstellen" zu Float-Wert (z.B. 4 -> 0.0001)
                p_prec = 1 / (10 ** tick_size) if tick_size >= 0 else 1.0
                q_prec = 1 / (10 ** step_size) if step_size >= 0 else 1.0

                symbol_info_cache[symbol] = {"price_step": p_prec, "qty_step": q_prec}
                return symbol_info_cache[symbol]
    
    return {"price_step": 0.0001, "qty_step": 0.0001} # Safe default

def round_step(value, step):
    """Rundet value korrekt auf das nächste Vielfache von step ab"""
    if step == 0: return value
    # Nutzung von string formatting für Präzision, um Float-Fehler zu vermeiden
    inv = 1.0 / step
    return round(floor(value * inv) / inv, 10)

def get_price(symbol):
    r = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
    return float(r["data"]["price"]) if r and "data" in r else None

def get_positions():
    r = api_request("GET", "/openApi/swap/v2/user/positions")
    return r.get("data", []) if r else []

# ============================================================
#   EXCHANGE TP/SL
# ============================================================

def set_exchange_tp_sl(symbol, side, entry, tp_p, sl_p):
    time.sleep(2)
    prec = get_symbol_precision(symbol)
    
    tp_raw = entry * (1 + tp_p/100 if side == "LONG" else 1 - tp_p/100)
    sl_raw = entry * (1 - sl_p/100 if side == "LONG" else 1 + sl_p/100)
    
    tp = round_step(tp_raw, prec["price_step"])
    sl = round_step(sl_raw, prec["price_step"])
    
    for price, otype in [(tp, "TAKE_PROFIT_MARKET"), (sl, "STOP_MARKET")]:
        res = api_request("POST", "/openApi/swap/v2/trade/order", {
            "symbol": symbol, "side": "SELL" if side == "LONG" else "BUY",
            "positionSide": side, "type": otype, "stopPrice": str(price),
            "workingType": "MARK_PRICE", "closePosition": "true"
        })
        msg = res.get('msg') if res else "Unknown"
        logging.info(f"[EXCHANGE] TP/SL {otype} für {symbol} @ {price}: {msg}")

# ============================================================
#   TRADE EXECUTION
# ============================================================

def execute_trade(symbol, direction, leverage, trade_size, tp_percent, sl_percent):
    if symbol in processing_symbols:
        return
    
    processing_symbols.add(symbol)
    try:
        # Check API if position exists
        pos = get_positions()
        if any(p["symbol"] == symbol and float(p["positionAmt"]) != 0 for p in pos):
            logging.warning(f"[SKIP] Position für {symbol} bereits offen.")
            return

        price = get_price(symbol)
        if not price: return

        # Set Leverage
        api_request("POST", "/openApi/swap/v2/trade/leverage", {
            "symbol": symbol, "leverage": leverage, "side": "BUY" if direction == "LONG" else "SELL"
        })

        # Market Order mit korrekter Präzision
        prec = get_symbol_precision(symbol)
        qty_raw = trade_size / price
        qty = round_step(qty_raw, prec["qty_step"])

        res = api_request("POST", "/openApi/swap/v2/trade/order", {
            "symbol": symbol, "side": "BUY" if direction == "LONG" else "SELL",
            "positionSide": direction, "type": "MARKET", "quantity": str(qty)
        })

        if res and res.get("code") == 0:
            logging.info(f"[ORDER] {symbol} {direction} Qty:{qty} Entry:{price} erfolgreich.")
            with dca_lock:
                active_dca[symbol] = {
                    "side": direction, 
                    "entry_static": price, 
                    "entry_dynamic": price,
                    "executed": 0, 
                    "base_trade_size": trade_size
                }
                save_dca_data()
            
            # TP/SL auf Exchange setzen
            set_exchange_tp_sl(symbol, direction, price, tp_percent, sl_percent)
        else:
            logging.error(f"[ORDER FAIL] {symbol}: {res}")

    except Exception as e:
        logging.error(f"Fehler in execute_trade: {e}")
        traceback.print_exc()
    finally:
        time.sleep(2)
        processing_symbols.discard(symbol)

# ============================================================
#   WORKER (DCA & BE)
# ============================================================

def monitor_worker():
    logging.info("DCA Monitor gestartet...")
    while True:
        try:
            positions = get_positions()
            # Nur Positionen die nicht 0 sind
            active_list = {p["symbol"]: p for p in positions if float(p["positionAmt"]) != 0}

            with dca_lock:
                to_delete = [s for s in active_dca if s not in active_list]
                for s in to_delete: 
                    logging.info(f"Position {s} geschlossen, entferne aus DCA.")
                    del active_dca[s]
                if to_delete: save_dca_data()

            # Iteriere über eine Kopie der Keys, um Thread-Probleme zu vermeiden
            current_dca_symbols = list(active_dca.keys())

            for symbol in current_dca_symbols:
                with dca_lock:
                    if symbol not in active_dca: continue
                    d = active_dca[symbol]
                
                # Check ob wir die Position noch in active_list haben
                if symbol not in active_list: continue

                side = d["side"]
                curr = get_price(symbol)
                if not curr: continue

                # --- DCA TRIGGER (Korrigierte Logik) ---
                if d["executed"] < 5: 
                    # Nächster Trigger muss basierend auf dem 'executed' Level tiefer liegen
                    # Beispiel: Level 0 -> Trigger bei -5%. Level 1 -> Trigger bei -10% vom Start
                    required_drop_percent = DCA_DEVIATION_PERCENT * (d["executed"] + 1)
                    
                    triggered = False
                    target_price = 0
                    
                    if side == "LONG":
                        target_price = d["entry_static"] * (1 - required_drop_percent/100)
                        if curr <= target_price: triggered = True
                    else: # SHORT
                        target_price = d["entry_static"] * (1 + required_drop_percent/100)
                        if curr >= target_price: triggered = True
                    
                    if triggered:
                        logging.info(f"[DCA TRIGGER] {symbol} Level {d['executed']+1} bei {curr} (Target: {target_price})")
                        
                        # 1. Exchange TP/SL löschen
                        r = api_request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol})
                        for o in r.get("data", {}).get("orders", []):
                            api_request("POST", "/openApi/swap/v2/trade/cancelOrder", {"symbol": symbol, "orderId": o["orderId"]})
                        
                        # 2. Nachkaufen
                        prec = get_symbol_precision(symbol)
                        # DCA Volumen: Base * 2^(executed+1) -> 100, 200, 400...
                        usd_amount = d["base_trade_size"] * (DCA_VOLUME_MULTIPLIER ** (d["executed"] + 1))
                        qty_raw = usd_amount / curr
                        qty = round_step(qty_raw, prec["qty_step"])

                        res = api_request("POST", "/openApi/swap/v2/trade/order", {
                            "symbol": symbol, "side": "BUY" if side == "LONG" else "SELL",
                            "positionSide": side, "type": "MARKET", "quantity": str(qty)
                        })

                        if res and res.get("code") == 0:
                            with dca_lock:
                                d["executed"] += 1
                                save_dca_data()
                            
                            # Update entry_dynamic (Avg Price)
                            time.sleep(2) # Warten bis Position geupdated
                            p_now = next((p for p in get_positions() if p["symbol"] == symbol), None)
                            if p_now:
                                with dca_lock:
                                    d["entry_dynamic"] = float(p_now["avgPrice"])
                                    save_dca_data()
                                logging.info(f"[DCA DONE] Neuer Avg Price für {symbol}: {d['entry_dynamic']}")
                        else:
                            logging.error(f"[DCA FAIL] {symbol}: {res}")

                # --- BE EXIT (Mit Gebühren-Buffer) ---
                # Nur Exit, wenn wir schon mindestens einmal nachgekauft haben (DCA aktiv)
                if d["executed"] > 0:
                    exit_triggered = False
                    # Wir addieren FEE_BUFFER, um nicht mit Verlust durch Gebühren rauszugehen
                    if side == "LONG":
                        # Preis muss höher sein als Avg Entry + Gebühr
                        if curr >= d["entry_dynamic"] * (1 + FEE_BUFFER): exit_triggered = True
                    else:
                        # Preis muss tiefer sein als Avg Entry - Gebühr
                        if curr <= d["entry_dynamic"] * (1 - FEE_BUFFER): exit_triggered = True

                    if exit_triggered:
                        logging.info(f"[BE EXIT] {symbol} bei {curr} (Avg: {d['entry_dynamic']})")
                        api_request("POST", "/openApi/swap/v2/trade/order", {
                            "symbol": symbol, "side": "SELL" if side == "LONG" else "BUY",
                            "positionSide": side, "type": "MARKET", "closePosition": "true"
                        })
                        # Position wird im nächsten Loop aus active_dca entfernt

        except Exception as e:
            logging.error(f"Fehler im Worker: {e}")
            traceback.print_exc() # Zeigt genau wo der Fehler war
        
        time.sleep(5)



# ============================================================
#   HEALTH CHECK (WICHTIG FÜR RENDER)
# ============================================================

@app.route("/")
@app.route("/ping")
def health_check():
    # Einfacher Ping, damit Render weiß, dass der Bot läuft
    return "OK", 200
    
# ============================================================
#   FLASK
# ============================================================

@app.route("/testorder", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    # Fallback falls TradingView "ticker" statt "currency" sendet
    raw_symbol = data.get("currency") or data.get("ticker", "")
    symbol = f"{str(raw_symbol).upper()}-USDT"
    
    direction = str(data.get("direction", "")).upper()
    
    if direction in ("LONG", "SHORT"):
        # Parameter mit Defaults
        lev = int(data.get("leverage", LEVERAGE))
        size = float(data.get("trade_size", TRADE_SIZE))
        tp = float(data.get("tp_percent", TP_PERCENT))
        sl = float(data.get("sl_percent", SL_PERCENT))

        threading.Thread(target=execute_trade, args=(
            symbol, direction, lev, size, tp, sl
        )).start()
        return jsonify({"status": "processing", "symbol": symbol}), 200
    
    return jsonify({"error": "Invalid direction"}), 400

if __name__ == "__main__":
    load_dca_data()
    threading.Thread(target=monitor_worker, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))