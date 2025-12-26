import os
import threading
import time
from flask import Flask
from scanner import scan

app = Flask(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def scanner_loop():
    while True:
        try:
            scan(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)
        except Exception as e:
            print("SCAN ERROR:", e)
        time.sleep(60)

@app.route("/")
def home():
    return "ok"

threading.Thread(target=scanner_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
