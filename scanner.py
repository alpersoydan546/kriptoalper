import os
import time
import requests
from datetime import datetime

# ================== ENV ==================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BINANCE_URL = "https://fapi.binance.com/fapi/v1/klines"

# ================== AYARLAR ==================
SCAN_INTERVAL = 300          # 5 dk
HEARTBEAT_INTERVAL = 1800    # 30 dk
CONF_MIN = 70
LEVERAGE = "7x"

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT",
    "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT"
]

sent_cache = {}
last_heartbeat = 0

# ================== TELEGRAM ==================
def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram ENV yok")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "parse_mode": "HTML"
    }
    try:
        r = requests.post(url, data=payload, timeout=10)
        print("[TG]", r.status_code)
    except Exception as e:
        print("[TG ERROR]", e)

# ================== BINANCE ==================
def get_price(symbol):
    try:
        r = requests.get(
            BINANCE_URL,
            params={"symbol": symbol, "interval": "5m", "limit": 1},
            timeout=10
        )
        return float(r.json()[0][4])
    except:
        return None

# ================== SİNYAL ==================
def generate_signal(symbol):
    price = get_price(symbol)
    if not price:
        return None

    direction = "SHORT" if int(time.time()) % 2 == 0 else "LONG"
    conf = 70  # stabil sürüm — ileri filtre sonra

    if conf < CONF_MIN:
        return None

    tp = price * (0.994 if direction == "SHORT" else 1.006)
    sl = price * (1.004 if direction == "SHORT" else 0.996)

    return {
        "symbol": symbol,
        "dir": direction,
        "price": price,
        "tp": tp,
        "sl": sl,
        "conf": conf
    }

# ================== FORMAT ==================
def format_signal(s):
    return (
        f"📌 <b>{s['symbol']}</b> | <b>{s['dir']}</b> | 5m\n"
        f"💵 Giriş: <b>{s['price']:.4f}</b>\n"
        f"🎯 TP: <b>{s['tp']:.4f}</b>\n"
        f"🛑 SL: <b>{s['sl']:.4f}</b>\n"
        f"⚡ Güven: <b>{s['conf']}</b>\n"
        f"🧰 Kaldıraç: <b>{LEVERAGE}</b>"
    )

# ================== ANA DÖNGÜ ==================
def run():
    global last_heartbeat
    print("KriptoAlper scanner started")

    while True:
        now = time.time()

        # ❤️ Hayattayım
        if now - last_heartbeat > HEARTBEAT_INTERVAL:
            send_telegram("🟢 KriptoAlper hayatta")
            last_heartbeat = now

        # 🔍 Sinyal tarama
        for sym in SYMBOLS:
            sig = generate_signal(sym)
            if not sig:
                continue

            key = f"{sym}_{sig['dir']}"
            if sent_cache.get(key):
                continue  # spam engel

            send_telegram(format_signal(sig))
            sent_cache[key] = datetime.utcnow()

        time.sleep(SCAN_INTERVAL)
