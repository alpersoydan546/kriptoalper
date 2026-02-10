import ccxt
import pandas as pd
import pandas_ta as ta
import time
import requests
import logging

# --- LOGLAMA ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()

# --- AYARLAR ---
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
MIN_SCORE = 70  # %70 Güven Skoru
CHECK_INTERVAL = 300  # 5 Dakika bekleme

# --- BİNANCE BAĞLANTISI ---
exchange = ccxt.binance({
    'rateLimit': 1200,
    'enableRateLimit': True,
    'options': {'defaultType': 'future'}
})

def send_telegram_message(token, chat_id, message):
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
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
        
        adx = ta.adx(df['high'], df['low'], df['close'], length=14)
        df['ADX'] = adx['ADX_14']
        
        df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=14)
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

        if score >= MIN_SCORE:
            if last['RSI'] < 45 and last['MACD'] > last['MACD_SIGNAL']: signal = "LONG"
            elif last['RSI'] > 55 and last['MACD'] < last['MACD_SIGNAL']: signal = "SHORT"
        
        return signal, score, last['close'], last['ATR']
    except Exception as e:
        logger.error(f"{symbol} hatası: {e}")
        return "ERROR", 0, 0, 0

# --- BURASI KRİTİK! app.py BU FONKSİYONU ARIYOR ---
def run(token, chat_id):
    logger.info("🦁 ASLAN BOT BAŞLATILDI (Scanner Modu)")
    send_telegram_message(token, chat_id, "🦁 **ASLAN BOT DEVREDE!**\n\n🎯 **Hedef:** %70+ Güven Skoru\n✅ **Sistem:** Stabil\n🚀 **Bol Kazançlar!**")
    
    while True:
        try:
            logger.info("Piyasa taranıyor...")
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
                    send_telegram_message(token, chat_id, msg)
                    logger.info(f"SİNYAL: {symbol} Skor: {score}")
                
                time.sleep(1) # API limit koruma
            
            logger.info("Bekleniyor...")
            time.sleep(CHECK_INTERVAL)
            
        except Exception as e:
            logger.error(f"Bot Döngü Hatası: {e}")
            time.sleep(10)
