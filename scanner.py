import ccxt
import pandas as pd
import pandas_ta as ta
import requests
import time
from datetime import datetime

# --- KONFIGURATION ---
WEBHOOK_URL = "https://flask-webhook-bot-1.onrender.com/testorder" 

# Deine Watchlist
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

# --- RSI SETTINGS (PINE SCRIPT: len=14, src=close) ---
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70

# --- DIVERGENCE SETTINGS (PINE SCRIPT MATCH) ---
# lbL = 5, lbR = 5, rangeUpper = 60, rangeLower = 5
PIVOT_LEFT = 5      
PIVOT_RIGHT = 5     
RANGE_MIN = 5       
RANGE_MAX = 60      

# OPTIONALER RAUSCH-FILTER
# Das Pine Script hat diesen Filter NICHT (0%). 
# Wenn du doch zu viele "Mini-Divergenzen" bekommst, setze hier 0.05 oder 0.1.
MIN_PRICE_DIFF_PERCENT = 0.0  

# Cooldown
SIGNAL_COOLDOWN = 180 
last_signal_time = {}

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def get_binance_data(symbol, limit=150):
    try:
        exchange = ccxt.binance({'options': {'defaultType': 'future'}})
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=limit)
        if not ohlcv: return None

        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['close'] = df['close'].astype(float)
        df['high'] = df['high'].astype(float) # WICHTIG für Divergenz Check
        return df
    except Exception as e:
        log(f"Data Error {symbol}: {e}")
        return None

def calculate_indicators(df):
    # PINE: osc = ta.rsi(src, len) -> src ist close
    df['rsi'] = df.ta.rsi(length=RSI_PERIOD, close=df['close'])
    return df

# --- LOGIK 1: RSI CROSS DOWN ---
def check_rsi_cross_down(df, symbol):
    if len(df) < 3: return None
    rsi_now      = df['rsi'].iloc[-1]
    rsi_last     = df['rsi'].iloc[-2]
    rsi_pre_last = df['rsi'].iloc[-3]
    
    # 1. Cross in letzter geschlossener Kerze
    if rsi_pre_last >= RSI_OVERBOUGHT and rsi_last < RSI_OVERBOUGHT:
        return f"RSI Cross Down (Closed: {rsi_pre_last:.1f} -> {rsi_last:.1f})"
    # 2. Cross Live
    if rsi_last >= RSI_OVERBOUGHT and rsi_now < RSI_OVERBOUGHT:
        return f"RSI Cross Down (Live: {rsi_last:.1f} -> {rsi_now:.1f})"
    return None

# --- LOGIK 2: BEARISH DIVERGENCE (PINE SCRIPT LOGIC) ---
def is_pivot_high_rsi(df, index, left, right):
    """
    Prüft ob df['rsi'][index] ein Pivot High ist.
    Entspricht: phFound = na(ta.pivothigh(osc, lbL, lbR)) ? false : true
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

def check_bearish_divergence(df):
    if len(df) < RANGE_MAX + PIVOT_RIGHT + 5: return None

    # PINE: Wir prüfen "jetzt", ob vor 'lbR' Bars ein Pivot war.
    # Da Python 0-basiert ist und wir von rechts schauen:
    curr_idx = len(df) - 1 - PIVOT_RIGHT 
    
    # 1. Ist 'curr_idx' ein RSI Pivot High?
    if not is_pivot_high_rsi(df, curr_idx, PIVOT_LEFT, PIVOT_RIGHT):
        return None

    # P1 gefunden (Aktueller Pivot)
    p1_rsi = df['rsi'].iloc[curr_idx]
    p1_high = df['high'].iloc[curr_idx] # PINE: priceHH nutzt HIGH, nicht Close!

    # 2. Suche den LETZTEN Pivot High (P0)
    # PINE LOGIK 'valuewhen(phFound, ..., 1)': Das bedeutet, wir nehmen den
    # UNMITTELBAREN Vorgänger-Pivot. Wir überspringen keinen!
    
    start_search = curr_idx - RANGE_MIN
    end_search = max(0, curr_idx - RANGE_MAX)
    
    found_prev_pivot = False
    p0_idx = -1

    for prev_idx in range(start_search, end_search, -1):
        if is_pivot_high_rsi(df, prev_idx, PIVOT_LEFT, PIVOT_RIGHT):
            p0_idx = prev_idx
            found_prev_pivot = True
            break # WICHTIG: Wir stoppen beim ERSTEN gefundenen Pivot (wie TradingView)
    
    if not found_prev_pivot:
        return None

    # P0 gefunden (Vorheriger Pivot)
    p0_rsi = df['rsi'].iloc[p0_idx]
    p0_high = df['high'].iloc[p0_idx]

    # --- DIVERGENZ PRÜFUNG ---
    # PINE: oscLH = osc[lbR] < valuewhen(...) -> P1 RSI < P0 RSI
    # PINE: priceHH = high[lbR] > valuewhen(...) -> P1 High > P0 High
    
    rsi_lower_high = p1_rsi < p0_rsi
    price_higher_high = p1_high > p0_high
    
    # Optionaler Rausch-Filter (Standard 0.0%)
    if MIN_PRICE_DIFF_PERCENT > 0:
         if p1_high < (p0_high * (1 + MIN_PRICE_DIFF_PERCENT/100)):
             return None

    if price_higher_high and rsi_lower_high:
        return f"Bearish Div (TV Style): High {p1_high:.4f} > {p0_high:.4f} | RSI {p1_rsi:.1f} < {p0_rsi:.1f}"

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

# --- MAIN LOOP ---
if __name__ == "__main__":
    log(f"TV Scanner gestartet (1m). Logic: RSI Cross < {RSI_OVERBOUGHT} OR TV Divergence.")
    
    while True:
        for symbol in WATCHLIST:
            last_time = last_signal_time.get(symbol, 0)
            if (time.time() - last_time) < SIGNAL_COOLDOWN:
                continue 

            df = get_binance_data(symbol, limit=150)
            if df is not None:
                df = calculate_indicators(df)
                signal_reason = None
                
                # Check A: RSI Cross
                rsi_signal = check_rsi_cross_down(df, symbol)
                if rsi_signal: signal_reason = rsi_signal
                
                # Check B: TV Divergence (wenn kein Cross)
                if not signal_reason:
                    div_signal = check_bearish_divergence(df)
                    if div_signal: signal_reason = div_signal
                
                if signal_reason:
                    send_webhook(symbol, signal_reason)
                    last_signal_time[symbol] = time.time()
            time.sleep(0.5) 
        time.sleep(CHECK_INTERVAL)