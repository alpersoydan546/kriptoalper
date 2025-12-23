import time
import requests
import threading
import math
import os
from datetime import datetime

# =========================
# CONFIG
# =========================
BINANCE_FAPI = "https://fapi.binance.com"
SYMBOLS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
    "DOGEUSDT","AVAXUSDT","ADAUSDT","LINKUSDT","OPUSDT",
    "ARBUSDT","ATOMUSDT","DOTUSDT","NEARUSDT","APTUSDT"
]

INTERVALS = ["5m", "15m"]
CONF_MIN = 70
SCAN_SLEEP = 60        # her 1 dk tarama
HEARTBEAT_MIN = 30     # 30 dk hayattayım

TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")

# =========================
# STATE
# =========================
last_heartbeat = 0

# =========================
# UTILS
# =========================
def tg_send(text):
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TG_CHAT,
            "text": text
        }, timeout=10)
    except:
        pass

def get_klines(symbol, interval, limit=100):
    url = f"{BINANCE_FAPI}/fapi/v1/klines"
    r = requests.get(url, params={
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    }, timeout=10)
    return r.json()

def rsi(closes, period=14):
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        if diff >= 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))
    if len(gains) < period:
        return 50
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# =========================
# SIGNAL LOGIC
# =========================
def analyze(symbol):
    score = 0
    directions = []

    for tf in INTERVALS:
        kl = get_klines(symbol, tf)
        closes = [float(x[4]) for x in kl]
        r = rsi(closes)

        if r > 55:
            score += 10
            directions.append("LONG")
        elif r < 45:
            score += 10
            directions.append("SHORT")

    if len(set(directions)) != 1:
        return None

    direction = directions[0]
    score += 50  # trend uyumu bonus

    if score < CONF_MIN:
        return None

    price = float(closes[-1])
    if direction == "LONG":
        tp = price * 1.006
        sl = price * 0.996
    else:
        tp = price * 0.994
        sl = price * 1.004

    return {
        "symbol": symbol,
        "dir": direction,
        "price": price,
        "tp": tp,
        "sl": sl,
        "conf": score
    }

# =========================
# MAIN LOOP
# =========================
def scanner_loop():
    global last_heartbeat
    tg_send("🟢 KriptoAlper Hayatta")

    while True:
        now = time.time()

        # HEARTBEAT
        if now - last_heartbeat >= HEARTBEAT_MIN * 60:
            tg_send("🟢 KriptoAlper Hayatta")
            last_heartbeat = now

        for sym in SYMBOLS:
            try:
                sig = analyze(sym)
                if not sig:
                    continue

                msg = (
                    f"📌 {sig['symbol']} | {sig['dir']} | 5m/15m\n"
                    f"💵 Giriş: {sig['price']:.6f}\n"
                    f"🎯 TP: {sig['tp']:.6f}\n"
                    f"🛑 SL: {sig['sl']:.6f}\n"
                    f"⚡ Güven: {sig['conf']}\n"
                    f"🧰 Kaldıraç: 7x"
                )
                tg_send(msg)
                time.sleep(5)

            except Exception as e:
                continue

        time.sleep(SCAN_SLEEP)

def start_scanner():
    t = threading.Thread(target=scanner_loop, daemon=True)
    t.start()
