import ccxt
import pandas as pd
import requests
import time
from datetime import datetime
import os

# ================= 參數設定區 =================
MAX_RUNTIME = 5.5 * 60 * 60  # 5.5 小時

# 1. Telegram 設定
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# 2. 監控設定
TOP_COIN_LIMIT = 2000
TIMEFRAME_LIST = ['30m', '1h', '2h', '4h']  # 多時區列表
TIMEFRAME_ENTRY = '5m'  # 進場級別

# 3. 指標參數
RSI_LENGTH = 14
RSI_OVERBOUGHT = 65
RSI_OVERSOLD = 35 
VEGAS_EMA_SHORT = 144
VEGAS_EMA_LONG = 169
VEGAS_TOLERANCE = 0.015  # 1.5% (這是價格距離通道的誤差)

# 4. [修改] 斐波那契設定 (寬鬆版)
FIB_LOOKBACK = 300 
FIB_TOLERANCE = 0.015  # <--- 修改這裡：改為 1.5% (0.015)，只要接近就算符合
FIB_LEVELS = [0.382, 0.5, 0.618, 0.786, 1.0, 1.13, 1.272, 1.414]

# 5. 進場與其他參數
ENTRY_EMA = 12
CHOCH_LOOKBACK = 50 
MIN_VOLUME_MILLION = 0.5  

# ========= 通知冷卻設定 =========
global alert_history
alert_history = {}  
TF_MAP = {
    '1m': 60, '5m': 300, '15m': 900, '30m': 1800,
    '1h': 3600, '2h': 7200, '4h': 14400, '6h': 21600, '12h': 43200, '1d': 86400,
}
# ===========================================

def send_telegram_msg(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "parse_mode": "HTML"
    }
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print(f"Telegram 發送失敗: {e}")

# def get_top_usdt_pairs(exchange, limit=TOP_COIN_LIMIT):
#     print(f"正在獲取市場數據 (前 {limit} 名)...")
#     try:
#         tickers = exchange.fetch_tickers()
#         valid_tickers = [
#             t for t in tickers.values() 
#             if t['symbol'].endswith('/USDT') 
#             and 'UP/' not in t['symbol'] 
#             and 'DOWN/' not in t['symbol']
#         ]
#         sorted_tickers = sorted(valid_tickers, key=lambda x: x['quoteVolume'], reverse=True)
        
#         final_symbols = []
#         for t in sorted_tickers[:limit]:
#             vol_in_million = t['quoteVolume'] / 1000000 
#             if vol_in_million >= MIN_VOLUME_MILLION:
#                 final_symbols.append(t['symbol'])
        
#         print(f"篩選後剩餘: {len(final_symbols)} 個幣種 (成交額 > {MIN_VOLUME_MILLION}M)")
#         return final_symbols
#     except:
#         return ['BTC/USDT', 'ETH/USDT']

def get_top_usdt_pairs(exchange, limit=TOP_COIN_LIMIT):
    print(f"取得 USDT 交易對（最多 {limit} 個）...")

    try:
        exchange.load_markets()

        symbols = [
            s for s in exchange.symbols
            if s.endswith('/USDT')
            and 'UP/' not in s
            and 'DOWN/' not in s
            and ':' not in s          # 排除期貨
        ]

        symbols = symbols[:limit]

        print(f"實際掃描幣種數: {len(symbols)}")
        return symbols

    except Exception as e:
        print(f"[嚴重錯誤] load_markets 失敗: {e}")

        # 極端保底（但給多一點）
        return [
            'BTC/USDT','ETH/USDT','BNB/USDT','SOL/USDT','XRP/USDT',
            'ADA/USDT','DOGE/USDT','AVAX/USDT','LINK/USDT','MATIC/USDT'
        ]

def get_market_data(exchange, symbol, timeframe, limit=300):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        return df
    except:
        return None

# --- 指標計算 ---

