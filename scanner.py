import ccxt
import pandas as pd
import pandas_ta as ta
import requests
import time
from datetime import datetime

# --- KONFIGURATION ---
# Deine Render Webhook URL
WEBHOOK_URL = "https://flask-webhook-bot-1.onrender.com/testorder" 

# HIER DEINE LISTE REINKOPIEREN:
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

TIMEFRAME = '1m'
CHECK_INTERVAL = 15    

# --- RSI SETTINGS ---
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70

# --- DIVERGENCE SETTINGS ---
PIVOT_LEFT = 5      
PIVOT_RIGHT = 5     
RANGE_MIN = 5       
RANGE_MAX = 60      

# Cooldown
SIGNAL_COOLDOWN = 300  
last_signal_time = {}

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def get_binance_data(symbol, limit=150):
    try:
        # Futures Modus
        exchange = ccxt.binance({'options': {'defaultType': 'future'}})
        
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=limit)
        if not ohlcv: return None

        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['close'] = df['close'].astype(float)
        df['high'] = df['high'].astype(float)
        return df
    except Exception as e:
        log(f"Data Error {symbol}: {e}")
        return None

def calculate_indicators(df):
    df['rsi'] = df.ta.rsi(length=RSI_PERIOD, close=df['close'])
    return df

# --- LOGIK 1: RSI CROSS DOWN ---
def check_rsi_cross_down(df):
    if len(df) < 2: return None
    
    curr_rsi = df['rsi'].iloc[-1]
    prev_rsi = df['rsi'].iloc[-2]
    
    if prev_rsi >= RSI_OVERBOUGHT and curr_rsi < RSI_OVERBOUGHT:
        return f"RSI Crossing Down 70 ({prev_rsi:.1f} -> {curr_rsi:.1f})"
    return None

# --- LOGIK 2: BEARISH DIVERGENCE ---
def is_pivot_high(df, index, left, right):
    if index - left < 0 or index + right >= len(df): return False
    current_high = df['high'].iloc[index]
    for i in range(1, left + 1):
        if df['high'].iloc[index - i] > current_high: return False
    for i in range(1, right + 1):
        if df['high'].iloc[index + i] > current_high: return False       
    return True

def check_bearish_divergence(df):
    if len(df) < RANGE_MAX + PIVOT_RIGHT + 5: return None

    curr_idx = len(df) - 1 - PIVOT_RIGHT 
    
    if not is_pivot_high(df, curr_idx, PIVOT_LEFT, PIVOT_RIGHT):
        return None

    p1_price = df['high'].iloc[curr_idx]
    p1_rsi = df['rsi'].iloc[curr_idx]
    
    start_search = curr_idx - RANGE_MIN
    end_search = max(0, curr_idx - RANGE_MAX)
    
    for prev_idx in range(start_search, end_search, -1):
        if is_pivot_high(df, prev_idx, PIVOT_LEFT, PIVOT_RIGHT):
            p0_price = df['high'].iloc[prev_idx]
            p0_rsi = df['rsi'].iloc[prev_idx]
            
            if p1_price > p0_price and p1_rsi < p0_rsi:
                if p1_rsi > 50: 
                    return f"Bearish Div (Price: {p1_price:.2f} > {p0_price:.2f} | RSI: {p1_rsi:.1f} < {p0_rsi:.1f})"
    return None

def send_webhook(symbol, reason):
    # Entfernt /USDT und ggf. 1000 für den Ticker im Webhook
    clean_symbol = symbol.replace("/", "").replace("1000", "") 
    
    payload = {
        "ticker": clean_symbol,
        "direction": "SHORT"
    }
    
    try:
        log(f"🚀 SIGNAL für {symbol}: {reason}")
        requests.post(WEBHOOK_URL, json=payload, timeout=5)
    except Exception as e:
        log(f"Webhook Fail: {e}")

# --- MAIN LOOP ---
if __name__ == "__main__":
    log(f"Dual-Scanner gestartet (Watchlist: {len(WATCHLIST)} Coins).")
    
    while True:
        for symbol in WATCHLIST:
            last_time = last_signal_time.get(symbol, 0)
            if (time.time() - last_time) < SIGNAL_COOLDOWN:
                continue 

            df = get_binance_data(symbol, limit=150)
            if df is not None:
                df = calculate_indicators(df)
                
                signal_reason = None
                
                # Check 1
                rsi_signal = check_rsi_cross_down(df)
                if rsi_signal: signal_reason = rsi_signal
                
                # Check 2 (nur wenn Check 1 leer)
                if not signal_reason:
                    div_signal = check_bearish_divergence(df)
                    if div_signal: signal_reason = div_signal
                
                if signal_reason:
                    send_webhook(symbol, signal_reason)
                    last_signal_time[symbol] = time.time()
            
            time.sleep(1) 

        time.sleep(CHECK_INTERVAL)