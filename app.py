import os
import threading
from flask import Flask
import scanner

app = Flask(__name__)

@app.route("/")
def health():
    return "Bot is Active", 200

# Botu başlatan fonksiyon
def run_bot():
    token = os.getenv("TELEGRAM_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if token and chat:
        scanner.run(token, chat)

# Flask başlar başlamaz botu arka planda zorla başlat
threading.Thread(target=run_bot, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
