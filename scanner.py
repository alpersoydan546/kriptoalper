import time
import math
import requests
import threading
from datetime import datetime, timedelta

# ================= CONFIG =================
BINANCE_FUTURES = "https://fapi.binance.com"
SYMBOLS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","AVAXUSDT",
    "LINKUSDT","INJUSDT","OPUSDT","ARBUSDT","SEIUSDT",
    "XRPUSDT","ADAUSDT"
]

CONF_MIN = 75
COOLDOWN_MIN = 90
SCAN_INTERVAL = 60
HEARTBEAT_MIN = 30

TG_TOKEN = None
TG_CHAT = None

# ============== STATE =====================
last_signal = {}
open_trades = []
last_heartbeat = 0

# ============== HELPERS ===================
def tg_send(msg):
    if not TG_TOKEN or not TG_CHAT:
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    requests.post(url, json={
        "chat_id": TG_CHAT,
        "text": msg,
        "parse_mode": "HTML"
    }, timeout=10)

def klines(symbol, interval, limit=100):
    url = f"{BINANCE_FUTURES}/fapi/v1/klines"
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

def rsi(values, period=14):
    gains, losses = [], []
    for i in range(1, len(values)):
        diff = values[i] - values[i-1]
        gains.append(max(diff,0))
        losses.append(abs(min(diff,0)))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# ============== CORE ======================
def analyze(symbol):
    try:
        c5 = klines(symbol,"5m",100)
        c15 = klines(symbol,"15m",100)
        c1h = klines(symbol,"1h",100)

        closes5 = [float(x[4]) for x in c5]
        closes15 = [float(x[4]) for x in c15]
        closes1h = [float(x[4]) for x in c1h]

        ema5 = ema(closes5[-30:],21)
        ema15 = ema(closes15[-30:],21)
        ema1h = ema(closes1h[-30:],50)

        rsi5 = rsi(closes5)
        rsi15 = rsi(closes15)

        price = closes5[-1]

        trend_up = price > ema1h
        trend_down = price < ema1h

        score = 0
        score += 20 if trend_up or trend_down else 0
        score += 20 if ema5 > ema15 or ema5 < ema15 else 0
        score += 20 if 45 < rsi5 < 65 else 0
        score += 15 if 45 < rsi15 < 65 else 0
        score += 20 if abs(price-ema5)/price < 0.003 else 0

        if score < CONF_MIN:
            return None

        side = "LONG" if trend_up else "SHORT"
        entry = price
        tp = entry * (1.004 if side=="LONG" else 0.996)
        sl = entry * (0.998 if side=="LONG" else 1.002)

        return {
            "symbol": symbol,
            "side": side,
            "entry": entry,
            "tp": tp,
            "sl": sl,
            "score": score
        }

    except Exception:
        return None

# ============== LOOP ======================
def main_loop():
    global last_heartbeat

    while True:
        now = time.time()

        # HEARTBEAT
        if now - last_heartbeat > HEARTBEAT_MIN*60:
            tg_send("🟢 <b>KriptoAlper Hayatta</b>")
            last_heartbeat = now

        for sym in SYMBOLS:
            if sym in last_signal:
                if now - last_signal[sym] < COOLDOWN_MIN*60:
                    continue

            sig = analyze(sym)
            if not sig:
                continue

            last_signal[sym] = now
            open_trades.append({
                **sig,
                "time": datetime.now()
            })

            msg = (
                f"📌 <b>{sig['symbol']}</b> | <b>{sig['side']}</b> | 5m/15m\n"
                f"💵 Giriş: {sig['entry']:.6f}\n"
                f"🎯 TP: {sig['tp']:.6f}\n"
                f"🛑 SL: {sig['sl']:.6f}\n"
                f"⚡ Güven: {sig['score']}\n"
                f"🧰 Kaldıraç: 7x"
            )
            tg_send(msg)

        time.sleep(SCAN_INTERVAL)

# ============== START =====================
def start(token, chat):
    global TG_TOKEN, TG_CHAT
    TG_TOKEN = token
    TG_CHAT = chat
    threading.Thread(target=main_loop, daemon=True).start()
