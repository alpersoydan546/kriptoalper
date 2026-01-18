import time
import requests
import pandas as pd
import pandas_ta as ta
import os
import logging
from datetime import datetime, timedelta

# LOGLAMA
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger()

# AYARLAR (Render'dan gelir)
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TF = os.getenv("TF", "15m") 
SYMBOLS = ["BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","ADAUSDT","DOGEUSDT","LINKUSDT","AVAXUSDT","DOTUSDT"]

# HAFIZA SİSTEMİ
last_sent_signals = {}  # { 'BTCUSDT_LONG': datetime }
COOLDOWN_MINUTES = 180  # 3 saat boyunca aynı yönü tekrar atma

def tg_send(msg):
    if not TOKEN or not CHAT_ID: return
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        logger.error(f"Telegram hatası: {e}")

def fetch_data(symbol):
    url = "https://fapi.binance.com/fapi/v1/klines"
    try:
        params = {"symbol": symbol, "interval": TF, "limit": 100}
        r = requests.get(url, params=params, timeout=10)
        df = pd.DataFrame(r.json(), columns=['t','o','h','l','c','v','ct','qv','nt','tbv','tqv','i'])
        df['close'] = df['close'].astype(float)
        df['volume'] = df['volume'].astype(float)
        return df
    except:
        return None

def calc_signal(symbol):
    df = fetch_data(symbol)
    if df is None or len(df) < 30: return None

    # RSI Hesapla
    rsi = ta.rsi(df['close'], length=14).iloc[-1]
    last_price = df['close'].iloc[-1]
    
    # Hacim Analizi
    avg_vol = df['volume'].rolling(20).mean().iloc[-1]
    curr_vol = df['volume'].iloc[-1]
    vol_boost = curr_vol > (avg_vol * 1.3)

    direction = None
    if rsi < 30:
        direction = "LONG"
        emoji = "🟢"
        stop = round(last_price * 0.98, 4) # %2 Stop
    elif rsi > 70:
        direction = "SHORT"
        emoji = "🔴"
        stop = round(last_price * 1.02, 4) # %2 Stop

    if direction:
        # Cooldown Kontrolü (Coin + Yön bazlı)
        key = f"{symbol}_{direction}"
        now = datetime.now()
        if key in last_sent_signals:
            if now - last_sent_signals[key] < timedelta(minutes=COOLDOWN_MINUTES):
                return None
        
        # Güven Skoru
        conf = 60
        if rsi < 25 or rsi > 75: conf += 20
        if vol_boost: conf += 20
        conf = min(conf, 100)

        # Sadece %75 ve üzeri güveni at (Orta seviye filtre)
        if conf < 75: return None

        last_sent_signals[key] = now
        duration = "2-4 Saat" if TF == "15m" else "8-12 Saat"

        return (
            f"🎯 <b>#{symbol} {direction}</b> {emoji}\n\n"
            f"💵 <b>Giriş:</b> {last_price}\n"
            f"🛑 <b>Stop:</b> {stop}\n"
            f"⚡ <b>Güven:</b> %{conf}\n"
            f"⏳ <b>Vade:</b> ~{duration}"
        )
    return None

def run(token, chat):
    global TOKEN, CHAT_ID
    TOKEN, CHAT_ID = token, chat
    
    tg_send("🚀 <b>KriptoAlper Scanner Aktif!</b>\nStrateji: RSI + Volume Spike")
    last_hb = datetime.now()

    while True:
        try:
            for sym in SYMBOLS:
                msg = calc_signal(sym)
                if msg:
                    tg_send(msg)
                time.sleep(2) # Binance sınırlaması için

            # 30 dk Hayattayım
            if datetime.now() - last_hb > timedelta(minutes=30):
                tg_send("🛠 <b>Sistem Aktif:</b> Tarama devam ediyor...")
                last_hb = datetime.now()

            time.sleep(120) # 2 dakikada bir tur dön
        except Exception as e:
            logger.error(f"Ana döngü hatası: {e}")
            time.sleep(60)
