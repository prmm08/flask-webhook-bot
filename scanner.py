import ccxt
import pandas as pd
import pandas_ta as ta
import requests
import time
from datetime import datetime

# --- KONFIGURATION ---
WEBHOOK_URL = "https://flask-webhook-bot-1.onrender.com/testorder" 

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
CHECK_INTERVAL = 10    

# --- RSI & DIVERGENCE SETTINGS (TradingView Match) ---
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70

PIVOT_LEFT = 5      
PIVOT_RIGHT = 5     
RANGE_MIN = 5       
RANGE_MAX = 60      

# Cooldown
SIGNAL_COOLDOWN = 180 
last_signal_time = {}

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def get_binance_data(symbol, limit=1000):
    """
    UPDATE: Limit auf 1000 erhöht! 
    RSI braucht viel Historie ("Warm-up"), um exakt wie TradingView zu sein.
    """
    try:
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
    # RSI Berechnung
    df['rsi'] = df.ta.rsi(length=RSI_PERIOD, close=df['close'])
    return df

# --- LOGIK 1: RSI CROSS DOWN ---
def check_rsi_cross_down(df, symbol):
    if len(df) < 3: return None
    rsi_now      = df['rsi'].iloc[-1]
    rsi_last     = df['rsi'].iloc[-2]
    rsi_pre_last = df['rsi'].iloc[-3]
    
    # Optional: Debugging aktivieren, um Werte mit TV zu vergleichen
    # log(f"[DEBUG] {symbol} RSI Last Closed: {rsi_last:.2f} (Vergleiche mit TV!)")

    if rsi_pre_last >= RSI_OVERBOUGHT and rsi_last < RSI_OVERBOUGHT:
        return f"RSI Cross Down (Closed: {rsi_pre_last:.1f} -> {rsi_last:.1f})"
    if rsi_last >= RSI_OVERBOUGHT and rsi_now < RSI_OVERBOUGHT:
        return f"RSI Cross Down (Live: {rsi_last:.1f} -> {rsi_now:.1f})"
    return None

# --- LOGIK 2: BEARISH DIVERGENCE ---
def is_pivot_high_rsi(df, index, left, right):
    """
    Prüft Pivot High im RSI.
    """
    if index - left < 0 or index + right >= len(df): return False
    current_rsi = df['rsi'].iloc[index]
    
    # Check Left
    for i in range(1, left + 1):
        if df['rsi'].iloc[index - i] > current_rsi: return False
    # Check Right
    for i in range(1, right + 1):
        if df['rsi'].iloc[index + i] > current_rsi: return False       
    return True

def check_bearish_divergence(df, symbol):
    if len(df) < RANGE_MAX + PIVOT_RIGHT + 5: return None

    # Index des potenziellen aktuellen Pivots (vor 'right' Bars)
    curr_idx = len(df) - 1 - PIVOT_RIGHT 
    
    # 1. Ist das ein RSI Pivot?
    if not is_pivot_high_rsi(df, curr_idx, PIVOT_LEFT, PIVOT_RIGHT):
        return None

    p1_rsi = df['rsi'].iloc[curr_idx]
    p1_high = df['high'].iloc[curr_idx] 

    # DEBUG: Wenn er einen Pivot findet, zeig ihn an
    # log(f"[DEBUG] {symbol} Pivot High gefunden bei RSI={p1_rsi:.2f}, Price={p1_high:.4f}")

    # 2. Suche Vorgänger (P0)
    start_search = curr_idx - RANGE_MIN
    end_search = max(0, curr_idx - RANGE_MAX)
    
    found_prev_pivot = False
    p0_idx = -1

    for prev_idx in range(start_search, end_search, -1):
        if is_pivot_high_rsi(df, prev_idx, PIVOT_LEFT, PIVOT_RIGHT):
            p0_idx = prev_idx
            found_prev_pivot = True
            break # Ersten Treffer nehmen (TV Logic)
    
    if not found_prev_pivot:
        return None

    p0_rsi = df['rsi'].iloc[p0_idx]
    p0_high = df['high'].iloc[p0_idx]

    # 3. DIVERGENZ LOGIK
    # Preis: Higher High
    # RSI: Lower High
    
    # Wir nutzen >= bei Preis, um "Double Tops" auch als Divergenz zu werten (wie TV oft)
    price_higher_high = p1_high > p0_high 
    rsi_lower_high = p1_rsi < p0_rsi
    
    if price_higher_high and rsi_lower_high:
        return f"Bearish Div (TV): Price {p1_high:.4f} > {p0_high:.4f} | RSI {p1_rsi:.1f} < {p0_rsi:.1f}"

    return None

def send_webhook(symbol, reason):
    clean_symbol = symbol.replace("/", "").replace("1000", "") 
    payload = {
        "ticker": clean_symbol, "direction": "SHORT",
        "leverage": 20, "trade_size": 100, "tp_percent": 3.0
    }
    try:
        log(f"🚀 SIGNAL für {symbol}: {reason}")
        requests.post(WEBHOOK_URL, json=payload, timeout=5)
    except Exception as e:
        log(f"Webhook Fail: {e}")

if __name__ == "__main__":
    log(f"Scanner v2 gestartet (Limit=1000 für RSI Präzision).")
    
    while True:
        for symbol in WATCHLIST:
            last_time = last_signal_time.get(symbol, 0)
            if (time.time() - last_time) < SIGNAL_COOLDOWN:
                continue 

            # Hier ist der Schlüssel: Limit 1000!
            df = get_binance_data(symbol, limit=1000)
            
            if df is not None:
                df = calculate_indicators(df)
                signal_reason = None
                
                # Check A: RSI Cross
                rsi_signal = check_rsi_cross_down(df, symbol)
                if rsi_signal: signal_reason = rsi_signal
                
                # Check B: Divergenz
                if not signal_reason:
                    div_signal = check_bearish_divergence(df, symbol)
                    if div_signal: signal_reason = div_signal
                
                if signal_reason:
                    send_webhook(symbol, signal_reason)
                    last_signal_time[symbol] = time.time()
            time.sleep(0.5) 
        time.sleep(CHECK_INTERVAL)