from flask import Flask
import threading
from scanner import scanner_loop

app = Flask(__name__)

@app.route("/")
def home():
    return "ok"

def start_scanner():
    t = threading.Thread(target=scanner_loop)
    t.daemon = True
    t.start()

start_scanner()
