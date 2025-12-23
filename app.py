from flask import Flask
import threading
import time
import scanner

app = Flask(__name__)

@app.route("/")
def home():
    return "ok"

def start_scanner():
    time.sleep(3)  # gunicorn tam kalksın
    scanner.run()

if __name__ == "__main__":
    t = threading.Thread(target=start_scanner, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=10000)
