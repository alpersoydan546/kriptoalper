# scanner_highfreq_reliable.py
# "Frequent but reliable" scanner for Binance (spot-first, no auto-trade)
# Requirements: requests, pandas, numpy, python-dotenv
# ENV required: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
# Optional ENV: GEO_FORCE_POOL (spot|vision|fapi), USE_FAPI_FALLBACK=1, REQ_SLEEP_SEC, KLINES_CACHE_TTL, TOP_SIGNALS_PER_CYCLE

import os, time, traceback, random, math
from collections import defaultdict, deque, Counter

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

# ---------- CONFIG ----------
BOT_NAME = "KriptoAlper"
DEBUG = True
SEND_TO_TELEGRAM = True

REQ_SLEEP_SEC = float(os.getenv("REQ_SLEEP_SEC", "0.18"))   # istek arası baz gecikme
CACHE_TTL = int(os.getenv("KLINES_CACHE_TTL", "12"))      # kısa cache -> sık tarama
TOP_SIGNALS_PER_CYCLE = int(os.getenv("TOP_SIGNALS_PER_CYCLE", "6"))  # her döngüde atılacak max sinyal

# timeframes to check (fast scanning)
TFs = ["1m","3m","5m","15m"]   # sık tarama için kısa TFlar (aynı zamanda 15m ile biraz daha güven)
TF_PRIORITY = {"1m":0,"3m":1,"5m":2,"15m":3}

# coin list (başlangıç: favori 15); istersen burayı 50/100 ile genişlet
COINS = ["BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
         "ADAUSDT","AVAXUSDT","LINKUSDT","DOGEUSDT","TRXUSDT",
         "MATICUSDT","DOTUSDT","ARBUSDT","OPUSDT","RNDRUSDT"]

# indicators params
EMA_FAST = 8
EMA_SLOW = 21
EMA_BASE = 200
RSI_LEN = 14
ATR_LEN = 14
MIN_CONF_SEND = 70   # final gönderim için avg score >= bu değer

# confluence requirements
MIN_TF_CONFLUENCE = 2       # en az kaç farklı TF aynı yönü desteklemeli
VOLUME_SPIKE_MULT = 1.35    # hacim spike eşiği
MIN_AVG_RR = 1.10           # minimum avg RR (konservatif)

# cooldown per symbol to avoid spam (kısa)
SYM_COOLDOWN_SEC = 30 * 60   # aynı sembol için minimal bekleme (30 dk)
GLOBAL_SEND_LIMIT_PER_CYCLE = TOP_SIGNALS_PER_CYCLE

# network / endpoints (spot-first)
SPOT_HOSTS = ["https://api.binance.com","https://api1.binance.com","https://api2.binance.com","https://api3.binance.com","https://api4.binance.com","https://api-gcp.binance.com","https://data-api.binance.vision"]
FAPI_HOSTS = ["https://fapi.binance.com","https://fapi1.binance.com","https://fapi2.binance.com","https://fapi3.binance.com"]
POOL_CURSOR = {"spot":0, "fapi":0}
GEO_FORCE_POOL = os.getenv("GEO_FORCE_POOL","spot").lower()
USE_FAPI_FALLBACK = os.getenv("USE_FAPI_FALLBACK","1") == "1"
MAX_TRIES = int(os.getenv("MAX_TRIES_PER_CALL","4"))

