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

# --- [ PIRANHA v19.4 - STEALTH MODE (API OPTİMİZASYONU) ] ---
# Sorun: 429 Too Many Requests (Binance IP Ban)
# Çözüm: BTC trendi tek sefere düşürüldü, Bekçi loop dışına alındı, delay eklendi.

# --- AYARLAR ---
TIMEFRAME = '5m'
LOOKBACK = 50
ADX_MAX_THRESHOLD = 25
WICK_RATIO = 2.0
RISK_REWARD = 1.5
CONFIDENCE_THRESHOLD = 80  

# --- LİMİTLER ---
SCAN_INTERVAL = 30             # 15 -> 30 sn yapıldı (API yormamak için ideal)
MAX_DAILY_SIGNALS = 9999
TIME_LIMIT_CANDLES = 20
COIN_COOLDOWN = 3600
TOP_COUNT = 60

# --- KİMLİK ---
DEFAULT_TOKEN = "8498989500:AAGmk-2OBpal04K4i6ZMk6YaYNC79Fa_xac"
DEFAULT_CHAT_ID = "8120732989"
TELEGRAM_TOKEN = DEFAULT_TOKEN
TELEGRAM_CHAT_ID = DEFAULT_CHAT_ID

# Dosyalar
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
def home(): return "☁️ PIRANHA v19.4 ONLINE"

def run_flask():
    try:
        port = int(os.environ.get("PORT", 10000))
        app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
    except: pass

def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}
        requests.post(url, data=data, timeout=10)
    except Exception as e: logger.error(f"Telegram Hatası: {e}")

def load_json(filename):
    with lock:
        if not os.path.exists(filename): return {}
        try:
            with open(filename, 'r') as f: return json.load(f)
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

# YENİ: Tek seferde BTC Trendi alır
def check_btc_correlation():
    try:
        btc = exchange.fetch_ohlcv('BTC/USDT', timeframe=TIMEFRAME, limit=2)
        if not btc: return "NEUTRAL"
        open_p = btc[-1][1]; close_p = btc[-1][4]
        change = (close_p - open_p) / open_p * 100
        if change < -0.2: return "DUMP"
        elif change > 0.2: return "PUMP"
        return "SAFE"
    except: return "SAFE"

def check_active_trades():
    try:
        trades = load_json(TRADES_FILE)
        if not trades: return

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
                if symbol not in all_tickers: continue
                current_price = float(all_tickers[symbol]['last'])
                symbol_short = symbol.replace('/USDT', '')
                
                pnl_real = (current_price - trade['entry']) / trade['entry'] * 100
                if trade['signal'] == "SHORT": pnl_real = -pnl_real

                result_type = None
                msg = ""

                if (current_time - trade['entry_time']) > (TIME_LIMIT_CANDLES * 5 * 60):
                    result_type = "TIMEOUT"
                    emoji = "✅" if pnl_real > 0 else "⚠️"
                    msg = (f"☁️ {symbol_short}\n"
                           f"⏱️ Zaman Doldu (Exit)\n"
                           f"{emoji} %{pnl_real:.2f}\n"
                           f"✨ Piranha")

                elif (trade['signal'] == "LONG" and current_price >= trade['tp']) or \
                     (trade['signal'] == "SHORT" and current_price <= trade['tp']):
                    result_type = "WIN"
                    msg = (f"☁️ {symbol_short}\n"
                           f"💎 Hedef Tamam\n"
                           f"💰 %{abs(pnl_real):.2f}\n"
                           f"✨ Piranha")

                elif (trade['signal'] == "LONG" and current_price <= trade['sl']) or \
                     (trade['signal'] == "SHORT" and current_price >= trade['sl']):
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
        logger.error(f"Genel Bekçi Hatası: {e}")

# YENİ: btc_status dışarıdan parametre olarak geliyor (API'yi yormamak için)
def analyze_scalp(symbol, btc_status):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=60)
        if not bars or len(bars) < 50: return None
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        adx = df.ta.adx(length=14)
        if adx is None or adx.empty or adx['ADX_14'].iloc[-1] > ADX_MAX_THRESHOLD: return None 

        row = df.iloc[-1]
        body = abs(row['close'] - row['open'])
        wick_len = body * WICK_RATIO
        
        signal = "NEUTRAL"
        upper_wick = row['high'] - max(row['open'], row['close'])
        lower_wick = min(row['open'], row['close']) - row['low']

        if lower_wick > wick_len and btc_status != "DUMP": signal = "LONG"
        elif upper_wick > wick_len and btc_status != "PUMP": signal = "SHORT"
            
        if signal == "NEUTRAL": return None

        score = 50
        avg_vol = df['volume'].rolling(20).mean().iloc[-1]
        if row['volume'] > (avg_vol * 1.5): score += 20 
        
        if (signal == "LONG" and lower_wick > body * 3) or \
           (signal == "SHORT" and upper_wick > body * 3): score += 20
            
        rsi = df.ta.rsi(length=14).iloc[-1]
        if (signal == "LONG" and rsi < 40) or (signal == "SHORT" and rsi > 60): score += 10

        if score < CONFIDENCE_THRESHOLD: return None

        atr = df.ta.atr(length=14).iloc[-1]
        if signal == "LONG":
            sl = row['close'] - (atr * 1.5)
            tp = row['close'] + (atr * 1.5 * RISK_REWARD)
        else:
            sl = row['close'] + (atr * 1.5)
            tp = row['close'] - (atr * 1.5 * RISK_REWARD)

        return {"signal": signal, "score": score, "price": row['close'], "sl": sl, "tp": tp, "entry_time": time.time()}
    except: return None

