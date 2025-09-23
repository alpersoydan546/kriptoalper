#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KriptoAlper — Futures TOP-30 • High-Signal • TF Merge • SQLite Cooldown • Auto-Recovery
- Saatlik '🟢 KriptoAlper Hayatta'
- 3 saatte bir minimal 'Durum' özeti
- Mesajlarda id/probe YOK (sade & emojili)
- Binance Futures USDT-M verisi
- Kalıcı cooldown (SQLite): aynı sembol 60 dk içinde tekrar sinyal atmaz
- TF birleştirme: 1m/5m/15m/1h sinyalleri tek kartta
- Rejim uyarlaması + 90 dk sinyal yoksa otomatik RELAX
- Telegram gönderim: SENKRON + teslim kontrolü
- app.py uyumu: from scanner import main
"""
import os, time, traceback, threading, queue, sqlite3, random
from collections import defaultdict, deque

import numpy as np
import pandas as pd
import requests

BOT_NAME = "KriptoAlper"

# ================== Ayarlar ==================
SEND_TO_TELEGRAM = True

# Alive/Status periyotları
ALIVE_MIN  = 60      # 1 saatte bir 'KriptoAlper Hayatta'
STATUS_MIN = 180     # 3 saatte bir Durum özeti
SILENCE_ALERT_MIN = 180  # 3 saat sinyal yoksa sessizlik uyarısı

TOP_N = 30
MIN_24H_USDT_VOL = 2_000_000  # evrene dahil minimum 24h quoteVolume (USDT)
COOLDOWN_MIN_PER_SYMBOL = 60  # aynı sembole 60 dk cooldown
SCAN_DURING_COOLDOWN = True   # cooldown'da analiz yap, sadece gönderimi engelle

TIMEFRAMES = ["1m", "5m", "15m", "1h"]

REQ_SLEEP_SEC = 0.18
KLINES_CACHE_TTL = 20
MAX_RETRY_429 = 3
BACKOFF_BASE = 0.8

MTF_CONFIRM_ENABLE = False
MTF_RELAX_MAP = {"1m":["5m","15m"], "5m":["15m","1h"], "15m":["1h","4h"], "1h":["4h"]}
TF_PRIORITY = ["5m","15m","1m","1h"]

# ================== Eşikler (NORMAL) ==================
EMA_FAST, EMA_SLOW, EMA_BASE = 12, 26, 200
BASE_SLOPE_MIN = 0.004
RSI_LEN = 14
RSI_LONG_MIN, RSI_SHORT_MAX = 38, 62
ATR_LEN = 14
ATR_MULT_SL, ATR_MULT_TP = 1.10, 2.40
WICK_BODY_MAX = 1.05
BB_LEN = 20
MIN_RR_BY_TF = {"1m":1.35, "5m":1.45, "15m":1.55, "1h":1.75}

# ================== HTTP / Binance ==================
FAPI_BASES = ["https://fapi.binance.com", "https://fapi.binance.us"]
_fapi_idx = 0
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": f"{BOT_NAME}/stable-v3"})
http_429_count = 0
http_req_count = 0

def _fapi_base():
    return FAPI_BASES[_fapi_idx % len(FAPI_BASES)]

def http_get(url, params=None, timeout=10):
    global _fapi_idx, http_429_count, http_req_count
    for i in range(MAX_RETRY_429 + 1):
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            http_req_count += 1
        except Exception:
            time.sleep((i+1)*0.5); continue
        if r.status_code == 429:
            http_429_count += 1
            time.sleep((BACKOFF_BASE**i)+0.6); continue
        if r.status_code in (418,451):
            _fapi_idx += 1
            time.sleep((i+1)*0.8); continue
        return r
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
            time.sleep(1.1)
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

# ================== TA / İndikatörler ==================
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
        body = abs(c-o); upper = h - max(o,c); lower = min(o,c) - l
        wick = upper + lower; body = body if body!=0 else 1e-9
        if (wick/body) > wick_body_max: return False
    return True

# ================== Yardımcılar ==================
def _tf_min(tf): return {"1m":1,"3m":3,"5m":5,"15m":15,"30m":30,"1h":60,"4h":240}.get(tf,15)

def _fmt_price(x: float):
    if x >= 100: return f"{x:,.3f}".replace(","," ")
    if x >= 1:  return f"{x:,.5f}".replace(","," ")
    return f"{x:.8f}".rstrip("0").rstrip(".")

def leverage_for_conf(conf: int) -> int:
    return 12 if conf>=90 else 9 if conf>=80 else 7 if conf>=70 else 5 if conf>=60 else 3

def est_minutes_to_tp(tf, atr_val, distance):
    if atr_val <= 0: return _tf_min(tf)
    bars = max(1.0, distance/atr_val) * 1.05
    return int(round(bars * _tf_min(tf)))

# ================== Rejim uyarlaması ==================
def detect_regime(df_close: pd.Series) -> str:
    pct = df_close.pct_change()
    vol = pct.rolling(240).std(ddof=0).iloc[-1]  # ~4h vol (1m için)
    if pd.isna(vol): return "normal"
    if vol > 0.06: return "high"
    if vol < 0.02: return "low"
    return "normal"

def regime_adjustments(regime: str, relax: bool):
    slope_min = BASE_SLOPE_MIN
    rsi_l, rsi_s = RSI_LONG_MIN, RSI_SHORT_MAX
    wick_max = WICK_BODY_MAX
    atr_sl, atr_tp = ATR_MULT_SL, ATR_MULT_TP
    if regime == "high":
        slope_min *= 1.2; rsi_l = max(35, rsi_l-2); rsi_s = min(65, rsi_s+2)
        wick_max = min(0.8, wick_max); atr_sl *= 1.15; atr_tp *= 1.15
    elif regime == "low":
        slope_min = max(0.003, slope_min*0.8)
        wick_max = max(1.2, wick_max); atr_tp *= 0.9
    if relax:
        slope_min = max(0.003, slope_min*0.85)
        rsi_l = min(rsi_l, 36); rsi_s = max(rsi_s, 64)
        wick_max = max(wick_max, 1.2)
    return dict(slope_min=slope_min, rsi_long_min=rsi_l, rsi_short_max=rsi_s,
                wick_body_max=wick_max, atr_sl=atr_sl, atr_tp=atr_tp)

# ================== Evren / cache ==================
_universe = []; _last_universe_ts = 0
_KLINES_CACHE = {}  # (sym, tf) -> (ts, df)

def refresh_top_futures(n=TOP_N):
    r = http_get(f"{_fapi_base()}/fapi/v1/ticker/24hr")
    if not r or r.status_code != 200:
        if not _universe:
            _universe[:] = ["BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","AVAXUSDT","ADAUSDT","DOGEUSDT"]
        return _universe
    arr = r.json()
    usdt = [it for it in arr if it.get("symbol","").endswith("USDT")]
    usdt_sorted = sorted(usdt, key=lambda x: float(x.get("quoteVolume",0.0)), reverse=True)
    top = [it["symbol"] for it in usdt_sorted if float(it.get("quoteVolume",0.0))>=MIN_24H_USDT_VOL][:n]
    _universe[:] = top if top else _universe
    return _universe

def get_klines_cached(symbol, interval, limit=250):
    key = (symbol, interval); now = time.time()
    ts_df = _KLINES_CACHE.get(key)
    if ts_df and (now - ts_df[0] <= KLINES_CACHE_TTL):
        return ts_df[1]
    time.sleep(REQ_SLEEP_SEC)
    r = http_get(f"{_fapi_base()}/fapi/v1/klines", params={"symbol":symbol,"interval":interval,"limit":limit})
    if not r or r.status_code != 200: return None
    arr = r.json()
    cols = ["open_time","open","high","low","close","volume","close_time","qav","nt","tb","tq","i"]
    df = pd.DataFrame(arr, columns=cols)
    for c in ["open","high","low","close","volume"]: df[c] = df[c].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    _KLINES_CACHE[key] = (now, df)
    return df

# ================== Skor & sinyal inşası ==================
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
    recent_cross_ok = (bars_ago is not None) and (bars_ago <= 30)
    wick_ok = wick_filter_ok(df, 2, adj["wick_body_max"])
    r_now = df["rsi"].iloc[-2]

    def push(side, entry, tp, sl, rr):
        out.append({"sym":sym,"tf":tf,"side":side,"entry":entry,"tp":tp,"sl":sl,
                    "rr":rr,"atr":atr_n,"slope":slp,"rsi":r_now})

    if long_tr and recent_cross_ok and dir_cross==1 and wick_ok and (r_now >= adj["rsi_long_min"]):
        entry = c; sl = entry - max(atr_n*adj["atr_sl"], 1e-9*entry); tp = entry + atr_n*adj["atr_tp"]
        rr = (tp-entry)/max((entry-sl),1e-9); push("LONG", entry, tp, sl, rr)

    if short_tr and recent_cross_ok and dir_cross==-1 and wick_ok and (r_now <= adj["rsi_short_max"]):
        entry = c; sl = entry + max(atr_n*adj["atr_sl"], 1e-9*entry); tp = entry - atr_n*adj["atr_tp"]
        rr = (entry-tp)/max((sl-entry),1e-9); push("SHORT", entry, tp, sl, rr)

    return out

def mtf_confirm_if_enabled(sym, tf, side):
    if not MTF_CONFIRM_ENABLE: return True
    req = MTF_RELAX_MAP.get(tf, [])
    if not req: return True
    need = max(1, len(req)-1); ok = 0
    for htf in req:
        try:
            dfh = get_klines_cached(sym, htf, 210)
            price = dfh["close"].iloc[-1]
            ema_b = ema(dfh["close"], EMA_BASE).iloc[-1]
            s_b   = slope(ema(dfh["close"], EMA_BASE), 15)
            if side=="LONG" and (price>ema_b and s_b>=BASE_SLOPE_MIN): ok += 1
            if side=="SHORT" and (price<ema_b and s_b<=-BASE_SLOPE_MIN): ok += 1
        except: pass
    return ok >= need

def pick_base_signal(signals):
    if not signals: return None
    return sorted(signals, key=lambda s: (TF_PRIORITY.index(s["tf"]) if s["tf"] in TF_PRIORITY else 99, -s["rr"]))[0]

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
        tf_bonus = min(8, 3 + 2*(len(tfs)-1))
        conf = confidence_score(base["rr"], abs(base["slope"]), base["rsi"], tf_bonus)
        base2 = base.copy(); base2["tf_list"] = tfs; base2["conf"] = conf
        out.append(base2)
    return out

# ================== Mesaj formatı ==================
def render_message_card(sym, tf_list, side, entry, tp, sl, rr, conf, est_minutes):
    tf_text = "/".join(tf_list) if isinstance(tf_list, list) else str(tf_list)
    lev = leverage_for_conf(int(conf))
    return (
        f"🪙 {sym} · ⏱ {tf_text} · {'📈 LONG' if side=='LONG' else '📉 SHORT'}\n\n"
        f"💵 {_fmt_price(entry)}\n"
        f"🎯 {_fmt_price(tp)}\n"
        f"🛑 {_fmt_price(sl)}\n\n"
        f"⚖️ R:R {rr:.2f}\n"
        f"🔒 Güven {int(conf)}/100\n"
        f"🚀 Kaldıraç {lev}x\n"
        f"⏳ ~{int(est_minutes)} dk"
    )

# ================== Alive & Status ==================
_last_alive_ts = 0.0
_last_status_ts = 0.0

def send_alive():
    send_info("🟢 KriptoAlper Hayatta")

def send_status(scanned, sig_count, last_sig_ts):
    last_sig = ("yok" if not last_sig_ts else f"{int((time.time()-last_sig_ts)//60)} dk önce")
    send_info(
        "💓 KriptoAlper — Durum\n"
        f"📊 {scanned} coin tarandı\n"
        f"📈 Sinyal: {sig_count}\n"
        f"🕒 Son sinyal: {last_sig}"
    )

def maybe_alive_and_status():
    global _last_alive_ts, _last_status_ts, scanned_total, scanned_effective, skipped_cooldown
    now = time.time()
    if now - _last_alive_ts >= ALIVE_MIN * 60:
        send_alive()
        _last_alive_ts = now
    if now - _last_status_ts >= STATUS_MIN * 60:
        send_status(scanned_total, perf_sent_total, _last_signal_ts)
        _last_status_ts = now
        scanned_total = scanned_effective = skipped_cooldown = 0  # periyot sonunda reset

def maybe_silence_alert():
    global _last_signal_ts
    if _last_signal_ts and (time.time() - _last_signal_ts) >= SILENCE_ALERT_MIN*60:
        send_info(f"🟡 {SILENCE_ALERT_MIN}+ dk sinyal yok.")
        _last_signal_ts = time.time()

# ================== Heartbeat metrikleri (sayıcılar) ==================
scanned_total = 0
scanned_effective = 0
skipped_cooldown = 0
_last_signal_ts = None
_last_no_signal_relax = False

perf_sent_total = 0
perf_sent_by_sym = defaultdict(int)
perf_rr_last = deque(maxlen=500)

# ================== Ana tarama döngüsü ==================
_universe = []; _last_universe_ts = 0

def loop_once():
    global scanned_total, scanned_effective, skipped_cooldown, perf_sent_total, _last_signal_ts, _universe, _last_universe_ts, _last_no_signal_relax

    # Evreni tazele
    if (time.time() - _last_universe_ts) >= 120 or not _universe:
        _universe = refresh_top_futures(TOP_N); _last_universe_ts = time.time()

    # Auto relax: 90 dk sinyal yoksa gevşet; sinyal gelince normale dön
    if (_last_signal_ts is None) or ((time.time() - _last_signal_ts) >= 90*60):
        _last_no_signal_relax = True
    else:
        _last_no_signal_relax = False

    symbol_bucket = defaultdict(list)

    for sym in list(_universe):
        scanned_total += 1
        in_cd = not cooldown_ok(sym)
        if in_cd and not SCAN_DURING_COOLDOWN:
            skipped_cooldown += 1
            continue

        df1m = get_klines_cached(sym, "1m", 350)
        if df1m is None or len(df1m) < 120:
            continue
        regime = detect_regime(df1m["close"])
        adj = regime_adjustments(regime, relax=_last_no_signal_relax)

        for tf in TIMEFRAMES:
            df = df1m if tf=="1m" else get_klines_cached(sym, tf, 250)
            sigs = build_signals_for_tf(df, tf, sym, adj)
            if not sigs: continue
            valids = [s for s in sigs if mtf_confirm_if_enabled(sym, tf, s["side"])]
            if not valids: continue
            scanned_effective += 1
            symbol_bucket[sym].extend(valids)

    # Gönderim: TF birleştir ve gönder
    for sym, arr in symbol_bucket.items():
        merged = merge_signals_same_symbol(arr)
        if not merged: continue
        for ms in merged:
            tf_list = ms.get("tf_list", [ms["tf"]])
            rr = float(ms["rr"])
            est_min = est_minutes_to_tp(tf_list[0] if isinstance(tf_list, list) else ms["tf"], ms["atr"], abs(ms["tp"]-ms["entry"]))
            msg = render_message_card(sym, tf_list, ms["side"], ms["entry"], ms["tp"], ms["sl"], rr, ms["conf"], est_min)
            ok = send_tg_signal_sync(msg)
            if ok:
                print(f"[DELIVERED] {sym}")
                mark_sent(sym)
                perf_sent_total += 1
                perf_sent_by_sym[sym] += 1
                perf_rr_last.append(rr)
                _last_signal_ts = time.time()
            else:
                print(f"[DROP] delivery failed {sym}")

# ================== Main ==================
def main_loop():
    print("KriptoAlper — Futures TOP-30 • High-Signal başlatıldı. Tarama başlıyor…")
    send_info("🟢 KriptoAlper (Futures TOP-30) çalışıyor. Tarama başladı.")
    # açılışta hemen bir kez alive at
    send_alive()
    while True:
        t0 = time.time()
        try:
            loop_once()
            maybe_alive_and_status()
            maybe_silence_alert()
        except Exception as e:
            print("Döngü istisna:", e); traceback.print_exc()
        time.sleep(max(1, 12 - (time.time()-t0)))

# app.py import uyumu
def main():
    return main_loop()

if __name__ == "__main__":
    main_loop()
