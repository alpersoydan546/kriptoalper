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

# --- [ PIRANHA v19.8 - BTC TREND FILTER ] ---
# Sorun: BTC trendine karşı açılan işlemler stop oluyor, çok fazla sinyal spamı var.
# Çözüm: 1H BTC Trend filtresi eklendi. Ana trende zıt işlem açılması yasaklandı.

TIMEFRAME = '5m'
LOOKBACK = 50
ADX_MAX_THRESHOLD = 25
ADX_MIN_THRESHOLD = 15     
WICK_RATIO = 2.5           
RISK_REWARD = 1.5
CONFIDENCE_THRESHOLD = 80  

SCAN_INTERVAL = 30
MAX_DAILY_SIGNALS = 15     
TIME_LIMIT_CANDLES = 20
COIN_COOLDOWN = 3600
TOP_COUNT = 15             

DEFAULT_TOKEN = "8498989500:AAGmk-2OBpal04K4i6ZMk6YaYNC79Fa_xac"
DEFAULT_CHAT_ID = "8120732989"
TELEGRAM_TOKEN = DEFAULT_TOKEN
TELEGRAM_CHAT_ID = DEFAULT_CHAT_ID

STATS_FILE = "daily_stats_render.json"
TRADES_FILE = "active_trades_render.json"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [PIRANHA] - %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger()

try:
    exchange = ccxt.binance({
        'rateLimit': 1200,
        'enableRateLimit': True,
        'options': {'defaultType': 'future'}
    })
except Exception as e:
    logger.error(f"Borsa Hatası: {e}")

app = Flask(__name__)
lock = threading.Lock()

@app.route('/')
def home(): 
    return "☁️ PIRANHA v19.8 ONLINE"

def run_flask():
    try:
        port = int(os.environ.get("PORT", 10000))
        app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
    except Exception: 
        pass

def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}
        requests.post(url, data=data, timeout=10)
    except Exception as e: 
        logger.error(f"Telegram Hatası: {e}")

def load_json(filename):
    with lock:
        if not os.path.exists(filename): 
            return {}
        try:
            with open(filename, 'r') as f: 
                return json.load(f)
        except Exception: 
            return {}

def save_json(filename, data):
    with lock:
        try:
            with open(filename, 'w') as f: 
                json.dump(data, f, indent=4)
        except Exception: 
            pass

def update_stats(result, pnl):
    stats = load_json(STATS_FILE)
    today = datetime.now().strftime("%Y-%m-%d")
    if stats.get("date") != today:
        stats = {"date": today, "win": 0, "loss": 0, "timeout": 0, "pnl": 0.0, "daily_signals": 0, "last_signals": {}}
    
    if result == "WIN": stats["win"] += 1
    elif result == "LOSS": stats["loss"] += 1
    elif result == "TIMEOUT": stats.setdefault("timeout", 0); stats["timeout"] += 1
    
    stats["pnl"] += pnl
    save_json(STATS_FILE, stats)

def check_cooldown(symbol, stats):
    last_signals = stats.get("last_signals", {})
    if symbol in last_signals:
        if time.time() - last_signals[symbol] < COIN_COOLDOWN:
            return True
    return False

# YENİ: 1 Saatlik (1H) Ana Trend Kontrolü
def get_btc_macro_trend():
    try:
        bars = exchange.fetch_ohlcv('BTC/USDT', timeframe='1h', limit=250)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        ema50 = ta.ema(df['close'], length=50).iloc[-1]
        ema200 = ta.ema(df['close'], length=200).iloc[-1]
        
        if ema50 > ema200: 
            return "BULLISH" # BTC Yükselişte
        elif ema50 < ema200: 
            return "BEARISH" # BTC Düşüşte
        return "NEUTRAL"
    except Exception: 
        return "NEUTRAL"

