#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Scanner (updated)
- 1m used as booster only (katı mod: 1m tek başına sinyal oluşturmaz)
- Base TFs: 5m / 15m / 1h
- CONF >= 70 filter
- Card-style signal format (Seçenek 1)
- Perf integration (perf.py)
- 3h summary removed (no PERF_SUMMARY)
"""
import os, time, traceback, threading, queue, sqlite3, random
from collections import defaultdict, deque

import numpy as np
import pandas as pd
import requests

from perf import record_signal, evaluate_pending, render_detail_text

BOT_NAME = "KriptoAlper"

# ================== Ayarlar ==================
SEND_TO_TELEGRAM = True
ALIVE_MIN         = 60    # 1 saatte bir 'KriptoAlper Hayatta'
STATUS_MIN        = 60*24 # rarely used (not necessary)
SILENCE_ALERT_MIN = 180   # 3 saat sinyal yoksa uyarı
PERF_DETAIL_MIN   = 60    # 1 saatte bir detaylı liste (son 60 dk)

TOP_N = 30
MIN_24H_USDT_VOL = 2_000_000
COOLDOWN_MIN_PER_SYMBOL = 60
SCAN_DURING_COOLDOWN = True

# TIMEFRAMES: include 1m but it won't act as base signal
TIMEFRAMES = ["1m","5m","15m","1h"]
BASE_SIGNAL_TFS = {"5m","15m","1h"}  # only these can act as base
TF_PRIORITY = ["5m","15m","1h","1m"]  # pick base preferring non-1m

CONF_MIN = 70  # Güven eşiği

REQ_SLEEP_SEC = 0.18
KLINES_CACHE_TTL = 20
MAX_RETRY_429 = 3
BACKOFF_BASE = 0.8

# ================== HTTP / Binance (Futures + Spot fallback) ==================
FAPI_BASES = ["https://fapi.binance.com","https://futures.binance.com"]
SPOT_BASES = ["https://api.binance.com","https://api1.binance.com","https://api2.binance.com"]
_fapi_idx = 0
_session = requests.Session()
_session.headers.update({"User-Agent": f"{BOT_NAME}/stable-v4"})
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

# ================== SQLite cooldown ==================
DB_PATH = "state.db"
_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.execute("CREATE TABLE IF NOT EXISTS cooldown (sym TEXT PRIMARY KEY, ts REAL)")
_db.commit()

def cooldown_ok(sym: str) -> bool:
    row = _db.execute("SELECT ts FROM cooldown WHERE sym=?", (sym,)).fetchone()
    last = 0.0 if not row else float(row[0])
    return (time.time() - last) >= COOLDOWN_MIN_PER_SYMBOL * 60

def mark_sent(sym: str):
    _db.execute("INSERT OR REPLACE INTO cooldown(sym, ts) VALUES (?,?)", (sym, time.time()))
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

# ================== TA helpers ==================
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
def wick_filter_ok(df, lookback=2, wick_body_max=1.25):
    if len(df) < lookback+1: return True
    for i in range(1, lookback+1):
        o = df["open"].iloc[-i]; c = df["close"].iloc[-i]
        h = df["high"].iloc[-i]; l = df["low"].iloc[-i]
        body = abs(c-o); body = body if body!=0 else 1e-9
        wick = (h - max(o,c)) + (min(o,c) - l)
        if (wick/body) > wick_body_max: return False
    return True

# ================== helpers ==================
def _tf_min(tf): return {"1m":1,"3m":3,"5m":5,"15m":15,"30m":30,"1h":60,"4h":240}.get(tf,15)
def _fmt_price(x: float):
    if x >= 100: return f"{x:,.3f}".replace(","," ")
    if x >= 1:  return f"{x:,.5f}".replace(","," ")
    return f"{x:.8f}".rstrip("0").rstrip(".")
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
WICK_BODY_MAX = 1.25
BB_LEN = 20
MIN_RR_BY_TF = {"5m":1.35, "15m":1.45, "1h":1.60}

def confidence_score(rr, slope_abs, rsi_now, tf_bonus):
    base = 55 + min(20, max(0.0, (rr-1.3)*10)) + min(10, slope_abs/0.05) + (4 if 45<=rsi_now<=65 else 0) + tf_bonus
    return int(max(0, min(100, round(base))))

def build_signals_for_tf(df, tf, sym, adj):
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
    long_tr  = (c > base) and (slp >= adj["slope_min"])
    short_tr = (c < base) and (slp <= -adj["slope_min"])
    bars_ago, dir_cross = last_cross_bars(df["ema_fast"], df["ema_slow"])
    recent_cross_ok = (bars_ago is not None) and (bars_ago <= 60)
    wick_ok = wick_filter_ok(df, 2, adj["wick_body_max"])
    r_now = df["rsi"].iloc[-2]
    def push(side, entry, tp, sl, rr):
        out.append({"sym":sym,"tf":tf,"side":side,"entry":entry,"tp":tp,"sl":sl,"rr":rr,"atr":atr_n,"slope":slp,"rsi":r_now})
    if long_tr and recent_cross_ok and dir_cross==1 and wick_ok and (r_now >= adj["rsi_long_min"]):
        entry = c; sl = entry - max(atr_n*adj["atr_sl"], 1e-9*entry); tp = entry + atr_n*adj["atr_tp"]
        rr = (tp-entry)/max((entry-sl),1e-9)
        if rr >= MIN_RR_BY_TF.get(tf,1.35): push("LONG", entry, tp, sl, rr)
    if short_tr and recent_cross_ok and dir_cross==-1 and wick_ok and (r_now <= adj["rsi_short_max"]):
        entry = c; sl = entry + max(atr_n*adj["atr_sl"], 1e-9*entry); tp = entry - atr_n*adj["atr_tp"]
        rr = (entry-tp)/max((sl-entry),1e-9)
        if rr >= MIN_RR_BY_TF.get(tf,1.35): push("SHORT", entry, tp, sl, rr)
    dist_base = abs(c - base) / max(base, 1e-9)
    if (c > bb_up.iloc[-2]) and (slp > 0 or c > base) and (dist_base <= 0.03) and wick_ok:
        entry = c
        sl = min(df["low"].iloc[-3:-1].min(), entry - max(atr_n*ATR_MULT_SL, 1e-9*entry))
        tp = entry + atr_n*ATR_MULT_TP
        rr = (tp-entry)/max((entry-sl),1e-9)
        if rr >= MIN_RR_BY_TF.get(tf,1.35):
            push("LONG", entry, tp, sl, rr)
    if (c < bb_lo.iloc[-2]) and (slp < 0 or c < base) and (dist_base <= 0.03) and wick_ok:
        entry = c
        sl = max(df["high"].iloc[-3:-1].max(), entry + max(atr_n*ATR_MULT_SL, 1e-9*entry))
        tp = entry - atr_n*ATR_MULT_TP
        rr = (entry-tp)/max((sl-entry),1e-9)
        if rr >= MIN_RR_BY_TF.get(tf,1.35):
            push("SHORT", entry, tp, sl, rr)
    return out

def pick_base_signal(signals):
    if not signals: return None
    # prefer base TF in BASE_SIGNAL_TFS via TF_PRIORITY order
    by_priority = sorted(signals, key=lambda s: (TF_PRIORITY.index(s["tf"]) if s["tf"] in TF_PRIORITY else 99, -s["rr"]))
    # ensure base is not 1m if any non-1m exists
    for s in by_priority:
        if s["tf"] in BASE_SIGNAL_TFS:
            return s
    # fallback (shouldn't happen): return best one
    return by_priority[0] if by_priority else None

def merge_signals_same_symbol(symbol_sigs):
    if not symbol_sigs: return []
    out = []
    longs  = [s for s in symbol_sigs if s["side"]=="LONG"]
    shorts = [s for s in symbol_sigs if s["side"]=="SHORT"]
    for group in (longs, shorts):
        if not group: continue
        base = pick_base_signal(group)
        if not base: continue
        tfs = sorted({s["tf"] for s in group}, key=lambda x: TF_PRIORITY.index(x) if x in TF_PRIORITY else 99)
        # ensure base.tf in BASE_SIGNAL_TFS; if base is 1m only, skip (we require base)
        if base["tf"] not in BASE_SIGNAL_TFS:
            # find alternative base
            alt = next((s for s in group if s["tf"] in BASE_SIGNAL_TFS), None)
            if not alt:
                continue
            base = alt
            tfs = sorted({s["tf"] for s in group}, key=lambda x: TF_PRIORITY.index(x) if x in TF_PRIORITY else 99)
        # tf_bonus: count of supporting TFs (1m counts as bonus but doesn't allow base)
        tf_bonus = min(12, 3 + 3*(len(tfs)-1))
        conf = confidence_score(base["rr"], abs(base["slope"]), base["rsi"], tf_bonus)
        base2 = base.copy(); base2["tf_list"] = tfs; base2["conf"] = conf
        out.append(base2)
    return out

# ================== Message format (Seçenek 1 - Kart) ==================
def render_message_card(sym, tf_list, side, entry, tp, sl, rr, conf, est_minutes):
    tf_text = "/".join(tf_list) if isinstance(tf_list, list) else str(tf_list)
    lev = leverage_for_conf(int(conf))
    return (
        f"📌 {sym} · {'LONG' if side=='LONG' else 'SHORT'} [{tf_text}]\n"
        f"💵 Entry: {_fmt_price(entry)}\n"
        f"🎯 TP: {_fmt_price(tp)}\n"
        f"🛑 SL: {_fmt_price(sl)}\n"
        f"⚡ Güven: {int(conf)}"
    )

# ================== Alive & status ==================
_last_alive_ts = 0.0
_last_detail_ts = 0.0

def send_alive():
    send_info("🟢 KriptoAlper Hayatta")

def maybe_alive_and_perf():
    global _last_alive_ts, _last_detail_ts
    now = time.time()
    if now - _last_alive_ts >= ALIVE_MIN * 60:
        send_alive(); _last_alive_ts = now
    if now - _last_detail_ts >= PERF_DETAIL_MIN * 60:
        try:
            send_info(render_detail_text(60))
        except Exception as e:
            print("[PERF DETAIL ERR]", e)
        _last_detail_ts = now

# ================== Counters ==================
scanned_total = 0
scanned_effective = 0
skipped_cooldown = 0
_last_signal_ts = None
_last_no_signal_relax = False

# ================== Main loop ==================
def loop_once():
    global scanned_total, scanned_effective, skipped_cooldown, _last_signal_ts, _universe, _last_universe_ts, _last_no_signal_relax
    if (time.time() - _last_universe_ts) >= 120 or not _universe:
        _universe = refresh_top_futures(TOP_N); _last_universe_ts = time.time()
    if (_last_signal_ts is None) or ((time.time() - _last_signal_ts) >= 90*60):
        _last_no_signal_relax = True
    else:
        _last_no_signal_relax = False
    symbol_bucket = defaultdict(list)
    fetched_ok = 0
    for sym in list(_universe):
        scanned_total += 1
        in_cd = not cooldown_ok(sym)
        if in_cd and not SCAN_DURING_COOLDOWN:
            skipped_cooldown += 1
            continue
        # fetch 1m always (for regime + booster) and other TFs as needed
        df1m = get_klines_cached(sym, "1m", 350)
        if df1m is None or len(df1m) < 60:
            continue
        fetched_ok += 1
        regime = "normal"
        adj = {"slope_min":0.0025, "rsi_long_min":35, "rsi_short_max":65, "wick_body_max":WICK_BODY_MAX, "atr_sl":ATR_MULT_SL, "atr_tp":ATR_MULT_TP}
        # build signals across TFs
        for tf in TIMEFRAMES:
            df = df1m if tf=="1m" else get_klines_cached(sym, tf, 250)
            sigs = build_signals_for_tf(df, tf, sym, adj)
            if not sigs: continue
            # if MTF confirm needed, we already disabled; keep as pass
            valid = sigs
            symbol_bucket[sym].extend(valid)
    if random.random() < 0.02:
        print(f"[DIAG] fetched_ok={fetched_ok}/{len(_universe)}")
    # send merged signals (ensuring base TF is not 1m and conf >= CONF_MIN)
    for sym, arr in symbol_bucket.items():
        merged = merge_signals_same_symbol(arr)
        if not merged: continue
        for ms in merged:
            if int(ms.get("conf",0)) < CONF_MIN:
                continue
            tf_list = ms.get("tf_list", [ms.get("tf")])
            rr = float(ms["rr"])
            msg = render_message_card(sym, tf_list, ms["side"], ms["entry"], ms["tp"], ms["sl"], rr, ms["conf"], 0)
            ok = send_tg_signal_sync(msg)
            if ok:
                print(f"[DELIVERED] {sym} conf={ms['conf']}")
                mark_sent(sym)
                _last_signal_ts = time.time()
                try:
                    record_signal({
                        "sym": sym, "side": ms["side"], "tf_list": tf_list,
                        "entry": ms["entry"], "sl": ms["sl"], "tp": ms["tp"],
                        "rr": rr, "conf": ms["conf"]
                    })
                except Exception as e:
                    print("[PERF REC ERR]", e)
            else:
                print(f"[DROP] {sym} delivery failed")

def main_loop():
    print("KriptoAlper scanner (1m booster mode) started.")
    send_info("🟢 KriptoAlper (scanner) çalışıyor.")
    send_alive()
    while True:
        t0 = time.time()
        try:
            try:
                evaluate_pending(get_klines_cached)
            except Exception as e:
                print("[PERF EVAL ERR]", e)
            loop_once()
            maybe_alive_and_perf()
            if _last_signal_ts and (time.time() - _last_signal_ts) >= SILENCE_ALERT_MIN*60:
                send_info(f"🟡 {SILENCE_ALERT_MIN}+ dk sinyal yok.")
                _last_signal_ts = time.time()
        except Exception as e:
            print("Döngü istisna:", e); traceback.print_exc()
        time.sleep(max(1, 12 - (time.time()-t0)))

def main():
    return main_loop()

if __name__ == "__main__":
    main_loop()
