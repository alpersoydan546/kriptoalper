#!/usr/bin/env python3
# scanner.py — KriptoAlper (TOP-50 Futures, high-signal mode, 1h per-symbol cooldown)
import os, time, sys, traceback
from collections import defaultdict, deque
import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

BOT_NAME = "KriptoAlper"
SEND_TO_TELEGRAM = os.getenv("SEND_TO_TELEGRAM", "1") == "1"

# ---------------- CONFIG ----------------
HEARTBEAT_EVERY_MIN = int(os.getenv("HEARTBEAT_MIN", "30"))  # 30 dk heartbeat
HEARTBEAT_FORCE_ON_START = os.getenv("HEARTBEAT_FORCE", "1") == "1"
KLINES_CACHE_TTL = int(os.getenv("KLINES_CACHE_TTL", "20"))
REQ_SLEEP_SEC = float(os.getenv("REQ_SLEEP_SEC", "0.18"))

# universe
TOP_N = int(os.getenv("TOP_N", "50"))  # top 50 futures
USE_FUTURES = True  # use fapi endpoints

# filters (gevşek = daha çok sinyal)
EMA_FAST = 12; EMA_SLOW = 26; EMA_BASE = 200
RSI_LEN = 14
ATR_LEN = 14
ATR_MULT_SL = 1.1
ATR_MULT_TP = 2.5

WICK_BODY_MAX = 0.6
BASE_SLOPE_MIN = 0.01  # daha gevşek trend gereksinimi

MIN_RR_BY_TF = {"1m":1.6,"5m":1.7,"15m":1.8,"1h":1.9,"4h":2.0}
MIN_CONFIDENCE_BY_TF = {"1m":70,"5m":72,"15m":76,"1h":80,"4h":82}

# cooldown: aynı coinden tekrar sinyal gelmesin -> 60 dakika
COOLDOWN_MINUTES_PER_SYMBOL = int(os.getenv("COOLDOWN_MINUTES_PER_SYMBOL", "60"))

# timeframes
TIMEFRAMES = ["1m","5m","15m","1h"]  # agresif + orta vade
ENABLE_SCALP = True; ENABLE_SWING = True

# volume (daha geniş, ama yine de makul)
MIN_24H_USDT_VOL = float(os.getenv("MIN_24H_USDT_VOL", "200000"))  # düşük eşik

# binance endpoints (futures)
FAPI_BASES = [
    "https://fapi.binance.com",
    "https://fapi.binance.us",
]
_spot_idx = 0

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": f"{BOT_NAME}/open-1.1"})

# state
_last_sent = {}         # sym -> ts (cooldown)
perf_rr_last = deque(maxlen=500)

# ---------------- helpers ----------------
def _sleep(s): 
    try: time.sleep(s)
    except: pass

def _fapi_base():
    global _spot_idx
    return FAPI_BASES[_spot_idx % len(FAPI_BASES)]

def http_get(url, params=None, timeout=10):
    global _spot_idx
    for attempt in range(4):
        try:
            r = SESSION.get(url, params=params, timeout=10)
        except Exception as e:
            _sleep(0.5*(attempt+1)); continue
        if r.status_code == 429:
            _sleep(1.0*(attempt+1)); continue
        if r.status_code in (418,451):
            _spot_idx += 1
            _sleep(0.8*(attempt+1)); continue
        return r
    return None

# ---------------- indicators ----------------
def ema(s, n): return s.ewm(span=n, adjust=False).mean()
def rsi(series, n=14):
    ch = series.diff()
    gain = (ch.where(ch>0,0)).ewm(alpha=1/n, adjust=False).mean()
    loss = (-ch.where(ch<0,0)).ewm(alpha=1/n, adjust=False).mean()
    rs = gain/(loss.replace(0,np.nan))
    return (100 - (100/(1+rs))).fillna(50)
def atr(df, n=14):
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl,hc,lc], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()
def slope(series, length=15):
    if len(series) < length+1: return 0.0
    y = series.iloc[-length:].values; x = np.arange(length)
    m = np.polyfit(x, y, 1)[0]
    base = np.mean(np.abs(y)) + 1e-9
    return (m / base) * 100

def last_cross_bars(fast, slow):
    sign = np.sign((fast - slow).values)
    d = np.diff(sign)
    idx = np.where(d != 0)[0]
    if len(idx)==0: return None, None
    bars_ago = len(fast)-1 - idx[-1]
    direction = 1 if (fast.iloc[idx[-1]+1] > slow.iloc[idx[-1]+1]) else -1
    return bars_ago, direction