def check_active_trades():
    try:
        trades = load_json(TRADES_FILE)
        if not trades: 
            return

        updated_trades = trades.copy()
        trades_changed = False
        current_time = time.time()
        
        try:
            trade_symbols = list(trades.keys())
            all_tickers = exchange.fetch_tickers(trade_symbols)
        except Exception as e:
            logger.error(f"Toplu fiyat çekilemedi: {e}")
            return

        for symbol, trade in trades.items():
            try:
                if symbol not in all_tickers: 
                    continue
                current_price = float(all_tickers[symbol]['last'])
                symbol_short = symbol.replace('/USDT', '')
                
                entry_price = trade.get('price', trade.get('entry')) 
                if not entry_price: 
                    continue

                pnl_real = (current_price - entry_price) / entry_price * 100
                if trade.get('signal') == "SHORT": 
                    pnl_real = -pnl_real

                result_type = None
                msg = ""

                if (current_time - trade.get('entry_time', current_time)) > (TIME_LIMIT_CANDLES * 5 * 60):
                    result_type = "TIMEOUT"
                    emoji = "✅" if pnl_real > 0 else "⚠️"
                    msg = (f"☁️ {symbol_short}\n"
                           f"⏱️ Zaman Doldu (Exit)\n"
                           f"{emoji} %{pnl_real:.2f}\n"
                           f"✨ Piranha")

                elif (trade.get('signal') == "LONG" and current_price >= trade.get('tp', 999999)) or \
                     (trade.get('signal') == "SHORT" and current_price <= trade.get('tp', 0)):
                    result_type = "WIN"
                    msg = (f"☁️ {symbol_short}\n"
                           f"💎 Hedef Tamam\n"
                           f"💰 %{abs(pnl_real):.2f}\n"
                           f"✨ Piranha")

                elif (trade.get('signal') == "LONG" and current_price <= trade.get('sl', 0)) or \
                     (trade.get('signal') == "SHORT" and current_price >= trade.get('sl', 999999)):
                    result_type = "LOSS"
                    msg = (f"☁️ {symbol_short}\n"
                           f"❌ Stop\n"
                           f"📉 -%{abs(pnl_real):.2f}\n"
                           f"✨ Piranha")

                if result_type:
                    send_telegram(msg)
                    update_stats(result_type, pnl_real)
                    del updated_trades[symbol]
                    trades_changed = True
                    logger.info(f"İşlem Sonucu: {symbol} -> {result_type}")

            except Exception as e:
                continue
        
        if trades_changed:
            save_json(TRADES_FILE, updated_trades)

    except Exception as e:
        pass

def analyze_scalp(symbol, macro_trend):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=60)
        if not bars or len(bars) < 50: 
            return None
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        adx = df.ta.adx(length=14)
        if adx is None or adx.empty: return None 
        
        current_adx = adx['ADX_14'].iloc[-1]
        
        if current_adx > ADX_MAX_THRESHOLD or current_adx < ADX_MIN_THRESHOLD: 
            return None 

        row = df.iloc[-1]
        body = abs(row['close'] - row['open'])
        wick_len = body * WICK_RATIO
        
        signal = "NEUTRAL"
        upper_wick = row['high'] - max(row['open'], row['close'])
        lower_wick = min(row['open'], row['close']) - row['low']

        # YENİ: Makro trend filtresi eklendi! BTC Düşüyorsa Long açmak YASAK.
        if lower_wick > wick_len and macro_trend != "BEARISH": signal = "LONG"
        elif upper_wick > wick_len and macro_trend != "BULLISH": signal = "SHORT"
            
        if signal == "NEUTRAL": 
            return None

        score = 40 
        avg_vol = df['volume'].rolling(20).mean().iloc[-1]
        
        if row['volume'] > (avg_vol * 2.0): 
            score += 25 
        
        if (signal == "LONG" and lower_wick > body * 3) or \
           (signal == "SHORT" and upper_wick > body * 3): score += 20
            
        rsi = df.ta.rsi(length=14).iloc[-1]
        
        if (signal == "LONG" and rsi < 35) or (signal == "SHORT" and rsi > 65): 
            score += 15

        if score < CONFIDENCE_THRESHOLD: 
            return None

        atr = df.ta.atr(length=14).iloc[-1]
        if signal == "LONG":
            sl = row['close'] - (atr * 1.5)
            tp = row['close'] + (atr * 1.5 * RISK_REWARD)
        else:
            sl = row['close'] + (atr * 1.5)
            tp = row['close'] - (atr * 1.5 * RISK_REWARD)

        return {"signal": signal, "score": score, "price": row['close'], "sl": sl, "tp": tp, "entry_time": time.time()}
    except Exception: 
        return None

