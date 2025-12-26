from flask import Flask
import threading
import scanner

app = Flask(__name__)

@app.route("/")
def health():
    return "ok"

def run_scanner():
    pass  # scanner zaten import ile çalışıyor

threading.Thread(target=run_scanner, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
