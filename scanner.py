import ccxt
import pandas as pd
import pandas_ta as ta
import time
import requests
import logging
import json
import os
import threading
from datetime import datetime
from flask import Flask

# --- [ PIRANHA (HYPER ACTIVE) AYARLARI ] ---
TIMEFRAME = '5m'           
LOOKBACK = 100             
SCAN_INTERVAL = 20         # Binance engellemesin diye biraz frenledik
TRADE_CHECK_INTERVAL = 5   
STATS_FILE = "daily_stats_render.json"  
TRADES_FILE = "active_trades_render.json"
TOP_COUNT = 50             
CACHE_REFRESH = 900        

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger()

exchange = ccxt.binance({
    'rateLimit': 1200,
    'enableRateLimit': True,
    'options': {'defaultType': 'future'}
})

app = Flask(__name__)
lock = threading.Lock()

@app.route('/')
def home(): return "☁️ PIRANHA v16.4 HYPER ONLINE"

def run_flask():
    try:
        port = int(os.environ.get("PORT", 10000))
        app.run(host='0.0.0.0', port=port)
    except: pass

def send_telegram(token, chat_id, message):
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
        requests.post(url, data=data, timeout=10)
    except Exception as e: logger.error(f"Telegram Hatası: {e}")

# --- [ DOSYA YÖNETİMİ ] ---
def load_json(filename):
    with lock:
        try:
            if os.path.exists(filename):
                with open(filename, 'r') as f: return json.load(f)
            return {}
        except: return {}

def save_json(filename, data):
    with lock:
        try:
            with open(filename, 'w') as f: json.dump(data, f, indent=4)
        except: pass

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
    
    msg = (
        f"☁️ Piranha Sonuç\n"
        f"🎯 {stats['win']} Hedef\n"
        f"🛡️ {stats['loss']} Stop\n"
        f"💰 %{stats['pnl']:.2f}"
    )
    send_telegram(token, chat_id, msg)

# --- [ BEKÇİ MODÜLÜ ] ---
def monitor_trades_thread(token, chat_id):
    logger.info("🛡️ PIRANHA BEKÇİSİ AKTİF")
    while True:
        try:
            trades = load_json(TRADES_FILE)
            if not trades:
                time.sleep(TRADE_CHECK_INTERVAL)
                continue

            updated_trades = trades.copy()
            trades_changed = False

            for symbol, trade in trades.items():
                try:
                    ticker = exchange.fetch_ticker(symbol)
                    current_price = ticker['last']
                    symbol_short = symbol.replace('/USDT', '')
                    
                    if (trade['signal'] == "LONG" and current_price >= trade['tp']) or \
                       (trade['signal'] == "SHORT" and current_price <= trade['tp']):
                        
                        pnl = abs((current_price - trade['entry']) / trade['entry']) * 100
                        msg = (f"☁️ {symbol_short}\n"
                               f"✅ Cepte\n"
                               f"💰 %{pnl:.2f}\n"
                               f"💎 Piranha")
                        send_telegram(token, chat_id, msg)
                        update_stats("WIN", pnl)
                        del updated_trades[symbol]
                        trades_changed = True
                    
                    elif (trade['signal'] == "LONG" and current_price <= trade['sl']) or \
                         (trade['signal'] == "SHORT" and current_price >= trade['sl']):
                        
                        loss = abs((current_price - trade['entry']) / trade['entry']) * 100
                        msg = (f"☁️ {symbol_short}\n"
                               f"❌ Stop\n"
                               f"📉 -%{loss:.2f}\n"
                               f"💎 Piranha")
                        send_telegram(token, chat_id, msg)
                        update_stats("LOSS", -loss)
                        del updated_trades[symbol]
                        trades_changed = True
                        
                except: continue
            
            if trades_changed:
                save_json(TRADES_FILE, updated_trades)

        except: pass
        time.sleep(TRADE_CHECK_INTERVAL)

