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
#   1. KONFIGURATION
# ============================================================
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = "https://open-api.bingx.com"

# --- Standard Trading Parameter (Falls Webhook leer ist) ---
LEVERAGE = 20
TRADE_SIZE = 100
TP_PERCENT = 1.0        # Initialer TP im Orderbuch
SL_PERCENT = 40.0       # Initialer SL im Orderbuch

# --- DCA (Nachkauf) Einstellungen ---
DCA_DEVIATION_PERCENT = 5.0    # Nachkauf alle 5% Kursabfall
DCA_VOLUME_MULTIPLIER = 2      # Verdopplung der Menge
DCA_MAX_STEPS = 5              # Max. 5 Nachkäufe
DCA_TP_PERCENT = 1.2           # Profit-Ziel nach DCA (Virtual)
DCA_SL_PERCENT = 20.0          # Not-Aus nach DCA (Virtual)
DCA_SAVE_FILE = "active_dca.json"

# --- System ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
app = Flask(__name__)

active_dca = {}
dca_lock = threading.Lock()
symbol_info_cache = {}
processing_symbols = set()

# ============================================================
#   2. HELPERS (API & PRÄZISION)
# ============================================================

def api_request(method, endpoint, params=None):
    """Führt signierte Requests an BingX aus"""
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
        return resp.json()
    except Exception as e:
        logging.error(f"[API ERROR] {e}")
        return None

def get_symbol_info(symbol):
    """Holt die erlaubten Nachkommastellen für Preis und Menge"""
    if symbol in symbol_info_cache: return symbol_info_cache[symbol]
    r = api_request("GET", "/openApi/swap/v2/quote/contracts")
    if r and "data" in r:
        for item in r["data"]:
            if item["symbol"] == symbol:
                p_p = 1 / (10 ** float(item.get("pricePrecision", 4)))
                q_p = 1 / (10 ** float(item.get("quantityPrecision", 2)))
                symbol_info_cache[symbol] = {"price_step": p_p, "qty_step": q_p}
                return symbol_info_cache[symbol]
    return {"price_step": 0.0001, "qty_step": 0.0001} # Fallback

def round_step(value, step):
    """Rundet mathematisch korrekt ab (Floor)"""
    if not step or step == 0: return value
    inv = 1.0 / step
    return round(floor(value * inv + 0.00000001) / inv, 8)

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
            logging.info(f"DCA-Status geladen: {len(active_dca)} aktive Trades.")
        except: pass

# ============================================================
#   3. CORE FEATURE: EXAKTES TP/SL SETZEN
# ============================================================

def set_exchange_tp_sl(symbol, side, entry, tp_p, sl_p, quantity):
    """
    Setzt TP/SL Orders.
    WICHTIG: Sendet 'quantity' UND 'closePosition: true'.
    Dies ist die einzige Methode, die bei Hedge Mode + Altcoins zuverlässig funktioniert.
    """
    # 6 Sekunden warten, damit BingX die Position intern verbucht hat
    logging.info(f"[TP/SL] Warte 6s auf Sync für {symbol}...")
    time.sleep(6)

    info = get_symbol_info(symbol)
    
    # Preise berechnen
    tp_price = round_step(entry * (1 + tp_p/100 if side == "LONG" else 1 - tp_p/100), info["price_step"])
    sl_price = round_step(entry * (1 - sl_p/100 if side == "LONG" else 1 + sl_p/100), info["price_step"])
    
    # Hedge Mode Logik: Schließen = Gegenteilige Order Side
    order_side = "SELL" if side == "LONG" else "BUY"
    
    # Menge runden (BingX akzeptiert keine unsauberen Floats)
    safe_qty = round_step(quantity, info["qty_step"])

    # Loop für TP und SL
    for o_type, price in [("TAKE_PROFIT_MARKET", tp_price), ("STOP_MARKET", sl_price)]:
        success = False
        # Bis zu 3 Versuche
        for attempt in range(3):
            payload = {
                "symbol": symbol,
                "side": order_side,       # BUY oder SELL
                "positionSide": side,     # LONG oder SHORT (Bleibt gleich!)
                "type": o_type,
                "stopPrice": str(price),
                "workingType": "MARK_PRICE",
                "quantity": str(safe_qty), # Zwingend erforderlich für manche Coins
                "closePosition": "true"    # Zwingend erforderlich für Hedge Mode
            }
            
            res = api_request("POST", "/openApi/swap/v2/trade/order", payload)
            
            if res and res.get("code") == 0:
                logging.info(f"[SUCCESS] {o_type} für {symbol} @ {price} gesetzt.")
                success = True
                break
            else:
                logging.warning(f"[RETRY {attempt+1}] {o_type} fehlgeschlagen: {res.get('msg')}")
                time.sleep(2) # Kurz warten vor Retry
        
        if not success:
            logging.error(f"[FATAL] Konnte {o_type} für {symbol} nicht setzen! Trade läuft ohne Schutz!")

