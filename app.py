# app.py — scanner'ı garanti başlatan sade sürüm
from flask import Flask
import threading, os, time
import importlib

app = Flask(__name__)

_started = False
_lock = threading.Lock()

def ensure_scanner():
    """scanner.main()'i 1 kez başlatır ve loga build yazar."""
    global _started
    with _lock:
        if _started:
            return
        # scanner'ı import et
        scanner = importlib.import_module("scanner")
        try:
            ver = getattr(scanner, "VERSION", "unknown")
        except Exception:
            ver = "unknown"
        print(f"[APP] launching scanner… build={ver}")
        t = threading.Thread(target=scanner.main, daemon=True)
        t.start()
        _started = True

@app.route("/")
def index():
    # İlk istek geldiğinde de garantile
    ensure_scanner()
    return "OK", 200

@app.route("/start")
def start_now():
    # Elle tetiklemek istersen
    ensure_scanner()
    return "started", 200

if __name__ == "__main__":
    # Lokal koşumda da çalışsın
    ensure_scanner()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
