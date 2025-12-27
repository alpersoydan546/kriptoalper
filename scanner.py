import time
import requests
import os
from datetime import datetime
import math

BINANCE_URL = "https://fapi.binance.com"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOLS = ["BTCUSDT", "ETHUSDT"]
INTERVAL = "5m"

last_signal_time = {}
last_heartbeat = 0

def send_tg(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg})

def get_klines(symbol, limit=210):
    url = f"{BINANCE_URL}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": INTERVAL, "limit": limit}
    return requests.get(url, params=params, timeout=10).json()

def ema(values, period):
    k = 2 / (period + 1)
    ema_val = values[0]
    for v in values[1:]:
        ema_val = v * k + ema_val * (1 - k)
    return ema_val

def rsi(values, period=14):
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calculate_confidence(rsi_val, trend_ok, ema_cross):
    score = 50
    if trend_ok:
        score += 15
    if ema_cross:
        score += 15
    if 45 < rsi_val < 65:
        score += 10
    return min(score, 90)

def scanner_loop():
    global last_heartbeat

    send_tg("🟢 KriptoAlper başlatıldı")

    while True:
        now = time.time()

        if now - last_heartbeat > 1800:
            send_tg("🟢 KriptoAlper Hayatta")
            last_heartbeat = now

        for symbol in SYMBOLS:
            try:
                klines = get_klines(symbol)
                closes = [float(k[4]) for k in klines]

                ema200 = ema(closes[-200:], 200)
                ema12_prev = ema(closes[-26:-14], 12)
                ema26_prev = ema(closes[-26:-14], 26)
                ema12_now = ema(closes[-14:], 12)
                ema26_now = ema(closes[-14:], 26)

                price = closes[-1]
                rsi_val = rsi(closes[-15:])

                trend_ok = price > ema200
                ema_cross = ema12_prev < ema26_prev and ema12_now > ema26_now

                confidence = calculate_confidence(rsi_val, trend_ok, ema_cross)

                if confidence < 65:
                    continue

                last_time = last_signal_time.get(symbol, 0)
                if time.time() - last_time < 3600:
                    continue

                direction = "LONG" if trend_ok else "SHORT"
                tp = round(price * 1.007, 2)
                sl = round(price * 0.993, 2)

                msg = (
                    f"📌 {symbol} | {direction} | 5m\n"
                    f"💰 Giriş: {round(price,2)}\n"
                    f"🎯 TP: {tp}\n"
                    f"🛑 SL: {sl}\n"
                    f"⚡ Güven: {confidence}\n"
                    f"📊 Kaldıraç: 5x"
                )

                send_tg(msg)
                last_signal_time[symbol] = time.time()

            except Exception as e:
                print("HATA:", e)

        time.sleep(60)
