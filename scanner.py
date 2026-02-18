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

# --- [ PIRANHA v19.0 - PROFESSIONAL SCALPER ] ---
# Format: Orijinal v17.0 (Dokunulmaz)
# Strateji: Range Scalp + BTC Filtresi + Akıllı Puan

# --- AYARLAR ---
TIMEFRAME = '5m'
LOOKBACK = 50              # Range tespiti
ADX_MAX_THRESHOLD = 25     # Yatay piyasa filtresi
WICK_RATIO = 2.0           # İğne oranı
RISK_REWARD = 1.5          # Kar/Zarar Oranı
CONFIDENCE_THRESHOLD = 70  # Giriş Puanı

# --- LİMİTLER (SINIRSIZ MOD) ---
SCAN_INTERVAL = 15         # 15 saniyede bir tara
MAX_DAILY_SIGNALS = 9999   # Limit Yok
TIME_LIMIT_CANDLES = 20    # 100 dk sonra kapat
COIN_COOLDOWN = 3600       # 1 Saat (Daha agresif)
TOP_COUNT = 60             # Taranacak coin sayısı

# --- SABİT KİMLİK BİLGİLERİ (GÖMÜLÜ) ---
TELEGRAM_TOKEN = "8498989500:AAGmk-2OBpal04K4i6ZMk6YaYNC79Fa_xac"
TELEGRAM_CHAT_ID = "8120732989"

# Dosya İsimleri
STATS_FILE = "daily_stats_render.json"
TRADES_FILE = "active_trades_render.json"
CACHE_REFRESH = 900

# Loglama (Hata tespiti için)
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - [PIRANHA] - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger()

# Borsa Bağlantısı
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

@app.route('/')
def home(): return "☁️ PIRANHA v19.0 PRO ONLINE"

def run_flask():
    try:
        port = int(os.environ.get("PORT", 10000))
        app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
    except: pass

# --- [ TELEGRAM (ORİJİNAL FORMAT KORUYUCU) ] ---
def send_telegram(message):
    try:
        # Hata durumunda konsola yaz, sessiz kalma
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {
            "chat_id": TELEGRAM_CHAT_ID, 
            "text": message, 
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        resp = requests.post(url, data=data, timeout=10)
        if resp.status_code != 200:
            logger.error(f"Telegram API Hatası: {resp.text}")
    except Exception as e: 
        logger.error(f"Telegram Bağlantı Hatası: {e}")

# --- [ DOSYA YÖNETİMİ ] ---
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

# --- [ PROFESYONEL ÖZELLİK 1: BTC FİLTRESİ ] ---
def check_btc_correlation():
    try:
        btc = exchange.fetch_ohlcv('BTC/USDT', timeframe=TIMEFRAME, limit=2)
        if not btc: return "NEUTRAL"
        
        open_p = btc[-1][1]
        close_p = btc[-1][4]
        change = (close_p - open_p) / open_p * 100
        
        # BTC %0.2'den fazla düşüyorsa LONG girme
        if change < -0.2: return "DUMP"
        # BTC %0.2'den fazla çıkıyorsa SHORT girme
        elif change > 0.2: return "PUMP"
        
        return "SAFE"
    except: return "SAFE"

# --- [ BEKÇİ MODÜLÜ (ORİJİNAL FORMATLAR) ] ---
def monitor_trades_thread():
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
                    current_price = float(ticker['last'])
                    symbol_short = symbol.replace('/USDT', '')
                    
                    pnl_pct = abs((current_price - trade['entry']) / trade['entry']) * 100
                    
                    # Yön Kontrolü ve Gerçek PNL
                    pnl_real = (current_price - trade['entry']) / trade['entry'] * 100
                    if trade['signal'] == "SHORT": pnl_real = -pnl_real

                    result_type = None
                    msg = ""

                    # 1. ZAMAN LİMİTİ (Exit)
                    if (current_time - trade['entry_time']) > (TIME_LIMIT_CANDLES * 5 * 60):
                        result_type = "TIMEOUT"
                        emoji = "✅" if pnl_real > 0 else "⚠️"
                        # FORMAT: v17.0
                        msg = (f"☁️ {symbol_short}\n"
                               f"⏱️ Zaman Doldu (Exit)\n"
                               f"{emoji} %{pnl_real:.2f}\n"
                               f"✨ Piranha")

                    # 2. KAR AL (TP)
                    elif (trade['signal'] == "LONG" and current_price >= trade['tp']) or \
                         (trade['signal'] == "SHORT" and current_price <= trade['tp']):
                        result_type = "WIN"
                        # FORMAT: v17.0
                        msg = (f"☁️ {symbol_short}\n"
                               f"💎 Hedef Tamam\n"
                               f"💰 %{abs(pnl_real):.2f}\n"
                               f"✨ Piranha")

                    # 3. STOP OL (SL)
                    elif (trade['signal'] == "LONG" and current_price <= trade['sl']) or \
                         (trade['signal'] == "SHORT" and current_price >= trade['sl']):
                        result_type = "LOSS"
                        # FORMAT: v17.0
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

        except: pass
        time.sleep(5)

# --- [ STRATEJİ: SMART SCALP ] ---
def analyze_scalp(symbol):
    try:
        # Veri Çek
        bars = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=60)
        if not bars or len(bars) < 50: return None
        
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        # 1. BTC KORELASYON KONTROLÜ (YENİ)
        btc_status = check_btc_correlation()
        
        # 2. TREND FİLTRESİ
        adx = df.ta.adx(length=14)
        if adx is None or adx.empty: return None
        current_adx = adx['ADX_14'].iloc[-1]
        
        if current_adx > ADX_MAX_THRESHOLD: return None 

        # 3. İĞNE ANALİZİ
        row = df.iloc[-1]
        body = abs(row['close'] - row['open'])
        upper_wick = row['high'] - max(row['open'], row['close'])
        lower_wick = min(row['open'], row['close']) - row['low']
        
        signal = "NEUTRAL"
        
        # Range dışına çıkıp içeri giren iğneler
        if lower_wick > (body * WICK_RATIO):
            if btc_status != "DUMP": signal = "LONG" # BTC çöküyorsa girme
            
        elif upper_wick > (body * WICK_RATIO):
            if btc_status != "PUMP": signal = "SHORT" # BTC uçuyorsa girme
            
        if signal == "NEUTRAL": return None

        # 4. AKILLI PUANLAMA (YENİ)
        score = 50 # Baz Puan
        
        # Hacim Bonusu
        avg_vol = df['volume'].rolling(20).mean().iloc[-1]
        if row['volume'] > (avg_vol * 1.5): score += 20 # Balina Hacmi
        
        # İğne Gücü Bonusu
        if (signal == "LONG" and lower_wick > body * 3) or \
           (signal == "SHORT" and upper_wick > body * 3):
            score += 20
            
        # RSI Bonusu (Uyuşmazlık/Aşırı Bölge)
        rsi = df.ta.rsi(length=14).iloc[-1]
        if signal == "LONG" and rsi < 40: score += 10
        if signal == "SHORT" and rsi > 60: score += 10

        if score < CONFIDENCE_THRESHOLD: return None

        # Hedefler (ATR Bazlı)
        atr = df.ta.atr(length=14).iloc[-1]
        current_price = row['close']
        
        if signal == "LONG":
            sl = current_price - (atr * 1.5)
            tp = current_price + (atr * 1.5 * RISK_REWARD)
        else:
            sl = current_price + (atr * 1.5)
            tp = current_price - (atr * 1.5 * RISK_REWARD)

        return {
            "signal": signal, "score": score, "price": current_price, 
            "sl": sl, "tp": tp, "entry_time": time.time()
        }

    except: return None

