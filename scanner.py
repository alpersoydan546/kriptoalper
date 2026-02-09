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

# --- GENİŞLETİLMİŞ AV SAHASI (60 COIN) ---
SYMBOLS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","ADAUSDT","AVAXUSDT","TRXUSDT","DOTUSDT","LINKUSDT",
    "MATICUSDT","LTCUSDT","BCHUSDT","UNIUSDT","ATOMUSDT","ETCUSDT","FILUSDT","NEARUSDT","ALGOUSDT",
    "FETUSDT","RNDRUSDT","AGIXUSDT","WLDUSDT","GRTUSDT","OCEANUSDT","ARKMUSDT","AIUSDT",
    "DOGEUSDT","SHIBUSDT","PEPEUSDT","FLOKIUSDT","BONKUSDT","WIFUSDT","MEMEUSDT","ORDIUSDT","1000SATSUSDT",
    "ARBUSDT","OPUSDT","SUIUSDT","APTUSDT","SEIUSDT","TIAUSDT","INJUSDT","STXUSDT","IMXUSDT","LDOUSDT",
    "RUNEUSDT","FTMUSDT","SANDUSDT","MANAUSDT","AXSUSDT","GALAUSDT","CHZUSDT","EOSUSDT","KASUSDT","PYTHUSDT",
    "JUPUSDT","DYDXUSDT","SNXUSDT"
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
        
        # --- TP: AV BAŞARILI ---
        if (sig['side'] == "LONG" and last_price >= sig['tp']) or \
           (sig['side'] == "SHORT" and last_price <= sig['tp']):
            daily_report['tp'] += 1
            tg_send(f"🦁 <b>AV BAŞARILI: #{sig['symbol']}</b> 🍖")
            active_signals.remove(sig)
            
        # --- SL: AV KAÇTI ---
        elif (sig['side'] == "LONG" and last_price <= sig['sl']) or \
             (sig['side'] == "SHORT" and last_price >= sig['sl']):
            daily_report['sl'] += 1
            tg_send(f"🐾 <b>AV KAÇTI: #{sig['symbol']}</b> 🩹")
            active_signals.remove(sig)

def send_daily_summary():
    global daily_report, last_report_date
    now = datetime.now()
    if now.date() > last_report_date:
        if daily_report['total'] > 0:
            # --- GÜNLÜK RAPOR (SEÇENEK 2: AV ÇETELESİ) ---
            yorum = "🦁 Sonuç: Aslan karnını doyurdu." if daily_report['tp'] >= daily_report['sl'] else "🦁 Sonuç: Aslan dinlenmeye çekildi."
            
            msg = (
                f"🔥 <b>GÜNLÜK AV RAPORU</b>\n"
                f"-------------------\n"
                f"🍖 Yakalanan : {daily_report['tp']}\n"
                f"🩹 Kaçan     : {daily_report['sl']}\n"
                f"-------------------\n"
                f"{yorum}"
            )
            tg_send(msg)
            
        daily_report = {"tp": 0, "sl": 0, "total": 0}
        last_report_date = now.date()

def calc_signal(symbol):
    global active_signals
    try:
        df = fetch_data(symbol, TF)
        if df is None or len(df) < 200: return None

        # İNDİKATÖRLER
        rsi = ta.rsi(df['c'], length=14).iloc[-1]
        atr = ta.atr(df['h'], df['l'], df['c'], length=14).iloc[-1]
        
        bb = ta.bbands(df['c'], length=20, std=2.0)
        lower_band = bb['BBL_20_2.0'].iloc[-1]
        upper_band = bb['BBU_20_2.0'].iloc[-1]
        
        last_price = df['c'].iloc[-1]
        
        # Hacim Kontrolü (Opsiyonel Puan Artırıcı)
        avg_vol = df['v'].rolling(20).mean().iloc[-1]
        curr_vol = df['v'].iloc[-1]

        direction = None
        score = 50 # Taban Puan (Artık 50'den başlıyoruz)

        # --- YENİ STRATEJİ: v7.3 (BANDI DELENİ YAKALA) ---
        # "Hook" (Dönüş) şartı kaldırıldı. Sadece Aşırı Alım/Satım ve Bant Dışı.

        # LONG Fırsatı: Fiyat Alt Bandın Altında + RSI Düşük
        if last_price <= lower_band:
            direction = "LONG"
            # Puanlama: RSI ne kadar düşükse o kadar yüksek puan
            if rsi < 45: score += 5
            if rsi < 35: score += 10
            if rsi < 30: score += 15 # Aşırı Satım Bonusu

        # SHORT Fırsatı: Fiyat Üst Bandın Üstünde + RSI Yüksek
        elif last_price >= upper_band:
            direction = "SHORT"
            # Puanlama: RSI ne kadar yüksekse o kadar yüksek puan
            if rsi > 55: score += 5
            if rsi > 65: score += 10
            if rsi > 70: score += 15 # Aşırı Alım Bonusu

        if direction:
            # Hacim Bonusu
            if curr_vol > avg_vol: score += 5
            
            # --- YENİ BARAJ: 55 ---
            # En ufak bir RSI aşırılığı veya Hacim varsa sinyal gelir.
            if score < 55: return None 
            
            score = min(int(score), 100)
            if any(s['symbol'] == symbol for s in active_signals): return None

            # Stop / TP Ayarları (Scanner Modu İçin Biraz Daha Geniş)
            stop = round(last_price - (atr * 2.5), 4) if direction == "LONG" else round(last_price + (atr * 2.5), 4)
            tp = round(last_price + (atr * 3.5), 4) if direction == "LONG" else round(last_price - (atr * 3.5), 4)

            active_signals.append({'symbol': symbol, 'side': direction, 'entry': last_price, 'tp': tp, 'sl': stop})
            daily_report['total'] += 1

            # --- SİNYAL MESAJI (SEÇENEK B - ASLAN HUD) ---
            icon = "🟢" if direction == "LONG" else "🔴"
            
            return (
                f"🦁 <b>#{symbol} | {direction}</b> {icon}\n\n"
                f"📍 {last_price} (Giriş)\n\n"
                f"🎯 {tp}\n"
                f"🛑 {stop}\n\n"
                f"🔥 <b>Skor: %{score}</b>"
            )
    except: pass
    return None

def run(token, chat):
    global TOKEN, CHAT_ID
    TOKEN, CHAT_ID = token, chat
    # --- BAŞLANGIÇ MESAJI ---
    tg_send("🦁 <b>KriptoAlper v7.3 Av Başladı</b>\n(Mod: Agresif Tarama / Baraj: 55)")
    
    last_health_check = datetime.now()

    while True:
        try:
            check_results() 
            send_daily_summary() 
            
            # --- 4 SAATLİK NÖBET MESAJI ---
            if datetime.now() - last_health_check > timedelta(hours=4):
                tg_send("🐾 <b>İz Sürmeye Devam Ediyorum...</b>\n(Sessizlik hakim.)")
                last_health_check = datetime.now()

            for sym in SYMBOLS:
                msg = calc_signal(sym)
                if msg: tg_send(msg)
                time.sleep(0.8) 

            time.sleep(45) 
        except:
            time.sleep(60)
