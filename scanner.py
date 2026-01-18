import time
import requests
import pandas as pd
import pandas_ta as ta
import os
import logging
from datetime import datetime, timedelta

# LOG AYARLARI
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger()

# AYARLAR (Render Environment Variables)
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TF = os.getenv("TF", "15m") 

# TARANACAK COINLER (25 Adet)
SYMBOLS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
    "ADAUSDT","DOGEUSDT","LINKUSDT","AVAXUSDT","DOTUSDT",
    "MATICUSDT","LTCUSDT","BCHUSDT","TRXUSDT","ETCUSDT",
    "NEARUSDT","FILUSDT","APTUSDT","SUIUSDT","OPUSDT",
    "ARBUSDT","INJUSDT","TIAUSDT","ORDIUSDT","STXUSDT"
]

# BELLEK VE RAPORLAMA DEĞİŞKENLERİ
active_signals = [] 
daily_report = {"tp": 0, "sl": 0, "total": 0}
last_report_date = datetime.now().date()
last_sent_signals = {} 
COOLDOWN_MINUTES = 120 

def tg_send(msg):
    if not TOKEN or not CHAT_ID: return
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        logger.error(f"Telegram Hatası: {e}")

def fetch_data(symbol, interval, limit=250):
    url = "https://fapi.binance.com/fapi/v1/klines"
    try:
        r = requests.get(url, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=10)
        df = pd.DataFrame(r.json(), columns=['t','o','h','l','c','v','ct','qv','nt','tbv','tqv','i'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        return df
    except: return None

def check_results():
    """Açık olan sinyallerin hedefe mi yoksa stopa mı değdiğini kontrol eder"""
    global daily_report, active_signals
    for sig in active_signals[:]:
        current_data = fetch_data(sig['symbol'], TF, limit=5)
        if current_data is None: continue
        
        last_price = current_data['c'].iloc[-1]
        
        # Kar Alındı (TP)
        if (sig['side'] == "LONG" and last_price >= sig['tp']) or \
           (sig['side'] == "SHORT" and last_price <= sig['tp']):
            daily_report['tp'] += 1
            tg_send(f"✅ <b>KÂR ALINDI!</b>\n#{sig['symbol']} hedefine ulaştı.\nGiriş: {sig['entry']} ➡️ TP: {sig['tp']}")
            active_signals.remove(sig)
            
        # Stop Olundu (SL)
        elif (sig['side'] == "LONG" and last_price <= sig['sl']) or \
             (sig['side'] == "SHORT" and last_price >= sig['sl']):
            daily_report['sl'] += 1
            tg_send(f"🛑 <b>STOP OLUNDU.</b>\n#{sig['symbol']} risk yönetimi gereği kapatıldı.\nGiriş: {sig['entry']} ➡️ SL: {sig['sl']}")
            active_signals.remove(sig)

def send_daily_summary():
    """Gece yarısı günlük özet gönderir"""
    global daily_report, last_report_date
    now = datetime.now()
    if now.date() > last_report_date:
        win_rate = (daily_report['tp'] / daily_report['total'] * 100) if daily_report['total'] > 0 else 0
        msg = (
            f"📊 <b>GÜNLÜK PERFORMANS ÖZETİ</b>\n"
            f"----------------------------------\n"
            f"💰 Başarılı (TP): {daily_report['tp']}\n"
            f"🛑 Başarısız (SL): {daily_report['sl']}\n"
            f"📈 Toplam İşlem: {daily_report['total']}\n"
            f"⚡ Başarı Oranı: %{round(win_rate, 1)}\n"
            f"----------------------------------\n"
            f"4 Dolar Challenge Devam Ediyor! 🚀"
        )
        tg_send(msg)
        daily_report = {"tp": 0, "sl": 0, "total": 0}
        last_report_date = now.date()

def calc_signal(symbol):
    global daily_report, active_signals
    try:
        df = fetch_data(symbol, TF)
        if df is None or len(df) < 200: return None

        rsi = ta.rsi(df['c'], length=14).iloc[-1]
        ema200 = ta.ema(df['c'], length=200).iloc[-1]
        atr = ta.atr(df['h'], df['l'], df['c'], length=14).iloc[-1]
        last_price = df['c'].iloc[-1]
        avg_vol = df['v'].rolling(20).mean().iloc[-1]
        curr_vol = df['v'].iloc[-1]

        direction = None
        reasons = []

        # STRATEJİ: Trend Üstü RSI Pullback
        if last_price > ema200 and rsi < 38:
            direction = "LONG"
            reasons.append("✅ Trend Üstü (Boğa)")
            reasons.append(f"📉 RSI Dipte ({round(rsi,1)})")
        elif last_price < ema200 and rsi > 62:
            direction = "SHORT"
            reasons.append("✅ Trend Altı (Ayı)")
            reasons.append(f"📈 RSI Tepede ({round(rsi,1)})")

        if direction:
            conf_score = 65
            if curr_vol > avg_vol * 1.5:
                conf_score += 20
                reasons.append("🔥 Yüksek Hacim Onayı")
            elif curr_vol > avg_vol:
                conf_score += 10
                reasons.append("📊 Hacim Artışı")

            if rsi < 25 or rsi > 75:
                conf_score += 15
                reasons.append("⚡ Aşırı Bölge Bonusu")

            if conf_score < 70: return None

            # Aynı coin için aktif sinyal varsa gönderme
            if any(s['symbol'] == symbol for s in active_signals): return None

            # Stop ve TP Seviyeleri (ATR Bazlı)
            stop = round(last_price - (atr * 1.8), 4) if direction == "LONG" else round(last_price + (atr * 1.8), 4)
            tp = round(last_price + (atr * 3.2), 4) if direction == "LONG" else round(last_price - (atr * 3.2), 4)

            active_signals.append({'symbol': symbol, 'side': direction, 'entry': last_price, 'tp': tp, 'sl': stop})
            daily_report['total'] += 1

            reason_str = "\n".join(reasons)
            return (
                f"🎯 <b>#{symbol} {direction} Sinyali</b>\n"
                f"----------------------------------\n"
                f"{reason_str}\n"
                f"----------------------------------\n"
                f"💵 Giriş: {last_price}\n"
                f"🛑 Stop: {stop}\n"
                f"💰 Hedef: {tp}\n\n"
                f"⚡ <b>GÜVEN PUANI: %{conf_score}</b>"
            )
    except Exception as e:
        logger.error(f"Analiz Hatası ({symbol}): {e}")
    return None

def run(token, chat):
    global TOKEN, CHAT_ID
    TOKEN, CHAT_ID = token, chat
    tg_send("🚀 <b>PRO Scanner v3.1 Aktif!</b>\nDetaylı Analiz + Takip + Nöbetçi Sistemi Başlatıldı.")
    
    last_health_check = datetime.now()
    scan_count = 0

    while True:
        try:
            check_results() 
            send_daily_summary() 
            
            # HER 30 DAKİKADA BİR DURUM BİLGİSİ
            if datetime.now() - last_health_check > timedelta(minutes=30):
                tg_send(f"🤖 <b>Nöbetçi Raporu:</b>\nSistem sorunsuz çalışıyor.\nSon 30 dk'da {scan_count} tarama yapıldı.\nUygun sinyal bekleniyor... ⏳")
                last_health_check = datetime.now()
                scan_count = 0

            for sym in SYMBOLS:
                msg = calc_signal(sym)
                if msg: tg_send(msg)
                scan_count += 1
                time.sleep(1.1)

            time.sleep(120)
        except Exception as e:
            logger.error(f"Döngü Hatası: {e}")
            time.sleep(60)
