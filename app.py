import os
import threading
from flask import Flask
import scanner

app = Flask(__name__)

@app.route("/")
def health():
    return "ok", 200

def start():
    token = os.getenv("TELEGRAM_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    scanner.run(token, chat)

threading.Thread(target=start, daemon=True).start()
