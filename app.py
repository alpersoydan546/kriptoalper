from flask import Flask
import threading
import scanner

app = Flask(__name__)

@app.route("/")
def health():
    return "ok", 200

def start_bot():
    print("[APP] Scanner starting...")
    scanner.start()

# 🔥 GUNICORN SAFE
threading.Thread(target=start_bot, daemon=True).start()

