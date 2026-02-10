import time
import requests
import pandas as pd
import pandas_ta as ta
import os
import logging
from datetime import datetime, timedelta

# LOG AYARLARI
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger()

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TF = "15m" 

# --- HİPER-AKTİF LİSTE (60 COIN) ---
SYMBOLS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","ADAUSDT","AVAXUSDT","TRXUSDT","DOTUSDT","LINKUSDT",
    "MATICUSDT","LTCUSDT","BCHUSDT","UNIUSDT","ATOMUSDT","ETCUSDT","FILUSDT","NEARUSDT","ALGOUSDT",
    "FETUSDT","RNDRUSDT","AGIXUSDT","WLDUSDT","GRTUSDT","OCEANUSDT","ARKMUSDT","AIUSDT",
    "DOGEUSDT","SHIBUSDT","1000PEPEUSDT","1000FLOKIUSDT","1000BONKUSDT","WIFUSDT","MEMEUSDT","ORDIUSDT","1000SATSUSDT",
    "ARBUSDT","OPUSDT","SUIUSDT","APTUSDT","SEIUSDT","TIAUSDT","INJUSDT","STXUSDT","IMXUSDT","LDOUSDT",
    "RUNEUSDT","FTMUSDT","SANDUSDT","MANAUSDT","AXSUSDT","GALAUSDT","CHZUSDT","EOSUSDT","KASUSDT","PYTHUSDT",
    "JUPUSDT","DYDXUSDT","SNXUSDT", "1000SHIBUSDT"
]

active_signals = [] 
cooldown_list = {} # Stop olan coinleri bekletmek için
daily_report = {"tp": 0, "sl": 0, "total": 0}
last_report_date = datetime.now().date()
error_count = 0 

def tg_send(msg):
    if not TOKEN or not CHAT_ID: return
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except: pass

def fetch_data(symbol, interval, limit=100):
    global error_count
    url = "https://fapi.binance.com/fapi/v1/klines"
    try:
        r = requests.get(url, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=5)
        if r.status_code != 200:
            error_count += 1
            return None
        
        df = pd.DataFrame(r.json(), columns=['t','o','h','l','c','v','ct','qv','nt','tbv','tqv','i'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        error_count = 0 
        return df
    except: 
        error_count += 1
        return None

def check_results():
    global daily_report, active_signals, cooldown_list
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
            
            # CEZA SİSTEMİ: Stop olan coini 60 dakika beklet (İntikam alma)
            cooldown_list[sig['symbol']] = datetime.now() + timedelta(minutes=60)
            active_signals.remove(sig)

def send_daily_summary():
    global daily_report, last_report_date
    now = datetime.now()
    if now.date() > last_report_date:
        if daily_report['total'] > 0:
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
    global active_signals, cooldown_list
    try:
        # CEZA KONTROLÜ
        if symbol in cooldown_list:
            if datetime.now() < cooldown_list[symbol]:
                return None # Hala cezalı, işlem açma
            else:
                del cooldown_list[symbol] # Süre doldu, affet

        df = fetch_data(symbol, TF)
        if df is None: return None 

        rsi = ta.rsi(df['c'], length=14).iloc[-1]
        stoch = ta.stochrsi(df['c'], length=14, rsi_length=14, k=3, d=3)
        k = stoch['STOCHRSIk_14_14_3_3'].iloc[-1]

        last_price = df['c'].iloc[-1]
        atr = ta.atr(df['h'], df['l'], df['c'], length=14).iloc[-1]

        direction = None
        score = 0

        # LONG: RSI < 30 (Daha güvenli olsun diye 35'ten 30'a çektim)
        if rsi < 30 and k < 20:
            direction = "LONG"
            score = 60 + (30 - rsi) 

        # SHORT: RSI > 70 (Daha güvenli olsun diye 65'ten 70'e çektim)
        elif rsi > 70 and k > 80:
            direction = "SHORT"
            score = 60 + (rsi - 70) 

        if direction:
            if any(s['symbol'] == symbol for s in active_signals): return None
            
            # --- FİYAT HASSASİYET AYARI (v8.2) ---
            # Meme coinler için 8 basamak, diğerleri için 4 basamak
            precision = 8 if last_price < 0.01 else 4

            stop = round(last_price - (atr * 2.5), precision) if direction == "LONG" else round(last_price + (atr * 2.5), precision)
            tp = round(last_price + (atr * 3.5), precision) if direction == "LONG" else round(last_price - (atr * 3.5), precision)

            active_signals.append({'symbol': symbol, 'side': direction, 'entry': last_price, 'tp': tp, 'sl': stop})
            daily_report['total'] += 1

            icon = "🟢" if direction == "LONG" else "🔴"
            return (
                f"🦁 <b>#{symbol} | {direction}</b> {icon}\n\n"
                f"📍 {last_price}\n"
                f"🎯 {tp}\n"
                f"🛑 {stop}\n\n"
                f"🔥 <b>Skor: %{int(score)}</b>"
            )

    except: pass
    return None

def run(token, chat):
    global TOKEN, CHAT_ID, error_count
    TOKEN, CHAT_ID = token, chat
    
    tg_send("🦁 <b>KriptoAlper v8.2 Sahada</b>\n(Hata Düzeltildi + Soğuma Modu Aktif)")
    
    test_df = fetch_data("BTCUSDT", "15m")
    if test_df is not None:
        tg_send(f"✅ <b>BAĞLANTI BAŞARILI!</b>\nAslan avlanmaya hazır.")
    else:
        tg_send("⚠️ <b>KRİTİK HATA!</b>\nBinance bağlantısı yok!")

    last_health_check = datetime.now()

    while True:
        try:
            if error_count > 10:
                tg_send("⚠️ <b>DİKKAT:</b> Veri akışı koptu!")
                error_count = 0 

            check_results() 
            send_daily_summary() 
            
            if datetime.now() - last_health_check > timedelta(hours=2): 
                tg_send("🐾 <b>İz Sürmeye Devam Ediyorum...</b>")
                last_health_check = datetime.now()

            for sym in SYMBOLS:
                msg = calc_signal(sym)
                if msg: tg_send(msg)
                time.sleep(0.5) 

            time.sleep(30) 
        except:
            time.sleep(60)
