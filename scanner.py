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
ADX_MAX_THRESHOLD = 25      # Biraz esnetildi (Daha çok fırsat için)
WICK_RATIO = 2.0            # İğne oranı optimize edildi
RISK_REWARD = 1.5
CONFIDENCE_THRESHOLD = 70   # Giriş puanı

SCAN_INTERVAL = 15          # Tarama hızı artırıldı
MAX_DAILY_SIGNALS = 10      # Günlük işlem limiti
TIME_LIMIT_CANDLES = 12     # 1 saat (12 x 5dk) sonra kapat
COIN_COOLDOWN = 3600        # Aynı coine 1 saat bulaşma

# Dosya Yolları
STATS_FILE = "piranha_stats.json"
TRADES_FILE = "piranha_trades.json"
TOP_COUNT = 60              # Taranacak coin sayısı

# --- [ LOGLAMA AYARLARI ] ---
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
def home(): return "☁️ PIRANHA v18.0 SCALPER ONLINE"

def run_flask():
    try:
        # Render portunu otomatik alır, yoksa 10000 kullanır
        port = int(os.environ.get("PORT", 10000))
        app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
    except Exception as e:
        logger.error(f"Flask Hatası: {e}")

# --- [ TELEGRAM MOTORU ] ---
def send_telegram(token, chat_id, message):
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = {
            "chat_id": chat_id, 
            "text": message, 
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        requests.post(url, data=data, timeout=5)
    except Exception as e: 
        logger.error(f"Telegram Gönderilemedi: {e}")

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
        except Exception as e:
            logger.error(f"Dosya Kayıt Hatası ({filename}): {e}")

def update_stats(result, pnl):
    stats = load_json(STATS_FILE)
    today = datetime.now().strftime("%Y-%m-%d")
    
    if stats.get("date") != today:
        stats = {
            "date": today, 
            "win": 0, "loss": 0, "timeout": 0, "pnl": 0.0, 
            "daily_signals": 0,
            "last_signals": stats.get("last_signals", {})
        }
    
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
                    
                    # PNL Hesapla
                    raw_pnl = (current_price - trade['entry']) / trade['entry'] * 100
                    if trade['signal'] == "SHORT": raw_pnl = -raw_pnl
                    
                    result_type = None

                    # 1. STOP LOSS
                    if (trade['signal'] == "LONG" and current_price <= trade['sl']) or \
                       (trade['signal'] == "SHORT" and current_price >= trade['sl']):
                        result_type = "LOSS"
                        msg = (f"🔴 <b>STOP OLDUK</b> ({symbol_short})\n"
                               f"📉 PNL: %{raw_pnl:.2f}\n"
                               f"💀 Fiyat: {current_price}")

                    # 2. TAKE PROFIT
                    elif (trade['signal'] == "LONG" and current_price >= trade['tp']) or \
                         (trade['signal'] == "SHORT" and current_price <= trade['tp']):
                        result_type = "WIN"
                        msg = (f"🟢 <b>HEDEF ALINDI</b> ({symbol_short})\n"
                               f"💰 PNL: %{raw_pnl:.2f}\n"
                               f"🚀 Fiyat: {current_price}")

                    # 3. ZAMAN AŞIMI (Timeout)
                    elif (current_time - trade['entry_time']) > (TIME_LIMIT_CANDLES * 5 * 60):
                        result_type = "TIMEOUT"
                        emoji = "✅" if raw_pnl > 0 else "⚠️"
                        msg = (f"⏱️ <b>ZAMAN DOLDU</b> ({symbol_short})\n"
                               f"{emoji} PNL: %{raw_pnl:.2f}\n"
                               f"Pozisyon kapatılıyor.")

                    # --- İŞLEM SONUCU VARSA ---
                    if result_type:
                        send_telegram(token, chat_id, msg)
                        update_stats(result_type, raw_pnl)
                        del updated_trades[symbol]
                        trades_changed = True
                        logger.info(f"İşlem Bitti: {symbol} -> {result_type}")

                except Exception as e:
                    logger.error(f"Bekçi Hatası ({symbol}): {e}")
                    continue
            
            if trades_changed:
                save_json(TRADES_FILE, updated_trades)

        except Exception as e:
            logger.error(f"Genel Bekçi Hatası: {e}")
        
        time.sleep(5)

# --- [ TEKNİK ANALİZ MOTORU ] ---
def analyze_scalp(symbol):
    try:
        # Veri çek
        bars = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=60)
        if not bars or len(bars) < 50: return None
        
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        # ADX Hesapla (Trend Gücü)
        # pandas_ta kütüphanesi df.ta.adx şeklinde de kullanılabilir
        adx = df.ta.adx(length=14)
        if adx is None or adx.empty: return None
        current_adx = adx['ADX_14'].iloc[-1]
        
        # Sadece Zayıf Trend / Yatay Piyasa (Range)
        if current_adx > ADX_MAX_THRESHOLD: return None 

        # Mum Analizi
        last_row = df.iloc[-1]
        open_p = last_row['open']
        close_p = last_row['close']
        high_p = last_row['high']
        low_p = last_row['low']
        
        body = abs(close_p - open_p)
        upper_wick = high_p - max(open_p, close_p)
        lower_wick = min(open_p, close_p) - low_p
        
        signal = "NEUTRAL"
        
        # LONG Sinyali: Aşağıda uzun iğne (Alıcı baskısı)
        if lower_wick > (body * WICK_RATIO):
            signal = "LONG"
            
        # SHORT Sinyali: Yukarıda uzun iğne (Satıcı baskısı)
        elif upper_wick > (body * WICK_RATIO):
            signal = "SHORT"
            
        if signal == "NEUTRAL": return None

        # --- PUANLAMA ALGORİTMASI ---
        score = 50 # Başlangıç puanı
        
        # RSI Filtresi
        rsi_val = df.ta.rsi(length=14).iloc[-1]
        
        if signal == "LONG":
            if rsi_val < 30: score += 20 # Aşırı satım
            elif rsi_val < 40: score += 10
        elif signal == "SHORT":
            if rsi_val > 70: score += 20 # Aşırı alım
            elif rsi_val > 60: score += 10
            
        # Wick Gücü Bonusu
        if signal == "LONG" and lower_wick > (body * 3): score += 15
        if signal == "SHORT" and upper_wick > (body * 3): score += 15

        if score < CONFIDENCE_THRESHOLD: return None

        # Hedef Belirleme
        current_price = close_p
        atr = df.ta.atr(length=14).iloc[-1] # Volatilite bazlı SL/TP
        
        if signal == "LONG":
            sl = current_price - (atr * 1.5)
            tp = current_price + (atr * 1.5 * RISK_REWARD)
        else:
            sl = current_price + (atr * 1.5)
            tp = current_price - (atr * 1.5 * RISK_REWARD)

        return {
            "signal": signal,
            "score": score,
            "price": current_price,
            "sl": sl, 
            "tp": tp,
            "entry_time": time.time()
        }

    except Exception as e:
        # logger.error(f"Analiz Hatası ({symbol}): {e}") # Log kirliliği yapmasın
        return None

