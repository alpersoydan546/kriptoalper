import ccxt
import pandas as pd
import pandas_ta as ta
import time
import requests
import logging
import json
import os
import threading
import sys
from datetime import datetime
from flask import Flask

# --- [ PIRANHA - LİKİDİTE AVCISI ] ---

# AYARLAR
TIMEFRAME = '5m'
ADX_MAX_THRESHOLD = 30
WICK_RATIO = 1.6
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 65
CONFIDENCE_THRESHOLD = 65

# LİMİTLER
SCAN_INTERVAL = 15
MAX_DAILY_SIGNALS = 20
TIME_LIMIT_CANDLES = 12
COIN_COOLDOWN = 1800
TOP_COUNT = 70

# DOSYA SİSTEMİ
STATS_FILE = "piranha_stats.json"
TRADES_FILE = "piranha_trades.json"
TELEGRAM_TOKEN = "8498989500:AAGmk-2OBpal04K4i6ZMk6YaYNC79Fa_xac"
TELEGRAM_CHAT_ID = "8120732989"

# LOGLAMA
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s', handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger()

# FLASK (En Başta Tanımla)
app = Flask(__name__)
lock = threading.Lock()

# --- [ TELEGRAM ] ---
def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}
        requests.post(url, data=data, timeout=10)
    except Exception as e: logger.error(f"Telegram Hatası: {e}")

# --- [ DOSYA YÖNETİMİ ] ---
def load_json(filename):
    with lock:
        if not os.path.exists(filename): return {}
        try: with open(filename, 'r') as f: return json.load(f)
        except: return {}

def save_json(filename, data):
    with lock:
        try: with open(filename, 'w') as f: json.dump(data, f, indent=4)
        except: pass

# --- [ ANALİZ MOTORU ] ---
def analyze_scalp(exchange, symbol):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=60)
        if not bars or len(bars) < 50: return None
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

        # ADX
        adx = df.ta.adx(length=14)
        if adx is None or adx.empty: return None
        if adx['ADX_14'].iloc[-1] > ADX_MAX_THRESHOLD: return None

        # RSI & WICK
        rsi = df.ta.rsi(length=14).iloc[-1]
        row = df.iloc[-1]
        body = abs(row['close'] - row['open'])
        upper_wick = row['high'] - max(row['open'], row['close'])
        lower_wick = min(row['open'], row['close']) - row['low']

        signal = "NEUTRAL"
        if (lower_wick > (body * WICK_RATIO)) and (rsi < RSI_OVERSOLD): signal = "LONG"
        elif (upper_wick > (body * WICK_RATIO)) and (rsi > RSI_OVERBOUGHT): signal = "SHORT"
        
        if signal == "NEUTRAL": return None

        # SCORE
        score = 60
        if signal == "LONG":
            if rsi < 25: score += 15
            if lower_wick > (body * 2.5): score += 15
        elif signal == "SHORT":
            if rsi > 75: score += 15
            if upper_wick > (body * 2.5): score += 15
            
        if score < CONFIDENCE_THRESHOLD: return None

        # HEDEFLER
        atr = df.ta.atr(length=14).iloc[-1]
        sl = row['close'] - (atr * 1.5) if signal == "LONG" else row['close'] + (atr * 1.5)
        tp = row['close'] + (atr * 2.25) if signal == "LONG" else row['close'] - (atr * 2.25)

        return {"signal": signal, "score": score, "price": row['close'], "sl": sl, "tp": tp, "entry_time": time.time()}
    except: return None

