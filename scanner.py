# scanner.py
import time
import math
import requests
from datetime import datetime, timedelta

BINANCE_URL = "https://fapi.binance.com/fapi/v1/klines"
TELEGRAM_URL = f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}/sendMessage"
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SYMBOLS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
    "DOGEUSDT","ADAUSDT","AVAXUSDT","LINKUSDT","DOTUSDT",
    "NEARUSDT","APTUSDT","ARBUSDT","OPUSDT"
]

TIMEFRAMES = ["5m","15m","1m"]

CONF_MIN = 71
COOLDOWN_MIN = 45
MAX_DAILY_PER_SYMBOL = 3
LEVERAGE = "7x"

last_signal = {}
daily_counter = {}
last_alive_ping = 0


def send_telegram(msg):
    requests.post(TELEGRAM_URL, json={
        "chat_id": CHAT_ID,
        "text": msg,
        "parse_mode": "HTML"
    })


def alive_ping():
    global last_alive_ping
    if time.time() - last_alive_ping > 1800:
        send_telegram("🟢 KriptoAlper Hayatta")
        last_alive_ping = time.time()


def fetch_klines(symbol, tf, limit=100):
    r = requests.get(BINANCE_URL, params={
        "symbol": symbol,
        "interval": tf,
        "limit": limit
    })
    return r.json()


def calc_confidence(tf_hits):
    base = 65
    base += 5 * tf_hits
    return min(base, 90)


def can_send(signature):
    now = time.time()
    if signature in last_signal:
        if now - last_signal[signature] < COOLDOWN_MIN * 60:
            return False
    return True


def daily_limit_ok(symbol, side):
    today = datetime.utcnow().date()
    key = f"{symbol}-{side}-{today}"
    daily_counter.setdefault(key, 0)
    if daily_counter[key] >= MAX_DAILY_PER_SYMBOL:
        return False
    daily_counter[key] += 1
    return True


def scan():
    for symbol in SYMBOLS:
        signals = []
        tf_hits = 0

        for tf in TIMEFRAMES:
            klines = fetch_klines(symbol, tf)
            closes = [float(k[4]) for k in klines]

            ema_fast = sum(closes[-9:]) / 9
            ema_slow = sum(closes[-21:]) / 21

            if ema_fast > ema_slow:
                signals.append("LONG")
                tf_hits += 1
            elif ema_fast < ema_slow:
                signals.append("SHORT")
                tf_hits += 1

        if tf_hits < 2:
            continue

        side = max(set(signals), key=signals.count)
        confidence = calc_confidence(tf_hits)

        if confidence <= CONF_MIN:
            continue

        signature = f"{symbol}-{side}"

        if not can_send(signature):
            continue

        if not daily_limit_ok(symbol, side):
            continue

        price = closes[-1]
        tp = price * (1.004 if side == "LONG" else 0.996)
        sl = price * (0.998 if side == "LONG" else 1.002)

        msg = (
            f"📌 <b>{symbol}</b> | <b>{side}</b> | 5m/15m\n"
            f"💵 Giriş: {price:.6f}\n"
            f"🎯 TP: {tp:.6f}\n"
            f"🛑 SL: {sl:.6f}\n"
            f"⚡ Güven: {confidence}\n"
            f"🧰 Kaldıraç: {LEVERAGE}"
        )

        send_telegram(msg)
        last_signal[signature] = time.time()


def main():
    while True:
        try:
            alive_ping()
            scan()
            time.sleep(60)
        except Exception as e:
            send_telegram(f"❌ Scanner Hata: {e}")
            time.sleep(30)


if __name__ == "__main__":
    main()
