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

# --- [ AYARLAR & LİMİTLER ] ---
TIMEFRAME = '5m'
LOOKBACK = 50
ADX_MAX_THRESHOLD = 25      # Yatay piyasa filtresi
WICK_RATIO = 2.0            # İğne oranı
CONFIDENCE_THRESHOLD = 70   # Giriş puanı

SCAN_INTERVAL = 15          # Tarama hızı
MAX_DAILY_SIGNALS = 15      # Günlük işlem limiti (Biraz artırdım)
TIME_LIMIT_CANDLES = 12     # Zaman aşımı (1 saat)
COIN_COOLDOWN = 3600        # 1 Saatlik ban
TOP_COUNT = 60              # Taranacak coin sayısı

# Dosya Yolları
STATS_FILE = "piranha_stats.json"
TRADES_FILE = "piranha_trades.json"

# --- [ LOGLAMA ] ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [PIRANHA] - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger()

# --- [ BORSA BAĞLANTISI ] ---
try:
    exchange = ccxt.binance({
        'rateLimit': 1200,
        'enableRateLimit': True,
        'options': {'defaultType': 'future'}
    })
except Exception as e:
    logger.error(f"Borsa Bağlantı Hatası: {e}")

app = Flask(__name__)
lock = threading.Lock()

# --- [ FLASK (Render İçin) ] ---
@app.route('/')
def home(): return "☁️ PIRANHA v18.1 ONLINE"

def run_flask():
    try:
        port = int(os.environ.get("PORT", 10000))
        app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
    except: pass

# --- [ TELEGRAM MOTORU (GÜNCELLENDİ) ] ---
def send_telegram(token, chat_id, message):
    try:
        # Terminale de bilgi verelim
        print(f"📩 Telegram Gönderiliyor: {message.splitlines()[0]}") 
        
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = {
            "chat_id": chat_id, 
            "text": message, 
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        resp = requests.post(url, data=data, timeout=10)
        
        if resp.status_code != 200:
            logger.error(f"Telegram API Hatası: {resp.text}")
            
    except Exception as e: 
        logger.error(f"Telegram Bağlantı Hatası: {e}")

# --- [ DOSYA İŞLEMLERİ ] ---
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
        stats = {"date": today, "win": 0, "loss": 0, "timeout": 0, "pnl": 0.0, 
                 "daily_signals": 0, "last_signals": {}}
    
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

# --- [ BEKÇİ (POZİSYON TAKİPÇİSİ) ] ---
def monitor_trades_thread(token, chat_id):
    logger.info("🛡️ Bekçi Modülü Devrede...")
    while True:
        try:
            trades = load_json(TRADES_FILE)
            if not trades:
                time.sleep(10)
                continue

            updated_trades = trades.copy()
            trades_changed = False
            current_time = time.time()

            for symbol, trade in trades.items():
                try:
                    ticker = exchange.fetch_ticker(symbol)
                    current_price = float(ticker['last'])
                    symbol_short = symbol.replace('/USDT', '')
                    
                    raw_pnl = (current_price - trade['entry']) / trade['entry'] * 100
                    if trade['signal'] == "SHORT": raw_pnl = -raw_pnl
                    
                    result_type = None

                    # STOP LOSS
                    if (trade['signal'] == "LONG" and current_price <= trade['sl']) or \
                       (trade['signal'] == "SHORT" and current_price >= trade['sl']):
                        result_type = "LOSS"
                        msg = (f"🔴 <b>STOP ({symbol_short})</b>\n"
                               f"📉 PNL: %{raw_pnl:.2f}\n"
                               f"💀 Fiyat: {current_price}")

                    # TAKE PROFIT
                    elif (trade['signal'] == "LONG" and current_price >= trade['tp']) or \
                         (trade['signal'] == "SHORT" and current_price <= trade['tp']):
                        result_type = "WIN"
                        msg = (f"🟢 <b>HEDEF ({symbol_short})</b>\n"
                               f"💰 PNL: %{raw_pnl:.2f}\n"
                               f"🚀 Fiyat: {current_price}")

                    # ZAMAN AŞIMI
                    elif (current_time - trade['entry_time']) > (TIME_LIMIT_CANDLES * 5 * 60):
                        result_type = "TIMEOUT"
                        emoji = "✅" if raw_pnl > 0 else "⚠️"
                        msg = (f"⏱️ <b>ZAMAN DOLDU ({symbol_short})</b>\n"
                               f"{emoji} PNL: %{raw_pnl:.2f}\n"
                               f"Pozisyon kapatılıyor.")

                    if result_type:
                        send_telegram(token, chat_id, msg)
                        update_stats(result_type, raw_pnl)
                        del updated_trades[symbol]
                        trades_changed = True
                        logger.info(f"İşlem Bitti: {symbol} -> {result_type}")

                except: continue
            
            if trades_changed:
                save_json(TRADES_FILE, updated_trades)

        except: pass
        time.sleep(5)

# --- [ TEKNİK ANALİZ MOTORU ] ---
def analyze_scalp(symbol):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=60)
        if not bars or len(bars) < 50: return None
        
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        # ADX (Trend Zayıflığı Kontrolü)
        adx = df.ta.adx(length=14)
        if adx is None or adx.empty: return None
        current_adx = adx['ADX_14'].iloc[-1]
        
        if current_adx > ADX_MAX_THRESHOLD: return None # Trend varsa girme, yatay lazım

        # Mum İğne Analizi
        row = df.iloc[-1]
        body = abs(row['close'] - row['open'])
        upper_wick = row['high'] - max(row['open'], row['close'])
        lower_wick = min(row['open'], row['close']) - row['low']
        
        signal = "NEUTRAL"
        
        # Aşağı uzun iğne -> Dönüş (LONG)
        if lower_wick > (body * WICK_RATIO): signal = "LONG"
        # Yukarı uzun iğne -> Dönüş (SHORT)
        elif upper_wick > (body * WICK_RATIO): signal = "SHORT"
            
        if signal == "NEUTRAL": return None

        # Puanlama
        score = 50
        rsi = df.ta.rsi(length=14).iloc[-1]
        
        if signal == "LONG":
            if rsi < 30: score += 20
            elif rsi < 40: score += 10
            if lower_wick > (body * 3): score += 15
            
        elif signal == "SHORT":
            if rsi > 70: score += 20
            elif rsi > 60: score += 10
            if upper_wick > (body * 3): score += 15

        if score < CONFIDENCE_THRESHOLD: return None

        # Hedefler (ATR Bazlı)
        atr = df.ta.atr(length=14).iloc[-1]
        current_price = row['close']
        
        if signal == "LONG":
            sl = current_price - (atr * 1.5)
            tp = current_price + (atr * 1.5 * 1.5)
        else:
            sl = current_price + (atr * 1.5)
            tp = current_price - (atr * 1.5 * 1.5)

        return {"signal": signal, "score": score, "price": current_price, "sl": sl, "tp": tp, "entry_time": time.time()}

    except: return None

