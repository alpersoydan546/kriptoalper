import time
import os
import requests
import hashlib
from datetime import datetime, timedelta
from collections import defaultdict
import math

BINANCE_URL = "https://fapi.binance.com"
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")

SYMBOLS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
    "DOGEUSDT","ADAUSDT","AVAXUSDT","LINKUSDT","DOTUSDT"
]

MIN_CONFIDENCE = 75
SCAN_INTERVAL = 60
COOLDOWN_MINUTES = 45
MAX_DIRECTION_PER_15M = 2

last_sent = {}
direction_counter = defaultdict(list)
last_heartbeat = datetime.utcnow()

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TG_CHAT, "text": text})

def get_klines(symbol, interval="5m", limit=100):
    r = requests.get(f"{BINANCE_URL}/klines", params={
        "symbol": symbol, "interval": interval, "limit": limit
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
    for i in range(1, period + 1):
        diff = closes[-i] - closes[-i-1]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))
    if sum(losses) == 0:
        return 100
    rs = (sum(gains)/period) / (sum(losses)/period)
    return 100 - (100 / (1 + rs))

def atr(highs, lows, closes, period=14):
    trs = []
    for i in range(1, period + 1):
        tr = max(
            highs[-i] - lows[-i],
            abs(highs[-i] - closes[-i-1]),
            abs(lows[-i] - closes[-i-1])
        )
        trs.append(tr)
    return sum(trs) / period

def confidence_score(trend, rsi_val, atr_ratio, btc_penalty):
    score = 0
    score += 25 if trend else 0
    score += 20 if rsi_val < 30 or rsi_val > 70 else 10
    score += 20 if atr_ratio > 0.003 else 10
    score -= btc_penalty
    return score

def can_send(symbol, direction):
    now = datetime.utcnow()

    if symbol in last_sent:
        if now - last_sent[symbol] < timedelta(minutes=COOLDOWN_MINUTES):
            return False

    direction_counter[direction] = [
        t for t in direction_counter[direction]
        if now - t < timedelta(minutes=15)
    ]

    if len(direction_counter[direction]) >= MAX_DIRECTION_PER_15M:
        return False

    direction_counter[direction].append(now)
    return True

def scan():
    global last_heartbeat

    # HEARTBEAT
    if datetime.utcnow() - last_heartbeat > timedelta(minutes=30):
        send_telegram("🟢 KriptoAlper Hayatta")
        last_heartbeat = datetime.utcnow()

    btc_klines = get_klines("BTCUSDT")
    btc_closes = [float(k[4]) for k in btc_klines]
    btc_trend = btc_closes[-1] > ema(btc_closes[-50:], 50)

    for symbol in SYMBOLS:
        klines = get_klines(symbol)
        closes = [float(k[4]) for k in klines]
        highs = [float(k[2]) for k in klines]
        lows  = [float(k[3]) for k in klines]

        price = closes[-1]
        ema50 = ema(closes[-50:], 50)
        ema200 = ema(closes[-200:], 200)
        trend = price > ema50 > ema200 or price < ema50 < ema200

        direction = "LONG" if price > ema50 else "SHORT"
        rsi_val = rsi(closes)
        atr_val = atr(highs, lows, closes)
        atr_ratio = atr_val / price

        btc_penalty = 15 if direction == ("LONG" if btc_trend else "SHORT") else 0

        confidence = confidence_score(trend, rsi_val, atr_ratio, btc_penalty)

        if confidence < MIN_CONFIDENCE:
            continue

        if not can_send(symbol, direction):
            continue

        tp = price + atr_val * (1 if direction == "LONG" else -1.2)
        sl = price - atr_val * (0.8 if direction == "LONG" else -0.8)

        msg = (
            f"📌 {symbol} | {direction} | 5m/15m\n"
            f"💵 Giriş: {price:.4f}\n"
            f"🎯 TP: {tp:.4f}\n"
            f"🛑 SL: {sl:.4f}\n"
            f"⚡ Güven: {confidence}\n"
            f"🧰 Kaldıraç: 7x"
        )

        send_telegram(msg)
        last_sent[symbol] = datetime.utcnow()

while True:
    try:
        scan()
        time.sleep(SCAN_INTERVAL)
    except Exception as e:
        print("SCAN ERROR:", e)
        time.sleep(10)
