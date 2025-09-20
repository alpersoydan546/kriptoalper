#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KriptoAlper — Futures TOP-30 • High-Signal • TF Birleştirme • Kalıcı Cooldown (SQLite)
- Binance Futures (USDT-M) fapi kullanır
- TOP-30 USDT parite (24h quoteVolume'a göre)
- Coin başına 60 dk cooldown (kalıcı: SQLite)
- 1 saatlik heartbeat (metrikli: scan, sent, avg RR, top syms, http429, req sayısı)
- Rejim (volatilite) uyarlaması: low/normal/high → wick ve ATR katsayılarını ayarlar
- TF birleştirme: aynı anda 1m/5m/15m/1h sinyalleri tek kartta
- Gelişmiş güven skoru + güvenden kaldıraç önerisi
- Telegram throttle (kuyruk) ve basit backoff
- Import uyumu: app.py `from scanner import main` ile çağırır
"""
import os, time, math, traceback, threading, queue, sqlite3
from collections import defaultdict, deque

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

BOT_NAME = "KriptoAlper"

# ================== ENV / Sabitler ==================
SEND_TO_TELEGRAM = True

HEARTBEAT_MIN = int(os.getenv("HEARTBEAT_MIN", "60"))          # 1 saat
HEARTBEAT_FORCE = os.getenv("HEARTBEAT_FORCE", "1") == "1"
SILENCE_ALERT_MIN = int(os.getenv("SILENCE_ALERT_MIN", "180")) # 3 saat

TOP_N = int(os.getenv("TOP_N", "30"))                           # evren büyüklüğü
MIN_24H_USDT_VOL = float(os.getenv("MIN_24H_USDT_VOL", "5000000"))
COOLDOWN_MIN_PER_SYMBOL = int(os.getenv("COOLDOWN_MINUTES_PER_SYMBOL", "60"))

TIMEFRAMES = ["1m", "5m", "15m", "1h"]                         # üretken set

REQ_SLEEP_SEC = float(os.getenv("REQ_SLEEP_SEC","0.18"))
KLINES_CACHE_TTL = int(os.getenv("KLINES_CACHE_TTL","20"))
MAX_RETRY_429 = 3
BACKOFF_BASE = 0.8

MTF_CONFIRM_ENABLE = os.getenv("MTF_CONFIRM","0") == "1"
MTF_RELAX_MAP = {"1m":["5m","15m"], "5m":["15m","1h"], "15m":["1h","4h"], "1h":["4h"]}

# ================== Göstergeler / Eşikler ==================
EMA_FAST = 12
EMA_SLOW = 26
EMA_BASE = 200

# baz eşikler (rejime göre ayarlayacağız)
BASE_SLOPE_MIN = 0.005   # %0.5
RSI_LEN = 14
RSI_LONG_MIN = 40
RSI_SHORT_MAX = 60
ATR_LEN = 14
ATR_MULT_SL = 1.10
ATR_MULT_TP = 2.40
WICK_BODY_MAX = 0.90
BB_LEN = 20

MIN_RR_BY_TF = {"1m":1.4, "5m":1.5, "15m":1.6, "1h":1.8}
MIN_CONF_BY_TF = {"1m":65, "5m":68, "15m":72, "1h":78}

# ================== HTTP / Binance ==================
FAPI_BASES = ["https://fapi.binance.com", "https://fapi.binance.us"]
_fapi_idx = 0
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": f"{BOT_NAME}/v2-high-signal"})

http_429_count = 0
http_req_count = 0

def _fapi_base():
    global _fapi_idx
    return FAPI_BASES[_fapi_idx % len(FAPI_BASES)]

def http_get(url, params=None, timeout=10):
    """GET with simple backoff & host rotate."""
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

# ================== DB (kalıcı cooldown) ==================
DB_PATH = os.getenv("STATE_DB_PATH", "state.db")
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

# ================== Telegram throttle (kuyruk) ==================
TG_TOKEN = os.getenv("TELEGRAM_TOKEN","")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID","")
tg_queue: "queue.Queue[str]" = queue.Queue()
tg_worker_started = False

def _tg_sender_worker():
    backoff = 1.0
    while True:
        text = tg_queue.get()
        if not SEND_TO_TELEGRAM or not TG_TOKEN or not TG_CHAT:
            print("[TG] disabled or not configured:", (len(text) if text else 0))
            time.sleep(0.2)
            continue
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": text},
                timeout=10
            )
            code = r.status_code
            if code == 200:
                backoff = 1.0
            elif code == 429:
                # Telegram flood — yumuşak bekle ve yeniden dene
                time.sleep(backoff); backoff = min(backoff*1.7, 8.0)
            else:
                print("[TG ERR]", code, r.text[:180])
            # temel throttle: 1 msg / ~1.1s
            time.sleep(1.1)
        except Exception as e:
            print("[TG EX]", e); time.sleep(backoff); backoff = min(backoff*1.7, 8.0)

def send_tg(text: str):
    global tg_worker_started
    if not tg_worker_started:
        threading.Thread(target=_tg_sender_worker, daemon=True).start()
        tg_worker_started = True
    tg_queue.put(text)

# ================== İndikatörler ==================
def ema(s, n): return s.ewm(span=n, adjust=False).mean()

def rsi(series, n=14):
    ch = series.diff()
    gain = (ch.where(ch>0,0)).ewm(alpha=1/n, adjust=False).mean()
    loss = (-ch.where(ch<0,0)).ewm(alpha=1/n, adjust=False).mean()
    rs = gain / (loss.replace(0, np.nan))
    return (100 - (100/(1+rs))).fillna(50)

def atr(df, n=14):
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()

def slope(series, length=15):
    if len(series) < length+1: return 0.0
    y = series.iloc[-length:].values; x = np.arange(length)
    m = np.polyfit(x, y, 1)[0]
    base = np.mean(np.abs(y)) + 1e-9
    return (m / base) * 100

def boll_mid(series, n=20):
    ma = series.rolling(n).mean()
    std = series.rolling(n).std(ddof=0)
    return ma, ma+2*std, ma-2*std

def last_cross_bars(fast, slow):
    sign = np.sign((fast - slow).values); d = np.diff(sign)
    idx = np.where(d != 0)[0]
    if len(idx)==0: return None, None
    bars_ago = len(fast)-1 - idx[-1]
    direction = 1 if (fast.iloc[idx[-1]+1] > slow.iloc[idx[-1]+1]) else -1
    return bars_ago, direction

def wick_filter_ok(df, lookback=2, wick_body_max=0.9):
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
    if x >= 1: return f"{x:,.5f}".replace(","," ")
    return f"{x:.8f}".rstrip("0").rstrip(".")

def leverage_for_conf(conf: int) -> int:
    if conf >= 90: return 12
    if conf >= 80: return 9
    if conf >= 70: return 7
    if conf >= 60: return 5
    return 3

def est_minutes_to_tp(tf, atr_val, distance):
    if atr_val <= 0: return _tf_min(tf)
    bars = max(1.0, distance/atr_val) * 1.05
    return int(round(bars * _tf_min(tf)))

# ================== Rejim (volatilite) uyarlaması ==================
def detect_regime(df_close: pd.Series) -> str:
    """Close serisinden basit volatilite rejimi (rolling std pct)."""
    pct = df_close.pct_change()
    vol = pct.rolling(240).std(ddof=0).iloc[-1]  # ~ 4h bar sayısı (1m için)
    if pd.isna(vol): return "normal"
    if vol > 0.06: return "high"
    if vol < 0.02: return "low"
    return "normal"

def regime_adjustments(regime: str):
    """Rejime göre eşik ayarları döndür."""
    if regime == "high":
        return {
            "slope_min": BASE_SLOPE_MIN * 1.2,
            "rsi_long_min": max(35, RSI_LONG_MIN - 2),
            "rsi_short_max": min(65, RSI_SHORT_MAX + 2),
            "wick_body_max": min(0.7, WICK_BODY_MAX),
            "atr_sl": ATR_MULT_SL * 1.15,
            "atr_tp": ATR_MULT_TP * 1.15,
        }
    if regime == "low":
        return {
            "slope_min": max(0.003, BASE_SLOPE_MIN * 0.8),
            "rsi_long_min": RSI_LONG_MIN,
            "rsi_short_max": RSI_SHORT_MAX,
            "wick_body_max": max(1.2, WICK_BODY_MAX),
            "atr_sl": ATR_MULT_SL * 1.0,
            "atr_tp": ATR_MULT_TP * 0.9,
        }
    return {
        "slope_min": BASE_SLOPE_MIN,
        "rsi_long_min": RSI_LONG_MIN,
        "rsi_short_max": RSI_SHORT_MAX,
        "wick_body_max": WICK_BODY_MAX,
        "atr_sl": ATR_MULT_SL,
        "atr_tp": ATR_MULT_TP,
    }

# ================== Evren / Klines Cache ==================
_universe = []; _last_universe_ts = 0
_KLINES_CACHE = {}  # (symbol, interval) -> (ts, df)

def refresh_top_futures(n=TOP_N):
    global _universe, _last_universe_ts, _fapi_idx
    r = http_get(f"{_fapi_base()}/fapi/v1/ticker/24hr")
    if not r or r.status_code != 200:
        if not _universe:
            _universe = ["BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","AVAXUSDT","ADAUSDT","DOGEUSDT"]
        _last_universe_ts = time.time(); return _universe
    arr = r.json()
    usdt = [it for it in arr if it.get("symbol","").endswith("USDT")]
    usdt_sorted = sorted(usdt, key=lambda x: float(x.get("quoteVolume",0.0)), reverse=True)
    top = [it["symbol"] for it in usdt_sorted if float(it.get("quoteVolume",0.0))>=MIN_24H_USDT_VOL][:n]
    _universe = top if top else _universe
    _last_universe_ts = time.time()
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

# ================== Skor & Sinyal İnşası ==================
def confidence_score(rr, slope_abs, rsi_now, tf_bonus):
    base = 55
    base += min(20, max(0.0, (rr-1.3)*10))
    base += min(10, slope_abs/0.05)
    base += 4 if 45 <= rsi_now <= 65 else 0
    base += tf_bonus           # MTF uyum/aynı anda çok TF bonusu
    return int(max(0, min(100, round(base))))

def build_signals_for_tf(df, tf, sym, adj):
    """Tek TF için sinyal listesi döndür."""
    out = []
    if df is None or len(df) < 60: return out
    df = df.copy()
    df["ema_fast"] = ema(df["close"], EMA_FAST)
    df["ema_slow"] = ema(df["close"], EMA_SLOW)
    df["ema_base"] = ema(df["close"], EMA_BASE)
    df["rsi"] = rsi(df["close"], RSI_LEN)
    df["atr"] = atr(df, ATR_LEN)
    bb_mid, bb_up, bb_lo = boll_mid(df["close"], BB_LEN)

    c     = df["close"].iloc[-2]
    atr_n = df["atr"].iloc[-2]
    base  = df["ema_base"].iloc[-2]
    slp   = slope(df["ema_base"], 15)

    long_tr  = (c > base) and (slp >= adj["slope_min"])
    short_tr = (c < base) and (slp <= -adj["slope_min"])

    bars_ago, dir_cross = last_cross_bars(df["ema_fast"], df["ema_slow"])
    recent_cross_ok = (bars_ago is not None) and (bars_ago <= 25)
    wick_ok = wick_filter_ok(df, 2, adj["wick_body_max"])
    r_now = df["rsi"].iloc[-2]

    # BB aşırılık kontrolünü rejime göre gevşek bırakıyoruz
    bbm = bb_mid.iloc[-2]; bbu = bb_up.iloc[-2]; bbl = bb_lo.iloc[-2]
    not_extreme_long  = (c <= bbu + 1e-9)
    not_extreme_short = (c >= bbl - 1e-9)

    def push(side, entry, tp, sl, rr, conf):
        min_rr   = MIN_RR_BY_TF.get(tf, 1.5)
        min_conf = MIN_CONF_BY_TF.get(tf, 70)
        if rr >= min_rr and conf >= min_conf:
            out.append({
                "sym": sym, "tf": tf, "side": side,
                "entry": entry, "tp": tp, "sl": sl,
                "rr": rr, "conf": int(conf),
                "atr": atr_n, "slope": slp, "rsi": r_now
            })

    # LONG
    if long_tr and recent_cross_ok and dir_cross == 1 and wick_ok and (r_now >= adj["rsi_long_min"]) and not_extreme_long:
        entry = c
        sl = entry - max(atr_n * adj["atr_sl"], 1e-9*entry)
        tp = entry + atr_n * adj["atr_tp"]
        rr = (tp-entry)/max((entry-sl),1e-9)
        # conf hesaplaması MTF bonusu sonra eklenecek (birleştirirken)
        out.append({"sym":sym,"tf":tf,"side":"LONG","entry":entry,"tp":tp,"sl":sl,"rr":rr,
                    "conf":0,"atr":atr_n,"slope":slp,"rsi":r_now})
    # SHORT
    if short_tr and recent_cross_ok and dir_cross == -1 and wick_ok and (r_now <= adj["rsi_short_max"]) and not_extreme_short:
        entry = c
        sl = entry + max(atr_n * adj["atr_sl"], 1e-9*entry)
        tp = entry - atr_n * adj["atr_tp"]
        rr = (entry-tp)/max((sl-entry),1e-9)
        out.append({"sym":sym,"tf":tf,"side":"SHORT","entry":entry,"tp":tp,"sl":sl,"rr":rr,
                    "conf":0,"atr":atr_n,"slope":slp,"rsi":r_now})
    return out

# ================== TF Birleştirme + MTF onayı ==================
def mtf_confirm_if_enabled(sym, tf, side):
    if not MTF_CONFIRM_ENABLE: return True
    req = MTF_RELAX_MAP.get(tf, [])
    if not req: return True
    need = max(1, len(req)-1)
    ok = 0
    for htf in req:
        try:
            dfh = get_klines_cached(sym, htf, limit=210)
            price = dfh["close"].iloc[-1]
            ema_b = ema(dfh["close"], EMA_BASE).iloc[-1]
            s_b   = slope(ema(dfh["close"], EMA_BASE), 15)
            if side=="LONG" and (price>ema_b and s_b>=BASE_SLOPE_MIN): ok += 1
            if side=="SHORT" and (price<ema_b and s_b<=-BASE_SLOPE_MIN): ok += 1
        except: pass
    return ok >= need

TF_PRIORITY = ["5m","15m","1m","1h"]  # baz seçimde öncelik

def pick_base_signal(signals):
    """Aynı sembol & aynı yön için baz sinyali seç (önce TF önceliği, sonra en iyi RR)."""
    if not signals: return None
    signals_sorted = sorted(signals, key=lambda s: (TF_PRIORITY.index(s["tf"]) if s["tf"] in TF_PRIORITY else 99, -s["rr"]))
    return signals_sorted[0]

def merge_signals_same_symbol(symbol_sigs):
    """
    symbol_sigs: list[ {tf, side, entry, tp, sl, rr, atr, slope, rsi} ]
    -> dict with base + merged TFs and boosted confidence
    """
    if not symbol_sigs: return []
    out = []
    # iki yönü ayır
    longs  = [s for s in symbol_sigs if s["side"]=="LONG"]
    shorts = [s for s in symbol_sigs if s["side"]=="SHORT"]
    for group in (longs, shorts):
        if not group: continue
        base = pick_base_signal(group)
        if not base: continue
        tfs = sorted({s["tf"] for s in group}, key=lambda x: TF_PRIORITY.index(x) if x in TF_PRIORITY else 99)
        tf_bonus = min(8, 3 + 2*(len(tfs)-1))   # çok TF → +bonus (max ~8)
        conf = confidence_score(base["rr"], abs(base["slope"]), base["rsi"], tf_bonus)
        base2 = base.copy()
        base2["tf_list"] = tfs
        base2["conf"] = conf
        out.append(base2)
    return out

# ================== Mesaj Formatı — Seçenek 1 (Kart Stil) ==================
def render_message_card(sym, tf_list, side, entry, tp, sl, rr, conf, est_minutes):
    # TF'leri "1m/5m/15m" olarak yaz
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

# ================== Heartbeat / Metrikler ==================
_scanned_counter = 0
_last_heartbeat_ts = 0.0
_last_signal_ts = None
perf_sent_total = 0
perf_sent_by_sym = defaultdict(int)
perf_rr_last = deque(maxlen=500)

def heartbeat_text():
    avg_rr = (sum(perf_rr_last)/len(perf_rr_last)) if perf_rr_last else 0.0
    top_syms = sorted(perf_sent_by_sym.items(), key=lambda x: x[1], reverse=True)[:3]
    top_txt = ", ".join([f"{s}:{c}" for s,c in top_syms]) if top_syms else "—"
    last_sig = ("yok" if not _last_signal_ts else f"{int((time.time()-_last_signal_ts)//60)} dk önce")
    return (
        "💓 KriptoAlper — Heartbeat\n"
        f"• Tarama: ~{_scanned_counter}  • Sinyal: {perf_sent_total}\n"
        f"• En çok sinyal: {top_txt}\n"
        f"• Ortalama R:R: {avg_rr:.2f}\n"
        f"• HTTP: req={http_req_count} 429={http_429_count}\n"
        f"• Son sinyal: {last_sig}"
    )

def maybe_heartbeat():
    global _last_heartbeat_ts, _scanned_counter
    if (time.time() - _last_heartbeat_ts) >= HEARTBEAT_MIN * 60:
        send_tg(heartbeat_text()); _last_heartbeat_ts = time.time(); _scanned_counter = 0

def maybe_silence_alert():
    global _last_signal_ts
    if _last_signal_ts and (time.time() - _last_signal_ts) >= SILENCE_ALERT_MIN*60:
        send_tg(f"🟡 {SILENCE_ALERT_MIN}+ dk sinyal yok."); _last_signal_ts = time.time()

# ================== Ana Döngü ==================
def loop_once():
    global _scanned_counter, perf_sent_total, _last_signal_ts, _last_universe_ts, _universe

    # evreni tazele
    if (time.time() - _last_universe_ts) >= 120 or not _universe:
        _universe = refresh_top_futures(TOP_N)

    # sinyalleri önce topla (TF birleştirme için)
    symbol_bucket = defaultdict(list)

    for sym in list(_universe):
        if not cooldown_ok(sym):
            continue
        # rejim: sembolün 1m'inde volatiliteye bak
        df1m = get_klines_cached(sym, "1m", 350)
        if df1m is None or len(df1m) < 120:
            continue
        regime = detect_regime(df1m["close"])
        adj = regime_adjustments(regime)

        for tf in TIMEFRAMES:
            _scanned_counter += 1
            df = df1m if tf=="1m" else get_klines_cached(sym, tf, 250)
            sigs = build_signals_for_tf(df, tf, sym, adj)
            if not sigs: continue

            # MTF doğrulama (opsiyonel)
            valid = []
            for s in sigs:
                if mtf_confirm_if_enabled(sym, tf, s["side"]):
                    valid.append(s)
            if not valid: continue

            symbol_bucket[sym].extend(valid)

    # şimdi gönderim: her sembolde aynı anda gelenleri birleştir
    for sym, arr in symbol_bucket.items():
        merged = merge_signals_same_symbol(arr)
        if not merged: continue
        # tek kart per direction; baz sinyalin metrikleri
        for ms in merged:
            tf_list = ms.get("tf_list", [ms["tf"]])
            rr = float(ms["rr"])
            est_min = est_minutes_to_tp(tf_list[0] if isinstance(tf_list, list) else ms["tf"], ms["atr"], abs(ms["tp"]-ms["entry"]))
            msg = render_message_card(sym, tf_list, ms["side"], ms["entry"], ms["tp"], ms["sl"], rr, ms["conf"], est_min)
            send_tg(msg)
            mark_sent(sym)  # coin başına 60 dk cooldown
            perf_sent_total += 1
            perf_sent_by_sym[sym] += 1
            perf_rr_last.append(rr)
            _set_last_signal()

def _set_last_signal():
    global _last_signal_ts
    _last_signal_ts = time.time()

def telegram_diag():
    if not TG_TOKEN or not TG_CHAT:
        print("[TG] token/chat missing")
        return
    try:
        r1 = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getMe", timeout=10)
        print("[TG DIAG] getMe:", r1.status_code, r1.text[:140])
    except Exception as e:
        print("[TG DIAG] EX:", e)

def main_loop():
    global _last_heartbeat_ts
    print("KriptoAlper — Futures TOP-30 • High-Signal • TF Merge • SQLite Cooldown")
    telegram_diag()
    if HEARTBEAT_FORCE:
        send_tg("✅ KriptoAlper açıldı. Heartbeat aktif.")
    send_tg("🟢 KriptoAlper (Futures TOP-30) çalışıyor. Tarama başladı.")
    _last_heartbeat_ts = time.time()

    while True:
        t0 = time.time()
        try:
            loop_once()
            maybe_heartbeat()
            maybe_silence_alert()
        except Exception as e:
            print("Döngü istisna:", e)
            traceback.print_exc()
        # CPU'yu rahatlat
        time.sleep(max(1, 12 - (time.time()-t0)))

# app.py import uyumu
def main():
    return main_loop()

if __name__ == "__main__":
    main_loop()