def wick_filter_ok(df, lookback=2):
    if len(df) < lookback+1: return True
    for i in range(1, lookback+1):
        o = df["open"].iloc[-i]; c = df["close"].iloc[-i]
        h = df["high"].iloc[-i]; l = df["low"].iloc[-i]
        body = abs(c-o); upper = h - max(o,c); lower = min(o,c) - l
        wick = upper + lower; body = body if body!=0 else 1e-9
        if (wick/body) > WICK_BODY_MAX: return False
    return True

# ---------------- klines cache ----------------
_KLINES_CACHE = {}
def get_klines_cached(symbol, interval, limit=250):
    key = (symbol, interval)
    ts_df = _KLINES_CACHE.get(key)
    now = time.time()
    if ts_df and (now - ts_df[0] <= KLINES_CACHE_TTL):
        return ts_df[1]
    _sleep(REQ_SLEEP_SEC)
    base = _fapi_base() if USE_FUTURES else "https://api.binance.com"
    url = f"{base}/fapi/v1/klines" if USE_FUTURES else f"{base}/api/v3/klines"
    r = http_get(url, params={"symbol": symbol, "interval": interval, "limit": limit})
    if not r: return None
    if r.status_code != 200: return None
    arr = r.json()
    cols = ["open_time","open","high","low","close","volume","close_time","qav","nt","tb","tq","i"]
    df = pd.DataFrame(arr, columns=cols)
    for c in ["open","high","low","close","volume"]:
        df[c] = df[c].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    _KLINES_CACHE[key] = (now, df)
    return df

# ---------------- universe: top N futures by quoteVolume ----------------
_VOL_SNAPSHOT = {"ts":0,"map":{}}
VOL_CACHE_TTL = 120

def refresh_top_futures(n=TOP_N):
    global _VOL_SNAPSHOT, _spot_idx
    base = _fapi_base()
    url = f"{base}/fapi/v1/ticker/24hr"
    r = http_get(url, params=None)
    if not r or r.status_code != 200:
        # fallback: use static major list
        return []
    arr = r.json()
    usdt = [it for it in arr if it.get("symbol","").endswith("USDT")]
    usdt_sorted = sorted(usdt, key=lambda x: float(x.get("quoteVolume",0.0)), reverse=True)
    top = [it["symbol"] for it in usdt_sorted if float(it.get("quoteVolume",0.0))>=MIN_24H_USDT_VOL][:n]
    return top

# ---------------- build signal ----------------
def build_signal(df, tf, sym):
    out = []
    if df is None or len(df) < 50: return out
    df = df.copy()
    df["ema_fast"] = ema(df["close"], EMA_FAST)
    df["ema_slow"] = ema(df["close"], EMA_SLOW)
    df["ema_base"] = ema(df["close"], EMA_BASE)
    df["rsi"] = rsi(df["close"], RSI_LEN)
    df["atr"] = atr(df, ATR_LEN)

    c = df["close"].iloc[-2]  # use last closed candle
    atr_now = df["atr"].iloc[-2] if len(df)>ATR_LEN else df["atr"].iloc[-1]
    slope_base = slope(df["ema_base"], 15)
    long_trend = (c > df["ema_base"].iloc[-2]) and (slope_base >= BASE_SLOPE_MIN)
    short_trend = (c < df["ema_base"].iloc[-2]) and (slope_base <= -BASE_SLOPE_MIN)

    bars_ago, dir_cross = last_cross_bars(df["ema_fast"], df["ema_slow"])
    recent_cross_ok = (bars_ago is not None) and (bars_ago <= 20)
    wick_ok = wick_filter_ok(df, 2)

    r_now = df["rsi"].iloc[-2]

    def push(side, entry, tp, sl, rr, conf, est_min):
        min_rr = MIN_RR_BY_TF.get(tf,1.6)
        min_conf = MIN_CONFIDENCE_BY_TF.get(tf, 70)
        if rr >= min_rr and conf >= min_conf:
            out.append({
                "sym":sym,"tf":tf,"side":side,"entry":entry,"tp":tp,"sl":sl,"rr":rr,"conf":int(conf),"est_min":int(est_min)
            })

    # LONG
    if long_trend and recent_cross_ok and dir_cross==1 and wick_ok and r_now >= 35:
        entry = c
        sl = entry - max(atr_now*ATR_MULT_SL, 1e-8*entry)
        tp = entry + atr_now*ATR_MULT_TP
        rr = (tp-entry)/max((entry-sl),1e-9)
        conf = 50 + min(45, (rr-1.0)*12) + min(10, abs(slope_base)*50/1.0)
        est_min = max(5, int((tp-entry)/max(atr_now,1e-9)*_tf_min(tf)))
        push("LONG", entry, tp, sl, rr, conf, est_min)

    # SHORT
    if short_trend and recent_cross_ok and dir_cross==-1 and wick_ok and r_now <= 65:
        entry = c
        sl = entry + max(atr_now*ATR_MULT_SL, 1e-8*entry)
        tp = entry - atr_now*ATR_MULT_TP
        rr = (entry-tp)/max((sl-entry),1e-9)
        conf = 50 + min(45, (rr-1.0)*12) + min(10, abs(slope_base)*50/1.0)
        est_min = max(5, int((entry-tp)/max(atr_now,1e-9)*_tf_min(tf)))
        push("SHORT", entry, tp, sl, rr, conf, est_min)

    if out == []:
        return []
    return out

