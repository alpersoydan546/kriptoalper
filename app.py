from flask import Flask
import threading
import scanner

app = Flask(__name__)

@app.route("/")
def home():
    return "ok"

def start_scanner():
    scanner.main()

t = threading.Thread(target=start_scanner)
t.daemon = True
t.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