# ============================================================
#   4. CORE FEATURE: TRADE AUSFÜHREN
# ============================================================

def execute_trade(symbol, direction, leverage, trade_size, tp_p, sl_p):
    if symbol in processing_symbols: return
    processing_symbols.add(symbol)
    
    try:
        # 1. Prüfen ob Position schon existiert
        pos_data = api_request("GET", "/openApi/swap/v2/user/positions", {"symbol": symbol})
        if pos_data and any(float(p["positionAmt"]) != 0 for p in pos_data.get("data", [])):
            logging.info(f"[SKIP] Position {symbol} existiert bereits.")
            return

        # 2. Preis holen
        price_res = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
        if not price_res or "data" not in price_res: return
        price = float(price_res["data"]["price"])

        # 3. Hebel setzen
        api_request("POST", "/openApi/swap/v2/trade/leverage", {"symbol": symbol, "leverage": leverage, "side": direction})
        
        # 4. Menge berechnen
        info = get_symbol_info(symbol)
        qty = round_step(trade_size / price, info["qty_step"])

        logging.info(f"[ORDER] Starte {direction} {symbol} | Entry: {price} | Qty: {qty}")
        
        # 5. Market Order senden
        res = api_request("POST", "/openApi/swap/v2/trade/order", {
            "symbol": symbol,
            "side": "BUY" if direction == "LONG" else "SELL",
            "positionSide": direction,
            "type": "MARKET",
            "quantity": str(qty)
        })

        if res and res.get("code") == 0:
            # 6. DCA Eintrag anlegen
            with dca_lock:
                active_dca[symbol] = {
                    "side": direction,
                    "entry_static": price,  # Ursprünglicher Entry (für DCA Trigger)
                    "entry_dynamic": price, # Durchschnittspreis (für BE/Exit)
                    "executed": 0,
                    "base_trade_size": trade_size,
                    "qty": qty
                }
                save_dca()
            
            # 7. TP/SL in Hintergrund-Thread starten (Blockiert Webhook nicht)
            threading.Thread(target=set_exchange_tp_sl, args=(symbol, direction, price, tp_p, sl_p, qty)).start()
        else:
            logging.error(f"[FAIL] Order abgelehnt: {res}")

    except Exception as e:
        logging.error(f"[CRASH] Fehler in execute_trade: {e}")
    finally:
        time.sleep(2)
        processing_symbols.discard(symbol)

# ============================================================
#   5. WATCHER (DCA & EXIT LOGIK)
# ============================================================

