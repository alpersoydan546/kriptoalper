import os
import time
import math
import threading
import requests
from datetime import datetime, timezone, timedelta

# =========================
# ENV
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BINANCE_BASE = "https://fapi.binance.com"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
TIMEFRAMES = ["15m", "1h"]

SCAN_INTERVAL = 60 * 5  # 5 dk
DAILY_REPORT_HOUR = 0   # 00:00

MIN_CONFIDENCE = 70     # %70 altını gönderme
COOLDOWN_MIN = 30       # aynı coine 30 dk tekrar sinyal yok

# =========================
# STATE
# =========================
last_signal_ts = {}
daily_candidates = []

# =========================
# HELPERS
# =========================
def tg_send(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }, timeout=10)
    except:
        pass


def fetch_klines(symbol, interval, limit=200):
    url = f"{BINANCE_BASE}/fapi/v1/klines"
    r = requests.get(url, params={
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    }, timeout=10)
    r.raise_for_status()
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
        diff = values[-i] - values[-i - 1]
        if diff >= 0:
            gains.append(diff)
        else:
            losses.append(abs(diff))
    avg_gain = sum(gains) / period if gains else 0.0001
    avg_loss = sum(losses) / period if losses else 0.0001
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def atr(highs, lows, closes, period=14):
    trs = []
    for i in range(1, period + 1):
        tr = max(
            highs[-i] - lows[-i],
            abs(highs[-i] - closes[-i - 1]),
            abs(lows[-i] - closes[-i - 1])
        )
        trs.append(tr)
    return sum(trs) / period


# =========================
# SIGNAL LOGIC
# =========================
def analyze(symbol, tf):
    kl = fetch_klines(symbol, tf)
    closes = [float(k[4]) for k in kl]
    highs = [float(k[2]) for k in kl]
    lows  = [float(k[3]) for k in kl]

    ema200 = ema(closes[-200:], 200)
    ema50  = ema(closes[-50:], 50)
    r = rsi(closes)
    a = atr(highs, lows, closes)

    price = closes[-1]
    trend_up = price > ema200 and ema50 > ema200
    trend_down = price < ema200 and ema50 < ema200

    score = 0
    direction = None

    if trend_up:
        score += 40
        direction = "LONG"
    if trend_down:
        score += 40
        direction = "SHORT"

    if direction == "LONG" and r < 60:
        score += 30
    if direction == "SHORT" and r > 40:
        score += 30

    score += min(30, int(a / price * 1000))

    if score < MIN_CONFIDENCE or not direction:
        return None

    entry = price
    if direction == "LONG":
        sl = entry - a * 1.2
        tp = entry + a * 2.4
    else:
        sl = entry + a * 1.2
        tp = entry - a * 2.4

    rr = abs(tp - entry) / abs(entry - sl)

    return {
        "symbol": symbol,
        "tf": tf,
        "dir": direction,
        "entry": round(entry, 4),
        "tp": round(tp, 4),
        "sl": round(sl, 4),
        "rr": round(rr, 2),
        "score": score
    }


# =========================
# MAIN LOOP
# =========================
def scanner_loop():
    tg_send("🤖 KriptoAlper scanner başladı.")

    last_daily_report = None

    while True:
        now = datetime.now(timezone(timedelta(hours=3)))

        # === DAILY REPORT ===
        if now.hour == DAILY_REPORT_HOUR and (not last_daily_report or last_daily_report.date() != now.date()):
            if daily_candidates:
                msg = "📊 <b>GÜN SONU RAPORU</b>\n\n"
                for c in sorted(daily_candidates, key=lambda x: -x["score"]):
                    msg += (
                        f"{c['symbol']} {c['tf']} {c['dir']}\n"
                        f"Güven: %{c['score']} | R:R {c['rr']}\n\n"
                    )
                tg_send(msg)
            daily_candidates.clear()
            last_daily_report = now

        # === SCAN ===
        for s in SYMBOLS:
            for tf in TIMEFRAMES:
                key = f"{s}_{tf}"
                if key in last_signal_ts:
                    if time.time() - last_signal_ts[key] < COOLDOWN_MIN * 60:
                        continue

                try:
                    sig = analyze(s, tf)
                except:
                    continue

                if not sig:
                    continue

                last_signal_ts[key] = time.time()
                daily_candidates.append(sig)

                tg_send(
                    f"🚨 <b>SİNYAL</b>\n\n"
                    f"{sig['symbol']} | {sig['tf']}\n"
                    f"Yön: {sig['dir']}\n"
                    f"Giriş: {sig['entry']}\n"
                    f"TP: {sig['tp']}\n"
                    f"SL: {sig['sl']}\n"
                    f"R:R: {sig['rr']}\n"
                    f"Güven: %{sig['score']}"
                )

        time.sleep(SCAN_INTERVAL)


# =========================
# THREAD START
# =========================
def start_scanner():
    t = threading.Thread(target=scanner_loop, daemon=True)
    t.start()
