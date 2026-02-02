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

# Coin Listesini Biraz Daha Hareketli Coinlerle Güncelledim
SYMBOLS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
    "ADAUSDT","DOGEUSDT","AVAXUSDT","DOTUSDT","MATICUSDT",
    "LTCUSDT","TRXUSDT","NEARUSDT","LINKUSDT","APTUSDT",
    "SUIUSDT","OPUSDT","ARBUSDT","INJUSDT","TIAUSDT",
    "FETUSDT","RNDRUSDT","PEPEUSDT","SEIUSDT","STXUSDT" 
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
        
        # TP
        if (sig['side'] == "LONG" and last_price >= sig['tp']) or \
           (sig['side'] == "SHORT" and last_price <= sig['tp']):
            daily_report['tp'] += 1
            tg_send(f"✅ <b>TP VURULDU: #{sig['symbol']}</b>\nKasa Büyüyor! 💵")
            active_signals.remove(sig)
            
        # SL
        elif (sig['side'] == "LONG" and last_price <= sig['sl']) or \
             (sig['side'] == "SHORT" and last_price >= sig['sl']):
            daily_report['sl'] += 1
            tg_send(f"⚠️ <b>STOP: #{sig['symbol']}</b>\nRisk Kapatıldı. 🛡️")
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

        # İNDİKATÖRLER
        rsi = ta.rsi(df['c'], length=14).iloc[-1]
        prev_rsi = ta.rsi(df['c'], length=14).iloc[-2]
        atr = ta.atr(df['h'], df['l'], df['c'], length=14).iloc[-1]
        
        bb = ta.bbands(df['c'], length=20, std=2.0)
        lower_band = bb['BBL_20_2.0'].iloc[-1]
        upper_band = bb['BBU_20_2.0'].iloc[-1]
        
        last_price = df['c'].iloc[-1]
        real_open = df['o'].iloc[-1] # Mum rengi kontrolü için
        
        avg_vol = df['v'].rolling(20).mean().iloc[-1]
        curr_vol = df['v'].iloc[-1]

        direction = None
        score = 0

        # --- GÜNCELLENMİŞ STRATEJİ (v7.1) ---
        
        # LONG:
        # 1. Fiyat Alt banda %0.5 yakın veya altında (Esnedi)
        # 2. RSI < 45 (Önceki 40 idi, yumuşattık)
        # 3. Mum Yeşil OLMAK ZORUNDA DEĞİL ama RSI artışta olmalı (Dönüş sinyali)
        if last_price <= lower_band * 1.005 and rsi < 45:
             if rsi > prev_rsi: # RSI kafayı kaldırdıysa yeterli
                direction = "LONG"
                score = 65 # Taban puan
                score += (45 - rsi) # RSI ne kadar düşükse puan artar
                if last_price > real_open: score += 10 # Yeşil mumsa ekstra puan

        # SHORT:
        # 1. Fiyat Üst banda %0.5 yakın veya üstünde
        # 2. RSI > 55 (Önceki 60 idi, yumuşattık)
        if last_price >= upper_band * 0.995 and rsi > 55:
            if rsi < prev_rsi: # RSI kafayı indirdiyse yeterli
                direction = "SHORT"
                score = 65
                score += (rsi - 55)
                if last_price < real_open: score += 10 # Kırmızı mumsa ekstra puan

        if direction:
            # Hacim Bonusu
            if curr_vol > avg_vol: score += 5
            
            # --- YENİ EŞİK: 70 ---
            if score < 70: return None # 80'den 70'e çektik
            
            score = min(int(score), 100)
            if any(s['symbol'] == symbol for s in active_signals): return None

            # Stop/TP
            stop = round(last_price - (atr * 2.0), 4) if direction == "LONG" else round(last_price + (atr * 2.0), 4)
            tp = round(last_price + (atr * 3.0), 4) if direction == "LONG" else round(last_price - (atr * 3.0), 4)

            active_signals.append({'symbol': symbol, 'side': direction, 'entry': last_price, 'tp': tp, 'sl': stop})
            daily_report['total'] += 1

            return (
                f"⚡ <b>KriptoAlper v7.1 Sinyali</b>\n"
                f"🚀 <b>#{symbol} {direction}</b>\n"
                f"📉 Fiyat: {last_price}\n"
                f"🛡️ Stop: {stop}\n"
                f"💰 Hedef: {tp}\n"
                f"🔥 <b>GÜVEN PUANI: %{score}</b>"
            )
    except: pass
    return None

def run(token, chat):
    global TOKEN, CHAT_ID
    TOKEN, CHAT_ID = token, chat
    # Başlangıç Mesajı (Botun çalıştığını teyit etmek için)
    tg_send("✅ <b>SİSTEM BAŞLATILDI")
    
    last_health_check = datetime.now()

    while True:
        try:
            check_results() 
            send_daily_summary() 
            
            if datetime.now() - last_health_check > timedelta(hours=4):
                tg_send("🟢 Tarama Devam Ediyor...")
                last_health_check = datetime.now()

            for sym in SYMBOLS:
                msg = calc_signal(sym)
                if msg: tg_send(msg)
                time.sleep(1.0) 

            time.sleep(60)
        except:
            time.sleep(60)
