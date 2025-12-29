import os
from flask import Flask
import scanner

app = Flask(__name__)

@app.route("/")
def home():
    return "ok"

if __name__ == "__main__":
    token = os.getenv("TELEGRAM_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    scanner.start(token, chat)
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
