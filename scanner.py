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

# --- [ SCALPER AYARLARI ] ---
TIMEFRAME = '15m'          # Scalp için ideal
LOOKBACK = 100             # Çok derin geçmişe gerek yok, anlık bakıyoruz
SCAN_INTERVAL = 45         # Daha sık tarasın (45 saniye)
TRADE_CHECK_INTERVAL = 5   # Açık işlemleri 5 saniyede bir kontrol et
STATS_FILE = "daily_stats_render.json"  # Dosya ismi farklı olsun karışmasın
TRADES_FILE = "active_trades_render.json"

# Sadece Hacimli "Baba" Coinler (Vurkaç için en güvenlileri)
SCALP_COINS = ['ETH/USDT', 'BTC/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT', 'DOGE/USDT', 'AVAX/USDT']

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
def home(): return "🦁 KRİPTOALPER v16.0 - PİRANHA (SCALPER) AKTİF"

def run_flask():
    try:
        port = int(os.environ.get("PORT", 10000))
        app.run(host='0.0.0.0', port=port)
    except: pass

def send_telegram(token, chat_id, message):
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
        requests.post(url, data=data, timeout=10)
    except Exception as e: logger.error(f"Telegram Hatası: {e}")

# --- [ DOSYA SİSTEMİ ] ---
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
        f"☁️ **RENDER (SCALP) RAPORU**\n\n"
        f"✅ **Başarılı:** {stats['win']}\n"
        f"❌ **Başarısız:** {stats['loss']}\n\n"
        f"💰 **Net PnL:** %{stats['pnl']:.2f}"
    )
    send_telegram(token, chat_id, msg)

# --- [ BEKÇİ MODÜLÜ (SCALP TAKİP) ] ---
def monitor_trades_thread(token, chat_id):
    logger.info("🛡️ SCALP BEKÇİSİ AKTİF")
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
                    
                    # KAR AL (TP)
                    if (trade['signal'] == "LONG" and current_price >= trade['tp']) or \
                       (trade['signal'] == "SHORT" and current_price <= trade['tp']):
                        
                        pnl = abs((current_price - trade['entry']) / trade['entry']) * 100
                        msg = (f"✅ **{symbol.replace('/USDT', '')} | HEDEF**\n"
                               f"☁️ Scalp Başarılı\n\n"
                               f"💰 **Kâr:** +%{pnl:.2f}\n"
                               f"💵 **Fiyat:** {current_price}")
                        send_telegram(token, chat_id, msg)
                        update_stats("WIN", pnl)
                        del updated_trades[symbol]
                        trades_changed = True
                    
                    # ZARAR DURDUR (SL)
                    elif (trade['signal'] == "LONG" and current_price <= trade['sl']) or \
                         (trade['signal'] == "SHORT" and current_price >= trade['sl']):
                        
                        loss = abs((current_price - trade['entry']) / trade['entry']) * 100
                        msg = (f"❌ **{symbol.replace('/USDT', '')} | STOP**\n"
                               f"☁️ Scalp Stop\n\n"
                               f"📉 **Zarar:** -%{loss:.2f}\n"
                               f"💵 **Fiyat:** {current_price}")
                        send_telegram(token, chat_id, msg)
                        update_stats("LOSS", -loss)
                        del updated_trades[symbol]
                        trades_changed = True
                        
                except: continue
            
            if trades_changed:
                save_json(TRADES_FILE, updated_trades)

        except: pass
        time.sleep(TRADE_CHECK_INTERVAL)