# --- [ GÜNLÜK RAPOR ] ---
def send_daily_report(token, chat_id):
    stats = load_json(STATS_FILE)
    msg = (f"🌙 <b>PIRANHA GÜNLÜK RAPOR</b>\n"
           f"📅 Tarih: {stats.get('date')}\n"
           f"✅ Win: {stats.get('win', 0)}\n"
           f"❌ Loss: {stats.get('loss', 0)}\n"
           f"💰 <b>PNL: %{stats.get('pnl', 0.0):.2f}</b>")
    send_telegram(token, chat_id, msg)
    
    new_stats = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "win": 0, "loss": 0, "timeout": 0, "pnl": 0.0,
        "daily_signals": 0, "last_signals": stats.get("last_signals", {})
    }
    save_json(STATS_FILE, new_stats)

# --- [ ANA KOMUTA MERKEZİ ] ---
def run_piranha(token, chat_id):
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=monitor_trades_thread, args=(token, chat_id), daemon=True).start()
    
    logger.info("☁️ PIRANHA GÖREVE BAŞLADI")
    send_telegram(token, chat_id, "☁️ <b>PIRANHA v18.1 ONLINE</b>\nToken ve ID Güncellendi 🚀")
    
    last_report_day = datetime.now().day

    while True:
        try:
            stats = load_json(STATS_FILE)
            if stats.get("daily_signals", 0) >= MAX_DAILY_SIGNALS:
                logger.info("Günlük limit doldu, bekleniyor...")
                time.sleep(600)
                continue

            if datetime.now().day != last_report_day:
                send_daily_report(token, chat_id)
                last_report_day = datetime.now().day

            try:
                tickers = exchange.fetch_tickers()
                symbols = [s for s in tickers if "/USDT" in s and "quoteVolume" in tickers[s]]
                symbols.sort(key=lambda x: tickers[x]['quoteVolume'], reverse=True)
                target_list = symbols[:TOP_COUNT]
            except:
                target_list = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]

            for symbol in target_list:
                trades = load_json(TRADES_FILE)
                if symbol in trades: continue
                if check_cooldown(symbol, stats): continue
                
                result = analyze_scalp(symbol)
                
                if result:
                    symbol_clean = symbol.replace("/USDT", "")
                    emoji = "🟢 LONG" if result['signal'] == "LONG" else "🔴 SHORT"
                    
                    msg = (f"☁️ <b>{symbol_clean}</b> | PIRANHA\n"
                           f"{emoji} Fırsat\n"
                           f"💵 Giriş: {result['price']}\n"
                           f"🎯 TP: {result['tp']:.4f}\n"
                           f"🛡️ SL: {result['sl']:.4f}\n"
                           f"📊 Güven: {result['score']}/100")
                    
                    send_telegram(token, chat_id, msg)
                    logger.info(f"Sinyal: {symbol} {result['signal']}")
                    
                    trades[symbol] = result
                    save_json(TRADES_FILE, trades)
                    
                    stats["daily_signals"] = stats.get("daily_signals", 0) + 1
                    stats["last_signals"][symbol] = time.time()
                    save_json(STATS_FILE, stats)
                
                time.sleep(1.5) # API Limiti için bekleme

            logger.info("Tarama turu bitti...")
            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            print("Kapatılıyor...")
            break
        except Exception as e:
            logger.error(f"Döngü Hatası: {e}")
            time.sleep(10)

if __name__ == "__main__":
    # --- AYARLAR OTOMATİK DOLDURULDU ---
    TELEGRAM_TOKEN = "8498989500:AAGmk-2OBpal04K4i6ZMk6YaYNC79Fa_xac"
    TELEGRAM_CHAT_ID = "8120732989"
    
    run_piranha(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)
