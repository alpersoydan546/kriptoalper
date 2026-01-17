import time
import requests
import math
from datetime import datetime, timedelta

# =====================
# CONFIG
# =====================
BINANCE_FUTURES = "https://fapi.binance.com"
SYMBOLS = ["BTCUSDT","ETHUSDT","DOGEUSDT","LINKUSDT","DOTUSDT","OPUSDT"]
LEVERAGE = 7
SCAN_INTERVAL = 30  # saniye
COOLDOWN_MINUTES = 30
MAX_SAME_DIRECTION = 2

MIN_RR = 1.5
MIN_TP_PERCENT = 0.25

telegram_token = None
telegram_chat = None

last_signal_time = {}
active_directions = {"LONG": 0, "SHORT": 0}

# =====================
# ENV
# =====================
def set_env(token, chat):
    global telegram_token, telegram_chat
    telegram_token = token
    telegram_chat = chat

# =====================
# HELPERS
# =====================
def send_telegram(msg):
    url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
    requests.post(url, json={"chat_id": telegram_chat, "text": msg})

def get_klines(symbol, interval, limit=100):
    url = f"{BINANCE_FUTURES}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    return requests.get(url, params=params, timeout=10).json()

def ema(values, period):
    k = 2 / (period + 1)
    ema_val = values[0]
    for v in values[1:]:
        ema_val = v * k + ema_val * (1 - k)
    return ema_val

def rr_ratio(entry, tp, sl, direction):
    if direction == "LONG":
        return abs(tp - entry) / abs(entry - sl)
    else:
        return abs(entry - tp) / abs(sl - entry)

# =====================
# STRATEGY
# =====================
def analyze(symbol):
    global active_directions

    # Cooldown
    if symbol in last_signal_time:
        if datetime.utcnow() - last_signal_time[symbol] < timedelta(minutes=COOLDOWN_MINUTES):
            return

    kl5 = get_klines(symbol, "5m")
    kl15 = get_klines(symbol, "15m")

    closes5 = [float(k[4]) for k in kl5]
    closes15 = [float(k[4]) for k in kl15]

    price = closes5[-1]

    ema200_5 = ema(closes5[-200:], 200)
    ema200_15 = ema(closes15[-200:], 200)

    # Trend
    if price < ema200_5 and price < ema200_15:
        direction = "SHORT"
    elif price > ema200_5 and price > ema200_15:
        direction = "LONG"
    else:
        return

    if active_directions[direction] >= MAX_SAME_DIRECTION:
        return

    # TP / SL (ATR benzeri basit oran)
    tp = price * (1 - 0.003) if direction == "SHORT" else price * (1 + 0.003)
    sl = price * (1 + 0.0018) if direction == "SHORT" else price * (1 - 0.0018)

    tp_percent = abs(tp - price) / price * 100
    rr = rr_ratio(price, tp, sl, direction)

    if tp_percent < MIN_TP_PERCENT:
        return
    if rr < MIN_RR:
        return

    # PASS ALL FILTERS ✅
    last_signal_time[symbol] = datetime.utcnow()
    active_directions[direction] += 1

    msg = f"""📌 {symbol} | {direction} | 5m/15m
💵 Giriş: {price:.6f}
🎯 TP: {tp:.6f}
🛑 SL: {sl:.6f}
⚡ Güven: 85
🧰 Kaldıraç: {LEVERAGE}x"""

    send_telegram(msg)

# =====================
# LOOP
# =====================
def scanner_loop():
    send_telegram("🚀 KriptoAlper scanner başlatıldı")
    while True:
        try:
            active_directions["LONG"] = 0
            active_directions["SHORT"] = 0
            for sym in SYMBOLS:
                analyze(sym)
            time.sleep(SCAN_INTERVAL)
        except Exception as e:
            print("Scanner error:", e)
            time.sleep(10)
