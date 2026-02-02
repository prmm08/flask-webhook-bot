import ccxt
import pandas as pd
import pandas_ta as ta
import requests
import time
import os
from datetime import datetime

# --- KONFIGURATION --- #
# Deine Render Webhook URL (wo app.py läuft)
WEBHOOK_URL = "https://flask-webhook-bot-1.onrender.com/testorder" 

# Welche Coins sollen überwacht werden?
WATCHLIST = [
    'AIN/USDT',
    'AIXBT/USDT',
    'ALLO/USDT',
    'APR/USDT',
    'ARIA/USDT',
    'ASTER/USDT',
    'A/USDT',
    'AVNT/USDT',
    'BANK/USDT',
    'BAS/USDT',
    'BREV/USDT',
    'CLO/USDT',
    'C/USDT',
    'DEEP/USDT',
    'DUSK/USDT',
    'ELSA/USDT',
    'FRAX/USDT',
    'GRASS/USDT',
    'GRIFFAIN/USDT',
    'GUN/USDT',
    'GWEI/USDT',
    'HIGH/USDT',
    'ILV/USDT',
    'IP/USDT',
    'JCT/USDT',
    'KAIA/USDT',
    'KAITO/USDT',
    'KERNEL/USDT',
    'LAB/USDT',
    'LAYER/USDT',
    'LYN/USDT',
    'ME/USDT',
    'MEW/USDT',
    'MMT/USDT',
    'MOVE/USDT',
    'NOT/USDT',
    'OG/USDT',
    'PENDLE/USDT',
    'PENGU/USDT',
    'PLUME/USDT',
    'PROVE/USDT',
    'RESOLV/USDT',
    'REZ/USDT',
    'SAFE/USDT',
    'SAND/USDT',
    'SIGN/USDT',
    'SIREN/USDT',
    'SKL/USDT',
    'SKYAI/USDT',
    'SOON/USDT',
    'SQD/USDT',
    'STORJ/USDT',
    'STO/USDT',
    'TOSHI/USDT',
    'TURBO/USDT',
    'TURTLE/USDT',
    'UAI/USDT',
    'USELESS/USDT',
    'VANRY/USDT',
    'VINE/USDT',
    'WCT/USDT',
    'XPL/USDT',
    'YFI/USDT',
    'ZAMA/USDT',
    'ZKJ/USDT'
]

TIMEFRAME = '1m'       # 1-Minute Kerzen für schnelle Signale
RSI_LENGTH = 14
CHECK_INTERVAL = 15    # Alle 30 Sekunden prüfen

# Cooldown: Verhindert Spam. Wenn Signal gesendet, warte X Sekunden für diesen Coin.
SIGNAL_COOLDOWN = 300  # 5 Minuten Ruhe pro Coin nach Signal
last_signal_time = {}

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def get_binance_data(symbol, limit=50):
    """Holt Kerzendaten von Binance Futures"""
    try:
        # HIER IST DER FIX: Wir zwingen ccxt in den Futures-Modus
        exchange = ccxt.binance({
            'options': {
                'defaultType': 'future' 
            }
        })
        
        # ccxt kümmert sich um die Details.
        # WICHTIG: Sollte ein Symbol trotzdem nicht gehen, prüfe ob es "1000" im Namen hat 
        # (z.B. 1000PEPE/USDT statt PEPE/USDT bei Futures).
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=limit)
        
        if not ohlcv:
            return None

        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['close'] = df['close'].astype(float)
        return df
    except Exception as e:
        log(f"Data Error {symbol}: {e}")
        return None

def calculate_indicators(df):
    """Berechnet RSI"""
    # RSI berechnen mit pandas_ta
    df['rsi'] = df.ta.rsi(length=RSI_LENGTH)
    return df

def check_conditions(df, symbol):
    """Prüft auf Short Signale"""
    if df is None or len(df) < RSI_LENGTH + 2: return None

    # Wir schauen uns die vorletzte Kerze an (die gerade abgeschlossen ist)
    # und die aktuelle (die noch läuft).
    
    # Indizes: -1 = Aktuelle Kerze, -2 = Letzte fertige Kerze, -3 = Vorletzte
    curr_rsi = df['rsi'].iloc[-1]
    prev_rsi = df['rsi'].iloc[-2]
    
    curr_price = df['close'].iloc[-1]
    prev_high = df['high'].iloc[-2]
    
    signal_reason = None

    # --- STRATEGIE 1: RSI CROSS DOWN 70 ---
    # RSI war über 70 und ist jetzt darunter gefallen
    if prev_rsi >= 70 and curr_rsi < 70:
        signal_reason = f"RSI Cross Down (Prev: {prev_rsi:.1f}, Curr: {curr_rsi:.1f})"

    # --- STRATEGIE 2: SIMPLE BEARISH DIVERGENCE CHECK ---
    # Wenn RSI extrem hoch war (>70), Preis steigt, aber RSI fällt
    # (Dies ist eine vereinfachte Divergenz-Prüfung)
    elif prev_rsi > 65 and curr_rsi < prev_rsi and curr_price > prev_high:
         # Nur signalisieren, wenn RSI auch wirklich fällt
         if curr_rsi < 68: # Filter: Nicht zu früh shorten
            signal_reason = "Potential Bearish Divergence"

    return signal_reason

def send_webhook(symbol, reason):
    """Sendet das Signal an deinen eigenen Bot (app.py)"""
    # Formatierung für app.py
    clean_symbol = symbol.replace("/", "") # Macht BTCUSDT aus BTC/USDT
    
    payload = {
        "ticker": clean_symbol,
        "direction": "SHORT"
    }
    
    try:
        log(f"🚀 SIGNAL für {symbol}: {reason} -> Sende an Webhook...")
        requests.post(WEBHOOK_URL, json=payload, timeout=5)
    except Exception as e:
        log(f"Webhook Fail: {e}")

# --- MAIN LOOP ---
if __name__ == "__main__":
    log(f"Markt-Scanner gestartet. Überwache {len(WATCHLIST)} Coins auf {TIMEFRAME}.")
    log("Strategie: RSI Cross Down 60 & Bearish Div.")

    while True:
        for symbol in WATCHLIST:
            # 1. Cooldown Check
            last_time = last_signal_time.get(symbol, 0)
            if (time.time() - last_time) < SIGNAL_COOLDOWN:
                continue # Diesen Coin überspringen

            # 2. Daten holen & Berechnen
            df = get_binance_data(symbol)
            if df is not None:
                df = calculate_indicators(df)
                
                # 3. Prüfen
                reason = check_conditions(df, symbol)
                
                if reason:
                    # 4. Feuern
                    send_webhook(symbol, reason)
                    last_signal_time[symbol] = time.time() # Cooldown setzen
            
            time.sleep(1) # Kurz atmen zwischen Coins, um Binance Rate Limit nicht zu ärgern

        time.sleep(CHECK_INTERVAL)