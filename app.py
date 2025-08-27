import os, threading, time
from flask import Flask
from scanner import main  # scanner.py içindeki main()

app = Flask(__name__)

@app.route("/")
def health():
    return "ok", 200

def run_scanner():
    while True:
        try:
            print("[APP] scanner thread starting …")
            main()
        except Exception as e:
            print("[APP] scanner crashed:", repr(e))
            time.sleep(5)  # kısa backoff ve tekrar dene

# Render/Gunicorn için worker başlarken tarayıcıyı ayrı thread’de aç
t = threading.Thread(target=run_scanner, daemon=True)
t.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"[APP] Flask running on port {port}")
    app.run(host="0.0.0.0", port=port)
