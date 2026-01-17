import os
import time
import requests
from datetime import datetime, timedelta
from threading import Thread, Lock

# ================== AYARLAR ==================
BINANCE_URL = "https://fapi.binance.com/fapi/v1/klines"
INTERVAL = "5m"
SCAN_SLEEP = 60

HEARTBEAT_MIN = 30
COOLDOWN_MIN = 45
MIN_CONF = 75

SYMBOLS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
    "ADAUSDT","LINKUSDT","AVAXUSDT","DOGEUSDT","DOTUSDT"
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
        print("[TG] ENV missing")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text},
            timeout=10
        )
        print("[TG]", r.status_code)
    except Exception as e:
        print("[TG ERROR]", e)

# ---------------- BINANCE ----------------
def get_closes(symbol, limit=50):
    r = requests.get(
        BINANCE_URL,
        params={"symbol": symbol, "interval": INTERVAL, "limit": limit},
        timeout=10
    )
    data = r.json()
    return [float(k[4]) for k in data]

# ---------------- STRATEJİ ----------------
def analyze(symbol):
    closes = get_closes(symbol)
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

    confidence = 75 + int(abs(last - prev) / last * 1000)
    confidence = min(confidence, 90)

    if confidence < MIN_CONF:
        return None

    return {
        "symbol": symbol,
        "dir": direction,
        "price": last,
        "conf": confidence
    }

# ---------------- COOLDOWN ----------------
def can_send(symbol, direction):
    key = f"{symbol}_{direction}_{INTERVAL}"
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
        now = datetime.utcnow()
        if now - last_heartbeat >= timedelta(minutes=HEARTBEAT_MIN):
            tg_send("🫀 KriptoAlper hayattayım (30 dk)")
            last_heartbeat = now
        time.sleep(10)

# ---------------- SCANNER ----------------
def scanner_loop():
    tg_send("🚀 KriptoAlper V1 başlatıldı")
    while True:
        for sym in SYMBOLS:
            try:
                sig = analyze(sym)
                if not sig:
                    continue

                if not can_send(sig["symbol"], sig["dir"]):
                    continue

                msg = (
                    f"📊 SİNYAL\n"
                    f"Coin: {sig['symbol']}\n"
                    f"TF: {INTERVAL}\n"
                    f"Yön: {sig['dir']}\n"
                    f"Fiyat: {sig['price']:.4f}\n"
                    f"Güven: {sig['conf']}%"
                )
                tg_send(msg)

            except Exception as e:
                print("[SCAN ERROR]", sym, e)

        time.sleep(SCAN_SLEEP)

# ---------------- START ----------------
def start():
    Thread(target=heartbeat_loop, daemon=True).start()
    scanner_loop()
