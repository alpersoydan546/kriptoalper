import time
import math
import requests
from datetime import datetime, timedelta

BINANCE_URL = "https://fapi.binance.com/fapi/v1/klines"
TELEGRAM_URL = "https://api.telegram.org/bot{}/sendMessage"

SYMBOLS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
    "DOGEUSDT","AVAXUSDT","ADAUSDT","LINKUSDT","DOTUSDT"
]

TF_TRIGGER = "5m"
TF_TREND = "15m"

CONFIDENCE_MIN = 75
COOLDOWN_MINUTES = 60
HEARTBEAT_MINUTES = 30
MAX_DAILY_SIGNAL_PER_SYMBOL = 2

sent_setups = {}
daily_counter = {}
last_heartbeat = datetime.utcnow()

def get_klines(symbol, interval, limit=100):
    r = requests.get(BINANCE_URL, params={
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

def rsi(values, period=14):
    gains, losses = [], []
    for i in range(1, len(values)):
        diff = values[i] - values[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def atr(highs, lows, closes, period=14):
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1])
        ))
    return sum(trs[-period:]) / period

def calc_confidence(rsi_v, trend_ok, momentum):
    score = 50
    if trend_ok: score += 15
    if 55 < rsi_v < 70: score += 10
    if momentum: score += 10
    return min(score, 95)

def send_telegram(msg, token, chat_id):
    url = TELEGRAM_URL.format(token)
    requests.post(url, json={
        "chat_id": chat_id,
        "text": msg,
        "parse_mode": "HTML"
    }, timeout=10)

def scan(token, chat_id):
    global last_heartbeat

    now = datetime.utcnow()

    # ❤️ HEARTBEAT
    if now - last_heartbeat > timedelta(minutes=HEARTBEAT_MINUTES):
        send_telegram("🟢 KriptoAlper Hayatta", token, chat_id)
        last_heartbeat = now

    for symbol in SYMBOLS:
        daily_counter.setdefault(symbol, 0)
        if daily_counter[symbol] >= MAX_DAILY_SIGNAL_PER_SYMBOL:
            continue

        kl5 = get_klines(symbol, TF_TRIGGER)
        kl15 = get_klines(symbol, TF_TREND)

        closes5 = [float(k[4]) for k in kl5]
        closes15 = [float(k[4]) for k in kl15]
        highs5 = [float(k[2]) for k in kl5]
        lows5 = [float(k[3]) for k in kl5]

        price = closes5[-1]

        ema5 = ema(closes5[-50:], 20)
        ema15 = ema(closes15[-50:], 50)

        rsi5 = rsi(closes5)
        atr5 = atr(highs5, lows5, closes5)

        direction = None
        trend_ok = False
        momentum = False

        if price > ema5 and closes15[-1] > ema15:
            direction = "LONG"
            trend_ok = True
            momentum = rsi5 > 55
        elif price < ema5 and closes15[-1] < ema15:
            direction = "SHORT"
            trend_ok = True
            momentum = rsi5 < 45

        if not direction:
            continue

        confidence = calc_confidence(rsi5, trend_ok, momentum)
        if confidence < CONFIDENCE_MIN:
            continue

        setup_id = f"{symbol}-{direction}-{TF_TRIGGER}-{round(price,2)}"

        if setup_id in sent_setups:
            continue

        entry = price
        tp = entry + atr5*1.5 if direction=="LONG" else entry - atr5*1.5
        sl = entry - atr5 if direction=="LONG" else entry + atr5

        msg = (
            f"📌 {symbol} | {direction} | {TF_TRIGGER}/{TF_TREND}\n"
            f"💵 Giriş: {round(entry,4)}\n"
            f"🎯 TP: {round(tp,4)}\n"
            f"🛑 SL: {round(sl,4)}\n"
            f"⚡ Güven: {confidence}\n"
            f"🧰 Kaldıraç: 7x"
        )

        send_telegram(msg, token, chat_id)

        sent_setups[setup_id] = now
        daily_counter[symbol] += 1