# session
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept":"application/json,text/plain,*/*"
})

# ---------- TELEGRAM ----------
def send_tg(text):
    token = os.getenv("TELEGRAM_TOKEN","").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID","").strip()
    if not token or not chat_id:
        print("[TG] TELEGRAM_TOKEN or CHAT_ID not set"); return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    tries = 0
    while tries < 3:
        try:
            r = SESSION.post(url, json=payload, timeout=8)
            ct = r.headers.get("Content-Type","")
            body = r.text if "json" not in ct else r.json()
            print(f"[TG SEND] code={r.status_code} body={str(body)[:200]}")
            if r.status_code == 200:
                return
            if r.status_code in (400,401,403,404):
                print("[TG FATAL] token/chat_id/permission issue"); return
            time.sleep(0.9 + tries*0.6)
        except Exception as e:
            print("[TG EXC]", repr(e))
            time.sleep(1 + tries*0.6)
        tries += 1

# ---------- UTIL indicators ----------
def ema(series, n):
    return series.ewm(span=n, adjust=False).mean()

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

def slope(series, length=12):
    if len(series) < length+1: return 0.0
    y = series.iloc[-length:].values
    x = np.arange(length)
    m = np.polyfit(x, y, 1)[0]
    base = np.mean(np.abs(y)) + 1e-9
    return (m / base) * 100

# ---------- network: klines with pool & cache ----------
_kl_cache = {}

def _pick_host(pool):
    hosts = SPOT_HOSTS if pool=="spot" else FAPI_HOSTS
    i = POOL_CURSOR[pool] % len(hosts)
    POOL_CURSOR[pool] += 1
    return hosts[i]

def _make_url(pool, host, symbol, interval):
    if pool=="fapi":
        return f"{host}/fapi/v1/klines?symbol={symbol}&interval={interval}&limit=240"
    return f"{host}/api/v3/klines?symbol={symbol}&interval={interval}&limit=240"

def get_klines(symbol, interval="5m"):
    key = (symbol, interval)
    now = time.time()
    hit = _kl_cache.get(key)
    if hit and (now - hit[0]) <= CACHE_TTL:
        return hit[1].copy()
    pools = []
    if GEO_FORCE_POOL in ("spot","vision"):
        pools = ["spot","fapi"] if USE_FAPI_FALLBACK else ["spot"]
    elif GEO_FORCE_POOL=="fapi":
        pools = ["fapi"]
    else:
        pools = ["spot","fapi"]
    last_err = None
    for pool in pools:
        tries = 0
        while tries < MAX_TRIES:
            host = _pick_host(pool)
            url = _make_url(pool, host, symbol, interval)
            try:
                headers = {}
                if pool=="fapi":
                    headers = {"Origin":"https://www.binance.com","Referer":"https://www.binance.com/en/futures"}
                r = SESSION.get(url, timeout=10, allow_redirects=False, headers=headers or None)
                code = r.status_code
                if code in (301,302,303,307,308):
                    tries += 1; time.sleep(REQ_SLEEP_SEC); continue
                if code == 200:
                    ct = r.headers.get("Content-Type","").lower()
                    if "json" not in ct:
                        tries += 1; time.sleep(REQ_SLEEP_SEC); continue
                    arr = r.json()
                    cols = ["open_time","open","high","low","close","volume","ct","qv","trades","tb","tq","ig"]
                    df = pd.DataFrame(arr, columns=cols)
                    for c in ["open","high","low","close","volume"]:
                        df[c] = pd.to_numeric(df[c], errors="coerce")
                    _kl_cache[key] = (now, df)
                    time.sleep(REQ_SLEEP_SEC + random.random()*0.05)
                    return df.copy()
                else:
                    tries += 1
                    time.sleep(REQ_SLEEP_SEC)
            except Exception as e:
                last_err = e
                tries += 1
                time.sleep(REQ_SLEEP_SEC)
    raise RuntimeError("klines failed") from last_err

# ---------- signal math: per-TF micro-signal ----------
def analyze_tf(df, tf):
    # df assumed to be klines for that tf
    res = {}
    close = df["close"]
    vol = df["volume"]
    ema_fast = ema(close, EMA_FAST).iloc[-1]
    ema_slow = ema(close, EMA_SLOW).iloc[-1]
    ema_base = ema(close, EMA_BASE).iloc[-1]
    rsi_now = float(rsi(close, RSI_LEN).iloc[-1])
    atr_now = float(atr(df, ATR_LEN).iloc[-1])
    slope_val = float(slope(ema(close, EMA_BASE), length=12))

    c = float(close.iloc[-1])
    last_vol = float(vol.iloc[-1])
    vol_avg = float(vol.tail(50).mean() if len(vol)>=50 else vol.mean())

    # simple direction signals
    long_ok = (c > ema_base) and (ema_fast > ema_slow) and (slope_val > 0)
    short_ok = (c < ema_base) and (ema_fast < ema_slow) and (slope_val < 0)

    # volume spike
    vol_spike = last_vol >= max(1e-9, vol_avg * VOLUME_SPIKE_MULT)

    # TP/SL rough
    if long_ok:
        entry = c
        tp = c + 1.5 * atr_now
        sl = c - 1.0 * atr_now
    elif short_ok:
        entry = c
        tp = c - 1.5 * atr_now
        sl = c + 1.0 * atr_now
    else:
        entry,tp,sl = c,c,c

    # compute a micro score (0-100)
    score = 50
    if long_ok or short_ok:
        score += 10
    if vol_spike: score += 14
    # rsi neutral bias
    if 40 <= rsi_now <= 60: score += 6
    # slope magnitude
    score += min(20, abs(slope_val)*40)
    # rr
    rr = abs((tp-entry) / max(1e-9, (entry-sl)))
    score += min(20, max(-10, (rr-1.0)*12))

    score = int(max(0, min(100, round(score))))

    res.update({
        "tf": tf,
        "entry": entry, "tp": tp, "sl": sl, "rr": rr,
        "score": score, "side": "LONG" if long_ok and not short_ok else ("SHORT" if short_ok and not long_ok else None),
        "vol_spike": vol_spike, "rsi": rsi_now, "slope": slope_val
    })
    return res

# ---------- aggregate across TFs to produce candidate signals ----------
_last_sent_ts = {}  # (sym) -> ts

def aggregate_signals(sym):
    # collect per-TF analysis
    analyses = {}
    for tf in TFs:
        try:
            df = get_klines(sym, tf)
            analyses[tf] = analyze_tf(df, tf)
        except Exception as e:
            if DEBUG: print(f"[AGG] {sym} {tf} fail {e}")
            continue

    # require at least MIN_TF_CONFLUENCE agreement
    # count directions
    sides = [a["side"] for a in analyses.values() if a.get("side")]
    if not sides: 
        return None

    cnt = Counter(sides)
    candidate_side, cnt_side = cnt.most_common(1)[0]
    if cnt_side < MIN_TF_CONFLUENCE:
        if DEBUG: print(f"[AGG] {sym} confluence fail sides={dict(cnt)}"); return None

    # compute average score among TFs that support candidate_side
    supporting = [a for a in analyses.values() if a.get("side")==candidate_side]
    if len(supporting) < MIN_TF_CONFLUENCE: 
        return None

    avg_score = sum(a["score"] for a in supporting) / len(supporting)
    avg_rr = sum(a["rr"] for a in supporting) / max(1, len(supporting))
    # require volume spike in at least one TF
    vol_ok = any(a["vol_spike"] for a in supporting)

    # finalize only if meets minimums
    if avg_score < MIN_CONF_SEND: 
        if DEBUG: print(f"[AGG] {sym} avg_score {avg_score:.1f} < MIN_CONF_SEND"); return None
    if avg_rr < MIN_AVG_RR:
        if DEBUG: print(f"[AGG] {sym} avg_rr {avg_rr:.2f} < MIN_AVG_RR"); return None
    # require either vol_ok or slope magnitude decent
    if not vol_ok and all(abs(a["slope"])<0.01 for a in supporting):
        if DEBUG: print(f"[AGG] {sym} no vol & weak slope"); return None

    # pick best TF as primary (highest score)
    best = max(supporting, key=lambda x: x["score"])
    # craft candidate
    candidate = {
        "sym": sym,
        "side": candidate_side,
        "entry": best["entry"],
        "tp": best["tp"],
        "sl": best["sl"],
        "avg_score": avg_score,
        "avg_rr": avg_rr,
        "supporting_tfs": [(a["tf"], a["score"]) for a in supporting],
        "vol_ok": vol_ok
    }
    return candidate

# ---------- format message ----------
def _fmt_price(x):
    if x >= 1: return f"{x:,.4f}".replace(","," ")
    return f"{x:.6f}".rstrip("0").rstrip(".")

def format_signal_msg(cand):
    badge = "🟢" if cand["avg_score"]>=85 else ("🟡" if cand["avg_score"]>=75 else "🔴")
    header = f"{'📈' if cand['side']=='LONG' else '📉'} {cand['sym']} {badge} Score:{int(cand['avg_score'])}"
    body = (
        f"{header}\n"
        f"📌 Giriş: {_fmt_price(cand['entry'])}\n"
        f"🎯 TP: {_fmt_price(cand['tp'])}\n"
        f"🛑 SL: {_fmt_price(cand['sl'])}\n"
        f"⚖️ Avg R:R: {cand['avg_rr']:.2f}\n"
        f"🔗 Confluence: {', '.join([f'{t}:{s}' for t,s in cand['supporting_tfs']])}\n"
        f"{'🔥 Hacim spike var' if cand['vol_ok'] else ''}\n"
        f"❗ Öneri: sabit küçük stake (örn. 2 USDT) veya bakiye %1-2. AUTO-TRADE kapalı."
    )
    return body

# ---------- main loop ----------
def main_loop():
    last_cycle = 0
    while True:
        t0 = time.time()
        try:
            candidates = []
            for sym in COINS:
                # skip if recent send for same sym
                last_ts = _last_sent_ts.get(sym, 0)
                if time.time() - last_ts < SYM_COOLDOWN_SEC:
                    if DEBUG: print(f"[SKIP COOLDOWN] {sym}")
                    continue
                cand = aggregate_signals(sym)
                if cand:
                    candidates.append(cand)
            # sort by avg_score and avg_rr
            candidates.sort(key=lambda x: (x["avg_score"], x["avg_rr"]), reverse=True)
            sent = 0
            for c in candidates[:GLOBAL_SEND_LIMIT_PER_CYCLE]:
                msg = format_signal_msg(c)
                send_tg(msg)
                _last_sent_ts[c["sym"]] = time.time()
                sent += 1
                if sent >= GLOBAL_SEND_LIMIT_PER_CYCLE: break

            # heartbeat minimal
            if time.time() - last_cycle > 60*15:
                send_tg(f"✅ Bot aktif — {len(candidates)} aday, {sent} sinyal gönderildi.")
                last_cycle = time.time()

        except Exception as e:
            print("[LOOP ERROR]", repr(e))
            traceback.print_exc()

        # adjust sleep: allow short sleep but not too tight
        dt = time.time() - t0
        time.sleep(max(1.5, 6 - dt))   # döngü yaklaşık 6s baz; get_klines içinde REQ_SLEEP_SEC uyacak

if __name__ == "__main__":
    print("[BOOT] highfreq_reliable scanner starting")
    send_tg("🟢 KriptoAlper (highfreq_reliable) başladı.")
    main_loop()
