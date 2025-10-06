#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KriptoAlper Scanner — sticky + re-entry + meta heartbeat + predictive filter
- CONF_MIN = 70
- TOP_N = 10 (24h vol >= 20M USDT)
- 1m sadece booster; ana TF: 5m/15m/1h (5m & 15m birlikte ZORUNLU)
- Sıkı eşikler: RR (5m:1.45, 15m:1.55, 1h:1.70), wick 1.05, slope 0.0032, cross<=30 bar
- Sticky (imza tabanlı) tekrar önleme + 30 dk grace
- Re-entry freni: aynı (sym, side) 30 dk içinde küçük farkı atla (ATR ve CONF)
- DB tabanlı heartbeat/sessizlik kontrolü (persist)
- Predictive filter: P(TP) + EV eşiği (Wilson lower bound + conf karışımı)
- Rate limits: 30/saat, 150/gün; circuit breaker; mark price sanity (%2)
- 1h EMA200 trend uyumu; korelasyon freni
- Mesajda “🧰 Önerilen kaldıraç: Nx”
- Gün sonu raporu 00:00'da (perf.py)
"""
import os, time, traceback, threading, queue, sqlite3, math
from collections import defaultdict, deque

import numpy as np
import pandas as pd
import requests

from perf import (
    record_signal,
    evaluate_pending,          # evaluate_pending(get_klines_cached, return_closed_sigkeys=True)
    render_detail_text_daily,
)

BOT_NAME = "KriptoAlper"

# ================== Settings ==================
SEND_TO_TELEGRAM   = True
ALIVE_MIN          = 60    # heartbeat: 1 saatte bir
SILENCE_ALERT_MIN  = 180   # 3 saat sinyal yoksa uyar
IST_OFFSET         = 3 * 3600
_last_daily_sent_key = None

TOP_N = 10
MIN_24H_USDT_VOL = 20_000_000
COOLDOWN_MIN_PER_SYMBOL = 120
SCAN_DURING_COOLDOWN = True

TIMEFRAMES = ["1m","5m","15m","1h"]
BASE_SIGNAL_TFS = {"5m","15m","1h"}
TF_PRIORITY = ["5m","15m","1h","1m"]

CONF_MIN = 70

REQ_SLEEP_SEC     = 0.18
KLINES_CACHE_TTL  = 20
MAX_RETRY_429     = 3
BACKOFF_BASE      = 0.8

# limits
MAX_SIGNALS_PER_HOUR = 30
_last_hour_signals = deque()
DAILY_LIMIT = 150
_today_key = None
_daily_count = 0

# circuit breaker
SENTRY_LOOKBACK = 12
SENTRY_SL_TRIP = 8
SENTRY_COOLDOWN_MIN = 30
_sentry_sl_hits = deque(maxlen=SENTRY_LOOKBACK)
_sentry_pause_until = 0.0

# re-entry guard
REENTRY_MIN_MIN   = 30     # 30 dk
REENTRY_PX_ATR    = 0.6    # entry farkı >= 0.6*ATR değilse küçük fark say
REENTRY_CONF_UP   = 10     # güven +10 yoksa küçük upgrade say

# predictive filter (tahmin)
WIN_LOOKBACK_DAYS = 7      # son 7 gün
WIN_MIN_SIGNALS   = 25     # minimum örnek
PTP_THRESHOLD     = 0.60   # P(TP) alt sınır
EV_MIN_R          = 0.20   # en az +0.2R beklenen değer

# ================== HTTP / Binance ==================
FAPI_BASES = ["https://fapi.binance.com","https://futures.binance.com"]
SPOT_BASES = ["https://api.binance.com","https://api1.binance.com","https://api2.binance.com"]
_fapi_idx = 0
_session = requests.Session()
_session.headers.update({"User-Agent": f"{BOT_NAME}/safer-sticky"})
http_req_count = 0

def _fapi_base():
    return FAPI_BASES[_fapi_idx % len(FAPI_BASES)]

def http_get(url, params=None, timeout=10):
    global _fapi_idx, http_req_count
    for i in range(MAX_RETRY_429 + 1):
        try:
            r = _session.get(url, params=params, timeout=timeout)
            http_req_count += 1
        except Exception:
            time.sleep((i+1)*0.5); continue
        if r.status_code == 429:
            time.sleep((BACKOFF_BASE**i)+0.6); continue
        if r.status_code in (418,451):
            _fapi_idx += 1
            time.sleep((i+1)*0.8); continue
        return r
    return None

def http_get_spot(path, params=None, timeout=10):
    for base in SPOT_BASES:
        try:
            r = _session.get(base + path, params=params, timeout=timeout)
            if r.status_code == 200:
                return r
        except Exception:
            pass
    return None

# ================== DB (cooldown + sticky + meta + re-entry) ==================
DB_PATH = "state.db"
_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.execute("CREATE TABLE IF NOT EXISTS cooldown (sym TEXT PRIMARY KEY, ts REAL)")
_db.execute("""CREATE TABLE IF NOT EXISTS active_signals(
  sig_key TEXT PRIMARY KEY,
  first_ts REAL,
  last_ts REAL,
  conf INT,
  status TEXT
)""")
_db.execute("""CREATE TABLE IF NOT EXISTS meta(
  k TEXT PRIMARY KEY,
  v REAL
)""")
_db.execute("""CREATE TABLE IF NOT EXISTS recent_sends(
  sym TEXT,
  side TEXT,
  ts REAL,
  entry REAL,
  conf INT,
  PRIMARY KEY(sym, side)
)""")
_db.commit()

def cooldown_ok(sym: str) -> bool:
    row = _db.execute("SELECT ts FROM cooldown WHERE sym=?", (sym,)).fetchone()
    last = 0.0 if not row else float(row[0])
    return (time.time() - last) >= COOLDOWN_MIN_PER_SYMBOL * 60

def mark_sent(sym: str):
    _db.execute("INSERT OR REPLACE INTO cooldown(sym, ts) VALUES (?,?)", (sym, time.time()))
    _db.commit()

def meta_get(k, default=0.0):
    row = _db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return float(row[0]) if row else default

def meta_set(k, v):
    _db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES (?,?)", (k, float(v)))
    _db.commit()

# ---------- sticky helpers ----------
def _round_px(x: float) -> float:
    # daha kaba bucket: küçük titreşimler yeni imza üretmesin
    if x >= 100: return round(x, 2)   # 2 ondalık
    if x >= 1:  return round(x, 4)    # 4 ondalık
    return round(x, 7)

def make_sig_key(sym, side, tf_list, entry, tp, sl):
    # 1m sadece booster; imzadan hariç
    base_tfs = sorted([t for t in tf_list if t != "1m"])
    if not base_tfs:
        base_tfs = ["5m","15m"]
    tf_key = "/".join(base_tfs)
    return f"{sym}|{side}|{tf_key}|{_round_px(entry)}|{_round_px(tp)}|{_round_px(sl)}"

STICKY_GRACE_MIN = 30  # aynı imza açıkken 30 dk boyunca tekrar yok

def sticky_allowed(sig_key, new_conf):
    row = _db.execute("SELECT conf,status,last_ts FROM active_signals WHERE sig_key=?", (sig_key,)).fetchone()
    if not row:
        return True
    old_conf, status, last_ts = int(row[0]), row[1], float(row[2]) if row[2] is not None else 0.0
    if status != "OPEN":
        return True
    # 30 dk grace: yalnızca büyük upgrade olursa izin
    if time.time() - last_ts < STICKY_GRACE_MIN * 60:
        return new_conf >= old_conf + 10
    # 30 dk geçtiyse küçük farklara yine izin verme
    return new_conf >= old_conf + 7

def sticky_mark_open(sig_key, conf):
    ts = time.time()
    _db.execute("INSERT OR REPLACE INTO active_signals(sig_key,first_ts,last_ts,conf,status) VALUES (?,?,?,?,?)",
                (sig_key, ts, ts, int(conf), "OPEN"))
    _db.commit()

def sticky_mark_closed(sig_key):
    _db.execute("UPDATE active_signals SET status='CLOSED', last_ts=? WHERE sig_key=?",
                (time.time(), sig_key))
    _db.commit()

# ---------- re-entry guard ----------
def reentry_allowed(sym, side, entry_now, conf_now, atr_val):
    row = _db.execute("SELECT ts, entry, conf FROM recent_sends WHERE sym=? AND side=?", (sym, side)).fetchone()
    if not row:
        return True
    last_ts, last_entry, last_conf = float(row[0]), float(row[1]), int(row[2])
    if time.time() - last_ts >= REENTRY_MIN_MIN*60:
        return True
    big_conf = conf_now >= last_conf + REENTRY_CONF_UP
    big_move = False
    if atr_val and float(atr_val) > 0:
        big_move = abs(float(entry_now) - last_entry) >= (REENTRY_PX_ATR * float(atr_val))
    return big_conf or big_move

def reentry_mark(sym, side, entry_now, conf_now):
    _db.execute("INSERT OR REPLACE INTO recent_sends(sym,side,ts,entry,conf) VALUES (?,?,?,?,?)",
                (sym, side, time.time(), float(entry_now), int(conf_now)))
    _db.commit()

# ================== Telegram ==================
TG_TOKEN = os.getenv("TELEGRAM_TOKEN","")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID","")
tg_queue: "queue.Queue[str]" = queue.Queue()
tg_worker_started = False

def _tg_sender_worker():
    backoff = 1.0
    while True:
        text = tg_queue.get()
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": text},
                timeout=10
            )
            if r.status_code == 429:
                time.sleep(backoff); backoff = min(backoff*1.7, 8.0)
            else:
                backoff = 1.0
            time.sleep(0.9)
        except Exception as e:
            print("[TG-Q EX]", e); time.sleep(backoff); backoff = min(backoff*1.7, 8.0)

def send_info(text: str):
    global tg_worker_started
    if not tg_worker_started:
        threading.Thread(target=_tg_sender_worker, daemon=True).start()
        tg_worker_started = True
    tg_queue.put(text)

def send_tg_signal_sync(text: str, retries: int = 3) -> bool:
    if not SEND_TO_TELEGRAM or not TG_TOKEN or not TG_CHAT:
        print("[SIG] telegram not configured"); return False
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    back = 0.8
    for _ in range(retries):
        try:
            r = requests.post(url, json={"chat_id": TG_CHAT, "text": text}, timeout=12)
            if r.status_code == 200:
                return True
            if r.status_code == 429:
                time.sleep(back); back = min(back*1.8, 8.0)
            else:
                print("[SIG ERR]", r.status_code, r.text[:160]); time.sleep(0.5)
        except Exception as e:
            print("[SIG EX]", e); time.sleep(back); back = min(back*1.8, 8.0)
    return False

# ================== TA ==================
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
def boll_mid(series, n=20):
    ma = series.rolling(n).mean(); std = series.rolling(n).std(ddof=0)
    return ma, ma+2*std, ma-2*std
def last_cross_bars(fast, slow):
    sign = np.sign((fast - slow).values); d = np.diff(sign)
    idx = np.where(d != 0)[0]
    if len(idx)==0: return None, None
    bars_ago = len(fast)-1 - idx[-1]
    direction = 1 if (fast.iloc[idx[-1]+1] > slow.iloc[idx[-1]+1]) else -1
    return bars_ago, direction
def wick_filter_ok(df, lookback=2, wick_body_max=1.05):
    if len(df) < lookback+1: return True
    for i in range(1, lookback+1):
        o = df["open"].iloc[-i]; c = df["close"].iloc[-i]
        h = df["high"].iloc[-i]; l = df["low"].iloc[-i]
        body = abs(c-o); body = body if body!=0 else 1e-9
        wick = (h - max(o,c)) + (min(o,c) - l)
        if (wick/body) > wick_body_max: return False
    return True

# ================== helpers ==================
def _fmt_price(x: float):
    if x >= 100: return f"{x:,.2f}".replace(","," ")
    if x >= 1:  return f"{x:,.4f}".replace(","," ")
    return f"{x:.7f}".rstrip("0").rstrip(".")
def leverage_for_conf(conf: int) -> int:
    return 12 if conf>=90 else 9 if conf>=80 else 7 if conf>=70 else 5 if conf>=60 else 3

# ================== Universe / klines ==================
_universe = []; _last_universe_ts = 0
_KLINES_CACHE = {}  # (sym, tf) -> (ts, df)

def refresh_top_futures(n=TOP_N):
    r = http_get(f"{_fapi_base()}/fapi/v1/ticker/24hr")
    if r and r.status_code == 200:
        arr = r.json()
        usdt = [it for it in arr if it.get("symbol","").endswith("USDT")]
        usdt_sorted = sorted(usdt, key=lambda x: float(x.get("quoteVolume",0.0)), reverse=True)
        top = [it["symbol"] for it in usdt_sorted if float(it.get("quoteVolume",0.0))>=MIN_24H_USDT_VOL][:n]
        if top:
            _universe[:] = top
            return _universe
    r2 = http_get_spot("/api/v3/ticker/24hr")
    if r2 and r2.status_code == 200:
        arr2 = r2.json()
        usdt2 = [it for it in arr2 if it.get("symbol","").endswith("USDT")]
        usdt_sorted2 = sorted(usdt2, key=lambda x: float(x.get("quoteVolume",0.0)), reverse=True)
        top2 = [it["symbol"] for it in usdt_sorted2 if float(it.get("quoteVolume",0.0))>=MIN_24H_USDT_VOL][:n]
        if top2:
            _universe[:] = top2
            return _universe
    if not _universe:
        _universe[:] = ["BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","AVAXUSDT","ADAUSDT","DOGEUSDT"]
    return _universe

def get_spot_klines(symbol, interval, limit=250):
    r = http_get_spot("/api/v3/klines", params={"symbol": symbol.replace("PERP",""), "interval": interval, "limit": limit})
    if not r or r.status_code != 200:
        return None
    arr = r.json()
    cols = ["open_time","open","high","low","close","volume","close_time","qav","nt","tb","tq","i"]
    df = pd.DataFrame(arr, columns=cols)
    for c in ["open","high","low","close","volume"]: df[c] = df[c].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df

def get_klines_cached(symbol, interval, limit=250):
    key = (symbol, interval); now = time.time()
    ts_df = _KLINES_CACHE.get(key)
    if ts_df and (now - ts_df[0] <= KLINES_CACHE_TTL):
        return ts_df[1]
    time.sleep(REQ_SLEEP_SEC)
    r = http_get(f"{_fapi_base()}/fapi/v1/klines", params={"symbol":symbol,"interval":interval,"limit":limit})
    df = None
    if r and r.status_code == 200:
        arr = r.json()
        cols = ["open_time","open","high","low","close","volume","close_time","qav","nt","tb","tq","i"]
        df = pd.DataFrame(arr, columns=cols)
        for c in ["open","high","low","close","volume"]: df[c] = df[c].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    else:
        df = get_spot_klines(symbol, interval, limit)
        if df is not None:
            print(f"[FALLBACK] {symbol} {interval} → SPOT klines")
    if df is None: return None
    _KLINES_CACHE[key] = (now, df)
    return df

# ================== Signal construction ==================
EMA_FAST, EMA_SLOW, EMA_BASE = 12, 26, 200
RSI_LEN = 14
ATR_LEN = 14
ATR_MULT_SL, ATR_MULT_TP = 1.00, 2.20
WICK_BODY_MAX = 1.05
BB_LEN = 20
MIN_RR_BY_TF = {"5m":1.45, "15m":1.55, "1h":1.70}
BASE_SLOPE_MIN = 0.0032
MAX_CROSS_BARS = 30

def confidence_score(rr, slope_abs, rsi_now, tf_bonus):
    base = 55 + min(20, max(0.0, (rr-1.3)*10)) + min(10, slope_abs/0.05) + (4 if 45<=rsi_now<=65 else 0) + tf_bonus
    return int(max(0, min(100, round(base))))

def build_signals_for_tf(df, tf, sym):
    out = []
    if df is None or len(df) < 60: return out
    df = df.copy()
    df["ema_fast"] = ema(df["close"], EMA_FAST)
    df["ema_slow"] = ema(df["close"], EMA_SLOW)
    df["ema_base"] = ema(df["close"], EMA_BASE)
    df["rsi"] = rsi(df["close"], RSI_LEN)
    df["atr"] = atr(df, ATR_LEN)
    bb_mid, bb_up, bb_lo = boll_mid(df["close"], BB_LEN)

    c = df["close"].iloc[-2]
    atr_n = df["atr"].iloc[-2]
    base = df["ema_base"].iloc[-2]
    slp  = slope(df["ema_base"], 15)

    long_tr  = (c > base) and (slp >= BASE_SLOPE_MIN)
    short_tr = (c < base) and (slp <= -BASE_SLOPE_MIN)

    bars_ago, dir_cross = last_cross_bars(df["ema_fast"], df["ema_slow"])
    recent_cross_ok = (bars_ago is not None) and (bars_ago <= MAX_CROSS_BARS)
    wick_ok = wick_filter_ok(df, 2, WICK_BODY_MAX)
    r_now = df["rsi"].iloc[-2]

    def push(side, entry, tp, sl, rr):
        out.append({"sym":sym,"tf":tf,"side":side,"entry":entry,"tp":tp,"sl":sl,
                    "rr":rr,"atr":atr_n,"slope":slp,"rsi":r_now})

    # Trend + EMA cross
    if long_tr and recent_cross_ok and dir_cross==1 and wick_ok and (r_now >= 35):
        entry = c; sl = entry - max(atr_n*ATR_MULT_SL, 1e-9*entry); tp = entry + atr_n*ATR_MULT_TP
        rr = (tp-entry)/max((entry-sl),1e-9)
        if rr >= MIN_RR_BY_TF.get(tf,1.45): push("LONG", entry, tp, sl, rr)

    if short_tr and recent_cross_ok and dir_cross==-1 and wick_ok and (r_now <= 65):
        entry = c; sl = entry + max(atr_n*ATR_MULT_SL, 1e-9*entry); tp = entry - atr_n*ATR_MULT_TP
        rr = (entry-tp)/max((sl-entry),1e-9)
        if rr >= MIN_RR_BY_TF.get(tf,1.45): push("SHORT", entry, tp, sl, rr)

    # Breakout
    dist_base = abs(c - base) / max(base, 1e-9)
    if (c > bb_up.iloc[-2]) and (slp > 0 or c > base) and (dist_base <= 0.025) and wick_ok:
        entry = c
        sl = min(df["low"].iloc[-3:-1].min(), entry - max(atr_n*ATR_MULT_SL, 1e-9*entry))
        tp = entry + atr_n*ATR_MULT_TP
        rr = (tp-entry)/max((entry-sl),1e-9)
        if rr >= MIN_RR_BY_TF.get(tf,1.45): push("LONG", entry, tp, sl, rr)

    if (c < bb_lo.iloc[-2]) and (slp < 0 or c < base) and (dist_base <= 0.025) and wick_ok:
        entry = c
        sl = max(df["high"].iloc[-3:-1].max(), entry + max(atr_n*ATR_MULT_SL, 1e-9*entry))
        tp = entry - atr_n*ATR_MULT_TP
        rr = (entry-tp)/max((sl-entry),1e-9)
        if rr >= MIN_RR_BY_TF.get(tf,1.45): push("SHORT", entry, tp, sl, rr)

    return out

def pick_base_signal(signals):
    if not signals: return None
    by_priority = sorted(signals, key=lambda s: (TF_PRIORITY.index(s["tf"]) if s["tf"] in TF_PRIORITY else 99, -s["rr"]))
    for s in by_priority:
        if s["tf"] in BASE_SIGNAL_TFS:
            return s
    return None

def merge_signals_same_symbol(symbol_sigs):
    if not symbol_sigs: return []
    out = []
    for side in ("LONG","SHORT"):
        group = [s for s in symbol_sigs if s["side"]==side]
        if not group: continue
        base = pick_base_signal(group)
        if not base:
            continue  # 1m tek başına ise at
        tfs = sorted({s["tf"] for s in group}, key=lambda x: TF_PRIORITY.index(x) if x in TF_PRIORITY else 99)

        # ZORUNLU: 5m ve 15m birlikte onay
        if not (("5m" in tfs) and ("15m" in tfs)):
            continue

        tf_bonus = min(12, 3 + 3*(len(tfs)-1))  # 1m bonus sayılır
        conf = confidence_score(base["rr"], abs(base["slope"]), base["rsi"], tf_bonus)
        base2 = base.copy(); base2["tf_list"] = tfs; base2["conf"] = conf
        out.append(base2)
    return out

# ================== predictive helpers (P(TP) & EV) ==================
def _ist_range_days(days: int):
    now = time.time()
    start = now - days*86400
    return start, now

def wilson_lower_bound(success, total, z=1.2816):  # ~%80 güven
    if total <= 0: return 0.0
    phat = success / total
    denom = 1 + z*z/total
    centre = phat + z*z/(2*total)
    margin = z * math.sqrt((phat*(1-phat) + z*z/(4*total)) / total)
    return max(0.0, (centre - margin) / denom)

def recent_winrate(sym: str, side: str, tf_key: str):
    start_ts, end_ts = _ist_range_days(WIN_LOOKBACK_DAYS)
    # toplam
    row = _db.execute("""
        SELECT COUNT(*) FROM signals
        WHERE ts BETWEEN ? AND ? AND sym=? AND side=? AND tf LIKE ?
    """, (start_ts, end_ts, sym, side, f"%{tf_key}%")).fetchone()
    total = int(row[0]) if row else 0
    if total == 0:
        return 0.0, 0
    # TP sayısı
    row2 = _db.execute("""
        SELECT COUNT(*) FROM signals
        WHERE ts BETWEEN ? AND ? AND sym=? AND side=? AND tf LIKE ? AND status='TP'
    """, (start_ts, end_ts, sym, side, f"%{tf_key}%")).fetchone()
    tp_cnt = int(row2[0]) if row2 else 0
    wr_wlb = wilson_lower_bound(tp_cnt, total, z=1.2816)
    return wr_wlb, total

def ptp_estimate(sym, side, tf_list, conf_int):
    base_tfs = sorted([t for t in tf_list if t != "1m"])
    tf_key = "/".join(base_tfs) if base_tfs else "5m/15m"
    wr_wlb, n = recent_winrate(sym, side, tf_key)
    alpha = 0.6 if n >= WIN_MIN_SIGNALS else 0.3
    ptp = alpha*wr_wlb + (1-alpha)*(conf_int/100.0)
    return ptp, wr_wlb, n

# ================== Message format ==================
def _fmt_price(x: float):
    if x >= 100: return f"{x:,.2f}".replace(","," ")
    if x >= 1:  return f"{x:,.4f}".replace(","," ")
    return f"{x:.7f}".rstrip("0").rstrip(".")
def leverage_for_conf(conf: int) -> int:
    return 12 if conf>=90 else 9 if conf>=80 else 7 if conf>=70 else 5 if conf>=60 else 3

def render_message_card(sym, tf_list, side, entry, tp, sl, rr, conf):
    tf_text = "/".join(tf_list) if isinstance(tf_list, list) else str(tf_list)
    lev = leverage_for_conf(int(conf))
    return (
        f"📌 {sym} · {'LONG' if side=='LONG' else 'SHORT'} [{tf_text}]\n"
        f"💵 Entry: {_fmt_price(entry)}\n"
        f"🎯 TP: {_fmt_price(tp)}\n"
        f"🛑 SL: {_fmt_price(sl)}\n"
        f"⚡ Güven: {int(conf)}\n"
        f"🧰 Önerilen kaldıraç: {lev}x"
    )

# ================== Guards ==================
def _hour_quota_ok():
    now = time.time()
    while _last_hour_signals and now - _last_hour_signals[0] > 3600:
        _last_hour_signals.popleft()
    return len(_last_hour_signals) < MAX_SIGNALS_PER_HOUR
def _hour_quota_mark(): _last_hour_signals.append(time.time())

def _reset_daily_if_needed():
    global _today_key, _daily_count
    t = time.gmtime(time.time()+IST_OFFSET)
    key = f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"
    if key != _today_key:
        _today_key = key; _daily_count = 0
def _daily_ok(): _reset_daily_if_needed(); return _daily_count < DAILY_LIMIT
def _daily_mark(): globals()['_daily_count'] += 1

def _sentry_update(last_status):
    global _sentry_pause_until
    _sentry_sl_hits.append(1 if last_status=="SL" else 0)
    if sum(_sentry_sl_hits) >= SENTRY_SL_TRIP:
        _sentry_pause_until = time.time() + SENTRY_COOLDOWN_MIN*60
        _sentry_sl_hits.clear()
def _sentry_active(): return time.time() < _sentry_pause_until

# ================== Mark price sanity ==================
def get_mark_price(symbol):
    r = http_get(f"{_fapi_base()}/fapi/v1/premiumIndex", params={"symbol":symbol})
    if r and r.status_code==200:
        try: return float(r.json().get("markPrice"))
        except: return None
    return None

# ================== Alive & Daily report ==================
_last_alive_ts = 0.0
_last_signal_ts = None

def send_alive():
    # DB tabanlı throttle (restart olsa bile saatte 1)
    last = meta_get("last_alive_ts", 0.0)
    if time.time() - last < ALIVE_MIN*60:
        return
    send_info("🟢 KriptoAlper Hayatta")
    meta_set("last_alive_ts", time.time())

def istanbul_day_key(ts=None):
    if ts is None: ts = time.time()
    loc = ts + IST_OFFSET
    t = time.gmtime(loc)
    return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"

def maybe_send_daily_report():
    global _last_daily_sent_key
    now = time.time()
    key_now = istanbul_day_key(now)
    loc = now + IST_OFFSET
    t = time.gmtime(loc)
    if t.tm_hour == 0 and t.tm_min < 5:
        yesterday_ts = now - 86400
        if _last_daily_sent_key != key_now:
            try:
                text = render_detail_text_daily(yesterday_ts)
                send_info(text)
            except Exception as e:
                print("[DAILY ERR]", e)
            _last_daily_sent_key = key_now

# ================== Main ==================
def loop_once():
    global _last_signal_ts, _universe, _last_universe_ts
    if _sentry_active():
        return  # breaker aktifken üretme

    if (time.time() - _last_universe_ts) >= 120 or not _universe:
        _universe = refresh_top_futures(TOP_N); _last_universe_ts = time.time()

    symbol_bucket = defaultdict(list)

    for sym in list(_universe):
        in_cd = not cooldown_ok(sym)
        if in_cd and not SCAN_DURING_COOLDOWN:
            continue

        df1m = get_klines_cached(sym, "1m", 350)
        if df1m is None or len(df1m) < 60:
            continue

        for tf in TIMEFRAMES:
            df = df1m if tf=="1m" else get_klines_cached(sym, tf, 250)
            sigs = build_signals_for_tf(df, tf, sym)
            if not sigs: continue
            symbol_bucket[sym].extend(sigs)

    for sym, arr in symbol_bucket.items():
        merged = merge_signals_same_symbol(arr)
        if not merged: continue

        # Korelasyon freni: aynı anda aynı yönde en yüksek 10'u seç
        by_side = defaultdict(list)
        for ms in merged: by_side[ms["side"]].append(ms)

        for side, arr_side in by_side.items():
            arr_side.sort(key=lambda x: int(x["conf"]), reverse=True)
            keep = arr_side[:10]
            for ms in keep:
                if int(ms.get("conf",0)) < CONF_MIN:
                    continue

                # 1h trend uyumu (EMA200)
                dfh = get_klines_cached(sym, "1h", 120)
                if dfh is not None and len(dfh)>50:
                    ema200 = ema(dfh["close"], 200).iloc[-2]
                    c1h = dfh["close"].iloc[-2]
                    if (ms["side"]=="LONG" and c1h < ema200) or (ms["side"]=="SHORT" and c1h > ema200):
                        continue

                # Mark price sanity (%2)
                mk = get_mark_price(sym)
                if mk:
                    px = float(ms["entry"])
                    if abs(px - mk)/mk > 0.02:
                        continue

                # Sticky signature (duplicate önleme) — 1m imzadan hariç
                tf_list = ms.get("tf_list",[ms["tf"]])
                sig_key = make_sig_key(sym, ms["side"], tf_list, ms["entry"], ms["tp"], ms["sl"])
                if not sticky_allowed(sig_key, int(ms["conf"])):
                    continue

                # Re-entry guard (aynı sym+side 30 dk içinde önemsiz değişiklik)
                if not reentry_allowed(sym, ms["side"], float(ms["entry"]), int(ms["conf"]), ms.get("atr")):
                    continue

                # Rate limits
                if not _hour_quota_ok() or not _daily_ok():
                    continue

                # === Predictive filter: P(TP) & EV ===
                # TF anahtarı (1m hariç)
                base_tfs = sorted([t for t in tf_list if t != "1m"])
                tf_key = "/".join(base_tfs) if base_tfs else "5m/15m"
                ptp, wr_wlb, n_samp = ptp_estimate(sym, ms["side"], tf_list, int(ms["conf"]))
                risk = abs(float(ms["entry"]) - float(ms["sl"]))
                reward = abs(float(ms["tp"]) - float(ms["entry"]))
                R = (reward / max(risk, 1e-9))
                EV = ptp*R - (1 - ptp)*1.0
                if not (ptp >= PTP_THRESHOLD and EV >= EV_MIN_R):
                    # print(f"[FILTERED] {sym} {ms['side']} tf={tf_key} ptp={ptp:.2f} wr*={wr_wlb:.2f} n={n_samp} EV={EV:.2f}")
                    continue

                msg = render_message_card(sym, tf_list, ms["side"], ms["entry"], ms["tp"], ms["sl"], float(ms["rr"]), ms["conf"])
                if send_tg_signal_sync(msg):
                    reentry_mark(sym, ms["side"], float(ms["entry"]), int(ms["conf"]))
                    sticky_mark_open(sig_key, int(ms["conf"]))
                    mark_sent(sym)
                    _hour_quota_mark(); _daily_mark()
                    _last_signal_ts = time.time()
                    meta_set("last_signal_ts", _last_signal_ts)
                    try:
                        record_signal({
                            "sym": sym, "side": ms["side"], "tf_list": tf_list,
                            "entry": ms["entry"], "sl": ms["sl"], "tp": ms["tp"],
                            "rr": float(ms["rr"]), "conf": int(ms["conf"])
                        })
                    except Exception as e:
                        print("[PERF REC ERR]", e)

def main_loop():
    global _last_signal_ts  # UnboundLocalError fix
    print("KriptoAlper scanner (sticky+reentry+predictive) started.")
    send_info("🟢 KriptoAlper çalışıyor.")
    send_alive()
    while True:
        t0 = time.time()
        try:
            # Performans değerlendirme + breaker besleme + sticky close
            try:
                tp_c, sl_c, amb_c, exp_c, closed_sigkeys = evaluate_pending(get_klines_cached, return_closed_sigkeys=True)
                for sk in closed_sigkeys:
                    if sk: sticky_mark_closed(sk)
                for _ in range(sl_c): _sentry_update("SL")
                for _ in range(tp_c): _sentry_update("TP")
                for _ in range(amb_c): _sentry_update("AMB")
                for _ in range(exp_c): _sentry_update("EXPIRED")
            except Exception as e:
                print("[PERF EVAL ERR]", e)

            loop_once()

            # heartbeat (DB throttle)
            send_alive()

            # sessizlik uyarısı (DB kontrollü)
            last_sig = meta_get("last_signal_ts", 0.0)
            if last_sig and (time.time() - last_sig) >= SILENCE_ALERT_MIN*60:
                send_info(f"🟡 {SILENCE_ALERT_MIN}+ dk sinyal yok.")
                meta_set("last_signal_ts", time.time())

            # günlük rapor
            maybe_send_daily_report()

        except Exception as e:
            print("Döngü istisna:", e); traceback.print_exc()
        time.sleep(max(1, 12 - (time.time()-t0)))

def main():
    return main_loop()

if __name__ == "__main__":
    main_loop()
