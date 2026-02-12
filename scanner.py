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

TIMEFRAME = '15m'       # Analiz Zamanı
LOOKBACK = 50           # Geriye dönük kaç muma bakıp destek/direnç çizecek?
CHECK_INTERVAL = 300    # 5 Dakika
HEARTBEAT_INTERVAL = 1800 
TRADES_FILE = "active_trades.json"

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
    return "🦁 ASLAN v10.0 - MİMAR MODU AKTİF"

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

# --- MİMAR ANALİZİ (Price Action) ---
def analyze_price_action(symbol):
    try:
        # Son 50 mumu çek
        bars = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=LOOKBACK)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        current_price = df['close'].iloc[-1]
        
        # DESTEK (Swing Low) ve DİRENÇ (Swing High) Bul
        # Son 50 mumun en düşüğü ve en yükseği
        support = df['low'].min()
        resistance = df['high'].max()
        
        # RSI Kontrolü (Aşırı alım/satım var mı?)
        rsi = ta.rsi(df['close'], length=14).iloc[-1]
        
        signal = "NEUTRAL"
        tp = 0
        sl = 0
        
        # STRATEJİ: Fiyat Desteğe Yakınsa AL, Dirence Yakınsa SAT
        # Destekten %2 yukarıdaysa hala "Destek Bölgesi" sayılır.
        
        dist_to_support = (current_price - support) / support * 100
        dist_to_resistance = (resistance - current_price) / current_price * 100
        
        # LONG SENARYOSU (Destekten Dönüş)
        # Fiyat desteğe %3 kadar yakınsa VE RSI < 45 ise (Henüz şişmemişse)
        if dist_to_support < 3 and rsi < 45: 
            signal = "LONG"
            sl = support * 0.995 # Stopu desteğin HAFİF altına koy (%0.5 altı)
            tp = resistance * 0.99 # Hedefi direncin HAFİF altına koy
            
        # SHORT SENARYOSU (Dirençten Dönüş)
        # Fiyat dirence %3 kadar yakınsa VE RSI > 55 ise
        elif dist_to_resistance < 3 and rsi > 55:
            signal = "SHORT"
            sl = resistance * 1.005 # Stopu direncin HAFİF üstüne koy
            tp = support * 1.01 # Hedefi desteğin HAFİF üstüne koy
            
        # RİSK / KAZANÇ KONTROLÜ (Risk Reward Ratio)
        # Eğer Kazanç potansiyeli, Riskten büyük değilse girme!
        if signal != "NEUTRAL":
            risk = abs(current_price - sl)
            reward = abs(tp - current_price)
            if reward < (risk * 1.5): # En az 1.5 kat kazanç vaat etmeli
                return "NEUTRAL", 0, 0, 0
        
        return signal, current_price, tp, sl

    except Exception:
        return "ERROR", 0, 0, 0

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
                # MİNİMAL SONUÇ MESAJI
                msg = f"🦁 **{symbol.replace('/USDT', '')}** ✅ HEDEF GELDİ\n💰 **Fiyat:** {price}"
                send_telegram_message(token, chat_id, msg)
                del updated_trades[symbol]
                
            # STOP OLMA
            elif (trade['signal'] == "LONG" and price <= trade['sl']) or \
                 (trade['signal'] == "SHORT" and price >= trade['sl']):
                # MİNİMAL SONUÇ MESAJI
                msg = f"🦁 **{symbol.replace('/USDT', '')}** ❌ STOP OLDU\n📉 **Fiyat:** {price}"
                send_telegram_message(token, chat_id, msg)
                del updated_trades[symbol]
        except:
            continue
    save_trades(updated_trades)

def bot_loop(token, chat_id):
    logger.info("🦁 ASLAN v10.0 BAŞLATILDI")
    send_telegram_message(token, chat_id, "🦁 **ASLAN v10.0 (MİMAR)**\n🏗️ Destek/Direnç Analizi: Aktif\n⏳ Mesajlar: Minimal\n🚀 Başarılar Alperen!")
    
    last_heartbeat = time.time()
    
    while True:
        try:
            check_active_trades(token, chat_id)
            trades = load_trades()
            
            if time.time() - last_heartbeat > HEARTBEAT_INTERVAL:
                send_telegram_message(token, chat_id, "🦁 Nöbetteyim...")
                last_heartbeat = time.time()
            
            for symbol in SYMBOL_LIST:
                if symbol in trades: continue
                
                signal, price, tp, sl = analyze_price_action(symbol)
                
                if signal in ["LONG", "SHORT"]:
                    emoji = "🟢" if signal == "LONG" else "🔴"
                    
                    # --- MİNİMAL MESAJ FORMATI ---
                    msg = (
                        f"🦁 **#{symbol.replace('/USDT', '')} | {signal}** {emoji}\n"
                        f"💰 {price}\n"
                        f"🎯 {tp:.4f}\n"
                        f"🛡️ {sl:.4f}"
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
