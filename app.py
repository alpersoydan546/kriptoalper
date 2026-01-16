import os
from flask import Flask
import scanner

app = Flask(__name__)

# 🔥 SCANNER BURADA BAŞLIYOR (EN KRİTİK SATIR)
scanner.start(
    os.getenv("TELEGRAM_TOKEN"),
    os.getenv("TELEGRAM_CHAT_ID")
)

@app.route("/")
def home():
    return "ok"
