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

# --- [ PIRANHA (AGRESİF SCALP) AYARLARI ] ---
TIMEFRAME = '5m'           
LOOKBACK = 100             
SCAN_INTERVAL = 10         
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
def home(): return "☁️ PIRANHA v16.3 ONLINE"

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

            # --- HATA DÜZELTİLDİ: .copy() EKLENDİ ---
            updated_trades = trades.copy()
            trades_changed = False

            for symbol, trade in trades.items():
                try:
                    ticker = exchange.fetch_ticker(symbol)
                    current_price = ticker['last']
                    symbol_short = symbol.replace('/USDT', '')
                    
                    # KAR AL
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
                    
                    # STOP OL
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

# --- [ STRATEJİ: AGRESİF ] ---
def analyze_scalp(symbol):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=LOOKBACK)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', '
