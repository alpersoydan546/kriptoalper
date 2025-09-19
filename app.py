#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, threading
from flask import Flask
from scanner import main  # scanner.py içindeki main() (main_loop köprülenmiş)

app = Flask(__name__)

@app.route("/")
def health():
    return "ok", 200

def run_scanner():
    print("[APP] scanner thread starting …")
    # Sonsuz döngüyü scanner tarafı yönetiyor
    main()

# Render/Gunicorn worker ayağa kalkınca scanner'ı ayrı thread'de başlat
t = threading.Thread(target=run_scanner, daemon=True)
t.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"[APP] Flask running on port {port}")
    app.run(host="0.0.0.0", port=port)