# --- [ ARKA PLAN İŞÇİSİ (BOT) ] ---
def bot_loop():
    # Gecikmeli Başlatma (Flask'ın portu kapmasına izin ver)
    time.sleep(3) 
    logger.info("☁️ PIRANHA: Borsa Bağlantısı Kuruluyor...")

    # BORSA BAĞLANTISI (Burada Yapıyoruz ki Render Beklemesin)
    exchange = None
    while exchange is None:
        try:
            exchange = ccxt.binance({'rateLimit': 1200, 'enableRateLimit': True, 'options': {'defaultType': 'future'}})
            exchange.load_markets()
            logger.info("✅ Borsa Bağlandı!")
        except Exception as e:
            logger.error(f"⚠️ Borsa Bağlantı Hatası: {e} | 5sn Bekleniyor...")
            time.sleep(5)

    send_telegram("☁️ <b>PIRANHA v19.3 (Fast Boot)</b>\nSistem Online 🚀")
    last_report_day = datetime.now().day

    while True:
        try:
            stats = load_json(STATS_FILE)
            if datetime.now().day != last_report_day:
                msg = f"☁️ Piranha Rapor\n🎯 {stats.get('win', 0)} | 🛡️ {stats.get('loss', 0)} | 💰 %{stats.get('pnl', 0.0):.2f}"
                send_telegram(msg)
                stats = {"date": datetime.now().strftime("%Y-%m-%d"), "win": 0, "loss": 0, "timeout": 0, "pnl": 0.0, "daily_signals": 0, "last_signals": stats.get("last_signals", {})}
                save_json(STATS_FILE, stats)
                last_report_day = datetime.now().day

            # BEKÇİ (Monitor)
            trades = load_json(TRADES_FILE)
            updated_trades = trades.copy()
            trades_changed = False
            
            for symbol, trade in trades.items():
                try:
                    ticker = exchange.fetch_ticker(symbol)
                    curr_price = float(ticker['last'])
                    pnl = (curr_price - trade['entry']) / trade['entry'] * 100
                    if trade['signal'] == "SHORT": pnl = -pnl
                    
                    res = None
                    if (trade['signal']=="LONG" and curr_price<=trade['sl']) or (trade['signal']=="SHORT" and curr_price>=trade['sl']): res="LOSS"
                    elif (trade['signal']=="LONG" and curr_price>=trade['tp']) or (trade['signal']=="SHORT" and curr_price<=trade['tp']): res="WIN"
                    elif (time.time() - trade['entry_time']) > (TIME_LIMIT_CANDLES * 300): res="TIMEOUT"
                    
                    if res:
                        sym_clean = symbol.replace('/USDT', '')
                        if res == "LOSS": msg = f"☁️ {sym_clean}\n❌ Stop\n📉 -%{abs(pnl):.2f}\n✨ Piranha"
                        elif res == "WIN": msg = f"☁️ {sym_clean}\n💎 Hedef Tamam\n💰 %{pnl:.2f}\n✨ Piranha"
                        else: msg = f"☁️ {sym_clean}\n⏱️ Zaman Doldu\n{'🟢' if pnl>0 else '🔴'} %{pnl:.2f}\n✨ Piranha"
                        
                        send_telegram(msg)
                        if res == "WIN": stats["win"] += 1
                        elif res == "LOSS": stats["loss"] += 1
                        stats["pnl"] += pnl
                        save_json(STATS_FILE, stats)
                        del updated_trades[symbol]
                        trades_changed = True
                except: pass
            
            if trades_changed: save_json(TRADES_FILE, updated_trades)

            # TARAMA (Scanner)
            if stats.get("daily_signals", 0) < MAX_DAILY_SIGNALS:
                try:
                    tickers = exchange.fetch_tickers()
                    symbols = [s for s in tickers if "/USDT" in s and tickers[s]['quoteVolume'] > 0]
                    symbols.sort(key=lambda x: tickers[x]['quoteVolume'], reverse=True)
                    target_list = symbols[:TOP_COUNT]
                except: target_list = []

                for symbol in target_list:
                    trades = load_json(TRADES_FILE)
                    if symbol in trades: continue
                    if symbol in stats.get("last_signals", {}) and (time.time() - stats["last_signals"][symbol] < COIN_COOLDOWN): continue
                    
                    # Exchange objesini fonksiyona geçiriyoruz
                    res = analyze_scalp(exchange, symbol)
                    if res:
                        sym_clean = symbol.replace("/USDT", "")
                        sweep = "🟢 (Liquidity Sweep)" if res['signal'] == "LONG" else "🔴 (Liquidity Sweep)"
                        msg = f"☁️ {sym_clean} | 💎 %{res['score']} (Range)\n{sweep}\n📍 {res['price']}\n🎯 {res['tp']:.4f}\n🛡️ {res['sl']:.4f}"
                        send_telegram(msg)
                        
                        trades[symbol] = res
                        save_json(TRADES_FILE, trades)
                        stats["daily_signals"] = stats.get("daily_signals", 0) + 1
                        if "last_signals" not in stats: stats["last_signals"] = {}
                        stats["last_signals"][symbol] = time.time()
                        save_json(STATS_FILE, stats)
                        time.sleep(2)
                    time.sleep(1)
            
            time.sleep(SCAN_INTERVAL)
        except Exception as e:
            logger.error(f"Bot Döngü Hatası: {e}")
            time.sleep(10)

# --- [ FLASK (PATRON) ] ---
@app.route('/')
def home():
    return "☁️ PIRANHA v19.3 RUNNING"

# Botu arka planda başlat (DAEMON)
threading.Thread(target=bot_loop, daemon=True).start()

if __name__ == "__main__":
    # Render PORT'unu hemen dinle
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
