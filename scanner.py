import os
import time
import requests
from datetime import datetime

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = str(os.getenv("TELEGRAM_CHAT_ID"))

HEARTBEAT_MIN = 30  # 30 dakikada bir hayattayım
CONF_MIN = 70

LAST_HEARTBEAT = 0

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram ENV yok")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg
    }

    try:
        r = requests.post(url, data=payload, timeout=10)
        print("[TG]", r.status_code, r.text)
    except Exception as e:
        print("[TG ERROR]", e)

def heartbeat():
    global LAST_HEARTBEAT
    now = time.time()
    if now - LAST_HEARTBEAT >= HEARTBEAT_MIN * 60:
        send_telegram("🟢 KriptoAlper Hayatta")
        LAST_HEARTBEAT = now

def fake_signal_generator():
    """
    Şimdilik test sinyali.
    Gerçek scanner logic buraya gelecek.
    """
    signal = {
        "symbol": "BTCUSDT",
        "side": "LONG",
        "tf": "5m/15m",
        "entry": 87000,
        "tp": 87600,
        "sl": 86600,
        "confidence": 74,
        "lev": "7x"
    }

    if signal["confidence"] < CONF_MIN:
        return

    msg = (
        f"📌 {signal['symbol']} | {signal['side']} | {signal['tf']}\n"
        f"💵 Giriş: {signal['entry']}\n"
        f"🎯 TP: {signal['tp']}\n"
        f"🛑 SL: {signal['sl']}\n"
        f"⚡ Güven: {signal['confidence']}\n"
        f"🧰 Kaldıraç: {signal['lev']}"
    )

    send_telegram(msg)

def main():
    send_telegram("🚀 KriptoAlper scanner başladı")

    last_signal_time = 0

    while True:
        try:
            heartbeat()

            # 10 dakikada bir test sinyali
            if time.time() - last_signal_time > 600:
                fake_signal_generator()
                last_signal_time = time.time()

            time.sleep(5)

        except Exception as e:
            send_telegram(f"❌ Scanner hata: {e}")
            time.sleep(10)
