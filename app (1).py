from flask import Flask
import threading
import scanner

app = Flask(__name__)

@app.route("/")
def index():
    return "OK", 200

def run_scanner():
    try:
        scanner.main()
    except Exception as e:
        print("[APP SCANNER ERR]", repr(e))

if __name__ == "__main__":
    threading.Thread(target=run_scanner, daemon=True).start()
    app.run(host="0.0.0.0", port=10000)
