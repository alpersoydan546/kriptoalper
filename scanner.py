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

# --- [ PIRANHA v17.0 - LIQUIDITY HUNTER AYARLARI ] ---
# Strateji: Range Scalp (Yatay Piyasa Vur-Kaç)
TIMEFRAME = '5m'
LOOKBACK = 50              # Range tespiti için son 50 mum
ADX_MAX_THRESHOLD = 25     # 25 Üstü Trenddir, Piranha çalışmaz (Sniper'ın işi)
WICK_RATIO = 2.0           # İğne/Gövde oranı (Fakeout tespiti)
RISK_REWARD = 1.5          # Scalp için 1.5R yeterli
CONFIDENCE_THRESHOLD = 70  # 70 Puan altı girme

# Limitler
SCAN_INTERVAL = 20         # Hızlı tarama
MAX_DAILY_SIGNALS = 6      # Günde max 6 sinyal
TIME_LIMIT_CANDLES = 20    # 20 Mumda (100 dk) sonuç yoksa ÇIK
COIN_COOLDOWN = 10800      # Aynı coine 3 saat (10800 sn) küs

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
def home(): return "☁️ PIRANHA v17.0 LIQUIDITY ONLINE"

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
    if stats.get("date") != today:
        stats = {"date": today, "win": 0, "loss": 0, "timeout": 0, "pnl": 0.0, "daily_signals": 0}
    
    if result == "WIN": stats["win"] += 1
    elif result == "LOSS": stats["loss"] += 1
    elif result == "TIMEOUT": stats.setdefault("timeout", 0); stats["timeout"] += 1
    
    stats["pnl"] += pnl
    save_json(STATS_FILE, stats)

def send_daily_report(token, chat_id):
    stats = load_json(STATS_FILE)
    today = datetime.now().strftime("%Y-%m-%d")
    if stats.get("date") != today: return
    
    msg = (
        f"☁️ Piranha\n"
        f"🎯 {stats['win']} Hedef\n"
        f"🛡️ {stats['loss']} Stop\n"
        f"💰 %{stats['pnl']:.2f}"
    )
    send_telegram(token, chat_id, msg)

# --- [ COOLDOWN KONTROLÜ ] ---
def check_cooldown(symbol, stats):
    last_signals = stats.get("last_signals", {})
    if symbol in last_signals:
        if time.time() - last_signals[symbol] < COIN_COOLDOWN:
            return True
    return False

# --- [ BEKÇİ MODÜLÜ (ZAMAN LİMİTLİ) ] ---
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
                    
                    # 1. ZAMAN LİMİTİ KONTROLÜ (Scalp Beklemez!)
                    # 5m mum * 20 mum = 100 dakika (6000 saniye)
                    time_elapsed = current_time - trade.get('entry_time', current_time)
                    if time_elapsed > (TIME_LIMIT_CANDLES * 5 * 60):
                        # Piyasadan Çık
                        pnl_real = (current_price - trade['entry']) / trade['entry'] * 100
                        if trade['signal'] == "SHORT": pnl_real = -pnl_real
                        
                        emoji = "✅" if pnl_real > 0 else "⚠️"
                        msg = (f"☁️ {symbol_short}\n"
                               f"⏱️ Zaman Doldu (Exit)\n"
                               f"{emoji} %{pnl_real:.2f}\n"
                               f"✨ Piranha")
                        
                        send_telegram(token, chat_id, msg)
                        update_stats("TIMEOUT", pnl_real)
                        del updated_trades[symbol]
                        trades_changed = True
                        continue

                    # 2. KAR AL (TP)
                    is_tp = False
                    if trade['signal'] == "LONG" and current_price >= trade['tp']: is_tp = True
                    if trade['signal'] == "SHORT" and current_price <= trade['tp']: is_tp = True
                    
                    if is_tp:
                        msg = (f"☁️ {symbol_short}\n"
                               f"💎 Hedef Tamam\n"
                               f"💰 %{pnl_pct:.2f}\n"
                               f"✨ Piranha")
                        
                        send_telegram(token, chat_id, msg)
                        update_stats("WIN", pnl_pct)
                        del updated_trades[symbol]
                        trades_changed = True
                        continue
                    
                    # 3. STOP OL (SL)
                    is_sl = False
                    if trade['signal'] == "LONG" and current_price <= trade['sl']: is_sl = True
                    if trade['signal'] == "SHORT" and current_price >= trade['sl']: is_sl = True

                    if is_sl:
                        msg = (f"☁️ {symbol_short}\n"
                               f"❌ Stop\n"
                               f"📉 -%{pnl_pct:.2f}\n"
                               f"✨ Piranha")
                        
                        send_telegram(token, chat_id, msg)
                        update_stats("LOSS", -pnl_pct)
                        del updated_trades[symbol]
                        trades_changed = True
                        
                except: continue
            
            if trades_changed:
                save_json(TRADES_FILE, updated_trades)

        except: pass
        time.sleep(5)

