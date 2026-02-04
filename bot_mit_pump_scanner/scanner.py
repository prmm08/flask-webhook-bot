import ccxt
import time
import requests
import os
from datetime import datetime

# --- KONFIGURATION ---
# URL deiner Flask App (app.py) auf Render
# WICHTIG: Wenn app.py und scanner.py im selben Service laufen, nutze 'http://localhost:5000/testorder'
# Wenn es zwei Services sind, nutze die https://....onrender.com/testorder URL.
WEBHOOK_URL = "https://flask-webhook-bot-1.onrender.com/testorder" 

# Einstellungen aus dem Pump Detector
TIMEFRAME = '15m'       
LOOKBACK_4H = 16        # 16 Kerzen à 15m = 4 Stunden
CHANGE_THRESHOLD_4H = 15.0   # Alarm bei > 15% in 4h
CHANGE_THRESHOLD_FLASH = 10.0 # Alarm bei > 10% in 15m (Flash)

# Cooldown: Verhindert, dass derselbe Coin alle paar Sekunden geshortet wird
SIGNAL_COOLDOWN = 1800  # 30 Minuten

# Deine Watchlist (Hier kannst du die Liste aus der alten scanner.py oder pump_detector.py einfügen)
WATCHLIST = [
	'AAUSDT',
	'ABONDUSDT',
	'ACUUSDT',
	'ADOUSDT',
	'AIAVUSDT',
	'AICUSDT',
	'AINUSDT',
	'AIWUSDT',
	'AIXBTUSDT',
	'ALEOUSDT',
	'ALLOUSDT',
	'ALPACAUSDT',
	'APRUSDT',
	'AREAUSDT',
	'ARGUSDT',
	'ARIAUSDT',
	'ASTERUSDT',
	'ATUSDT',
	'AUREQUSDT',
	'AUSDT',
	'AVNTUSDT',
	'BANKUSDT',
	'BASUSDT',
	'BFICUSDT',
	'BIFIUSDT',
	'BIFUSDT',
	'BITLIGHTUSDT',
	'BLOCKUSDT',
	'BREVUSDT',
	'BRICUSDT',
	'BURGERUSDT',
	'BXEUSDT',
	'BYTEUSDT',
	'CHATUSDT',
	'CLOUSDT',
	'CLUBUSDT',
	'COGUSDT',
	'COLLECTUSDT',
	'CUSDT',
	'DEEPUSDT',
	'DGRAMUSDT',
	'DOVUUSDT',
	'DUSKUSDT',
	'ELSAUSDT',
	'FFUSDF',
	'FISHUSDT',
	'FRAXUSDT',
	'GRASSUSDT',
	'GRIFFAINUSDT',
	'GSDUSDT',
	'GUNUSDT',
	'GWEIUSDT',
	'HIGHUSDT',
	'HXDUSDT',
	'ILVUSDT',
	'IPUSDT',
	'IRWAUSDT',
	'IXFIUSDT',
	'JCTUSDT',
	'KAIAUSDT',
	'KAITOUSDT',
	'KARMAUSDT',
	'KERNELUSDT',
	'KLARAUSDT',
	'KOIIUSDT',
	'KOINUSDT',
	'KPCUSDT',
	'KRYONUSDT',
	'LABUSDT',
	'LAYERUSDT',
	'LKYUSDT',
	'LRTUSDT',
	'LYNUSDT',
	'MAXUSDT',
	'MCNUSDT',
	'MEMESUSDT',
	'MEUSDT',
	'MEWUSDT',
	'MMTUSDT',
	'MOVEUSDT',
	'NAIUSDT',
	'NOSUSDT',
	'NOTUSDT',
	'OBIUSDT',
	'OBORTECHUSDT',
	'OGUSDT',
	'OOOOUSDT',
	'PAYAIUSDT',
	'PENDLEUSDT',
	'PENGUUSDT',
	'PHILUSDT',
	'PHLUSDT',
	'PIGEONUSDT',
	'PLAYSOLANAUSDT',
	'PLUMEUSDT',
	'PROVEUSDT',
	'PSTAKEUSDT',
	'QUSDT',
	'RAIUSDT',
	'REPPOUSDT',
	'RESOLVUSDT',
	'REZUSDT',
	'RIBUSDT',
	'RIVERUSDT',
	'RKFIUSDT',
	'ROLLUSDT',
	'ROOTUSDT',
	'RSTUSDT',
	'RXSUSDT',
	'SAFEUSDT',
	'SANDUSDT',
	'SIGNUSDT',
	'SIRENUSDT',
	'SKLUSDT',
	'SKXUSDT',
	'SKYAIUSDT',
	'SMARTUSDT',
	'SOLTOMATOUSDT',
	'SOONUSDT',
	'SQDUSDT',
	'STORJUSDT',
	'STOUSDT',
	'SUPERDAPPUSDT',
	'TAGUSDT',
	'TOSHIUSDT',
	'TRADOORUSDT',
	'TUPUSDT',
	'TURBOUSDT',
	'TURTLEUSDT',
	'UAIUSDT',
	'USELESSUSDT',
	'VANRYUSDT',
	'VINEUSDT',
	'WCTUSDT',
	'WHITEWHALEUSDT',
	'WITCHUSDT',
	'WOTAMALAILEUSDT',
	'XFIUSDT',
	'XNLUSDT',
	'XPLUSDT',
	'XUEQIUUSDT',
	'YFIUSDT',
	'YURUUSDT',
	'ZAMAUSDC',
	'ZAMAUSDT',
	'ZILUSDT',
	'ZKJUSDT',
	'ZKPUSDC',
	'ZKPUSDT',
	'ZTEUSDT'
]

