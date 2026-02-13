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

# --- [ PIRANHA v18.1 - STABLE & UNCHAINED AYARLARI ] ---
TIMEFRAME = '5m'           # Mikro Scalp
LOOKBACK = 100             
SCAN_INTERVAL = 15         # 15 saniye ideal
TRADE_CHECK_INTERVAL = 5   
STATS_FILE = "daily_stats_v18.json"  
TRADES_FILE = "active_trades_v18.json"
TOP_COUNT = 50             

# --- [ RİSK YÖNETİMİ (GÜNCELLENDİ) ] ---
MAX_OPEN_TRADES = 10       # LİMİT ARTIRILDI: Aynı anda 10 işlem
DAILY_STOP_LOSS = -6.0     # %6 Zararda bot kapanır
DAILY_TAKE_PROFIT = 5.0    # %5 Kârda bot kapanır (Hedef artırıldı)
MAX_DAILY_LOSSES = 4       # Günlük 4 stopta bot 2 saat mola verir
PAUSE_DURATION = 7200      # 2 Saat (saniye cinsinden)

# --- [ TP / SL AYARLARI (Sabit %) ] ---
TP_PERCENT = 0.005         # %0.5 Fiyat Hareketi (10x ile %5 Kâr)
SL_PERCENT = 0.0035        # %0.35 Fiyat Hareketi (10x ile %3.5 Zarar)

# --- [ MARKET REJİMİ ] ---
BTC_PROTECTION_PCT = 1.5   # BTC %1.5 düşerse Long açma

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger()

exchange = ccxt.binance({
    'rateLimit': 1200,
    'enableRateLimit': True,
    'options': {'defaultType': 'future'}
})

app = Flask(__name__)
lock = threading.Lock()

# Global Durum
BOT_STATE = {
    "is_paused": False,
    "pause_end_time": 0,
    "consecutive_losses": 0,
    "daily_stopped": False
}

@app.route('/')
def home(): 
    status = "PAUSED" if BOT_STATE["is_paused"] else "RUNNING"
    if BOT_STATE["daily_stopped"]: status = "STOPPED (DAILY LIMIT)"
    return f"☁️ PIRANHA v18.1 UNCHAINED | Status: {status}"

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

def get_stats():
    stats = load_json(STATS_FILE)
    today = datetime.now().strftime("%Y-%m-%d")
    if stats.get("date") != today:
        stats = {"date": today, "win": 0, "loss": 0, "pnl": 0.0, "total_trades": 0}
        BOT_STATE["consecutive_losses"] = 0 
        BOT_STATE["daily_stopped"] = False
    return stats

def update_stats(result, pnl):
    stats = get_stats()
    
    if result == "WIN": 
        stats["win"] += 1
        BOT_STATE["consecutive_losses"] = 0 
    elif result == "LOSS": 
        stats["loss"] += 1
        BOT_STATE["consecutive_losses"] += 1
    
    stats["pnl"] += pnl
    stats["total_trades"] += 1
    save_json(STATS_FILE, stats)
    check_risk_management(stats) 

# --- [ RİSK KONTROLÜ ] ---
def check_risk_management(stats):
    global BOT_STATE
    
    if stats["pnl"] <= DAILY_STOP_LOSS:
        BOT_STATE["daily_stopped"] = True
        logger.warning("🚨 GÜNLÜK ZARAR LİMİTİ. STOP.")
    
    elif stats["pnl"] >= DAILY_TAKE_PROFIT:
        BOT_STATE["daily_stopped"] = True
        logger.info("🤑 GÜNLÜK KÂR LİMİTİ. PAYDOS.")

    if BOT_STATE["consecutive_losses"] >= MAX_DAILY_LOSSES:
        BOT_STATE["is_paused"] = True
        BOT_STATE["pause_end_time"] = time.time() + PAUSE_DURATION
        BOT_STATE["consecutive_losses"] = 0
        logger.warning(f"⚠️ Arka arkaya stop! 2 Saat mola.")