def calc_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).fillna(0)
    loss = (-delta.where(delta < 0, 0)).fillna(0)
    avg_gain = gain.ewm(com=period-1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period-1, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calc_ema(series, span):
    return series.ewm(span=span, adjust=False).mean()

def check_choch(df):
    if len(df) < CHOCH_LOOKBACK + 2: return None, 0
    recent_data = df.iloc[-CHOCH_LOOKBACK:-1] 
    last_swing_high = recent_data['high'].max()
    last_swing_low = recent_data['low'].min()
    current_close = df.iloc[-1]['close']
    
    if current_close > last_swing_high:
        return "Bullish", last_swing_high
    elif current_close < last_swing_low:
        return "Bearish", last_swing_low
    return None, 0

# --- 斐波那契共振檢查 (寬鬆版) ---
def check_fib_confluence(df, tunnel_price):
    """
    檢查維加斯通道價格是否接近某個斐波那契回撤位
    """
    if len(df) < FIB_LOOKBACK: return None
    
    # 抓取這段期間內的最高與最低
    recent_data = df.iloc[-FIB_LOOKBACK:]
    high_p = recent_data['high'].max()
    low_p = recent_data['low'].min()
    price_range = high_p - low_p
    
    if price_range == 0: return None
    
    matched_levels = []
    
    for level in FIB_LEVELS:
        # 計算斐波價位 (從低點往上算)
        fib_price = low_p + (price_range * level)
        
        # 計算通道價格與斐波價位的差距比例
        dist = abs(tunnel_price - fib_price) / fib_price
        
        # 只要差距小於設定的寬容度 (例如 2%)，就視為接近
        if dist <= FIB_TOLERANCE:
            matched_levels.append(str(level))
            
    if matched_levels:
        return ", ".join(matched_levels)
    return None

def analyze_symbol(exchange, symbol):
    global alert_history  
    
    for tf in TIMEFRAME_LIST:
        
        # 1. 大級別數據 (使用 FIB_LOOKBACK 確保夠長)
        df_main = get_market_data(exchange, symbol, tf, limit=FIB_LOOKBACK) 
        if df_main is None or len(df_main) < 200: continue

        close_main = df_main['close']
        rsi_val = calc_rsi(close_main, RSI_LENGTH).iloc[-1]
        ema144 = calc_ema(close_main, VEGAS_EMA_SHORT).iloc[-1]
        ema169 = calc_ema(close_main, VEGAS_EMA_LONG).iloc[-1]
        price_current = close_main.iloc[-1]

        # A. RSI 過濾
        is_rsi_buy = rsi_val <= RSI_OVERSOLD
        is_rsi_sell = rsi_val >= RSI_OVERBOUGHT and rsi_val <= 95
        if not (is_rsi_buy or is_rsi_sell): continue 

        # B. 通道過濾
        tunnel_max = max(ema144, ema169)
        tunnel_min = min(ema144, ema169)
        dist_max = abs(price_current - tunnel_max) / price_current
        dist_min = abs(price_current - tunnel_min) / price_current
        
        is_near_tunnel = (tunnel_min <= price_current <= tunnel_max) or \
                        (dist_max <= VEGAS_TOLERANCE) or \
                        (dist_min <= VEGAS_TOLERANCE)

        if not is_near_tunnel: continue
        
        # C. 穿越防護
        valid_for_long = price_current >= tunnel_min
        valid_for_short = price_current <= tunnel_max

        if is_rsi_buy and not valid_for_long: continue
        if is_rsi_sell and not valid_for_short: continue

        # ================= 2. 檢查 5m 進場 =================
        time.sleep(0.05)
        df_5m = get_market_data(exchange, symbol, TIMEFRAME_ENTRY, limit=100)
        if df_5m is None: continue

        ema12_5m = calc_ema(df_5m['close'], ENTRY_EMA).iloc[-1]
        price_5m_close = df_5m['close'].iloc[-1]

        # 進場訊號
        signal_long = is_rsi_buy and valid_for_long and (price_5m_close > ema12_5m)
        signal_short = is_rsi_sell and valid_for_short and (price_5m_close < ema12_5m)

        if signal_long or signal_short:
            
            # 冷卻檢查
            alert_key = f"{symbol}_{tf}"
            current_time = time.time()
            cooldown_seconds = TF_MAP.get(tf, 3600)

            if alert_key in alert_history:
                last_alert_time = alert_history[alert_key]
                if current_time - last_alert_time < cooldown_seconds:
                    continue 
            
            alert_history[alert_key] = current_time
            
            # D. 計算附加資訊
            choch_type, choch_level = check_choch(df_5m)
            
            # 計算斐波那契共振 (取通道均價比對)
            tunnel_avg = (ema144 + ema169) / 2
            fib_confluence = check_fib_confluence(df_main, tunnel_avg)

            signal_type = "📈 多頭進場 (Long)" if signal_long else "📉 空頭進場 (Short)"
            emoji = "🟢" if signal_long else "🔴"
            
            print(f"\n[觸發] {symbol} ({tf}) {signal_type}")
            
            msg = f"{emoji} <b>{signal_type}</b>\n"
            msg += f"幣種: <b>{symbol}</b>\n"
            msg += f"時區: <b>{tf}</b>\n"
            msg += f"💰 現價: {price_current}\n"
            msg += "--------------------------\n"
            msg += f"1️⃣ <b>{tf} 結構</b>:\n"
            msg += f"   • Vegas: ✅ 通道有效\n"
            
            # 顯示 Fib 共振 (接近就算)
            if fib_confluence:
                msg += f"   • Fib共振: 🔥 <b>{fib_confluence}</b> (接近)\n"
            
            msg += f"   • RSI: {rsi_val:.2f}\n"
            msg += f"2️⃣ <b>{TIMEFRAME_ENTRY} 進場</b>:\n"
            msg += f"   • EMA 12: ✅ 站穩/跌破\n"
            
            if choch_type:
                is_confluence = (signal_long and "Bullish" in choch_type) or (signal_short and "Bearish" in choch_type)
                icon = "🔥" if is_confluence else "⚡"
                msg += f"   • CHOCH: {icon} {choch_type}\n"
            
            send_telegram_msg(msg)

def main():
    exchange = ccxt.binance()
    
    init_target_symbols = get_top_usdt_pairs(exchange, limit=TOP_COIN_LIMIT)
    start_msg = f"🚀 <b>Crypto Monitor (Vegas + Fib)</b>\n"
    start_msg += f"範圍: {len(init_target_symbols)} 幣種\n"
    start_msg += f"時區: {TIMEFRAME_LIST}\n"
    start_msg += f"Fib誤差: {int(FIB_TOLERANCE*100)}%"
    print(start_msg)
    send_telegram_msg(start_msg)
    start_time = time.time()

    while True:
        if time.time() - start_time > MAX_RUNTIME:
            print("⏰ 達到最大執行時間，準備結束（讓 GitHub Actions 接力）")
            break
        try:
            loop_start = time.time()
            
            print("\n正在更新熱門幣種清單...")
            target_symbols = get_top_usdt_pairs(exchange, limit=TOP_COIN_LIMIT)
            
            total = len(target_symbols)
            for i, symbol in enumerate(target_symbols):
                print(f"[{i+1}/{total}] 掃描: {symbol} ...", end='\r')
                analyze_symbol(exchange, symbol)
                time.sleep(0.2) 
            
            duration = time.time() - loop_start
            print(f"\n--- 本輪耗時 {int(duration)} 秒 ---")
            
            sleep_time = max(60, 300 - int(duration))
            print(f"休眠 {sleep_time} 秒...")
            time.sleep(sleep_time)
            
        except KeyboardInterrupt:
            print("停止")
            break
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()