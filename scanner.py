import ccxt
import pandas as pd
import pandas_ta as ta
import time
import requests
import logging
import json
import os
from threading import Thread
from datetime import datetime
from flask import Flask

# --- AYARLAR ---
SYMBOL_LIST = [
    # --- MAJORS (Demirbaşlar) ---
    'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT', # Senin eski dost :)
    'BNB/USDT', 'AVAX/USDT', 'LINK/USDT',
    
    # --- MEME COINS (Volatilite Kralları) ---
    'DOGE/USDT', 'PEPE/USDT', 'WIF/USDT', 'BONK/USDT', 'FLOKI/USDT',
    
    # --- YENİ YILDIZLAR (Trend & Hacim) ---
    'SUI/USDT', 'SEI/USDT', 'TIA/USDT', 'APT/USDT',
    'ORDI/USDT', 'PYTH/USDT', 'ONDO/USDT', 'JUP/USDT',
    'PENDLE/USDT', 'ENS/USDT',
    
    # --- AI & RWA (Yapay Zeka & Teknoloji) ---
    'RNDR/USDT', 'FET/USDT', 'WLD/USDT', 'NEAR/USDT',
    
    # --- KATMAN 2 (Hızlılar) ---
    'ARB/USDT', 'OP/USDT', 'IMX/USDT', 'STX/USDT',
    
    # --- HAREKETLİ OYUNCULAR ---
    'TRX/USDT', 'GALA/USDT', 'INJ/USDT', 'LDO/USDT'
]

TIMEFRAME = '15m'       
LOOKBACK = 50           
CHECK_INTERVAL = 300    
HEARTBEAT_INTERVAL = 1800 
TRADES_FILE = "active_trades.json"
STATS_FILE = "daily_stats.json"

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
    return "🦁 ASLAN v10.1 ONLINE - GÜNCEL KADRO"

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

# --- DOSYA İŞLEMLERİ ---
def load_json(filename):
    try:
        if os.path.exists(filename):
            with open(filename, 'r') as f:
                return json.load(f)
        return {}
    except:
        return {}

def save_json(filename, data):
    try:
        with open(filename, 'w') as f:
            json.dump(data, f)
    except:
        pass

# --- GÜNLÜK İSTATİSTİK ---
def update_stats(result, pnl):
    stats = load_json(STATS_FILE)
    today = datetime.now().strftime("%Y-%m-%d")
    if stats.get("date") != today:
        stats = {"date": today, "win": 0, "loss": 0, "pnl": 0.0}
    
    if result == "WIN": stats["win"] += 1
    elif result == "LOSS": stats["loss"] += 1
    stats["pnl"] += pnl
    save_json(STATS_FILE, stats)

def send_daily_report(token, chat_id):
    stats = load_json(STATS_FILE)
    today = datetime.now().strftime("%Y-%m-%d")
    if stats.get("date") != today: return
        
    # GÜNLÜK RAPOR - SEÇENEK 1
    msg = (
        f"📅 **GÜNLÜK RAPOR**\n\n"
        f"✅ **Başarılı:** {stats['win']}\n"
        f"❌ **Başarısız:** {stats['loss']}\n\n"
        f"💰 **Net PnL:** %{stats['pnl']:.2f}"
    )
    send_telegram_message(token, chat_id, msg)

# --- MİMAR ANALİZİ ---
def analyze_price_action(symbol):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=LOOKBACK)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        current_price = df['close'].iloc[-1]
        
        support = df['low'].min()
        resistance = df['high'].max()
        rsi = ta.rsi(df['close'], length=14).iloc[-1]
        
        signal = "NEUTRAL"
        tp = 0; sl = 0; score = 50 
        
        dist_to_support = (current_price - support) / support * 100
        dist_to_resistance = (resistance - current_price) / current_price * 100
        
        # LONG KURALLARI
        if dist_to_support < 3 and rsi < 50: 
            signal = "LONG"
            sl = support * 0.995 
            tp = resistance * 0.99 
            score += (50 - rsi) + ((3 - dist_to_support) * 5)
            
        # SHORT KURALLARI
        elif dist_to_resistance < 3 and rsi > 50:
            signal = "SHORT"
            sl = resistance * 1.005 
            tp = support * 1.01 
            score += (rsi - 50) + ((3 - dist_to_resistance) * 5)
            
        if signal != "NEUTRAL":
            risk = abs(current_price - sl)
            reward = abs(tp - current_price)
            if reward < (risk * 1.5): return "NEUTRAL", 0, 0, 0, 0
        
        score = min(score, 99)
        return signal, current_price, tp, sl, int(score)
    except:
        return "ERROR", 0, 0, 0, 0