def send_daily_report():
    try:
        stats = load_json(STATS_FILE)
        msg = (f"☁️ Piranha\n"
               f"🎯 {stats.get('win', 0)} Hedef\n"
               f"🛡️ {stats.get('loss', 0)} Stop\n"
               f"⏱️ {stats.get('timeout', 0)} Zaman Aşımı\n"
               f"💰 PNL: %{stats.get('pnl', 0.0):.2f}")
        send_telegram(msg)
        new_stats = {"date": datetime.now().strftime("%Y-%m-%d"), "win": 0, "loss": 0, "timeout": 0, "pnl": 0.0, "daily_signals": 0, "last_signals": stats.get("last_signals", {})}
        save_json(STATS_FILE, new_stats)
    except Exception:
        pass

def run(token=None, chat_id=None):
    global TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
    if token: TELEGRAM_TOKEN = token
    if chat_id: TELEGRAM_CHAT_ID = chat_id

    threading.Thread(target=run_flask, daemon=True).start()
    
    logger.info("☁️ PIRANHA v19.8 ONLINE (TREND-ALIGNED)")
    send_telegram("☁️ Piranha: Aktif (BTC Makro Trend Devrede)")
    
    last_report_day = datetime.now().day
    target_list = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]

    while True:
        try:
            check_active_trades()

            if int(time.time()) % 21600 == 0: 
                send_telegram("☁️ Piranha Online | ⚡")
            
            if datetime.now().day != last_report_day:
                send_daily_report()
                last_report_day = datetime.now().day

            try:
                if int(time.time()) % 600 == 0: 
                    tickers = exchange.fetch_tickers()
                    symbols = [s for s in tickers if "/USDT" in s and "quoteVolume" in tickers[s]]
                    symbols.sort(key=lambda x: tickers[x]['quoteVolume'], reverse=True)
                    target_list = symbols[:TOP_COUNT]
            except Exception: 
                pass

            stats = load_json(STATS_FILE)
            
            if stats.get("daily_signals", 0) >= MAX_DAILY_SIGNALS:
                time.sleep(300) 
                continue

            # YENİ: BTC Makro Trendi tur başında çekiliyor
            macro_trend = get_btc_macro_trend()

            for symbol in target_list:
                trades = load_json(TRADES_FILE)
                if symbol in trades: 
                    continue
                if check_cooldown(symbol, stats): 
                    continue
                
                result = analyze_scalp(symbol, macro_trend)
                
                if result:
                    symbol_short = symbol.replace("/USDT", "")
                    emoji = "🟢 LONG" if result['signal'] == "LONG" else "🔴 SHORT"
                    
                    msg = (f"☁️ {symbol_short} | 💎 %{result['score']} (Range)\n"
                           f"{emoji} (Liquidity Sweep)\n"
                           f"📍 {result['price']}\n"
                           f"🎯 {result['tp']:.4f}\n"
                           f"🛡️ {result['sl']:.4f}")
                    
                    send_telegram(msg)
                    logger.info(f"Sinyal: {symbol} Yön: {result['signal']}")
                    
                    trades[symbol] = result
                    save_json(TRADES_FILE, trades)
                    
                    stats["daily_signals"] = stats.get("daily_signals", 0) + 1
                    stats.setdefault("last_signals", {})
                    stats["last_signals"][symbol] = time.time()
                    save_json(STATS_FILE, stats)
                
                time.sleep(1)

            time.sleep(SCAN_INTERVAL)

        except Exception as e:
            logger.error(f"Ana Döngü Hatası: {e}")
            time.sleep(5)

if __name__ == "__main__":
    run()