# --- [ BTC REJİMİ ] ---
def check_btc_regime():
    try:
        bars = exchange.fetch_ohlcv('BTC/USDT', timeframe='15m', limit=5)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        start_price = df['open'].iloc[-3]
        end_price = df['close'].iloc[-1]
        change_pct = ((end_price - start_price) / start_price) * 100
        
        can_long = True
        can_short = True
        
        if change_pct <= -BTC_PROTECTION_PCT: can_long = False  
        if change_pct >= BTC_PROTECTION_PCT: can_short = False  
        
        return can_long, can_short
    except:
        return True, True

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
                    
                    # KAR AL
                    if (trade['signal'] == "LONG" and current_price >= trade['tp']) or \
                       (trade['signal'] == "SHORT" and current_price <= trade['tp']):
                        
                        pnl = TP_PERCENT * 100 * 10 
                        msg = (f"☁️ {symbol_short}\n"
                               f"✅ HEDEF ALINDI (TP)\n"
                               f"💰 Kazanç: +%{pnl:.2f} (10x)\n"
                               f"💎 Piranha v18.1")
                        send_telegram(token, chat_id, msg)
                        update_stats("WIN", TP_PERCENT * 100) 
                        del updated_trades[symbol]
                        trades_changed = True
                    
                    # STOP OL
                    elif (trade['signal'] == "LONG" and current_price <= trade['sl']) or \
                         (trade['signal'] == "SHORT" and current_price >= trade['sl']):
                        
                        loss = SL_PERCENT * 100 * 10 
                        msg = (f"☁️ {symbol_short}\n"
                               f"❌ STOP LOSS\n"
                               f"📉 Kayıp: -%{loss:.2f} (10x)\n"
                               f"💎 Piranha v18.1")
                        send_telegram(token, chat_id, msg)
                        update_stats("LOSS", -(SL_PERCENT * 100))
                        del updated_trades[symbol]
                        trades_changed = True
                        
                except: continue
            
            if trades_changed:
                save_json(TRADES_FILE, updated_trades)

        except: pass
        time.sleep(TRADE_CHECK_INTERVAL)

# --- [ TOP 50 LİSTESİ ] ---
def get_top_volume_symbols():
    try:
        tickers = exchange.fetch_tickers()
        usdt_tickers = [{'symbol': s, 'quoteVolume': float(v['quoteVolume'])} for s, v in tickers.items() if '/USDT' in s and 'quoteVolume' in v]
        sorted_tickers = sorted(usdt_tickers, key=lambda x: x['quoteVolume'], reverse=True)
        return [t['symbol'] for t in sorted_tickers[:TOP_COUNT]]
    except: 
        return ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT']

# --- [ STRATEJİ: v18 STABLE ] ---
def analyze_stable(symbol, can_long, can_short):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=LOOKBACK)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        if len(df) < 25: return "NEUTRAL", 0, 0, 0, 0

        current_price = df['close'].iloc[-1]
        
        # 1. Volatilite (ATR) Kontrolü
        atr = ta.atr(df['high'], df['low'], df['close'], length=14).iloc[-1]
        if atr < (current_price * 0.004): return "NEUTRAL", 0, 0, 0, 0 # Çok durgunsa girme
        
        # 2. Hacim Kontrolü
        current_vol = df['volume'].iloc[-1]
        avg_vol = df['volume'].rolling(window=20).mean().iloc[-1]
        if current_vol < (avg_vol * 1.3): return "NEUTRAL", 0, 0, 0, 0 # Hacim yoksa girme

        # 3. İndikatörler (BB 1.8 + RSI 7)
        bb = ta.bbands(df['close'], length=14, std=1.8)
        lower_band = bb['BBL_14_1.8'].iloc[-1]
        upper_band = bb['BBU_14_1.8'].iloc[-1]
        rsi = ta.rsi(df['close'], length=7).iloc[-1]
        
        signal = "NEUTRAL"; tp = 0; sl = 0; score = 60

        # LONG
        if can_long and current_price <= lower_band and rsi < 30:
            signal = "LONG"
            score = 80 + (30 - rsi)
            tp = current_price * (1 + TP_PERCENT)
            sl = current_price * (1 - SL_PERCENT)

        # SHORT
        elif can_short and current_price >= upper_band and rsi > 70:
            signal = "SHORT"
            score = 80 + (rsi - 70)
            tp = current_price * (1 - TP_PERCENT)
            sl = current_price * (1 + SL_PERCENT)

        return signal, current_price, tp, sl, min(int(score), 99)
    except: return "ERROR", 0, 0, 0, 0

