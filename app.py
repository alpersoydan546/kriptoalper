import os
from flask import Flask
import threading
import scanner

app = Flask(__name__)

@app.route("/")
def health():
    return "ok"

def start_scanner():
    scanner.run_scanner()

# Scanner sadece 1 kez başlasın
threading.Thread(target=start_scanner, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