# --- [ BEYİN: TOP 50 ] ---
def get_top_volume_symbols():
    try:
        tickers = exchange.fetch_tickers()
        usdt_tickers = [{'symbol': s, 'quoteVolume': float(v['quoteVolume'])} for s, v in tickers.items() if '/USDT' in s and 'quoteVolume' in v]
        sorted_tickers = sorted(usdt_tickers, key=lambda x: x['quoteVolume'], reverse=True)
        return [t['symbol'] for t in sorted_tickers[:TOP_COUNT]]
    except: 
        return ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT']

# --- [ STRATEJİ: LIQUIDITY SWEEP & RANGE SCALP ] ---
def analyze_scalp(symbol):
    try:
        # Veri Çek (50 mum Range + biraz fazlası indikatörler için)
        bars = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=100)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        if len(df) < 50: return None

        # 1. REJİM FİLTRESİ: Range Only (ADX < 25)
        adx_df = ta.adx(df['high'], df['low'], df['close'], length=14)
        if adx_df is None or adx_df.empty: return None
        current_adx = adx_df['ADX_14'].iloc[-1]
        
        if current_adx > ADX_MAX_THRESHOLD: return None # Trend var, Piranha girmez.
        
        # 2. RANGE TANIMLAMA (Son 50 mumun en yükseği/düşüğü)
        # Mevcut mumu dahil etmiyoruz ki "Sweep" (temizlik) olup olmadığını görelim
        past_50 = df[-51:-1] 
        range_high = past_50['high'].max()
        range_low = past_50['low'].min()
        
        current_candle = df.iloc[-1]
        current_price = current_candle['close']
        current_open = current_candle['open']
        current_high = current_candle['high']
        current_low = current_candle['low']
        current_vol = current_candle['volume']
        
        # 3. LIQUIDITY SWEEP (Wick Oranı Hesabı)
        body_size = abs(current_close := current_price - current_open)
        upper_wick = current_high - max(current_price, current_open)
        lower_wick = min(current_price, current_open) - current_low
        
        # Wick Ratio: İğne gövdeden en az 2 kat büyük olmalı
        # Ayrıca fiyat Range dışına iğne atmış ama içine kapanmış olmalı
        
        signal = "NEUTRAL"
        
        # --- SHORT SETUP (Range High Sweep) ---
        if current_high > range_high and current_price < range_high:
            if upper_wick > (body_size * WICK_RATIO):
                signal = "SHORT"
        
        # --- LONG SETUP (Range Low Sweep) ---
        elif current_low < range_low and current_price > range_low:
            if lower_wick > (body_size * WICK_RATIO):
                signal = "LONG"
                
        if signal == "NEUTRAL": return None

        # 4. PUANLAMA (Confidence)
        score = 0
        
        # Range Puanı (30): Fiyat range sınırına ne kadar yakın?
        score += 30 # Zaten sınırda olduğumuz için sinyal ürettik
        
        # Hacim Spike (20): Ortalama hacmin üstünde mi?
        avg_vol = df['volume'].rolling(20).mean().iloc[-1]
        if current_vol > (avg_vol * 1.2): score += 20
        
        # RSI Divergence Kontrolü (Basitleştirilmiş) (20)
        rsi = ta.rsi(df['close'], length=14).iloc[-1]
        if signal == "SHORT" and rsi > 60: score += 20 # Aşırı alım bölgesinden dönüş
        if signal == "LONG" and rsi < 40: score += 20  # Aşırı satım bölgesinden dönüş
        
        # Volatilite/ATR Puanı (15)
        atr = ta.atr(df['high'], df['low'], df['close'], length=14).iloc[-1]
        if atr > (current_price * 0.001): score += 15 # Ölü piyasa değil
        
        # ADX Puanı (15): ADX ne kadar düşükse o kadar iyi range
        if current_adx < 15: score += 15
        elif current_adx < 20: score += 10
        
        if score < CONFIDENCE_THRESHOLD: return None
        
        # 5. DİNAMİK TP/SL (Scalp)
        # SL: Range dışına %0.2 pay
        # TP: 1.5R (Risk Reward)
        
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
            "signal": signal,
            "score": score,
            "price": current_price,
            "sl": sl,
            "tp": tp,
            "entry_time": time.time()
        }

    except Exception as e: return None