# --- [ ANA DÖNGÜ ] ---
def run(token, chat_id):
    threading.Thread(target=monitor_trades_thread, args=(token, chat_id), daemon=True).start()
    threading.Thread(target=run_flask, daemon=True).start()

    logger.info("☁️ PIRANHA v18.1 ONLINE")
    send_telegram(token, chat_id, "☁️ PIRANHA v18.1\nUnchained Stable Mode 🛡️\nMax 10 İşlem")
    
    last_heartbeat = time.time()
    last_cache_time = 0
    symbol_list = []
    last_report_date = datetime.now().day

    while True:
        try:
            # 1. Kontroller
            if BOT_STATE["daily_stopped"]:
                time.sleep(60)
                continue
            
            if BOT_STATE["is_paused"]:
                if time.time() > BOT_STATE["pause_end_time"]:
                    BOT_STATE["is_paused"] = False
                    send_telegram(token, chat_id, "🔔 Piranha Moladan Döndü.")
                else:
                    time.sleep(60)
                    continue

            # 2. Nabız
            if time.time() - last_heartbeat > 1800:
                send_telegram(token, chat_id, "☁️ Piranha v18.1 | Aktif ⚡")
                last_heartbeat = time.time()

            if datetime.now().day != last_report_date:
                save_json(STATS_FILE, {"date": datetime.now().strftime("%Y-%m-%d"), "win": 0, "loss": 0, "pnl": 0.0, "total_trades": 0})
                BOT_STATE["daily_stopped"] = False
                last_report_date = datetime.now().day

            # 3. Liste Yenile
            if time.time() - last_cache_time > CACHE_REFRESH:
                symbol_list = get_top_volume_symbols()
                last_cache_time = time.time()

            # 4. BTC Durumu
            can_long, can_short = check_btc_regime()

            trades = load_json(TRADES_FILE)
            
            # Max 10 İşlem Kontrolü
            if len(trades) >= MAX_OPEN_TRADES:
                time.sleep(SCAN_INTERVAL)
                continue

            for symbol in symbol_list:
                if symbol in trades: continue 

                signal, price, tp, sl, score = analyze_stable(symbol, can_long, can_short)

                if signal in ["LONG", "SHORT"]:
                    symbol_short = symbol.replace('/USDT', '')
                    emoji = "🟢 LONG" if signal == "LONG" else "🔴 SHORT"
                    
                    msg = (f"☁️ {symbol_short} | 💎 %{score}\n"
                           f"{emoji}\n"
                           f"📍 Giriş: {price}\n"
                           f"🎯 Hedef: {tp:.4f} (%0.5)\n"
                           f"🛡️ Stop: {sl:.4f} (%0.35)")
                    
                    send_telegram(token, chat_id, msg)
                    
                    trades[symbol] = {"signal": signal, "entry": price, "tp": tp, "sl": sl}
                    save_json(TRADES_FILE, trades)
                    
                    if len(trades) >= MAX_OPEN_TRADES: break 
                    time.sleep(1)

            time.sleep(SCAN_INTERVAL)

        except Exception as e:
            logger.error(f"Hata: {e}")
            time.sleep(10)

if __name__ == "__main__":
    MY_TOKEN = "8498989500:AAGmk-2OBpal04K4i6ZMk6YaYNC79Fa_xac"
    MY_ID = "8120732989"
    run(MY_TOKEN, MY_ID)