# --- [ PİRANHA STRATEJİSİ (BOLLINGER + RSI) ] ---
def analyze_scalp(symbol):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=LOOKBACK)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        if len(df) < 25: return "NEUTRAL", 0, 0, 0, 0

        current_price = df['close'].iloc[-1]
        
        # Bollinger Bantları (20, 2)
        bb = ta.bbands(df['close'], length=20, std=2)
        lower_band = bb['BBL_20_2.0'].iloc[-1]
        upper_band = bb['BBU_20_2.0'].iloc[-1]
        middle_band = bb['BBM_20_2.0'].iloc[-1]
        
        rsi = ta.rsi(df['close'], length=14).iloc[-1]
        
        signal = "NEUTRAL"; tp = 0; sl = 0; score = 50

        # --- LONG STRATEJİSİ ---
        # Fiyat Alt Banda çarptıysa VE RSI aşırı satımdaysa (<35) -> TEPKİ ALIMI
        if current_price <= lower_band and rsi < 35:
            signal = "LONG"
            # Hedef: Orta Bant (Mean Reversion)
            tp = middle_band 
            # Stop: Alt bandın %0.8 altı (Çok yakın stop)
            sl = lower_band * 0.992
            
            score = 80 + (35 - rsi) # RSI ne kadar düşükse puan artar

        # --- SHORT STRATEJİSİ ---
        # Fiyat Üst Banda çarptıysa VE RSI aşırı alımdaysa (>65) -> TEPKİ SATIŞI
        elif current_price >= upper_band and rsi > 65:
            signal = "SHORT"
            # Hedef: Orta Bant
            tp = middle_band
            # Stop: Üst bandın %0.8 üstü
            sl = upper_band * 1.008
            
            score = 80 + (rsi - 65) # RSI ne kadar yüksekse puan artar

        return signal, current_price, tp, sl, min(int(score), 99)
    except:
        return "ERROR", 0, 0, 0, 0

# --- [ ANA DÖNGÜ ] ---
def bot_loop(token, chat_id):
    # Bekçi ve Flask Başlat
    threading.Thread(target=monitor_trades_thread, args=(token, chat_id), daemon=True).start()
    threading.Thread(target=run_flask, daemon=True).start()

    logger.info("🦁 PİRANHA (RENDER) SAHADA")
    send_telegram(token, chat_id, "☁️ **Render Scalper Online**\n\n⚡ Mod: Bollinger Tepki (Vur-Kaç)\n🎯 Hedef: Orta Bant\n🛡️ Stop: Çok Sıkı")
    
    last_heartbeat = time.time()
    last_report_date = datetime.now().day

    while True:
        try:
            # Nabız (Bulut Emojisi ile)
            if time.time() - last_heartbeat > 1800:
                send_telegram(token, chat_id, "☁️ **Render Aktif**\n_Fırsat kolluyorum..._")
                last_heartbeat = time.time()

            # Gün Sonu Raporu
            if datetime.now().day != last_report_date:
                send_daily_report(token, chat_id)
                last_report_date = datetime.now().day

            trades = load_json(TRADES_FILE)

            # Sadece seçili SCALP coinlerini tara
            for symbol in SCALP_COINS:
                if symbol in trades: continue 

                signal, price, tp, sl, score = analyze_scalp(symbol)

                # Scalp için %80 üzeri güven arıyoruz (Bant dışına taşma şartı)
                if signal in ["LONG", "SHORT"] and score >= 80:
                    
                    emoji = "🟢 LONG" if signal == "LONG" else "🔴 SHORT"
                    
                    # Mesaj Formatı (BİLGİSAYARLA AYNI, SADECE İKON FARKLI ☁️)
                    msg = (f"🦁 **#{symbol.replace('/USDT', '')} | ☁️**\n"
                           f"{emoji}\n\n"
                           f"📍 **{price}**\n"
                           f"🎯 **{tp:.4f}**\n"
                           f"🛡️ **{sl:.4f}**\n"
                           f"💎 **Güven: %{score}**\n"
                           f"⚡ **Bollinger Tepkisi**")
                    
                    send_telegram(token, chat_id, msg)
                    
                    trades[symbol] = {"signal": signal, "entry": price, "tp": tp, "sl": sl}
                    save_json(TRADES_FILE, trades)
                    
                    time.sleep(1)

            time.sleep(SCAN_INTERVAL) # 45 saniyede bir tara

        except Exception as e:
            logger.error(f"Hata: {e}")
            time.sleep(10)

if __name__ == "__main__":
    MY_TOKEN = "BURAYA_TOKENINI_YAPISTIR"
    MY_ID = "BURAYA_ID_YAPISTIR"
    
    bot_loop(MY_TOKEN, MY_ID)
