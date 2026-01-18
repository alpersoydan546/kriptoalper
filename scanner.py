import time
import requests
import hashlib
from datetime import datetime, timedelta
from threading import Lock

BINANCE = "https://fapi.binance.com"
INTERVAL = "5m"

SYMBOLS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
    "ADAUSDT","DOGEUSDT","LINKUSDT","AVAXUSDT","DOTUSDT"
]

CONF_MIN = 80
SCAN_SLEEP = 90          # 1.5 dk
COOLDOWN_MIN = 45
HEARTBEAT_MIN = 30

TELEGRAM_TOKEN = None
CHAT_ID = None

last_sent = {}
last_heartbeat = datetime.utcnow()
lock = Lock()

# ------------------ TELEGRAM ------------------

def tg_send(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(
            url,
            json={"chat_id": CHAT_ID, "text": msg},
            timeout=10
        )
    except:
        pass

# ------------------ DATA ------------------

def fetch_klines(symbol, limit=30):
    r = requests.get(
        BINANCE + "/fapi/v1/klines",
        params={"symbol": symbol, "interval": INTERVAL, "limit": limit},
        timeout=10
    )
    return r.json()

# ------------------ SIGNAL ------------------

def calc_signal(symbol):
    kl = fetch_klines(symbol)
    closes = [float(k[4]) for k in kl]

    if len(closes) < 20:
        return None

    last = closes[-1]
    prev = closes[-2]

    if last > prev:
        direction = "LONG"
    elif last < prev:
        direction = "SHORT"
    else:
        return None

    move = abs(last - prev) / last * 100
    confidence = int(70 + move * 400)
    confidence = min(confidence, 85)

    if confidence < CONF_MIN:
        return None

    tp = last * (1.004 if direction == "LONG" else 0.996)
    sl = last * (0.997 if direction == "LONG" else 1.003)

    return {
        "symbol": symbol,
        "dir": direction,
        "entry": last,
        "tp": tp,
        "sl": sl,
        "conf": confidence,
        "lev": 7
    }

# ------------------ COOLDOWN ------------------

def sig_hash(sig):
    raw = f"{sig['symbol']}{sig['dir']}{round(sig['entry'],4)}"
    return hashlib.md5(raw.encode()).hexdigest()

def can_send(sig):
    h = sig_hash(sig)
    now = datetime.utcnow()

    with lock:
        if h in last_sent:
            if now - last_sent[h] < timedelta(minutes=COOLDOWN_MIN):
                return False
        last_sent[h] = now
        return True

# ------------------ FORMAT ------------------

def format_msg(sig):
    return (
        f"📌 {sig['symbol']} | {sig['dir']} | 5m/15m\n"
        f"💵 Giriş: {sig['entry']:.6f}\n"
        f"🎯 TP: {sig['tp']:.6f}\n"
        f"🛑 SL: {sig['sl']:.6f}\n"
        f"⚡ Güven: {sig['conf']}\n"
        f"🧰 Kaldıraç: {sig['lev']}x"
    )

# ------------------ HEARTBEAT ------------------

def heartbeat():
    global last_heartbeat
    now = datetime.utcnow()
    if now - last_heartbeat >= timedelta(minutes=HEARTBEAT_MIN):
        tg_send("🟢 KriptoAlper Hayatta")
        last_heartbeat = now

# ------------------ MAIN LOOP ------------------

def run(token, chat):
    global TELEGRAM_TOKEN, CHAT_ID
    TELEGRAM_TOKEN = token
    CHAT_ID = chat

    tg_send("🚀 KriptoAlper scanner başlatıldı")

    while True:
        try:
            heartbeat()
            sent_count = 0

            for sym in SYMBOLS:
                if sent_count >= 3:   # SPAM ENGELİ
                    break

                sig = calc_signal(sym)
                if not sig:
                    continue

                if can_send(sig):
                    tg_send(format_msg(sig))
                    sent_count += 1

            time.sleep(SCAN_SLEEP)

        except Exception as e:
            tg_send(f"❌ Scanner hata: {e}")
            time.sleep(60)