def check_active_trades(token, chat_id):
    trades = load_json(TRADES_FILE)
    if not trades: return
    updated_trades = trades.copy()
    
    for symbol, trade in trades.items():
        try:
            ticker = exchange.fetch_ticker(symbol)
            price = ticker['last']
            
            # SONUÇ MESAJI - SEÇENEK 1 (DİKEY NET)
            if (trade['signal'] == "LONG" and price >= trade['tp']) or \
               (trade['signal'] == "SHORT" and price <= trade['tp']):
                pnl = abs((price - trade['entry']) / trade['entry']) * 100
                msg = (
                    f"✅ **{symbol.replace('/USDT', '')} | HEDEF**\n\n"
                    f"💰 **Kâr:** +%{pnl:.2f}\n"
                    f"💵 **Fiyat:** {price}"
                )
                send_telegram_message(token, chat_id, msg)
                update_stats("WIN", pnl)
                del updated_trades[symbol]
                
            elif (trade['signal'] == "LONG" and price <= trade['sl']) or \
                 (trade['signal'] == "SHORT" and price >= trade['sl']):
                loss = abs((price - trade['entry']) / trade['entry']) * 100
                msg = (
                    f"❌ **{symbol.replace('/USDT', '')} | STOP**\n\n"
                    f"📉 **Zarar:** -%{loss:.2f}\n"
                    f"💵 **Fiyat:** {price}"
                )
                send_telegram_message(token, chat_id, msg)
                update_stats("LOSS", -loss)
                del updated_trades[symbol]
        except:
            continue
    save_json(TRADES_FILE, updated_trades)

def bot_loop(token, chat_id):
    logger.info("🦁 ASLAN v10.1 BAŞLATILDI")
    # BAŞLANGIÇ - SEÇENEK 1
    send_telegram_message(token, chat_id, "Sistem Online 🟢\nv10.1 (Güncel Kadro)\nBinance Bağlantısı: ✅")
    
    last_heartbeat = time.time()
    last_report_date = datetime.now().day
    
    while True:
        try:
            check_active_trades(token, chat_id)
            trades = load_json(TRADES_FILE)
            
            # NABIZ - SEÇENEK 1 (SADE)
            if time.time() - last_heartbeat > HEARTBEAT_INTERVAL:
                send_telegram_message(token, chat_id, "💓 **Sistem Aktif**\n_Tarama sürüyor..._")
                last_heartbeat = time.time()
            
            current_day = datetime.now().day
            if current_day != last_report_date:
                send_daily_report(token, chat_id)
                last_report_date = current_day

            for symbol in SYMBOL_LIST:
                if symbol in trades: continue
                
                signal, price, tp, sl, score = analyze_price_action(symbol)
                
                if signal in ["LONG", "SHORT"]:
                    emoji = "🟢" if signal == "LONG" else "🔴"
                    # SİNYAL - SEÇENEK 7 (MODERN DİKEY + SKOR)
                    msg = (
                        f"🦁 **#{symbol.replace('/USDT', '')}**\n"
                        f"{emoji} **{signal}**\n\n"
                        f"📍 **{price}**\n"
                        f"🎯 **{tp:.4f}**\n"
                        f"🛡️ **{sl:.4f}**\n"
                        f"💎 **%{score}**"
                    )
                    send_telegram_message(token, chat_id, msg)
                    trades[symbol] = {"signal": signal, "entry": price, "tp": tp, "sl": sl}
                    save_json(TRADES_FILE, trades)
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