# --- [ GÜNLÜK RAPOR ] ---
def send_daily_report(token, chat_id):
    stats = load_json(STATS_FILE)
    msg = (f"🌙 <b>GÜN SONU RAPORU</b>\n"
           f"📅 Tarih: {stats.get('date')}\n"
           f"✅ Win: {stats.get('win', 0)}\n"
           f"❌ Loss: {stats.get('loss', 0)}\n"
           f"💰 <b>PNL: %{stats.get('pnl', 0.0):.2f}</b>")
    send_telegram(token, chat_id, msg)
    
    # Yeni gün için sıfırla ama cooldownları koru
    new_stats = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "win": 0, "loss": 0, "timeout": 0, "pnl": 0.0,
        "daily_signals": 0,
        "last_signals": stats.get("last_signals", {})
    }
    save_json(STATS_FILE, new_stats)

# --- [ ANA KOMUTA MERKEZİ ] ---
def run_piranha(token, chat_id):
    # Flask sunucusu (Arka planda)
    threading.Thread(target=run_flask, daemon=True).start()
    
    # Bekçi (Arka planda)
    threading.Thread(target=monitor_trades_thread, args=(token, chat_id), daemon=True).start()
    
    logger.info("☁️ PIRANHA GÖREVE BAŞLADI")
    send_telegram(token, chat_id, "☁️ <b>PIRANHA v18.0 ONLINE</b>\nScalp Modu: Aktif 🚀")
    
    last_report_day = datetime.now().day

    while True:
        try:
            # 1. Günlük Limit Kontrolü
            stats = load_json(STATS_FILE)
            if stats.get("daily_signals", 0) >= MAX_DAILY_SIGNALS:
                logger.info("Günlük limite ulaşıldı. Uyku modu...")
                time.sleep(600)
                continue

            # 2. Rapor Kontrolü
            if datetime.now().day != last_report_day:
                send_daily_report(token, chat_id)
                last_report_day = datetime.now().day

            # 3. Tarama Listesi (Hacimli Coinler)
            try:
                tickers = exchange.fetch_tickers()
                # Sadece USDT çiftleri ve Hacme göre sırala
                symbols = [s for s in tickers if "/USDT" in s and "quoteVolume" in tickers[s]]
                symbols.sort(key=lambda x: tickers[x]['quoteVolume'], reverse=True)
                target_list = symbols[:TOP_COUNT]
            except:
                target_list = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]

            # 4. Analiz Döngüsü
            for symbol in target_list:
                # Açık işlem varsa veya cooldown varsa atla
                trades = load_json(TRADES_FILE)
                if symbol in trades: continue
                if check_cooldown(symbol, stats): continue
                
                # Analiz Et
                result = analyze_scalp(symbol)
                
                if result:
                    symbol_clean = symbol.replace("/USDT", "")
                    emoji = "🟢 LONG" if result['signal'] == "LONG" else "🔴 SHORT"
                    
                    # Sinyal Mesajı
                    msg = (f"☁️ <b>{symbol_clean}</b> | PIRANHA\n"
                           f"{emoji} Fırsat Yakalandı\n"
                           f"💵 Giriş: {result['price']}\n"
                           f"🎯 TP: {result['tp']:.4f}\n"
                           f"🛡️ SL: {result['sl']:.4f}\n"
                           f"📊 Güven: {result['score']}/100")
                    
                    send_telegram(token, chat_id, msg)
                    logger.info(f"Sinyal: {symbol} {result['signal']}")
                    
                    # Kayıtlar
                    trades[symbol] = result
                    save_json(TRADES_FILE, trades)
                    
                    stats["daily_signals"] = stats.get("daily_signals", 0) + 1
                    stats["last_signals"][symbol] = time.time()
                    save_json(STATS_FILE, stats)
                
                time.sleep(1) # API limitine takılmamak için bekleme

            logger.info("Tarama turu bitti, bekleniyor...")
            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            print("Kapatılıyor...")
            break
        except Exception as e:
            logger.error(f"Ana Döngü Hatası: {e}")
            time.sleep(10)

if __name__ == "__main__":
    # --- BURAYA KENDİ BİLGİLERİNİ GİR ---
    TELEGRAM_TOKEN = "BURAYA_TOKEN_GIR"
    TELEGRAM_CHAT_ID = "BURAYA_CHAT_ID_GIR"
    
    run_piranha(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)
