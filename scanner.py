import ccxt
import pandas as pd
import pandas_ta as ta
import time
import requests
import logging
import json
import os
from threading import Thread
from flask import Flask

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

TIMEFRAME = '15m'       # Giriş Sinyali
TREND_TIMEFRAME = '1h'  # Trend Teyidi (Filtre)
MIN_SCORE = 65          # Trend Filtreli Güven Skoru
CHECK_INTERVAL = 300    # 5 Dakika
TRADES_FILE = "active_trades.json"

# --- LOGLAMA ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()

# --- BİNANCE BAĞLANTISI ---
exchange = ccxt.binance({
    'rateLimit': 1200,
    'enableRateLimit': True,
    'options': {'defaultType': 'future'}
})

# --- FLASK (Render İçin) ---
app = Flask(__name__)

@app.route('/')
def home():
    return "🦁 ASLAN v9.0 - TREND AVCISI AKTİF"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

# --- YARDIMCI FONKSİYONLAR ---
def send_telegram_message(token, chat_id, message):
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
        requests.post(url, data=data)
    except Exception as e:
        logger.error(f"Telegram hatası: {e}")

def load_trades():
    try:
        if os.path.exists(TRADES_FILE):
            with open(TRADES_FILE, 'r') as f:
                return json.load(f)
        return {}
    except Exception:
        return {}

def save_trades(trades):
    try:
        with open(TRADES_FILE, 'w') as f:
            json.dump(trades, f)
    except Exception as e:
        logger.error(f"Dosya kaydetme hatası: {e}")

# --- İNDİKATÖRLER VE TREND ---
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

def get_trend_direction(symbol):
    """
    1 Saatlik grafiğe bakarak ANA TRENDİ belirler.
    EMA 50'nin üstündeyse YÜKSELİŞ, altındaysa DÜŞÜŞ.
    """
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=TREND_TIMEFRAME, limit=60)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        ema_50 = ta.ema(df['close'], length=50).iloc[-1]
        current_price = df['close'].iloc[-1]
        
        if current_price > ema_50:
            return "LONG"
        else:
            return "SHORT"
    except:
        return "NEUTRAL"

# --- ANALİZ VE TAKİP ---
def check_active_trades(token, chat_id):
    trades = load_trades()
    if not trades:
        return

    updated_trades = trades.copy()
    
    for symbol, trade in trades.items():
        try:
            ticker = exchange.fetch_ticker(symbol)
            current_price = ticker['last']
            
            signal = trade['signal']
            tp = trade['tp']
            sl = trade['sl']
            entry = trade['entry']
            
            # KÂR ALMA (AV BAŞARILI)
            if (signal == "LONG" and current_price >= tp) or \
               (signal == "SHORT" and current_price <= tp):
                
                pnl = abs((current_price - entry) / entry) * 100
                # SENİN İSTEDİĞİN FORMAT
                msg = (
                    f"🦁 **AV BAŞARILI!** 🟢\n\n"
                    f"**#{symbol.replace('/USDT', '')}** hedefe vurdu!\n"
                    f"💰 **Kâr:** %{pnl:.2f}\n"
                    f"💵 **Fiyat:** {current_price}"
                )
                send_telegram_message(token, chat_id, msg)
                del updated_trades[symbol]
                
            # STOP OLMA (AV KAÇTI)
            elif (signal == "LONG" and current_price <= sl) or \
                 (signal == "SHORT" and current_price >= sl):
                
                loss = abs((current_price - entry) / entry) * 100
                # SENİN İSTEDİĞİN FORMAT
                msg = (
                    f"🦁 **AV KAÇTI** 🔴\n\n"
                    f"**#{symbol.replace('/USDT', '')}** stop oldu.\n"
                    f"📉 **Zarar:** %{loss:.2f}\n"
                    f"💵 **Fiyat:** {current_price}"
                )
                send_telegram_message(token, chat_id, msg)
                del updated_trades[symbol]
                
        except Exception as e:
            logger.error(f"Takip hatası ({symbol}): {e}")

    save_trades(updated_trades)

def analyze_market(symbol):
    try:
        # 1. Önce 15 Dakikalık Verileri Çek
        bars = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=100)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df = calculate_indicators(df)
        last = df.iloc[-1]
        
        score = 0
        signal = "NEUTRAL"
        
        # --- PUANLAMA (15 Dakikalık) ---
        if last['RSI'] < 35: score += 20
        elif last['RSI'] > 65: score += 20
        
        if last['MACD'] > last['MACD_SIGNAL']: score += 15
        elif last['MACD'] < last['MACD_SIGNAL']: score += 15
        
        if last['STOCH_K'] < 20: score += 15
        elif last['STOCH_K'] > 80: score += 15
        
        if last['close'] > last['EMA_50']: score += 10
        elif last['close'] < last['EMA_50']: score += 10
        
        if last['ADX'] > 20: score += 25

        # Sinyal Yönü Belirle
        if score >= 50: # Geçici kontrol
            if last['RSI'] < 45 and last['MACD'] > last['MACD_SIGNAL']: signal = "LONG"
            elif last['RSI'] > 55 and last['MACD'] < last['MACD_SIGNAL']: signal = "SHORT"
        
        # --- KRİTİK: TREND FİLTRESİ (1 Saatlik Teyit) ---
        if signal in ["LONG", "SHORT"]:
            main_trend = get_trend_direction(symbol)
            
            if main_trend == signal:
                score += 10 # Trend arkamızda, puanı artır! 🚀
            else:
                score -= 25 # Trend ters, puanı düşür! ⚠️ (Filtreleme)
        
        return signal, score, last['close'], last['ATR']

    except Exception:
        return "ERROR", 0, 0, 0

# --- ANA DÖNGÜ ---
def bot_loop(token, chat_id):
    logger.info("🦁 ASLAN v9.0 BAŞLATILDI")
    send_telegram_message(token, chat_id, "🦁 **ASLAN v9.0 (Trend Avcısı) DEVREDE!**\n\n✅ **Özellik:** 15m Sinyal + 1h Trend Teyidi\n💾 **Hafıza:** Aktif\n🚀 **Bol Kazançlar Alperen!**")
    
    while True:
        try:
            # 1. Açık İşlemleri Kontrol Et
            check_active_trades(token, chat_id)
            
            # 2. Yeni Sinyal Ara
            trades = load_trades()
            
            for symbol in SYMBOL_LIST:
                if symbol in trades: continue
                    
                signal, score, price, atr = analyze_market(symbol)
                
                # FİLTRE: Skor barajı 65 (Trend uyumsuzsa zaten puan düşüyor)
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
                    )
                    send_telegram_message(token, chat_id, msg)
                    
                    trades[symbol] = {
                        "signal": signal,
                        "entry": price,
                        "tp": tp,
                        "sl": sl,
                        "time": time.time()
                    }
                    save_trades(trades)
                    logger.info(f"YENİ İŞLEM: {symbol} - Skor: {score}")
                
                time.sleep(1) 
            
            time.sleep(CHECK_INTERVAL)
            
        except Exception as e:
            logger.error(f"Döngü hatası: {e}")
            time.sleep(10)

# --- RENDER BAŞLATICI ---
def run(token, chat_id):
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    bot_loop(token, chat_id)
