import ccxt
import pandas as pd
import pandas_ta as ta
import time
import requests
import logging
import json
import os
import threading
from datetime import datetime
from flask import Flask

# --- [ PIRANHA v17.2 - RAPOR DÜZELTİLDİ ] ---
TIMEFRAME = '5m'
LOOKBACK = 50
ADX_MAX_THRESHOLD = 20     # Sadece yatay piyasa
WICK_RATIO = 2.5           # Belirgin iğne
RISK_REWARD = 1.5
CONFIDENCE_THRESHOLD = 75  # Yüksek güven

# Limitler
SCAN_INTERVAL = 20
MAX_DAILY_SIGNALS = 6
TIME_LIMIT_CANDLES = 20
COIN_COOLDOWN = 10800

# Dosya İsimleri
STATS_FILE = "daily_stats_render.json"
TRADES_FILE = "active_trades_render.json"
TOP_COUNT = 50
CACHE_REFRESH = 900

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger()

exchange = ccxt.binance({
    'rateLimit': 1200,
    'enableRateLimit': True,
    'options': {'defaultType': 'future'}
})

app = Flask(__name__)
lock = threading.Lock()

@app.route('/')
def home(): return "☁️ PIRANHA v17.2 ONLINE"

def run_flask():
    try:
        port = int(os.environ.get("PORT", 10000))
        app.run(host='0.0.0.0', port=port)
    except: pass

def send_telegram(token, chat_id, message):
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
        requests.post(url, data=data, timeout=10)
    except Exception as e: logger.error(f"Telegram Hatası: {e}")

# --- [ DOSYA YÖNETİMİ ] ---
def load_json(filename):
    with lock:
        try:
            if os.path.exists(filename):
                with open(filename, 'r') as f: return json.load(f)
            return {}
        except: return {}

def save_json(filename, data):
    with lock:
        try:
            with open(filename, 'w') as f: json.dump(data, f, indent=4)
        except: pass

def update_stats(result, pnl):
    stats = load_json(STATS_FILE)
    today = datetime.now().strftime("%Y-%m-%d")
    
    # Eğer tarih değişmişse ve henüz sıfırlanmamışsa sıfırla
    if stats.get("date") != today:
        stats = {
            "date": today, 
            "win": 0, "loss": 0, "timeout": 0, "pnl": 0.0, "daily_signals": 0,
            "last_signals": stats.get("last_signals", {})
        }
    
    if result == "WIN": stats["win"] += 1
    elif result == "LOSS": stats["loss"] += 1
    elif result == "TIMEOUT": stats.setdefault("timeout", 0); stats["timeout"] += 1
    
    stats["pnl"] += pnl
    save_json(STATS_FILE, stats)

# --- [ DÜZELTİLEN RAPOR FONKSİYONU ] ---
def send_daily_report(token, chat_id):
    stats = load_json(STATS_FILE)
    
    # 1. Mevcut (Dünün veya biten günün) raporunu gönder
    msg = (
        f"🌙 <b>GÜN SONU RAPORU</b>\n"
        f"📅 Tarih: {stats.get('date', 'Bilinmiyor')}\n"
        f"🎯 Hedef: {stats.get('win', 0)}\n"
        f"🛡️ Stop: {stats.get('loss', 0)}\n"
        f"⏱️ Zaman Aşımı: {stats.get('timeout', 0)}\n"
        f"💰 PNL: %{stats.get('pnl', 0.0):.2f}\n"
        f"✨ Piranha v17.2"
    )
    send_telegram(token, chat_id, msg)
    
    # 2. Raporu attıktan sonra yeni gün için dosyayı SIFIRLA
    today = datetime.now().strftime("%Y-%m-%d")
    new_stats = {
        "date": today,
        "win": 0, "loss": 0, "timeout": 0, "pnl": 0.0, "daily_signals": 0,
        "last_signals": stats.get("last_signals", {}) # Cooldownları koru
    }
    save_json(STATS_FILE, new_stats)

def check_cooldown(symbol, stats):
    last_signals = stats.get("last_signals", {})
    if symbol in last_signals:
        if time.time() - last_signals[symbol] < COIN_COOLDOWN:
            return True
    return False