# --- [ GÜNLÜK RAPOR (ORİJİNAL FORMAT) ] ---
def send_daily_report():
    stats = load_json(STATS_FILE)
    
    # FORMAT: v17.0
    msg = (f"☁️ Piranha\n"
           f"🎯 {stats.get('win', 0)} Hedef\n"
           f"🛡️ {stats.get('loss', 0)} Stop\n"
           f"💰 %{stats.get('pnl', 0.0):.2f}")
           
    send_telegram(msg)
    
    new_stats = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "win": 0, "loss": 0, "timeout": 0, "pnl": 0.0,
        "daily_signals": 0, "last_signals": stats.get("last_signals", {})
    }
    save_json(STATS_FILE, new_stats)

# --- [ ANA KOMUTA MERKEZİ ] ---
def run_piranha():
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=monitor_trades_thread, daemon=True).start()
    
    logger.info("☁️ PIRANHA ONLINE")
    send_telegram("☁️ Piranha: Aktif") # Orijinal Init Mesajı
    
    last_report_day = datetime.now().day

    while True:
        try:
            # Nabız (6 Saat)
            if int(time.time()) % 21600 == 0:
                send_telegram("☁️ Piranha Online | ⚡")

            # Günlük Rapor
            if datetime.now().day != last_report_day:
                send_daily_report()
                last_report_day = datetime.now().day

            # Liste (Top 50 Hacimli)
            try:
                tickers = exchange.fetch_tickers()
                symbols = [s for s in tickers if "/USDT" in s and "quoteVolume" in tickers[s]]
                symbols.sort(key=lambda x: tickers[x]['quoteVolume'], reverse=True)
                target_list = symbols[:TOP_COUNT]
            except:
                target_list = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]

            stats = load_json(STATS_FILE)

            # Tarama Döngüsü
            for symbol in target_list:
                trades = load_json(TRADES_FILE)
                
                # Filtreler (Limitsiz Modda da cooldown ve açık işlem kontrolü şart)
                if symbol in trades: continue
                if check_cooldown(symbol, stats): continue
                
                result = analyze_scalp(symbol)
                
                if result:
                    symbol_short = symbol.replace("/USDT", "")
                    emoji = "🟢 LONG" if result['signal'] == "LONG" else "🔴 SHORT"
                    
                    # FORMAT: v17.0
                    msg = (f"☁️ {symbol_short} | 💎 %{result['score']} (Range)\n"
                           f"{emoji} (Liquidity Sweep)\n"
                           f"📍 {result['price']}\n"
                           f"🎯 {result['tp']:.4f}\n"
                           f"🛡️ {result['sl']:.4f}")
                    
                    send_telegram(msg)
                    logger.info(f"Sinyal Bulundu: {symbol}")
                    
                    trades[symbol] = result
                    save_json(TRADES_FILE, trades)
                    
                    stats["daily_signals"] = stats.get("daily_signals", 0) + 1
                    stats.setdefault("last_signals", {})
                    stats["last_signals"][symbol] = time.time()
                    save_json(STATS_FILE, stats)
                
                time.sleep(0.5) # API rahatlatma

            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt: break
        except Exception as e:
            logger.error(f"Ana Döngü Hatası: {e}")
            time.sleep(10)

if __name__ == "__main__":
    run_piranha()
