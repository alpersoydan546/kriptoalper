import time
import math
import threading
import requests
from datetime import datetime, timedelta

# =========================
# CONFIG
# =========================
BINANCE_FAPI = "https://fapi.binance.com"
SYMBOLS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","DOGEUSDT",
    "ADAUSDT","AVAXUSDT","LINKUSDT","DOTUSDT","NEARUSDT","APTUSDT"
]

TIMEFRAMES = ["5m", "15m"]
TREND_TF = "1h"

CONF_MIN = 75
COOLDOWN_MIN = 60
HEARTBEAT_MIN = 30
SLEEP_BETWEEN_SCANS = 90

LEVERAGE = "7x"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# =========================
# STATE
# =========================
last_signal_time = {}
last_heartbeat = 0

# =========================
# HELPERS
# =========================
def tg_send(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text
    }, timeout=10)

def get_klines(symbol, interval, limit=200):
    url = f"{BINANCE_FAPI}/fapi/v1/klines"
    r = requests.get(url, params={
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    }, timeout=10)
    return r.json()

def ema(values, period):
    k = 2 / (period + 1)
    ema_val = values[0]
    for v in values[1:]:
        ema_val = v * k + ema_val * (1 - k)
    return ema_val

def rsi(closes, period=14):
    gains, losses = [], []
    for i in range(1, period+1):
        diff = closes[-i] - closes[-i-1]
        if diff >= 0:
            gains.append(diff)
        else:
            losses.append(abs(diff))
    avg_gain = sum(gains)/period if gains else 0.0001
    avg_loss = sum(losses)/period if losses else 0.0001
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# =========================
# CORE LOGIC
# =========================
def analyze(symbol):
    # Trend filter (1h EMA200)
    h1 = get_klines(symbol, TREND_TF)
    h1_closes = [float(c[4]) for c in h1]
    ema200 = ema(h1_closes[-200:], 200)
    price = h1_closes[-1]

    trend = "LONG" if price > ema200 else "SHORT"

    score = 50

    tfs_ok = []
    for tf in TIMEFRAMES:
        k = get_klines(symbol, tf)
        closes = [float(c[4]) for c in k]
        r = rsi(closes)
        tf_price = closes[-1]

        if trend == "LONG" and r > 50:
            tfs_ok.append(tf)
            score += 12
        elif trend == "SHORT" and r < 50:
            tfs_ok.append(tf)
            score += 12

    if len(tfs_ok) < 2:
        return None

    if score < CONF_MIN:
        return None

    entry = price
    if trend == "LONG":
        tp = entry * 1.008
        sl = entry * 0.996
    else:
        tp = entry * 0.992
        sl = entry * 1.004

    return {
        "symbol": symbol,
        "side": trend,
        "tfs": "/".join(tfs_ok),
        "entry": entry,
        "tp": tp,
        "sl": sl,
        "score": score
    }

def format_signal(s):
    return (
        f"📌 {s['symbol']} | {s['side']} | {s['tfs']}\n"
        f"💵 Giriş: {round(s['entry'],6)}\n"
        f"🎯 TP: {round(s['tp'],6)}\n"
        f"🛑 SL: {round(s['sl'],6)}\n"
        f"⚡ Güven: {s['score']}\n"
        f"🧰 Kaldıraç: {LEVERAGE}"
    )

# =========================
# MAIN LOOP
# =========================
def scanner_loop():
    global last_heartbeat

    tg_send("🟢 KriptoAlper Hayatta")

    while True:
        now = time.time()

        # Heartbeat
        if now - last_heartbeat > HEARTBEAT_MIN * 60:
            tg_send("🟢 KriptoAlper Hayatta")
            last_heartbeat = now

        for sym in SYMBOLS:
            last = last_signal_time.get(sym, 0)
            if now - last < COOLDOWN_MIN * 60:
                continue

            try:
                sig = analyze(sym)
                if sig:
                    tg_send(format_signal(sig))
                    last_signal_time[sym] = now
                    time.sleep(2)
            except Exception as e:
                continue

        time.sleep(SLEEP_BETWEEN_SCANS)

# =========================
# THREAD START
# =========================
def start():
    t = threading.Thread(target=scanner_loop, daemon=True)
    t.start()
