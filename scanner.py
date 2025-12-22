import os
import time
import math
import requests
from datetime import datetime, timezone
from collections import defaultdict

# ==========================
# AYARLAR
# ==========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BINANCE_FUTURES = "https://fapi.binance.com"

PAIRS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT",
    "XRPUSDT", "DOGEUSDT", "AVAXUSDT"
]

TIMEFRAMES = ["5m", "15m", "1h"]

CONF_MIN = 70
COOLDOWN_MIN = 30          # Aynı coin için tekrar sinyal süresi
SCAN_INTERVAL = 60         # 1 dk
RECOMMENDED_LEVERAGE = "7x"

_last_signal_time = defaultdict(lambda: 0)

# ==========================
# YARDIMCI FONKSİYONLAR
# ==========================
def tg_send(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True
    }
    requests.post(url, json=payload, timeout=10)


def get_klines(symbol, interval, limit=100):
    url = f"{BINANCE_FUTURES}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(url, params=params, timeout=10)
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
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period if sum(losses) != 0 else 1e-9
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# ==========================
# GÜVEN SKORU
# ==========================
def calc_confidence(closes):
    score = 50

    ema50 = ema(closes[-50:], 50)
    ema200 = ema(closes[-200:], 200)
    rsi14 = rsi(closes[-15:], 14)

    if ema50 > ema200:
        score += 10
    else:
        score -= 10

    if 45 < rsi14 < 65:
        score += 10
    else:
        score -= 5

    volatility = abs(closes[-1] - closes[-5]) / closes[-5]
    if volatility < 0.01:
        score += 5

    return max(0, min(100, score))


# ==========================
# FORMAT (SENİN SEÇTİĞİN)
# ==========================
def format_signal(symbol, side, tf_list, entry, tp, sl, confidence):
    tf_txt = "/".join(tf_list)
    return (
        f"📌 {symbol} · {side} [{tf_txt}]\n"
        f"💵 Entry: {entry:.5f}\n"
        f"🎯 TP: {tp:.5f}\n"
        f"🛑 SL: {sl:.5f}\n"
        f"⚡ Güven: {confidence}\n"
        f"🧰 Önerilen kaldıraç: {RECOMMENDED_LEVERAGE}"
    )


# ==========================
# ANA SİNYAL MANTIĞI
# ==========================
def analyze(symbol):
    closes_by_tf = {}
    for tf in TIMEFRAMES:
        kl = get_klines(symbol, tf)
        closes_by_tf[tf] = [float(k[4]) for k in kl]

    conf_scores = []
    for tf in TIMEFRAMES:
        conf_scores.append(calc_confidence(closes_by_tf[tf]))

    confidence = int(sum(conf_scores) / len(conf_scores))
    if confidence < CONF_MIN:
        return None

    now = time.time()
    if now - _last_signal_time[symbol] < COOLDOWN_MIN * 60:
        return None

    price = closes_by_tf["5m"][-1]

    # YÖN
    side = "LONG" if conf_scores.count(max(conf_scores)) >= 2 else "SHORT"

    if side == "LONG":
        sl = price * 0.99
        tp = price * 1.02
    else:
        sl = price * 1.01
        tp = price * 0.98

    _last_signal_time[symbol] = now

    return format_signal(
        symbol,
        side,
        TIMEFRAMES,
        price,
        tp,
        sl,
        confidence
    )


# ==========================
# LOOP
# ==========================
def main_loop():
    tg_send("🟢 KriptoAlper Hayatta")

    while True:
        try:
            for symbol in PAIRS:
                msg = analyze(symbol)
                if msg:
                    tg_send(msg)
            time.sleep(SCAN_INTERVAL)
        except Exception as e:
            print("ERR:", e)
            time.sleep(10)


if __name__ == "__main__":
    main_loop()
