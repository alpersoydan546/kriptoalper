from flask import Flask
import threading

app = Flask(__name__)

@app.route("/")
def health():
    return "ok"

def start_scanner():
    import scanner
    scanner.run_scanner()

threading.Thread(target=start_scanner, daemon=True).start()