# State Management
last_signal_time = {}
exchange = ccxt.binance()

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def get_market_data(symbol):
    try:
        # Wir holen genug Kerzen für den 4h Rückblick (mindestens 17)
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=20)
        if not ohlcv or len(ohlcv) < LOOKBACK_4H + 1:
            return None
        return ohlcv
    except Exception as e:
        log(f"Error fetching {symbol}: {e}")
        return None

def check_pump(symbol):
    data = get_market_data(symbol)
    if not data:
        return None

    # Daten extrahieren
    # ohlcv Format: [timestamp, open, high, low, close, volume]
    current_candle = data[-1]
    current_price = current_candle[4] # Close/Aktueller Preis
    
    # 1. Flash Check (Vergleich mit Open der aktuellen oder letzten Kerze)
    # Um sehr schnelle Pumps zu erkennen, vergleichen wir Current Price mit Open der aktuellen 15m Kerze
    flash_open = current_candle[1]
    pct_flash = ((current_price - flash_open) / flash_open) * 100

    # 2. 4h Trend Check (Vergleich mit Close vor 4 Stunden)
    # Index -1 ist aktuell, -17 ist vor 16 Kerzen (4h)
    old_candle = data[-(LOOKBACK_4H + 1)] 
    price_4h_ago = old_candle[4]
    pct_4h = ((current_price - price_4h_ago) / price_4h_ago) * 100

    reason = None
    
    # Logik aus pump_detector.py
    if pct_4h >= CHANGE_THRESHOLD_4H:
        reason = f"4h Trend Pump: +{pct_4h:.2f}%"
    elif pct_flash >= CHANGE_THRESHOLD_FLASH:
        reason = f"⚡ FLASH SPIKE: +{pct_flash:.2f}%"

    if reason:
        return {
            "symbol": symbol,
            "reason": reason,
            "price": current_price,
            "change_4h": pct_4h,
            "change_flash": pct_flash
        }
    
    return None

def send_signal(pump_data):
    symbol = pump_data['symbol']
    
    # Payload für app.py -> worker.py
    # Wir nutzen "SHORT" als Richtung, da du DCA Short betreiben willst
    payload = {
        "ticker": symbol, 
        "direction": "SHORT",
        "leverage": 20,         # Hebel wie in der alten scanner.py
        "trade_size": 20,       # Einstiegsgröße in USDT (anpassen!)
        "tp_percent": 5         # Take Profit initial
    }
    
    try:
        log(f"🚀 PUMP DETECTED für {symbol}: {pump_data['reason']}")
        response = requests.post(WEBHOOK_URL, json=payload, timeout=5)
        log(f"Webhook Status: {response.status_code} - {response.text}")
    except Exception as e:
        log(f"Webhook Fail: {e}")

if __name__ == "__main__":
    log(f"Pump Scanner gestartet (Thresholds: 4h={CHANGE_THRESHOLD_4H}%, Flash={CHANGE_THRESHOLD_FLASH}%)")
    
    while True:
        for symbol in WATCHLIST:
            # Cooldown prüfen
            last_time = last_signal_time.get(symbol, 0)
            if (time.time() - last_time) < SIGNAL_COOLDOWN:
                continue 

            # Check logic
            pump = check_pump(symbol)
            
            if pump:
                send_signal(pump)
                # Timestamp aktualisieren, damit wir nicht spammen
                last_signal_time[symbol] = time.time()
            
            # Rate Limit Schutz für Binance Public API
            time.sleep(0.1) 
            
        # Kurze Pause nach Durchlauf der ganzen Liste
        time.sleep(2)