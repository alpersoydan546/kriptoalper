from flask import Flask
import threading
import time

import scanner  # scanner.py aynı klasörde olmalı

app = Flask(__name__)


def run_scanner():
    try:
        scanner.start_scanner()
        while True:
            time.sleep(60)
    except Exception as e:
        print("Scanner crash:", e)


# Scanner thread
threading.Thread(target=run_scanner, daemon=True).start()


@app.route("/")
def home():
    return "ok", 200
