import os
import time
import math
import requests
import threading
from datetime import datetime

BINANCE_FUTURES = "https://fapi.binance.com"
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")

CONF_MIN = 75
COOLDOWN_MIN = 60
TOP_COINS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
    "DOGEUSDT","AVAXUSDT","LINKUSDT","ADAUSDT","DOTUSDT",
    "TONUSDT","TRXUSDT","MATICUSDT","ATOMUSDT","LTCUSDT"
]

last_signal_time = {}
last_heartbeat = 0


# ----------------- UTILS -----------------

def tg_send(msg):
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TG_CHAT, "text": msg})
    except:
        pass


def klines(symbol, interval, limit=100):
    r = requests.get(
        f"{BINANCE_FUTURES}/fapi/v1/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=10
    )
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
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# ----------------- CONF SCORE -----------------

def confidence_score(symbol):
    try:
        k15 = klines(symbol, "15m", 100)
        k5  = klines(symbol, "5m", 100)
        k1  = klines(symbol, "1m", 100)

        closes15 = [float(x[4]) for x in k15]
        closes5  = [float(x[4]) for x in k5]
        closes1  = [float(x[4]) for x in k1]

        price = closes15[-1]

        ema200 = ema(closes15[-200:], 200)
        trend = price > ema200

        rsi15 = rsi(closes15)
        rsi5  = rsi(closes5)
        rsi1  = rsi(closes1)

        conf = 0

        # TF uyumu
        conf += 15
        if (rsi5 > 55) == trend:
            conf += 10
        if (rsi1 > 55) == trend:
            conf += 5

        # Trend gücü
        conf += 20 if abs(price - ema200) / ema200 > 0.002 else 10

        # RSI
        if 55 < rsi15 < 65:
            conf += 15
        elif 50 < rsi15 < 70:
            conf += 10

        # Coin kalite bonusu
        conf += 10

        direction = "LONG" if trend else "SHORT"

        return conf, direction, price

    except:
        return None, None, None


# ----------------- MAIN LOOP -----------------

def scanner_loop():
    global last_heartbeat

    tg_send("🟢 KriptoAlper Hayatta")

    while True:
        now = time.time()

        # Heartbeat (1 saatte bir)
        if now - last_heartbeat > 3600:
            tg_send("🟢 KriptoAlper Hayatta")
            last_heartbeat = now

        for sym in TOP_COINS:
            last_ts = last_signal_time.get(sym, 0)
            if now - last_ts < COOLDOWN_MIN * 60:
                continue

            conf, direction, price = confidence_score(sym)
            if conf is None or conf < CONF_MIN:
                continue

            tp = price * (1.006 if direction == "LONG" else 0.994)
            sl = price * (0.997 if direction == "LONG" else 1.003)

            msg = (
                f"📌 {sym} · {direction} [15m/5m/1m]\n"
                f"💵 Entry: {price:.5f}\n"
                f"🎯 TP: {tp:.5f}\n"
                f"🛑 SL: {sl:.5f}\n"
                f"⚡ Güven: {conf} / 100\n"
                f"🧰 Önerilen kaldıraç: 7x"
            )

            tg_send(msg)
            last_signal_time[sym] = now
            time.sleep(3)

        time.sleep(30)


# ----------------- START -----------------

if __name__ == "__main__":
    scanner_loop()