# --- [ BEKÇİ MODÜLÜ ] ---
def monitor_trades_thread(token, chat_id):
    logger.info("🛡️ PIRANHA BEKÇİSİ AKTİF")
    while True:
        try:
            trades = load_json(TRADES_FILE)
            if not trades:
                time.sleep(5)
                continue

            updated_trades = trades.copy()
            trades_changed = False
            current_time = time.time()

            for symbol, trade in trades.items():
                try:
                    ticker = exchange.fetch_ticker(symbol)
                    current_price = ticker['last']
                    symbol_short = symbol.replace('/USDT', '')
                    
                    pnl_pct = abs((current_price - trade['entry']) / trade['entry']) * 100
                    
                    # 1. ZAMAN LİMİTİ
                    time_elapsed = current_time - trade.get('entry_time', current_time)
                    if time_elapsed > (TIME_LIMIT_CANDLES * 5 * 60):
                        pnl_real = (current_price - trade['entry']) / trade['entry'] * 100
                        if trade['signal'] == "SHORT": pnl_real = -pnl_real
                        
                        emoji = "✅" if pnl_real > 0 else "⚠️"
                        msg = (f"☁️ {symbol_short}\n"
                               f"⏱️ Zaman Doldu\n"
                               f"{emoji} %{pnl_real:.2f}\n"
                               f"✨ Piranha v17.2")
                        
                        send_telegram(token, chat_id, msg)
                        update_stats("TIMEOUT", pnl_real)
                        del updated_trades[symbol]
                        trades_changed = True
                        continue

                    # 2. KAR AL
                    is_tp = False
                    if trade['signal'] == "LONG" and current_price >= trade['tp']: is_tp = True
                    if trade['signal'] == "SHORT" and current_price <= trade['tp']: is_tp = True
                    
                    if is_tp:
                        msg = (f"☁️ {symbol_short}\n"
                               f"💎 Hedef Tamam\n"
                               f"💰 %{pnl_pct:.2f}\n"
                               f"✨ Piranha v17.2")
                        send_telegram(token, chat_id, msg)
                        update_stats("WIN", pnl_pct)
                        del updated_trades[symbol]
                        trades_changed = True
                        continue
                    
                    # 3. STOP OL
                    is_sl = False
                    if trade['signal'] == "LONG" and current_price <= trade['sl']: is_sl = True
                    if trade['signal'] == "SHORT" and current_price >= trade['sl']: is_sl = True

                    if is_sl:
                        msg = (f"☁️ {symbol_short}\n"
                               f"❌ Stop\n"
                               f"📉 -%{pnl_pct:.2f}\n"
                               f"✨ Piranha v17.2")
                        send_telegram(token, chat_id, msg)
                        update_stats("LOSS", -pnl_pct)
                        del updated_trades[symbol]
                        trades_changed = True
                        
                except: continue
            
            if trades_changed:
                save_json(TRADES_FILE, updated_trades)
        except: pass
        time.sleep(5)

def get_top_volume_symbols():
    try:
        tickers = exchange.fetch_tickers()
        usdt_tickers = [{'symbol': s, 'quoteVolume': float(v['quoteVolume'])} for s, v in tickers.items() if '/USDT' in s and 'quoteVolume' in v]
        sorted_tickers = sorted(usdt_tickers, key=lambda x: x['quoteVolume'], reverse=True)
        return [t['symbol'] for t in sorted_tickers[:TOP_COUNT]]
    except: 
        return ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT']

