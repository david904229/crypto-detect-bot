import ccxt
import pandas as pd
import requests
import time
from datetime import datetime
import os

# ================= 參數設定區 =================

MAX_RUNTIME = 5.5 * 60 * 60

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# 2. 監控設定
TOP_COIN_LIMIT = 2000

# ========= 【修改】改為多時區列表 =========
# 機器人會依序檢查這些級別
TIMEFRAME_LIST = ['30m', '1h', '2h', '4h'] 

TIMEFRAME_ENTRY = '5m'  # 小級別 (進場 + 結構)

# 3. 大級別指標參數 (Vegas + RSI)
RSI_LENGTH = 14
RSI_OVERBOUGHT_HEIGHT = 90
RSI_OVERBOUGHT_LOW = 65
RSI_OVERSOLD = 35 
VEGAS_EMA_SHORT = 144
VEGAS_EMA_LONG = 169
VEGAS_TOLERANCE = 0.02  # 2% 容許誤差

# 4. [新增] 斐波那契設定
# 程式會自動抓取過去 N 根K線的高低點來畫斐波
FIB_LOOKBACK = 300
FIB_TOLERANCE = 0.02 # 2% 容許誤差 (通道跟Fib價位的距離)
FIB_LEVELS = [0.382, 0.5, 0.618, 0.786, 1.0, 1.13, 1.272, 1.414]

# 5. 5m 進場參數
ENTRY_EMA = 12
CHOCH_LOOKBACK = 50  # 5m 回看 50 根 K 線找高低點

MIN_VOLUME_MILLION = 0.5  # 最小成交額 (單位: 百萬美金)，低於此數不掃描

# ========= 通知冷卻設定 =========
global alert_history
alert_history = {}  # 用來記錄上次通知時間的字典

