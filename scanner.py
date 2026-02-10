import ccxt
import pandas as pd
import pandas_ta as ta
import time
import requests
import logging

# --- AYARLAR (SENİN İÇİN OPTİMİZE EDİLDİ) ---
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
MIN_SCORE = 70  # Sadece %70 ve üzeri GÜÇLÜ sinyaller gelecek!
CHECK_INTERVAL = 300  # 5 dakikada bir tarar (Render dostu)

# --- TELEGRAM AYARLARI ---
TELEGRAM_TOKEN = "7939989932:AAFoR-x0_-x6XGg6wk4T-1Fw_xX7JgQo22U"
TELEGRAM_CHAT_ID = "6046182181"

# --- LOGLAMA ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger()

# --- BİNANCE BAĞLANTISI ---
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
        logger.error(f"Telegram mesajı gönderilemedi: {e}")

def calculate_indicators(df):
    try:
        # RSI
        df['RSI'] = ta.rsi(df['close'], length=14)
        
        # MACD
        macd = ta.macd(df['close'], fast=12, slow=26, signal=9)
        df['MACD'] = macd['MACD_12_26_9']
        df['MACD_SIGNAL'] = macd['MACDs_12_26_9']
        
        # Bollinger Bands
        bb = ta.bbands(df['close'], length=20, std=2)
        df['BB_UPPER'] = bb['BBU_20_2.0']
        df['BB_LOWER'] = bb['BBL_20_2.0']
        
        # EMA
        df['EMA_50'] = ta.ema(df['close'], length=50)
        df['EMA_200'] = ta.ema(df['close'], length=200)
        
        # Stochastic
        stoch = ta.stoch(df['high'], df['low'], df['close'], k=14, d=3, smooth_k=3)
        df['STOCH_K'] = stoch['STOCHk_14_3_3']
        df['STOCH_D'] = stoch['STOCHd_14_3_3']
        
        # ADX (Trend Gücü)
        adx = ta.adx(df['high'], df['low'], df['close'], length=14)
        df['ADX'] = adx['ADX_14']
        
        # ATR (Volatilite - Hedef/Stop için)
        df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        
        return df
    except Exception as e:
        logger.error(f"İndikatör hesaplama hatası: {e}")
        return df

def analyze_market(symbol):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=100)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df = calculate_indicators(df)
        
        last_row = df.iloc[-1]
        prev_row = df.iloc[-2]
        
        score = 0
        signal = "NEUTRAL"
        
        # --- PUANLAMA MANTIĞI (GÜVEN SKORU) ---
        
        # 1. RSI (Aşırı Alım/Satım)
        if last_row['RSI'] < 35: score += 20  # Aşırı satım, Long ihtimali
        elif last_row['RSI'] > 65: score += 20  # Aşırı alım, Short ihtimali
        
        # 2. MACD (Kesişim)
        if last_row['MACD'] > last_row['MACD_SIGNAL']: score += 15 # Long Sinyali
        elif last_row['MACD'] < last_row['MACD_SIGNAL']: score += 15 # Short Sinyali
        
        # 3. Bollinger Bantları (Tepki)
        if last_row['close'] < last_row['BB_LOWER']: score += 15
        elif last_row['close'] > last_row['BB_UPPER']: score += 15
        
        # 4. Stochastic (Onay)
        if last_row['STOCH_K'] < 20 and last_row['STOCH_D'] < 20: score += 15
        elif last_row['STOCH_K'] > 80 and last_row['STOCH_D'] > 80: score += 15
        
        # 5. Trend (EMA)
        if last_row['close'] > last_row['EMA_50']: score += 10
        elif last_row['close'] < last_row['EMA_50']: score += 10
        
        # 6. ADX (Trendin Gücü - Ölü piyasayı eler)
        if last_row['ADX'] > 20: score += 25 # Güçlü trend varsa puan artır!
        
        # --- SİNYAL YÖNÜ ---
        if score >= MIN_SCORE:
            if last_row['RSI'] < 45 and last_row['MACD'] > last_row['MACD_SIGNAL']:
                signal = "LONG"
            elif last_row['RSI'] > 55 and last_row['MACD'] < last_row['MACD_SIGNAL']:
                signal = "SHORT"
            else:
                score = 0 # Yön belirsizse puanı sıfırla
                
        return signal, score, last_row['close'], last_row['ATR']
        
    except Exception as e:
        logger.error(f"{symbol} analiz hatası: {e}")
        return "ERROR", 0, 0, 0

def run_bot():
    logger.info(f"🦁 ASLAN v8.3 BAŞLATILDI - HEDEF: %{MIN_SCORE} GÜVEN SKORU")
    send_telegram_message(f"🦁 **ASLAN v8.3 AKTİF!**\n\n🎯 **Hedef:** Yüksek Güven (%{MIN_SCORE}+)\n🛡️ **Mod:** Sniper (Hata Korumalı)\n🚀 **Bol Kazançlar Aslan!**")
    
    while True:
        try:
            logger.info("Piyasa taranıyor...")
            
            for symbol in SYMBOL_LIST:
                signal, score, price, atr = analyze_market(symbol)
                
                if score >= MIN_SCORE and signal in ["LONG", "SHORT"]:
                    # HEDEF VE STOP HESAPLAMA (Makul Seviyeler)
                    stop_loss = price - (atr * 1.5) if signal == "LONG" else price + (atr * 1.5)
                    take_profit = price + (atr * 3.0) if signal == "LONG" else price - (atr * 3.0)
                    
                    # Yüzdelik Hesap (Bilgi için)
                    tp_pct = abs((take_profit - price) / price) * 100
                    sl_pct = abs((stop_loss - price) / price) * 100
                    
                    # MESAJ FORMATI
                    emoji = "🟢" if signal == "LONG" else "🔴"
                    msg = (
                        f"🦁 **#{symbol.replace('/USDT', '')} | {signal}** {emoji}\n\n"
                        f"📍 **Giriş:** {price:.4f}\n"
                        f"🎯 **Hedef (TP):** {take_profit:.4f} (%{tp_pct:.2f})\n"
                        f"🛑 **Stop (SL):** {stop_loss:.4f} (%{sl_pct:.2f})\n\n"
                        f"🔥 **Güven Skoru:** %{score}\n"
                        f"📊 **ATR:** {atr:.4f}\n\n"
                        f"⚠️ _Manuel Giriş Yap - Stopu İhmal Etme!_"
                    )
                    
                    send_telegram_message(msg)
                    logger.info(f"SİNYAL BULUNDU: {symbol} - {signal} - Skor: {score}")
                    
                time.sleep(1) # API limitine takılmamak için kısa bekleme

            logger.info(f"Tarama bitti. {CHECK_INTERVAL} saniye bekleniyor...")
            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            # ANTI-CRASH BLOK (Bot hatada kapanmaz, tekrar dener)
            logger.error(f"⚠️ BEKLENMEDİK HATA: {e}")
            logger.info("Bot 10 saniye içinde kendini toparlayıp devam edecek...")
            time.sleep(10)

if __name__ == "__main__":
    run_bot()
