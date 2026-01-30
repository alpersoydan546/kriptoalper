import time
import requests
import pandas as pd
import pandas_ta as ta
import os
import logging
from datetime import datetime, timedelta

# LOG AYARLARI
logging.basicConfig(level=logging.ERROR, format='%(asctime)s - %(message)s')
logger = logging.getLogger()

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TF = os.getenv("TF", "15m") 

SYMBOLS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
    "ADAUSDT","DOGEUSDT","LINKUSDT","AVAXUSDT","DOTUSDT",
    "MATICUSDT","LTCUSDT","BCHUSDT","TRXUSDT","ETCUSDT",
    "NEARUSDT","FILUSDT","APTUSDT","SUIUSDT","OPUSDT",
    "ARBUSDT","INJUSDT","TIAUSDT","ORDIUSDT","STXUSDT"
]

active_signals = [] 
daily_report = {"tp": 0, "sl": 0, "total": 0}
last_report_date = datetime.now().date()

def tg_send(msg):
    if not TOKEN or not CHAT_ID: return
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except: pass

def fetch_data(symbol, interval, limit=200):
    url = "https://fapi.binance.com/fapi/v1/klines"
    try:
        r = requests.get(url, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=5)
        df = pd.DataFrame(r.json(), columns=['t','o','h','l','c','v','ct','qv','nt','tbv','tqv','i'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        return df
    except: return None

def check_results():
    global daily_report, active_signals
    for sig in active_signals[:]:
        current_data = fetch_data(sig['symbol'], TF, limit=5)
        if current_data is None: continue
        last_price = current_data['c'].iloc[-1]
        
        if (sig['side'] == "LONG" and last_price >= sig['tp']) or \
           (sig['side'] == "SHORT" and last_price <= sig['tp']):
            daily_report['tp'] += 1
            tg_send(f"✅ <b>TP ALINDI: #{sig['symbol']}</b> (+Kâr)")
            active_signals.remove(sig)
            
        elif (sig['side'] == "LONG" and last_price <= sig['sl']) or \
             (sig['side'] == "SHORT" and last_price >= sig['sl']):
            daily_report['sl'] += 1
            tg_send(f"⚠️ <b>STOP: #{sig['symbol']}</b> (Risk Kapatıldı)")
            active_signals.remove(sig)

def send_daily_summary():
    global daily_report, last_report_date
    now = datetime.now()
    if now.date() > last_report_date:
        if daily_report['total'] > 0:
            tg_send(f"📊 <b>GÜNLÜK:</b> {daily_report['tp']} TP | {daily_report['sl']} SL")
        daily_report = {"tp": 0, "sl": 0, "total": 0}
        last_report_date = now.date()

def calc_signal(symbol):
    global active_signals
    try:
        df = fetch_data(symbol, TF)
        if df is None or len(df) < 200: return None

        rsi = ta.rsi(df['c'], length=14).iloc[-1]
        ema200 = ta.ema(df['c'], length=200).iloc[-1]
        atr = ta.atr(df['h'], df['l'], df['c'], length=14).iloc[-1]
        last_price = df['c'].iloc[-1]
        prev_price = df['c'].iloc[-2]
        
        avg_vol = df['v'].rolling(20).mean().iloc[-1]
        curr_vol = df['v'].iloc[-1]

        direction = None
        reasons = [] # Puan hesabı için nedenler

        # --- GÜVEN PUANI ALGORİTMASI ---
        # Taban Puan: 60
        score = 60

        # 1. RSI ANALİZİ
        if last_price > ema200 and rsi < 35: # LONG
            if last_price > prev_price: # Dönüş Mumu Şart
                direction = "LONG"
                score += 15 # RSI 35 altı (+15)
                if rsi < 30: score += 10 # RSI 30 altı (Ekstra +10) -> Toplam 25

        elif last_price < ema200 and rsi > 65: # SHORT
            if last_price < prev_price:
                direction = "SHORT"
                score += 15
                if rsi > 70: score += 10

        if direction:
            # 2. HACİM ANALİZİ
            if curr_vol > avg_vol * 1.3: 
                score += 10 # %30 Hacim artışı
            if curr_vol > avg_vol * 2.0:
                score += 10 # 2 Kat hacim (Ekstra +10)

            # --- EŞİK KONTROLÜ ---
            if score < 85: return None # 85 Altını Çöpe At

            if any(s['symbol'] == symbol for s in active_signals): return None

            stop = round(last_price - (atr * 2.0), 4) if direction == "LONG" else round(last_price + (atr * 2.0), 4)
            tp = round(last_price + (atr * 3.0), 4) if direction == "LONG" else round(last_price - (atr * 3.0), 4)

            active_signals.append({'symbol': symbol, 'side': direction, 'entry': last_price, 'tp': tp, 'sl': stop})
            daily_report['total'] += 1

            # PUANLI & MİNİMAL MESAJ
            return (
                f"🚀 <b>#{symbol} {direction}</b>\n"
                f"💵 Giriş: {last_price}\n"
                f"💰 Hedef: {tp}\n"
                f"🛡️ Stop: {stop}\n"
                f"⚡ <b>GÜVEN PUANI: %{score}</b>"
            )
    except: pass
    return None

def run(token, chat):
    global TOKEN, CHAT_ID
    TOKEN, CHAT_ID = token, chat
    tg_send("💎 <b>v6 ELITE MOD BAŞLADI</b>\nFiltre: Güven Puanı >= %85")
    
    last_health_check = datetime.now()

    while True:
        try:
            check_results() 
            send_daily_summary() 
            
            # 4 Saatte bir yaşam belirtisi
            if datetime.now() - last_health_check > timedelta(hours=4):
                tg_send("🟢 Sistem Çalışıyor...")
                last_health_check = datetime.now()

            for sym in SYMBOLS:
                msg = calc_signal(sym)
                if msg: tg_send(msg)
                time.sleep(1.0) 

            time.sleep(60)
        except:
            time.sleep(60)