def send_daily_report():
    stats = load_json(STATS_FILE)
    msg = (f"☁️ Piranha\n"
           f"🎯 {stats.get('win', 0)} Hedef\n"
           f"🛡️ {stats.get('loss', 0)} Stop\n"
           f"💰 %{stats.get('pnl', 0.0):.2f}")
    send_telegram(msg)
    new_stats = {"date": datetime.now().strftime("%Y-%m-%d"), "win": 0, "loss": 0, "timeout": 0, "pnl": 0.0, "daily_signals": 0, "last_signals": stats.get("last_signals", {})}
    save_json(STATS_FILE, new_stats)

def run(token=None, chat_id=None):
    global TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
    if token: TELEGRAM_TOKEN = token
    if chat_id: TELEGRAM_CHAT_ID = chat_id

    threading.Thread(target=run_flask, daemon=True).start()
    
    logger.info("☁️ PIRANHA v19.4 ONLINE (STEALTH MODE)")
    send_telegram("☁️ Piranha: Aktif (Stealth Modu)")
    
    last_report_day = datetime.now().day
    target_list = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]

    while True:
        try:
            # 1. BEKÇİ KONTROLÜ (Döngü başında SADECE 1 kere)
            check_active_trades()

            # 2. NABIZ VE RAPOR
            if int(time.time()) % 21600 == 0: send_telegram("☁️ Piranha Online | ⚡")
            if datetime.now().day != last_report_day:
                send_daily_report()
                last_report_day = datetime.now().day

            # 3. LİSTE YENİLEME
            try:
                if int(time.time()) % 600 == 0: 
                    tickers = exchange.fetch_tickers()
                    symbols = [s for s in tickers if "/USDT" in s and "quoteVolume" in tickers[s]]
                    symbols.sort(key=lambda x: tickers[x]['quoteVolume'], reverse=True)
                    target_list = symbols[:TOP_COUNT]
            except: pass

            stats = load_json(STATS_FILE)
            
            # YENİ: BTC Yönünü sadece 1 kere sor! (60 kere sormak yok)
            global_btc_status = check_btc_correlation()

            # 4. TARAMA (AVCI)
            for symbol in target_list:
                trades = load_json(TRADES_FILE)
                if symbol in trades: continue
                if check_cooldown(symbol, stats): continue
                
                # BTC yönünü içeriye paslıyoruz
                result = analyze_scalp(symbol, global_btc_status)
                
                if result:
                    symbol_short = symbol.replace("/USDT", "")
                    emoji = "🟢 LONG" if result['signal'] == "LONG" else "🔴 SHORT"
                    
                    # 4. formatla birleştirildi (Hane sıfırları aynen korundu)
                    msg = (f"☁️ {symbol_short} | 💎 %{result['score']} (Range)\n"
                           f"{emoji} (Liquidity Sweep)\n"
                           f"📍 {result['price']}\n"
                           f"🎯 {result['tp']:.4f}\n"
                           f"🛡️ {result['sl']:.4f}")
                    send_telegram(msg)
                    logger.info(f"Sinyal: {symbol}")
                    
                    trades[symbol] = result
                    save_json(TRADES_FILE, trades)
                    
                    stats["daily_signals"] = stats.get("daily_signals", 0) + 1
                    stats.setdefault("last_signals", {})
                    stats["last_signals"][symbol] = time.time()
                    save_json(STATS_FILE, stats)
                
                time.sleep(1) # YENİ: Binance'i yormamak için her coin arasında 1 saniye nefes

            time.sleep(SCAN_INTERVAL) # YENİ: Döngü sonu beklemesi 30 sn.

        except Exception as e:
            logger.error(f"Ana Döngü Hatası: {e}")
            time.sleep(5)

if __name__ == "__main__":
    run()
