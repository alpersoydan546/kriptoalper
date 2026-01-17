import os
import time
import requests
from datetime import datetime, timedelta
from threading import Thread, Lock
import math

# ================== AYARLAR ==================
BINANCE = "https://fapi.binance.com"
TF_FAST = "5m"
TF_TREND = "15m"

SCAN_SLEEP = 60
HEARTBEAT_MIN = 30
COOLDOWN_MIN = 45

MIN_CONF = 75

SYMBOLS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
    "ADAUSDT","LINKUSDT","AVAXUSDT","DOGEUSDT","DOTUSDT",
    "INJUSDT","OPUSDT","ARBUSDT","SEIUSDT"
]
# ============================================

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

lock = Lock()
cooldowns = {}
last_heartbeat = datetime.utcnow()

# ---------------- TELEGRAM ----------------
def tg_send(text):
    if not TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text},
            timeout=10
        )
    except:
        pass

# ---------------- BINANCE ----------------
def get_klines(symbol, tf, limit=100):
    r = requests.get(
        f"{BINANCE}/fapi/v1/klines",
        params={"symbol": symbol, "interval": tf, "limit": limit},
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
    for i in range(1, period + 1):
        delta = values[-i] - values[-i - 1]
        if delta >= 0:
            gains.append(delta)
        else:
            losses.append(abs(delta))
    if not losses:
        return 100
    rs = (sum(gains) / period) / (sum(losses) / period)
    return 100 - (100 / (1 + rs))

def atr(klines, period=14):
    trs = []
    for i in range(1, period + 1):
        high = float(klines[-i][2])
        low = float(klines[-i][3])
        prev_close = float(klines[-i - 1][4])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    return sum(trs) / period

# ---------------- STRATEJİ ----------------
def analyze(symbol):
    k5 = get_klines(symbol, TF_FAST, 120)
    k15 = get_klines(symbol, TF_TREND, 120)

    closes5 = [float(k[4]) for k in k5]
    closes15 = [float(k[4]) for k in k15]

    ema200_15 = ema(closes15[-200:], 200)
    ema50_15 = ema(closes15[-50:], 50)

    trend = "LONG" if ema50_15 > ema200_15 else "SHORT"

    last = closes5[-1]
    prev = closes5[-2]

    if last > prev and trend == "LONG":
        direction = "LONG"
    elif last < prev and trend == "SHORT":
        direction = "SHORT"
    else:
        return None

    r = rsi(closes5)
    if direction == "LONG" and r > 70:
        return None
    if direction == "SHORT" and r < 30:
        return None

    a = atr(k5)
    entry = last

    tp = entry + (a * 1.5 if direction == "LONG" else -a * 1.5)
    sl = entry - (a if direction == "LONG" else -a)

    confidence = 60
    confidence += 10 if abs(last - prev) / entry > 0.001 else 0
    confidence += 10 if abs(ema50_15 - ema200_15) / ema200_15 > 0.002 else 0
    confidence += 10 if 40 < r < 60 else 0

    if confidence < MIN_CONF:
        return None

    return {
        "symbol": symbol,
        "dir": direction,
        "entry": entry,
        "tp": tp,
        "sl": sl,
        "conf": confidence,
        "lev": 7
    }

# ---------------- COOLDOWN ----------------
def can_send(symbol, direction):
    key = f"{symbol}_{direction}_{TF_FAST}"
    now = datetime.utcnow()
    with lock:
        if key in cooldowns and now < cooldowns[key]:
            return False
        cooldowns[key] = now + timedelta(minutes=COOLDOWN_MIN)
        return True

# ---------------- HEARTBEAT ----------------
def heartbeat_loop():
    global last_heartbeat
    while True:
        if datetime.utcnow() - last_heartbeat >= timedelta(minutes=HEARTBEAT_MIN):
            tg_send("🟢 KriptoAlper Hayatta")
            last_heartbeat = datetime.utcnow()
        time.sleep(10)

# ---------------- SCANNER ----------------
def scanner_loop():
    tg_send("🚀 KriptoAlper scanner başlatıldı")
    while True:
        for sym in SYMBOLS:
            try:
                sig = analyze(sym)
                if not sig:
                    continue

                if not can_send(sig["symbol"], sig["dir"]):
                    continue

                msg = (
                    f"📌 {sig['symbol']} | {sig['dir']} | 5m/15m\n"
                    f"💵 Giriş: {sig['entry']:.6f}\n"
                    f"🎯 TP: {sig['tp']:.6f}\n"
                    f"🛑 SL: {sig['sl']:.6f}\n"
                    f"⚡ Güven: {sig['conf']}\n"
                    f"🧰 Kaldıraç: {sig['lev']}x"
                )

                tg_send(msg)

            except Exception:
                pass

        time.sleep(SCAN_SLEEP)

# ---------------- START ----------------
def start():
    Thread(target=heartbeat_loop, daemon=True).start()
    scanner_loop()
