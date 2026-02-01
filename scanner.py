import time
import requests
import pandas as pd
import pandas_ta as ta
import os
import logging
from datetime import datetime, timedelta

# LOG AYARLARI (Sadece Hatalar)
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
        
        # TP KONTROL
        if (sig['side'] == "LONG" and last_price >= sig['tp']) or \
           (sig['side'] == "SHORT" and last_price <= sig['tp']):
            daily_report['tp'] += 1
            tg_send(f"✅ <b>TP VURULDU: #{sig['symbol']}</b>\nKasa Büyüyor! 💵")
            active_signals.remove(sig)
            
        # SL KONTROL
        elif (sig['side'] == "LONG" and last_price <= sig['sl']) or \
             (sig['side'] == "SHORT" and last_price >= sig['sl']):
            daily_report['sl'] += 1
            tg_send(f"⚠️ <b>STOP: #{sig['symbol']}</b>\nCan Sağlığı, Devam. 🛡️")
            active_signals.remove(sig)

def send_daily_summary():
    global daily_report, last_report_date
    now = datetime.now()
    if now.date() > last_report_date:
        if daily_report['total'] > 0:
            msg = f"📊 <b>GÜNLÜK SKOR:</b> {daily_report['tp']} Kazanç | {daily_report['sl']} Kayıp"
            tg_send(msg)
        daily_report = {"tp": 0, "sl": 0, "total": 0}
        last_report_date = now.date()

def calc_signal(symbol):
    global active_signals
    try:
        df = fetch_data(symbol, TF)
        if df is None or len(df) < 200: return None

        # VERİLER
        rsi = ta.rsi(df['c'], length=14).iloc[-1]
        prev_rsi = ta.rsi(df['c'], length=14).iloc[-2] # Önceki RSI (Yön tayini için)
        
        atr = ta.atr(df['h'], df['l'], df['c'], length=14).iloc[-1]
        
        # Bollinger (20, 2)
        bb = ta.bbands(df['c'], length=20, std=2.0)
        lower_band = bb['BBL_20_2.0'].iloc[-1]
        upper_band = bb['BBU_20_2.0'].iloc[-1]
        
        last_price = df['c'].iloc[-1]
        open_price = df['c'].iloc[-1] # Anlık mum açılışı değil, o anki fiyatla kıyas için open'ı alalım
        real_open = df['o'].iloc[-1]
        
        avg_vol = df['v'].rolling(20).mean().iloc[-1]
        curr_vol = df['v'].iloc[-1]

        direction = None
        score = 0

        # --- STRATEJİ: Bollinger Reversal + RSI Onayı + Mum Rengi ---

        # LONG KRİTERLERİ
        # 1. Fiyat Alt Banda değmiş veya altında.
        # 2. RSI < 40 (Ucuz).
        # 3. RSI Yükseliyor (prev_rsi < rsi) -> DÖNÜŞ BAŞLADI DEMEK.
        # 4. Mum Rengi YEŞİL (last_price > real_open).
        if last_price <= lower_band * 1.003 and rsi < 40:
            if rsi > prev_rsi and last_price > real_open:
                direction = "LONG"
                # Puanlama
                score = 75 # Taban puan
                score += (40 - rsi) # RSI ne kadar düşükse o kadar puan
                if curr_vol > avg_vol: score += 10 # Hacim bonusu

        # SHORT KRİTERLERİ
        # 1. Fiyat Üst Banda değmiş.
        # 2. RSI > 60.
        # 3. RSI Düşüyor (prev_rsi > rsi).
        # 4. Mum Rengi KIRMIZI.
        elif last_price >= upper_band * 0.997 and rsi > 60:
            if rsi < prev_rsi and last_price < real_open:
                direction = "SHORT"
                # Puanlama
                score = 75
                score += (rsi - 60)
                if curr_vol > avg_vol: score += 10

        if direction:
            # FİLTRE: Puan 80 altıysa riskli, atma.
            if score < 80: return None
            
            # Puan Sınırı
            score = min(int(score), 100)

            # Çifte Sinyal Önleme
            if any(s['symbol'] == symbol for s in active_signals): return None

            # STOP/TP (Bollinger Scalping için Optimize)
            stop = round(last_price - (atr * 2.0), 4) if direction == "LONG" else round(last_price + (atr * 2.0), 4)
            tp = round(last_price + (atr * 3.0), 4) if direction == "LONG" else round(last_price - (atr * 3.0), 4)

            active_signals.append({'symbol': symbol, 'side': direction, 'entry': last_price, 'tp': tp, 'sl': stop})
            daily_report['total'] += 1

            return (
                f"💎 <b>KriptoAlper v7 Sinyali</b>\n"
                f"🚀 <b>#{symbol} {direction}</b>\n"
                f"--------------------------\n"
                f"📉 Fiyat: {last_price}\n"
                f"📊 Durum: Bant Dışı Dönüş Onaylı\n"
                f"🛡️ Stop: {stop}\n"
                f"💰 Hedef: {tp}\n"
                f"⚡ <b>GÜVEN PUANI: %{score}</b>"
            )
    except: pass
    return None

def run(token, chat):
    global TOKEN, CHAT_ID
    TOKEN, CHAT_ID = token, chat
    tg_send("🦅 <b>KriptoAlper v7 (FİNAL) Yayında!</b>\nStrateji: Bollinger + Yeşil Mum Onayı + Dinamik Puan")
    
    last_health_check = datetime.now()

    while True:
        try:
            check_results() 
            send_daily_summary() 
            
            # 4 Saatte bir kontrol mesajı
            if datetime.now() - last_health_check > timedelta(hours=4):
                tg_send("👁️ v7 Nöbette | Bant Dışı Fırsat Bekleniyor...")
                last_health_check = datetime.now()

            for sym in SYMBOLS:
                msg = calc_signal(sym)
                if msg: tg_send(msg)
                time.sleep(1.0) 

            time.sleep(60)
        except:
            time.sleep(60)
