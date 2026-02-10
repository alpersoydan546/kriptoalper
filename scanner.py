from flask import Flask
from threading import Thread
import ccxt
import pandas as pd
import pandas_ta as ta
import time
import requests
import logging
import os

# --- FLASK AYARLARI (RENDER İÇİN GEREKLİ) ---
app = Flask(__name__)

@app.route('/')
def home():
    return "🦁 ASLAN BOT ÇALIŞIYOR - v8.4 AKTİF"

def run_flask():
    # Render'ın verdiği portu dinle, yoksa 8080 kullan
    port = int(os.environ.get("PORT", 8080)) 
    app.run(host='0.0.0.0', port=port)

# --- BOT AYARLARI ---
SYMBOL_LIST = [
    'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT', 'DOGE/USDT',
    'ADA/USDT', 'AVAX/USDT', 'TRX/USDT', 'LINK/USDT', 'MATIC/USDT',
    'DOT/USDT', 'LTC/USDT', 'BCH/USDT', 'ATOM/USDT', 'UNI/USDT',
    'FIL/USDT', 'IMX/USDT', 'APT/USDT', 'OP/USDT', 'ARB/USDT',
    'PEPE/USDT', 'RNDR/USDT', 'INJ/USDT', 'NEAR/USDT', 'STX/USDT',
    'FET/USDT', 'GALA/USDT', 'WIF/USDT', 'JUP/USDT', 'BONK/USDT',
    'FLOKI/USDT', 'SEI/USDT', 'SUI/USDT', 'TIA/USDT', 'LDO/USDT',
    'EOS/USDT', 'ALGO/USDT'
]

TIMEFRAME = '15m'
MIN_SCORE = 70  # Sadece %70 ve üzeri GÜÇLÜ sinyaller
CHECK_INTERVAL = 300 # 5 Dakika

# --- TELEGRAM AYARLARI ---
TELEGRAM_TOKEN = "7939989932:AAFoR-x0_-x6XGg6wk4T-1Fw_xX7JgQo22U"
TELEGRAM_CHAT_ID = "6046182181"

# --- LOGLAMA ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()

exchange = ccxt.binance({
    'rateLimit': 1200,
    'enableRateLimit': True,
    'options': {'defaultType': 'future'}
})

def send_telegram_message(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        requests.post(url, data=data)
    except Exception as e:
        logger.error(f"Telegram hatası: {e}")

def calculate_indicators(df):
    try:
        df['RSI'] = ta.rsi(df['close'], length=14)
        macd = ta.macd(df['close'], fast=12, slow=26, signal=9)
        df['MACD'] = macd['MACD_12_26_9']
        df['MACD_SIGNAL'] = macd['MACDs_12_26_9']
        df['EMA_50'] = ta.ema(df['close'], length=50)
        stoch = ta.stoch(df['high'], df['low'], df['close'], k=14, d=3, smooth_k=3)
        df['STOCH_K'] = stoch['STOCHk_14_3_3']
        df['STOCH_D'] = stoch['STOCHd_14_3_3']
        adx = ta.adx(df['high'], df['low'], df['close'], length=14)
        df['ADX'] = adx['ADX_14']
        df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        bb = ta.bbands(df['close'], length=20, std=2)
        df['BB_LOWER'] = bb['BBL_20_2.0']
        df['BB_UPPER'] = bb['BBU_20_2.0']
        return df
    except:
        return df

def analyze_market(symbol):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=100)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df = calculate_indicators(df)
        last = df.iloc[-1]
        
        score = 0
        signal = "NEUTRAL"
        
        # Puanlama
        if last['RSI'] < 35: score += 20
        elif last['RSI'] > 65: score += 20
        
        if last['MACD'] > last['MACD_SIGNAL']: score += 15
        elif last['MACD'] < last['MACD_SIGNAL']: score += 15
        
        if last['STOCH_K'] < 20: score += 15
        elif last['STOCH_K'] > 80: score += 15
        
        if last['close'] > last['EMA_50']: score += 10
        elif last['close'] < last['EMA_50']: score += 10
        
        if last['ADX'] > 20: score += 25
        
        if last['close'] < last['BB_LOWER']: score += 15
        elif last['close'] > last['BB_UPPER']: score += 15

        if score >= MIN_SCORE:
            if last['RSI'] < 45 and last['MACD'] > last['MACD_SIGNAL']: signal = "LONG"
            elif last['RSI'] > 55 and last['MACD'] < last['MACD_SIGNAL']: signal = "SHORT"
        
        return signal, score, last['close'], last['ATR']
    except Exception as e:
        logger.error(f"{symbol} hatası: {e}")
        return "ERROR", 0, 0, 0

def bot_loop():
    logger.info("🦁 ASLAN BOT v8.4 (Flask + Threading) BAŞLATILDI")
    send_telegram_message("🦁 **ASLAN v8.4 DEVREDE!**\n\n🛡️ **Render Modu:** Aktif\n🎯 **Hedef:** %70 Güven Skoru\n🔥 **Bol Kazançlar!**")
    
    while True:
        try:
            for symbol in SYMBOL_LIST:
                signal, score, price, atr = analyze_market(symbol)
                
                if score >= MIN_SCORE and signal in ["LONG", "SHORT"]:
                    sl = price - (atr * 1.5) if signal == "LONG" else price + (atr * 1.5)
                    tp = price + (atr * 3.0) if signal == "LONG" else price - (atr * 3.0)
                    
                    tp_pct = abs((tp - price) / price) * 100
                    sl_pct = abs((sl - price) / price) * 100
                    
                    emoji = "🟢" if signal == "LONG" else "🔴"
                    msg = (
                        f"🦁 **#{symbol.replace('/USDT', '')} | {signal}** {emoji}\n\n"
                        f"📍 **Giriş:** {price:.4f}\n"
                        f"🎯 **Hedef (TP):** {tp:.4f} (%{tp_pct:.2f})\n"
                        f"🛑 **Stop (SL):** {sl:.4f} (%{sl_pct:.2f})\n\n"
                        f"🔥 **Skor:** %{score}\n"
                        f"⚠️ _Manuel Giriş Yap!_"
                    )
                    send_telegram_message(msg)
                    logger.info(f"SİNYAL: {symbol} Skor: {score}")
                
                time.sleep(1) # API limit koruma
            
            logger.info("Tarama bitti, bekleniyor...")
            time.sleep(CHECK_INTERVAL)
            
        except Exception as e:
            logger.error(f"Bot Döngü Hatası: {e}")
            time.sleep(10)

if __name__ == "__main__":
    # Botu ayrı bir iş parçacığında (Thread) başlat
    t = Thread(target=bot_loop)
    t.start()
    
    # Flask sunucusunu başlat (Render'ın portu görmesi için)
    run_flask()