# --- [ BEYİN: TOP 50 ] ---
def get_top_volume_symbols():
    try:
        tickers = exchange.fetch_tickers()
        usdt_tickers = [{'symbol': s, 'quoteVolume': float(v['quoteVolume'])} for s, v in tickers.items() if '/USDT' in s and 'quoteVolume' in v]
        sorted_tickers = sorted(usdt_tickers, key=lambda x: x['quoteVolume'], reverse=True)
        return [t['symbol'] for t in sorted_tickers[:TOP_COUNT]]
    except: 
        return ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT']

# --- [ STRATEJİ: HYPER ACTIVE (DAR BANT) ] ---
def analyze_scalp(symbol):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=LOOKBACK)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        if len(df) < 25: return "NEUTRAL", 0, 0, 0, 0

        current_price = df['close'].iloc[-1]
        
        # --- KRİTİK DEĞİŞİKLİK: std=1.5 yapıldı (Bantlar daraldı) ---
        bb = ta.bbands(df['close'], length=20, std=1.5)
        
        lower_band = bb['BBL_20_1.5'].iloc[-1]
        upper_band = bb['BBU_20_1.5'].iloc[-1]
        middle_band = bb['BBM_20_1.5'].iloc[-1]
        rsi = ta.rsi(df['close'], length=14).iloc[-1]
        
        signal = "NEUTRAL"; tp = 0; sl = 0; score = 50

        # LONG (RSI < 40 ve Dar Bant Dışı)
        if current_price <= lower_band and rsi < 40:
            signal = "LONG"
            tp = middle_band 
            sl = lower_band * 0.992
            score = 80 + (40 - rsi) 

        # SHORT (RSI > 60 ve Dar Bant Dışı)
        elif current_price >= upper_band and rsi > 60:
            signal = "SHORT"
            tp = middle_band
            sl = upper_band * 1.008
            score = 80 + (rsi - 60) 

        return signal, current_price, tp, sl, min(int(score), 99)
    except: return "ERROR", 0, 0, 0, 0

# --- [ ANA DÖNGÜ ] ---
def run(token, chat_id):
    threading.Thread(target=monitor_trades_thread, args=(token, chat_id), daemon=True).start()
    threading.Thread(target=run_flask, daemon=True).start()

    logger.info("☁️ PIRANHA ONLINE (HYPER)")
    send_telegram(token, chat_id, "☁️ PIRANHA: ONLINE\nv16.4 | Hyper Active")
    
    last_heartbeat = time.time()
    last_cache_time = 0
    symbol_list = []
    last_report_date = datetime.now().day

    while True:
        try:
            if time.time() - last_heartbeat > 1800:
                send_telegram(token, chat_id, "☁️ Piranha Online | ⚡")
                last_heartbeat = time.time()

            if datetime.now().day != last_report_date:
                send_daily_report(token, chat_id)
                last_report_date = datetime.now().day

            if time.time() - last_cache_time > CACHE_REFRESH:
                symbol_list = get_top_volume_symbols()
                last_cache_time = time.time()

            trades = load_json(TRADES_FILE)

            for symbol in symbol_list:
                if symbol in trades: continue 

                signal, price, tp, sl, score = analyze_scalp(symbol)

                if signal in ["LONG", "SHORT"] and score >= 80:
                    symbol_short = symbol.replace('/USDT', '')
                    emoji = "🟢 LONG" if signal == "LONG" else "🔴 SHORT"
                    
                    msg = (f"☁️ {symbol_short} | 💎 %{score}\n"
                           f"{emoji}\n"
                           f"📍 {price}\n"
                           f"🎯 {tp:.4f}\n"
                           f"🛡️ {sl:.4f}")
                    
                    send_telegram(token, chat_id, msg)
                    
                    trades[symbol] = {"signal": signal, "entry": price, "tp": tp, "sl": sl}
                    save_json(TRADES_FILE, trades)
                    time.sleep(1)

            time.sleep(SCAN_INTERVAL)

        except Exception as e:
            logger.error(f"Hata: {e}")
            time.sleep(10)

if __name__ == "__main__":
    MY_TOKEN = "8498989500:AAGmk-2OBpal04K4i6ZMk6YaYNC79Fa_xac"
    MY_ID = "8120732989"
    run(MY_TOKEN, MY_ID)
