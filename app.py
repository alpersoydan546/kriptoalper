from flask import Flask
import threading
import time

app = Flask(__name__)

def start_scanner():
    import scanner
    scanner.main_loop()

# Scanner thread
t = threading.Thread(target=start_scanner, daemon=True)
t.start()

@app.route("/")
def home():
    return "ok"