TF_MAP = {
    '1m': 60,
    '5m': 300,
    '15m': 900,
    '30m': 1800,
    '1h': 3600,
    '2h': 7200,
    '4h': 14400,
    '6h': 21600,
    '12h': 43200,
    '1d': 86400,
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

def get_top_usdt_pairs(exchange, limit=TOP_COIN_LIMIT):
    print(f"正在獲取市場數據 (前 {limit} 名)...")
    try:
        tickers = exchange.fetch_tickers()
        
        # 1. 基本篩選：USDT 結尾，排除槓桿代幣
        valid_tickers = [
            t for t in tickers.values() 
            if t['symbol'].endswith('/USDT') 
            and 'UP/' not in t['symbol'] 
            and 'DOWN/' not in t['symbol']
        ]
        
        # 2. 排序：依照成交額 (quoteVolume) 由大到小
        sorted_tickers = sorted(valid_tickers, key=lambda x: x['quoteVolume'], reverse=True)
        
        # 3. 取前 N 名
        top_n = sorted_tickers[:limit]
        
        # 4. [重要] 二次過濾：剔除成交額太低的 (避免流動性風險)
        final_symbols = []
        for t in top_n:
            # quoteVolume 單位通常是 USDT
            vol_in_million = t['quoteVolume'] / 1000000 
            if vol_in_million >= MIN_VOLUME_MILLION:
                final_symbols.append(t['symbol'])
        
        print(f"篩選後剩餘: {len(final_symbols)} 個幣種 (成交額 > {MIN_VOLUME_MILLION}M)")
        return final_symbols
        
    except Exception as e:
        print(f"獲取失敗: {e}")
        return ['BTC/USDT', 'ETH/USDT']

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
    """檢測結構破壞 (這裡將用於 5m 數據)"""
    # 確保資料夠多
    if len(df) < CHOCH_LOOKBACK + 2:
        return None, 0
        
    recent_data = df.iloc[-CHOCH_LOOKBACK:-1] 
    last_swing_high = recent_data['high'].max()
    last_swing_low = recent_data['low'].min()
    current_close = df.iloc[-1]['close']
    
    if current_close > last_swing_high:
        return "Bullish", last_swing_high
    elif current_close < last_swing_low:
        return "Bearish", last_swing_low
    return None, 0

# --- [新增] 斐波那契共振檢查 ---
def check_fib_confluence(df, tunnel_price):
    """
    檢查維加斯通道價格是否剛好落在某個斐波那契回撤位上
    """
    if len(df) < FIB_LOOKBACK: return None
    
    # 抓取這段期間內的最高與最低 (作為波段結構)
    recent_data = df.iloc[-FIB_LOOKBACK:]
    high_p = recent_data['high'].max()
    low_p = recent_data['low'].min()
    price_range = high_p - low_p
    
    if price_range == 0: return None
    
    matched_levels = []
    
    for level in FIB_LEVELS:
        # 計算斐波價位 (從低點往上算)
        fib_price = low_p + (price_range * level)
        
        # 檢查通道價格是否跟這個斐波價位很接近
        dist = abs(tunnel_price - fib_price) / fib_price
        
        if dist <= FIB_TOLERANCE:
            matched_levels.append(str(level))
            
    if matched_levels:
        return ", ".join(matched_levels)
    return None

def analyze_symbol(exchange, symbol):
    global alert_history
    
    # ========= 【迴圈】依序檢查每個時間級別 =========
    for tf in TIMEFRAME_LIST:
        
        # 1. 檢查 大級別 (過濾器)
        df_main = get_market_data(exchange, symbol, tf, limit=FIB_LOOKBACK)
        if df_main is None or len(df_main) < 200: continue

        close_main = df_main['close']
        rsi_val = calc_rsi(close_main, RSI_LENGTH).iloc[-1]
        ema144 = calc_ema(close_main, VEGAS_EMA_SHORT).iloc[-1]
        ema169 = calc_ema(close_main, VEGAS_EMA_LONG).iloc[-1]
        price_current = close_main.iloc[-1]

        # A. RSI 過濾
        is_rsi_buy = rsi_val <= RSI_OVERSOLD
        is_rsi_sell = rsi_val >= RSI_OVERBOUGHT_LOW and rsi_val <= RSI_OVERBOUGHT_HEIGHT
        if not (is_rsi_buy or is_rsi_sell): continue # 如果 RSI 沒訊號，直接換下一個時區

        # B. 通道過濾
        tunnel_max = max(ema144, ema169)
        tunnel_min = min(ema144, ema169)
        dist_max = abs(price_current - tunnel_max) / price_current
        dist_min = abs(price_current - tunnel_min) / price_current
        
        is_near_tunnel = (tunnel_min <= price_current <= tunnel_max) or \
                        (dist_max <= VEGAS_TOLERANCE) or \
                        (dist_min <= VEGAS_TOLERANCE)

        if not is_near_tunnel: continue
        
        # 2. 判斷是否「已經穿越」(Valid Check)
        valid_for_long = price_current >= tunnel_min
        valid_for_short = price_current <= tunnel_max

        if is_rsi_buy and not valid_for_long: continue
        if is_rsi_sell and not valid_for_short: continue

        # ================= 2. 檢查 5m 小級別 (進場訊號) =================
        # 為了避免 API 太頻繁，小睡一下
        time.sleep(0.05)
        
        df_5m = get_market_data(exchange, symbol, TIMEFRAME_ENTRY, limit=100)
        if df_5m is None: continue

        ema12_5m = calc_ema(df_5m['close'], ENTRY_EMA).iloc[-1]
        price_5m_close = df_5m['close'].iloc[-1]

        # C. 5m EMA 12 進場確認
        signal_long = is_rsi_buy and valid_for_long and (price_5m_close > ema12_5m)
        signal_short = is_rsi_sell and valid_for_short and (price_5m_close < ema12_5m)
        # ================= 發送通知 =================

        if signal_long or signal_short:
            
            # 組合鍵值: "BTC/USDT_30m" 或 "ETH/USDT_4h"
            alert_key = f"{symbol}_{tf}"
            current_time = time.time()
            
            # 根據目前的 tf 取得對應的冷卻秒數
            cooldown_seconds = TF_MAP.get(tf, 3600)

            # 檢查冷卻
            if alert_key in alert_history:
                last_alert_time = alert_history[alert_key]
                if current_time - last_alert_time < cooldown_seconds:
                    continue # 還在冷卻，跳過
            
            # 更新記錄
            alert_history[alert_key] = current_time
            # =================================================
            
            # D. 檢查 5m CHOCH (使用 df_5m)
            choch_type, choch_level = check_choch(df_5m)
            
            # 使用通道的中間價 (EMA144+EMA169)/2 來跟 Fib 比對
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
            msg += f"1️⃣ <b>{tf} 環境</b> (Trend):\n"
            msg += f"   • Vegas: ✅ 通道附近/未破\n"
            if fib_confluence:
                msg += f"   • Fib共振: 🔥 <b>{fib_confluence}</b>\n"
            msg += f"   • RSI: {rsi_val:.2f} (極值)\n"
            msg += f"2️⃣ <b>{TIMEFRAME_ENTRY} 進場</b> (Entry):\n"
            msg += f"   • EMA 12: ✅ 確認站上/跌破\n"
            
            # 顯示 5m 結構狀態
            if choch_type:
                # 如果方向一致，加個 🔥
                is_confluence = (signal_long and "Bullish" in choch_type) or (signal_short and "Bearish" in choch_type)
                icon = "🔥" if is_confluence else "⚡"
                msg += f"   • CHOCH: {icon} {choch_type}\n"
            else:
                msg += f"   • CHOCH: 無明顯結構破壞\n"
            
            send_telegram_msg(msg)

def main():
    exchange = ccxt.binance()
    
    init_target_symbols = get_top_usdt_pairs(exchange, limit=TOP_COIN_LIMIT)
    start_msg = f"🚀 <b>大規模監控啟動 (多時區版)</b>\n"
    start_msg += f"範圍: {len(init_target_symbols)} 個幣種\n"
    start_msg += f"時區: {TIMEFRAME_LIST}\n"
    start_msg += f"進場: {TIMEFRAME_ENTRY} EMA12"
    print(start_msg)
    send_telegram_msg(start_msg)
    start_time = time.time()

    while True:
        if time.time() - start_time > MAX_RUNTIME:
            print("⏰ 達到最大執行時間，準備結束（讓 GitHub Actions 接力）")
            break
        try:
            # 紀錄開始時間
            loop_start = time.time()
            
            print("\n正在更新熱門幣種清單...")
            target_symbols = get_top_usdt_pairs(exchange, limit=TOP_COIN_LIMIT)
            
            total = len(target_symbols)
            for i, symbol in enumerate(target_symbols):
                # 顯示進度條
                print(f"[{i+1}/{total}] 掃描: {symbol} ...", end='\r')
                
                analyze_symbol(exchange, symbol)
                
                # 因為每個幣要掃 4 個時區，稍微減少一點休息時間
                time.sleep(0.1) 
            
            # 計算跑一輪花了多久
            duration = time.time() - loop_start
            print(f"\n--- 本輪耗時 {int(duration)} 秒 ---")
            
            # 動態調整休息時間
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