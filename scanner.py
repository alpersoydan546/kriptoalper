import time
import requests
import pandas as pd
import pandas_ta as ta
import os
import logging
from datetime import datetime, timedelta

# LOGLAMA AYARI (Hataları detaylı görmek için)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger()

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TF = os.getenv("TF", "15m") 
SYMBOLS = ["BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","ADAUSDT","DOGEUSDT","LINKUSDT","AVAXUSDT","DOTUSDT"]

last_sent_signals = {}
COOLDOWN_MINUTES = 180 

def tg_send(msg):
    if not TOKEN or not CHAT_ID: 
        logger.error("Telegram değişkenleri eksik!")
        return
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
        if r.status_code != 200:
            logger.error(f"Telegram API Hatası: {r.text}")
    except Exception as e:
        logger.error(f"Telegram Bağlantı Hatası: {e}")

def fetch_data(symbol, interval, limit=250):
    url = "https://fapi.binance.com/fapi/v1/klines"
    try:
        r = requests.get(url, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=10)
        if r.status_code != 200:
            logger.error(f"Binance Veri Hatası ({symbol}): {r.status_code}")
            return None
        df = pd.DataFrame(r.json(), columns=['t','o','h','l','c','v','ct','qv','nt','tbv','tqv','i'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        return df
    except Exception as e:
        logger.error(f"Bağlantı Hatası ({symbol}): {e}")
        return None

def calc_signal(symbol):
    try:
        df = fetch_data(symbol, TF)
        if df is None or len(df) < 200: 
            return None

        # İndikatör hesaplamaları
        rsi = ta.rsi(df['c'], length=14).iloc[-1]
        ema200 = ta.ema(df['c'], length=200).iloc[-1]
        atr = ta.atr(df['h'], df['l'], df['c'], length=14).iloc[-1]
        last_price = df['c'].iloc[-1]
        
        avg_vol = df['v'].rolling(20).mean().iloc[-1]
        curr_vol = df['v'].iloc[-1]
        vol_boost = curr_vol > (avg_vol * 1.5)

        # Sinyal Kontrolü
        direction = None
        if last_price > ema200 and rsi < 35:
            direction = "LONG"
            emoji = "🟢"
            stop = round(last_price - (atr * 2), 4)
            tp = round(last_price + (atr * 3), 4)
        elif last_price < ema200 and rsi > 65:
            direction = "SHORT"
            emoji = "🔴"
            stop = round(last_price + (atr * 2), 4)
            tp = round(last_price - (atr * 3), 4)

        if direction:
            key = f"{symbol}_{direction}"
            now = datetime.now()
            if key in last_sent_signals:
                if now - last_sent_signals[key] < timedelta(minutes=COOLDOWN_MINUTES):
                    return None
            
            conf = 70
            if rsi < 25 or rsi > 75: conf += 10
            if vol_boost: conf += 20
            conf = min(conf, 100)

            if conf < 80: return None

            last_sent_signals[key] = now
            return (
                f"🎯 <b>#{symbol} {direction}</b> {emoji}\n\n"
                f"💵 <b>Giriş:</b> {last_price}\n"
                f"🛑 <b>Stop:</b> {stop}\n"
                f"💰 <b>Hedef (TP):</b> {tp}\n"
                f"⚡ <b>Güven:</b> %{conf}\n"
                f"📈 <b>Trend:</b> {'Boğa' if direction == 'LONG' else 'Ayı'}\n"
            )
    except Exception as e:
        logger.error(f"Sinyal hesaplama hatası ({symbol}): {e}")
    return None

def run(token, chat):
    global TOKEN, CHAT_ID
    TOKEN, CHAT_ID = token, chat
    
    logger.info("Bot ana döngüsü başlıyor...")
    tg_send("🚀 <b>KriptoAlper PRO Aktif!</b>\nVeri tarama başladı.")
    
    last_hb = datetime.now()
    while True:
        try:
            for sym in SYMBOLS:
                msg = calc_signal(sym)
                if msg:
                    tg_send(msg)
                time.sleep(1.5)

            if datetime.now() - last_hb > timedelta(minutes=30):
                tg_send("🛠 Tarama sorunsuz devam ediyor...")
                last_hb = datetime.now()

            time.sleep(120)
        except Exception as e:
            logger.error(f"Ana döngüde kritik hata: {e}")
            time.sleep(60)
