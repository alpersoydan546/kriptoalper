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
TREND_TIMEFRAME = '1h'  # Trend Teyidi
MIN_SCORE = 55          # BARAJ DÜŞÜRÜLDÜ (Daha fazla işlem)
CHECK_INTERVAL = 300    # 5 Dakika
HEARTBEAT_INTERVAL = 1800 # 30 Dakikada bir Nabız
TRADES_FILE = "active_trades.json"

# --- LOGLAMA ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()

exchange = ccxt.binance({
    'rateLimit': 1200,
    'enableRateLimit': True,
    'options': {'defaultType': 'future'}
})

app = Flask(__name__)

@app.route('/')
def home():
    return "🦁 ASLAN v9.2 - AGRESİF MOD AKTİF"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

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
    except:
        return {}

def save_trades(trades):
    try:
        with open(TRADES_FILE, 'w') as f:
            json.dump(trades, f)
    except:
        pass

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
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=TREND_TIMEFRAME, limit=60)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        ema_50 = ta.ema(df['close'], length=50).iloc[-1]
        if df['close'].iloc[-1] > ema_50: return "LONG"
        else: return "SHORT"
    except:
        return "NEUTRAL"

def check_active_trades(token, chat_id):
    trades = load_trades()
    if not trades: return
    updated_trades = trades.copy()
    
    for symbol, trade in trades.items():
        try:
            ticker = exchange.fetch_ticker(symbol)
            price = ticker['last']
            
            # KÂR ALMA
            if (trade['signal'] == "LONG" and price >= trade['tp']) or \
               (trade['signal'] == "SHORT" and price <= trade['tp']):
                pnl = abs((price - trade['entry']) / trade['entry']) * 100
                msg = f"🦁 **AV BAŞARILI!** 🟢\n\n**#{symbol.replace('/USDT', '')}** Hedefe vurdu!\n💰 **Kâr:** %{pnl:.2f}\n💵 **Fiyat:** {price}"
                send_telegram_message(token, chat_id, msg)
                del updated_trades[symbol]
                
            # STOP OLMA
            elif (trade['signal'] == "LONG" and price <= trade['sl']) or \
                 (trade['signal'] == "SHORT" and price >= trade['sl']):
                loss = abs((price - trade['entry']) / trade['entry']) * 100
                msg = f"🦁 **AV KAÇTI** 🔴\n\n**#{symbol.replace('/USDT', '')}** Stop oldu.\n📉 **Zarar:** %{loss:.2f}\n💵 **Fiyat:** {price}"
                send_telegram_message(token, chat_id, msg)
                del updated_trades[symbol]
        except:
            continue
    save_trades(updated_trades)

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

        # Sinyal Yönü
        if score >= 40: # Temel sinyal varsa yön belirle
            if last['RSI'] < 45 and last['MACD'] > last['MACD_SIGNAL']: signal = "LONG"
            elif last['RSI'] > 55 and last['MACD'] < last['MACD_SIGNAL']: signal = "SHORT"
        
        # TREND FİLTRESİ (Gevşetilmiş)
        if signal in ["LONG", "SHORT"]:
            trend = get_trend_direction(symbol)
            if trend == signal: 
                score += 15 # Trend bizden yana, Puan artır
            else: 
                score -= 10 # Trend ters, ama sadece 10 puan kır (Eskiden 25'ti)
            
        return signal, score, last['close'], last['ATR']
    except:
        return "ERROR", 0, 0, 0

def bot_loop(token, chat_id):
    logger.info("🦁 ASLAN v9.2 BAŞLATILDI")
    send_telegram_message(token, chat_id, "🦁 **ASLAN v9.2 (AGRESİF) DEVREDE!**\n\n⚡ **Baraj:** 55 Puan\n🛡️ **Filtre:** Hafifletildi\n🚀 **Bol Kazançlar Alperen!**")
    
    last_heartbeat = time.time()
    
    while True:
        try:
            check_active_trades(token, chat_id)
            trades = load_trades()
            
            # Nabız Mesajı
            if time.time() - last_heartbeat > HEARTBEAT_INTERVAL:
                send_telegram_message(token, chat_id, "🦁 **Aslan Nöbette...**\nSistem aktif, tarama sürüyor. ⏳")
                last_heartbeat = time.time()
            
            for symbol in SYMBOL_LIST:
                if symbol in trades: continue
                signal, score, price, atr = analyze_market(symbol)
                
                # BARAJ 55 OLDU (Musluklar Açıldı)
                if score >= MIN_SCORE and signal in ["LONG", "SHORT"]:
                    sl = price - (atr * 1.5) if signal == "LONG" else price + (atr * 1.5)
                    tp = price + (atr * 3.0) if signal == "LONG" else price - (atr * 3.0)
                    
                    emoji = "🟢" if signal == "LONG" else "🔴"
                    msg = (
                        f"🦁 **#{symbol.replace('/USDT', '')} | {signal}** {emoji}\n\n"
                        f"📍 **Giriş:** {price:.4f}\n"
                        f"🎯 **Hedef:** {tp:.4f}\n"
                        f"🛑 **Stop:** {sl:.4f}\n"
                        f"🔥 **Skor:** %{score}\n"
                        f"⚠️ _Binance'ten Takip Et!_"
                    )
                    send_telegram_message(token, chat_id, msg)
                    trades[symbol] = {"signal": signal, "entry": price, "tp": tp, "sl": sl}
                    save_trades(trades)
                    time.sleep(1)
            
            time.sleep(CHECK_INTERVAL)
            
        except Exception as e:
            logger.error(f"Hata: {e}")
            time.sleep(10)

def run(token, chat_id):
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    bot_loop(token, chat_id)
