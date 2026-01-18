import os
import threading
import logging
from flask import Flask
import scanner

# Loglama ayarı (Render panelinde ne olup bittiğini görmek için)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

@app.route("/")
def health():
    return "Bot is running", 200

def start_bot():
    token = os.getenv("TELEGRAM_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    
    if not token or not chat:
        logger.error("HATA: TELEGRAM_TOKEN veya TELEGRAM_CHAT_ID bulunamadı!")
        return

    logger.info("Scanner başlatılıyor...")
    scanner.run(token, chat)

# GLOBAL DEĞİŞKEN: Thread'in sadece 1 kez başlamasını garanti eder
bot_started = False

@app.before_request
def initialize_bot():
    global bot_started
    if not bot_started:
        logger.info("İlk istek geldi, bot thread'i başlatılıyor...")
        thread = threading.Thread(target=start_bot, daemon=True)
        thread.start()
        bot_started = True

# Alternatif olarak gunicorn başlatıldığında direkt çalıştırmak için:
if __name__ != "__main__":
    # Gunicorn ile çalışırken burası tetiklenir
    if not bot_started:
        logger.info("Gunicorn üzerinden bot başlatılıyor...")
        thread = threading.Thread(target=start_bot, daemon=True)
        thread.start()
        bot_started = True

if __name__ == "__main__":
    # Localde (kendi bilgisayarında) test ederken burası çalışır
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