# --- [ STRATEJİ: AKILLI PUANLAMA ] ---
def analyze_scalp(symbol):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=100)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        if len(df) < 50: return None

        adx_df = ta.adx(df['high'], df['low'], df['close'], length=14)
        if adx_df is None or adx_df.empty: return None
        current_adx = adx_df['ADX_14'].iloc[-1]
        
        if current_adx > ADX_MAX_THRESHOLD: return None 
        
        past_50 = df[-51:-1] 
        range_high = past_50['high'].max()
        range_low = past_50['low'].min()
        
        current_candle = df.iloc[-1]
        current_price = current_candle['close']
        current_open = current_candle['open']
        current_high = current_candle['high']
        current_low = current_candle['low']
        current_vol = current_candle['volume']
        
        current_close = current_price - current_open
        body_size = abs(current_close)
        upper_wick = current_high - max(current_price, current_open)
        lower_wick = min(current_price, current_open) - current_low
        
        signal = "NEUTRAL"
        
        if current_high > range_high and current_price < range_high:
            if upper_wick > (body_size * WICK_RATIO):
                signal = "SHORT"
        elif current_low < range_low and current_price > range_low:
            if lower_wick > (body_size * WICK_RATIO):
                signal = "LONG"
                
        if signal == "NEUTRAL": return None

        # --- PUANLAMA ---
        score = 0
        
        # A) İĞNE GÜCÜ
        if (signal == "SHORT" and upper_wick > body_size * 3) or \
           (signal == "LONG" and lower_wick > body_size * 3):
            score += 40
        elif (signal == "SHORT" and upper_wick > body_size * 2.5) or \
             (signal == "LONG" and lower_wick > body_size * 2.5):
            score += 25
        else:
            score += 10

        # B) HACİM ANALİZİ
        avg_vol = df['volume'].rolling(20).mean().iloc[-1]
        if current_vol > (avg_vol * 3.5): score -= 20
        elif current_vol > (avg_vol * 1.5): score += 15
        else: score += 5

        # C) RSI
        rsi = ta.rsi(df['close'], length=14).iloc[-1]
        if signal == "SHORT":
            if rsi > 70: score += 30
            elif rsi > 60: score += 15
        if signal == "LONG":
            if rsi < 30: score += 30
            elif rsi < 40: score += 15

        # D) ADX
        if current_adx < 15: score += 15
        elif current_adx < 20: score += 5
        
        if score < CONFIDENCE_THRESHOLD: return None
        
        sl = 0; tp = 0
        range_buffer = current_price * 0.002
        
        if signal == "LONG":
            sl = range_low - range_buffer
            risk = current_price - sl
            tp = current_price + (risk * RISK_REWARD)
        elif signal == "SHORT":
            sl = range_high + range_buffer
            risk = sl - current_price
            tp = current_price - (risk * RISK_REWARD)
            
        return {
            "signal": signal, "score": score, "price": current_price,
            "sl": sl, "tp": tp, "entry_time": time.time()
        }

    except Exception as e: return None

# --- [ ANA DÖNGÜ ] ---
def run(token, chat_id):
    threading.Thread(target=monitor_trades_thread, args=(token, chat_id), daemon=True).start()
    threading.Thread(target=run_flask, daemon=True).start()

    logger.info("☁️ PIRANHA v17.2 ONLINE")
    send_telegram(token, chat_id, "☁️ Piranha v17.2: Rapor Düzeltildi & Akıllı Puan")
    
    last_heartbeat = time.time()
    last_cache_time = 0
    symbol_list = []
    
    # Başlangıçta last_report_date'i ayarla
    last_report_date = datetime.now().day

    while True:
        try:
            if time.time() - last_heartbeat > 21600:
                send_telegram(token, chat_id, "☁️ Piranha Online | ⚡")
                last_heartbeat = time.time()

            # --- GÜN SONU RAPORU TETİKLEYİCİSİ ---
            current_day = datetime.now().day
            if current_day != last_report_date:
                # Gün değişti! Raporu gönder.
                send_daily_report(token, chat_id)
                # Tarihi güncelle ki sürekli atmasın
                last_report_date = current_day

            if time.time() - last_cache_time > CACHE_REFRESH:
                symbol_list = get_top_volume_symbols()
                last_cache_time = time.time()

            trades = load_json(TRADES_FILE)
            stats = load_json(STATS_FILE)
            
            if stats.get("daily_signals", 0) >= MAX_DAILY_SIGNALS:
                time.sleep(300)
                continue

            for symbol in symbol_list:
                if symbol in trades: continue 
                if check_cooldown(symbol, stats): continue

                result = analyze_scalp(symbol)

                if result:
                    symbol_short = symbol.replace('/USDT', '')
                    emoji = "🟢 LONG" if result['signal'] == "LONG" else "🔴 SHORT"
                    
                    msg = (