def monitor_worker():
    logging.info("DCA Monitor gestartet.")
    while True:
        try:
            # Hole alle offenen Positionen von BingX
            pos_res = api_request("GET", "/openApi/swap/v2/user/positions")
            if not pos_res: 
                time.sleep(10)
                continue
                
            active_list = {p["symbol"]: p for p in pos_res.get("data", []) if float(p["positionAmt"]) != 0}

            # Datenbank bereinigen (geschlossene Trades entfernen)
            with dca_lock:
                to_delete = [s for s in active_dca if s not in active_list]
                for s in to_delete: del active_dca[s]
                if to_delete: save_dca()

            # DCA Logik prüfen
            for symbol in list(active_dca.keys()):
                d = active_dca[symbol]
                
                # Aktuellen Preis holen
                curr_res = api_request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
                if not curr_res: continue
                curr = float(curr_res["data"]["price"])

                # --- A: Nachkaufen (DCA) ---
                if d["executed"] < DCA_MAX_STEPS:
                    # Trigger berechnen: Level 1 bei 5%, Level 2 bei 10% etc.
                    req_drop = DCA_DEVIATION_PERCENT * (d["executed"] + 1)
                    
                    triggered = False
                    if d["side"] == "LONG" and curr <= d["entry_static"] * (1 - req_drop/100): triggered = True
                    if d["side"] == "SHORT" and curr >= d["entry_static"] * (1 + req_drop/100): triggered = True

                    if triggered:
                        logging.info(f"[DCA TRIGGER] {symbol} Level {d['executed']+1}. Lösche alte TP/SL...")
                        
                        # 1. Alte Orders löschen
                        ords = api_request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol})
                        for o in ords.get("data", {}).get("orders", []):
                            api_request("POST", "/openApi/swap/v2/trade/cancelOrder", {"symbol": symbol, "orderId": o["orderId"]})
                        
                        # 2. Nachkaufen (Martingale: Volumen verdoppeln)
                        info = get_symbol_info(symbol)
                        new_vol = d["base_trade_size"] * (DCA_VOLUME_MULTIPLIER ** (d["executed"] + 1))
                        new_qty = round_step(new_vol / curr, info["qty_step"])
                        
                        api_request("POST", "/openApi/swap/v2/trade/order", {
                            "symbol": symbol,
                            "side": "BUY" if d["side"] == "LONG" else "SELL", # Long = Buy More, Short = Sell More
                            "positionSide": d["side"],
                            "type": "MARKET",
                            "quantity": str(new_qty)
                        })

                        # 3. Daten updaten
                        time.sleep(2)
                        p_now = api_request("GET", "/openApi/swap/v2/user/positions", {"symbol": symbol})
                        if p_now and p_now.get("data"):
                            with dca_lock:
                                d["executed"] += 1
                                d["entry_dynamic"] = float(p_now["data"][0]["avgPrice"]) # Neuer Avg Price
                                save_dca()
                                logging.info(f"[DCA DONE] Neuer Avg Entry für {symbol}: {d['entry_dynamic']}")

                # --- B: Virtual Exit (Nur wenn DCA aktiv war) ---
                if d["executed"] > 0:
                    exit_reason = None
                    # TP Check (Basiert auf neuem Avg Price)
                    if d["side"] == "LONG" and curr >= d["entry_dynamic"] * (1 + DCA_TP_PERCENT/100):
                        exit_reason = "VIRTUAL_TP"
                    elif d["side"] == "SHORT" and curr <= d["entry_dynamic"] * (1 - DCA_TP_PERCENT/100):
                        exit_reason = "VIRTUAL_TP"
                    
                    # SL Check (Not-Aus)
                    elif d["side"] == "LONG" and curr <= d["entry_dynamic"] * (1 - DCA_SL_PERCENT/100):
                        exit_reason = "VIRTUAL_SL"
                    elif d["side"] == "SHORT" and curr >= d["entry_dynamic"] * (1 + DCA_SL_PERCENT/100):
                        exit_reason = "VIRTUAL_SL"

                    if exit_reason:
                        logging.info(f"[{exit_reason}] Schließe {symbol} komplett.")
                        api_request("POST", "/openApi/swap/v2/trade/order", {
                            "symbol": symbol,
                            "side": "SELL" if d["side"] == "LONG" else "BUY",
                            "positionSide": d["side"],
                            "type": "MARKET",
                            "closePosition": "true"
                        })

        except Exception as e:
            logging.error(f"[WATCHER ERROR] {e}")
        
        time.sleep(5) # Alle 5 Sekunden prüfen

# ============================================================
#   6. WEBHOOK
# ============================================================

@app.route("/testorder", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    
    # Intelligente Ticker-Erkennung
    raw = data.get("ticker") or data.get("currency") or data.get("pair") or data.get("symbol")
    if not raw:
        logging.error("[WEBHOOK FAIL] Kein Ticker/Symbol im JSON gefunden!")
        return jsonify({"error": "Missing ticker"}), 400
    
    # Symbol normalisieren (z.B. "ETH" -> "ETH-USDT")
    symbol = str(raw).upper().replace("USDT", "").replace("-", "") + "-USDT"
    
    direction = str(data.get("direction", "LONG")).upper()
    if direction not in ["LONG", "SHORT"]: direction = "LONG"

    # Thread starten
    threading.Thread(target=execute_trade, args=(
        symbol,
        direction,
        int(data.get("leverage", LEVERAGE)),
        float(data.get("trade_size", TRADE_SIZE)),
        float(data.get("tp_percent", TP_PERCENT)),
        float(data.get("sl_percent", SL_PERCENT))
    )).start()
    
    return jsonify({"status": "processing", "symbol": symbol}), 200

@app.route("/ping")
def ping(): return "OK", 200

if __name__ == "__main__":
    load_dca()
    threading.Thread(target=monitor_worker, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))