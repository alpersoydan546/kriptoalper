from flask import Flask
from scanner import start_scanner

app = Flask(__name__)

@app.route("/")
def home():
    return "ok"

start_scanner()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