# --- [ ANA DÖNGÜ ] ---
def run(token, chat_id):
    threading.Thread(target=monitor_trades_thread, args=(token, chat_id), daemon=True).start()
    threading.Thread(target=run_flask, daemon=True).start()

    logger.info("☁️ PIRANHA ONLINE (LIQUIDITY)")
    send_telegram(token, chat_id, "☁️ Piranha: Aktif")
    
    last_heartbeat = time.time()
    last_cache_time = 0
    symbol_list = []
    last_report_date = datetime.now().day

    while True:
        try:
            # Nabız: 6 SAAT
            if time.time() - last_heartbeat > 21600:
                send_telegram(token, chat_id, "☁️ Piranha Online | ⚡")
                last_heartbeat = time.time()

            # Gün Sonu Raporu
            if datetime.now().day != last_report_date:
                send_daily_report(token, chat_id)
                last_report_date = datetime.now().day

            # Liste Yenileme
            if time.time() - last_cache_time > CACHE_REFRESH:
                symbol_list = get_top_volume_symbols()
                last_cache_time = time.time()

            trades = load_json(TRADES_FILE)
            stats = load_json(STATS_FILE)
            
            # Günlük Limit
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
                    
                    # YENİ FORMAT (LİKİDİTE VURGULU)
                    msg = (f"☁️ {symbol_short} | 💎 %{result['score']} (Range)\n"
                           f"{emoji} (Liquidity Sweep)\n"
                           f"📍 {result['price']}\n"
                           f"🎯 {result['tp']:.4f}\n"
                           f"🛡️ {result['sl']:.4f}")
                    
                    send_telegram(token, chat_id, msg)
                    
                    trades[symbol] = {
                        "signal": result['signal'], 
                        "entry": result['price'], 
                        "tp": result['tp'], 
                        "sl": result['sl'],
                        "entry_time": result['entry_time']
                    }
                    save_json(TRADES_FILE, trades)
                    
                    # İstatistik ve Cooldown
                    stats.setdefault("daily_signals", 0)
                    stats["daily_signals"] += 1
                    stats.setdefault("last_signals", {})
                    stats["last_signals"][symbol] = time.time()
                    save_json(STATS_FILE, stats)
                    
                    time.sleep(1)

            time.sleep(SCAN_INTERVAL)

        except Exception as e:
            logger.error(f"Hata: {e}")
            time.sleep(10)

if __name__ == "__main__":
    MY_TOKEN = "8498989500:AAGmk-2OBpal04K4i6ZMk6YaYNC79Fa_xac"
    MY_ID = "8120732989"
    run(MY_TOKEN, MY_ID)
