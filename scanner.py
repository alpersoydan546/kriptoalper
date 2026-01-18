import time
import requests
import pandas as pd
import pandas_ta as ta
import os
import logging
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger()

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TF = os.getenv("TF", "15m") 

# COIN LİSTESİ GENİŞLETİLDİ (25 Popüler Coin)
SYMBOLS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
    "ADAUSDT","DOGEUSDT","LINKUSDT","AVAXUSDT","DOTUSDT",
    "MATICUSDT","LTCUSDT","BCHUSDT","TRXUSDT","ETCUSDT",
    "NEARUSDT","FILUSDT","APTUSDT","SUIUSDT","OPUSDT",
    "ARBUSDT","INJUSDT","TIAUSDT","ORDIUSDT","STXUSDT"
]

last_sent_signals = {}
COOLDOWN_MINUTES = 120 # Fırsatları kaçırmamak için 2 saate indirdik

def tg_send(msg):
    if not TOKEN or not CHAT_ID: return
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except: pass

def fetch_data(symbol, interval, limit=250):
    url = "https://fapi.binance.com/fapi/v1/klines"
    try:
        r = requests.get(url, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=10)
        df = pd.DataFrame(r.json(), columns=['t','o','h','l','c','v','ct','qv','nt','tbv','tqv','i'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        return df
    except: return None

def calc_signal(symbol):
    try:
        df = fetch_data(symbol, TF)
        if df is None or len(df) < 200: return None

        # GÖSTERGELER
        rsi = ta.rsi(df['c'], length=14).iloc[-1]
        ema200 = ta.ema(df['c'], length=200).iloc[-1]
        atr = ta.atr(df['h'], df['l'], df['c'], length=14).iloc[-1]
        last_price = df['c'].iloc[-1]
        
        avg_vol = df['v'].rolling(20).mean().iloc[-1]
        curr_vol = df['v'].iloc[-1]

        direction = None
        reasons = []

        # ANA STRATEJİ KONTROLÜ
        if last_price > ema200 and rsi < 38:
            direction = "LONG"
            reasons.append("✅ Trend Üstü (Boğa)")
            reasons.append(f"📉 RSI Dipte ({round(rsi,1)})")
        elif last_price < ema200 and rsi > 62:
            direction = "SHORT"
            reasons.append("✅ Trend Altı (Ayı)")
            reasons.append(f"📈 RSI Tepede ({round(rsi,1)})")

        if direction:
            # GÜVEN HESABI (Şeffaf Mod)
            conf_score = 60 # Baz puan
            
            # Hacim Bonusu
            if curr_vol > avg_vol * 1.5:
                conf_score += 20
                reasons.append("🔥 Yüksek Hacim Onayı")
            elif curr_vol > avg_vol:
                conf_score += 10
                reasons.append("📊 Hacim Artışı")

            # RSI Ekstrem Bonusu
            if rsi < 25 or rsi > 75:
                conf_score += 20
                reasons.append("⚡ Aşırı Alım/Satım Bölgesi")

            if conf_score < 70: return None # %70 altını ele

            key = f"{symbol}_{direction}"
            now = datetime.now()
            if key in last_sent_signals:
                if now - last_sent_signals[key] < timedelta(minutes=COOLDOWN_MINUTES):
                    return None

            last_sent_signals[key] = now
            
            # MESAJ FORMATI
            reason_text = "\n".join(reasons)
            stop = round(last_price - (atr * 1.5), 4) if direction == "LONG" else round(last_price + (atr * 1.5), 4)
            tp = round(last_price + (atr * 2.5), 4) if direction == "LONG" else round(last_price - (atr * 2.5), 4)

            return (
                f"🎯 <b>#{symbol} {direction} Sinyali</b>\n"
                f"----------------------------------\n"
                f"{reason_text}\n"
                f"----------------------------------\n"
                f"💵 <b>Giriş:</b> {last_price}\n"
                f"🛑 <b>Stop:</b> {stop}\n"
                f"💰 <b>Hedef:</b> {tp}\n\n"
                f"⚡ <b>GÜVEN PUANI: %{conf_score}</b>"
            )
    except Exception as e:
        logger.error(f"Hata {symbol}: {e}")
    return None

def run(token, chat):
    global TOKEN, CHAT_ID
    TOKEN, CHAT_ID = token, chat
    tg_send("🚀 <b>PRO Scanner v2 Başlatıldı!</b>\n25 Coin taranıyor, detaylı analiz aktif.")
    
    while True:
        try:
            for sym in SYMBOLS:
                msg = calc_signal(sym)
                if msg: tg_send(msg)
                time.sleep(1.2) # Rate limit koruması

            time.sleep(120)
        except Exception as e:
            time.sleep(60)