def _tf_min(tf):
    return {"1m":1,"3m":3,"5m":5,"15m":15,"30m":30,"1h":60,"4h":240}.get(tf,15)

# ---------------- cooldown helpers ----------------
def cooldown_ok(sym):
    now = time.time()
    last = _last_sent.get(sym, 0)
    return (now - last) >= (COOLDOWN_MINUTES_PER_SYMBOL * 60)

def mark_sent(sym):
    _last_sent[sym] = time.time()

# ---------------- telegram ----------------
def send_tg(text):
    token = os.getenv("TELEGRAM_TOKEN",""); chat_id = os.getenv("TELEGRAM_CHAT_ID","")
    if not token or not chat_id:
        print("[TG] TOKEN/CHAT_ID eksik.")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode":"MarkdownV2"}, timeout=10)
        print("[TG SEND]", r.status_code, r.text[:200])
        return r.status_code == 200
    except Exception as e:
        print("[TG EX]", e); return False

def format_msg(s):
    # sade mesaj, güven puanı başta
    return (
        f"🚦 *KriptoAlper Sinyal*\n"
        f"*Güven:* {s['conf']}/100\n"
        f"*Parite:* `{s['sym']}`  *TF:* {s['tf']}\n"
        f"*Yön:* {'LONG' if s['side']=='LONG' else 'SHORT'}\n"
        f"*Giriş:* `{_fmt(s['entry'])}`  *TP:* `{_fmt(s['tp'])}`  *SL:* `{_fmt(s['sl'])}`\n"
        f"*R:R:* `{s['rr']:.2f}`  *Tahmini süre:* ~{s['est_min']} dk\n"
        f"`ID`: {int(time.time())}\n— KriptoAlper"
    )

def _fmt(x):
    if x >= 100: return f"{x:,.3f}".replace(","," ")
    if x >= 1: return f"{x:,.5f}".replace(","," ")
    return f"{x:.8f}".rstrip("0").rstrip(".")

# ---------------- main loop ----------------
def main_loop():
    print("KriptoAlper — TOP50 Futures, agressive-signal mode starting...")
    if HEARTBEAT_FORCE_ON_START:
        send_tg("✅ KriptoAlper açıldı. Heartbeat aktif.")
    last_top_refresh = 0
    symbols = []
    while True:
        try:
            now = time.time()
            # refresh top universe periodically
            if (now - last_top_refresh) > 120:
                top = refresh_top_futures(TOP_N)
                if top:
                    symbols = top
                    print("[UNIVERSE] top fetched:", symbols[:6], "...")
                else:
                    # fallback to common majors if fetch fails
                    symbols = ["BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","MATICUSDT","XRPUSDT","AVAXUSDT","ADAUSDT"] 
                    print("[UNIVERSE] fallback majors used")
                last_top_refresh = now

            for sym in symbols:
                # per-symbol pacing
                _sleep(0.05)
                try:
                    if not cooldown_ok(sym):
                        # skip if in cooldown
                        continue
                    for tf in TIMEFRAMES:
                        df = get_klines_cached(sym, tf, limit=250)
                        if df is None: continue
                        sigs = build_signal(df, tf, sym)
                        if not sigs: continue
                        # send all signals from this symbol (could be multiple tf); mark cooldown once per sym
                        for s in sigs:
                            msg = format_msg(s)
                            ok = send_tg(msg)
                            mark_sent(sym)
                            perf_rr_last.append(float(s["rr"]))
                            print(f"[SENT] {sym} {tf} {s['side']} conf={s['conf']}")
                            # do not spam more than once per symbol (cooldown handles)
                except Exception as e:
                    print("Sym loop hata:", sym, e)
                    traceback.print_exc()

            # heartbeat
            if (time.time() - globals().get("_last_heartbeat_ts",0)) >= HEARTBEAT_EVERY_MIN*60:
                send_tg(f"💓 KriptoAlper — heartbeat {time.strftime('%Y-%m-%d %H:%M:%S')}")
                globals()["_last_heartbeat_ts"] = time.time()

        except Exception as e:
            print("Main loop except:", e)
            traceback.print_exc()
            _sleep(5)
        # small sleep to avoid 100% CPU
        _sleep(1)

if __name__ == "__main__":
    main_loop()
