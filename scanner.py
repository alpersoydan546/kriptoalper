#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KriptoAlper — Futures TOP-50, High-Signal + 60dk cooldown, Heartbeat, Telegram
- Mesaj şablonu: SENİN ORİJİNAL TEMPLATE_MINIMAL'IN (dokunulmadı).
- Dinamik evren: Binance Futures (fapi) 24h quoteVolume'a göre TOP-N (varsayılan 50).
- Her coinde 60 dk cooldown (aynı coin 1 saat boyunca tekrar sinyal yok).
- 30 dk heartbeat (ENV ile ayarlanabilir).
- Daha fazla sinyal için "gevşek ama güvenli" filtreler + Güven (confidence) puanı.
- Import uyumu: main() mevcut → app.py `from scanner import main` ile çağırabilir.
"""
import os, time, sys, traceback
from collections import defaultdict, deque
import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

BOT_NAME = "KriptoAlper"

# ================== DEBUG / TELEGRAM / HEARTBEAT ==================
DEBUG = True
PRINT_REASONS = True
SEND_TO_TELEGRAM = True

HEARTBEAT_EVERY_MIN = int(os.getenv("HEARTBEAT_MIN", "30"))   # 30 dk default
HEARTBEAT_FORCE_ON_START = os.getenv("HEARTBEAT_FORCE", "1") == "1"
SILENCE_ALERT_MIN = int(os.getenv("SILENCE_ALERT_MIN", "120"))  # 120dk sessizlik uyarısı
HEARTBEAT_SUMMARY = True

_last_heartbeat_ts = 0.0
_last_signal_ts = None
_scanned_counter = 0

perf_sent_total = 0
perf_sent_by_sym = defaultdict(int)
perf_rr_last = deque(maxlen=400)

# ================== EVREN / ZAMAN DİLİMLERİ ==================
TIMEFRAMES_SCALP = ["1m","5m","15m"]
TIMEFRAMES_SWING = ["1h"]
ENABLE_SCALP = True
ENABLE_SWING = True

# Dinamik evren için
TOP_N = int(os.getenv("TOP_N", "50"))  # Futures TOP-50
MIN_24H_USDT_VOL = float(os.getenv("MIN_24H_USDT_VOL", "200000"))  # makul alt eşik

# ================== FİLTRE AYARLARI (gevşek ama güvenli) ==================
EMA_FAST = 12
EMA_SLOW = 26
EMA_BASE = 200
BASE_SLOPE_MIN = 0.01     # gevşek eğim → daha çok sinyal

RSI_LEN = 14
RSI_LONG_MIN = 35
RSI_SHORT_MAX = 65

ATR_LEN = 14
ATR_MULT_SL = 1.10
ATR_MULT_TP = 2.50
MIN_RR_BY_TF = {"1m":1.6,"5m":1.7,"15m":1.8,"1h":1.9}
MIN_CONFIDENCE_BY_TF = {"1m":70,"5m":72,"15m":76,"1h":80}

WICK_FILTER = True
WICK_BODY_MAX = 0.60
BB_LEN = 20

# Cooldown (Aynı COIN → 60 dk)
COOLDOWN_MINUTES_PER_SYMBOL = int(os.getenv("COOLDOWN_MINUTES_PER_SYMBOL","60"))

# ================== RATE LIMIT / CACHE / HOST ROTASYONU ==================
REQ_SLEEP_SEC = float(os.getenv("REQ_SLEEP_SEC","0.18"))
BATCH_PAUSE_EVERY = 12
BATCH_PAUSE_SEC = 0.6
KLINES_CACHE_TTL = int(os.getenv("KLINES_CACHE_TTL","20"))

MAX_RETRY_429 = 3
BACKOFF_BASE = 0.8

# Futures API
FAPI_BASES = [
    "https://fapi.binance.com",
    "https://fapi.binance.us",
]
_fapi_idx = 0

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": f"{BOT_NAME}/manual-high-signal-1.0"})

# --- Telegram Mesaj Şablonu (SENİN ORİJİNAL DOSYADAKİ FORMAT) ---
TEMPLATE_MINIMAL = (
    "🔒 [ULTRA SAFE]\n"
    "Parite: 🪙 {PARITE}\n"
    "Zaman: ⏳ {TF} ({MOD})\n"
    "Yön: {YON}\n"
    "Giriş: 💵 {GIRIS}\n"
    "TP: 🎯 {TP}\n"
    "SL: 🛑 {SL}\n"
    "R:R: ⚖️ {RR}\n"
    "Güven: 🔒 {GUVEN}/100\n"
    "Süre: ⏳ ~{SURE}\n"
    "Kaynak: Binance"
)
# (Aynı şablon senin dosyana bire bir uygundur)  # ref: :contentReference[oaicite:3]{index=3}

# ================== yardımcılar ==================
def _sleep(s):
    try: time.sleep(s)
    except: pass

def _fapi_base():
    global _fapi_idx
    return FAPI_BASES[_fapi_idx % len(FAPI_BASES)]

def http_get(url, params=None, timeout=10):
    global _fapi_idx
    for i in range(MAX_RETRY_429 + 1):
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
        except Exception:
            _sleep((i+1)*0.5); continue
        if r.status_code == 429:
            wait = (BACKOFF_BASE ** i) + 0.5
            if DEBUG: print(f"[HTTP429] {url} backoff {wait:.2f}s")
            _sleep(wait); continue
        if r.status_code in (418,451):
            if DEBUG: print(f"[HTTP {r.status_code}] rotate host")
            _fapi_idx += 1
            _sleep((i+1)*0.8); continue
        return r
    return None

# ================== göstergeler ==================
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
    y = series.iloc[-length:].values
    x = np.arange(length)
    m = np.polyfit(x, y, 1)[0]
    base = np.mean(np.abs(y)) + 1e-9
    return (m / base) * 100

def bollinger_mid(series, n=20):
    ma = series.rolling(n).mean()
    std = series.rolling(n).std(ddof=0)
    upper = ma + 2*std
    lower = ma - 2*std
    return ma, upper, lower

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

def _tf_minutes(tf: str) -> int:
    return {"1m":1,"3m":3,"5m":5,"15m":15,"30m":30,"1h":60,"4h":240}.get(tf, 15)

def est_minutes_to_tp(tf, atr_val, distance):
    if atr_val <= 0: return _tf_minutes(tf)
    bars = max(1.0, distance/atr_val) * 1.05
    return int(round(bars * _tf_minutes(tf)))

def _fmt_price(x: float) -> str:
    if x >= 100: return f"{x:,.3f}".replace(","," ")
    if x >= 1:   return f"{x:,.5f}".replace(","," ")
    return f"{x:.8f}".rstrip("0").rstrip(".")

def confidence_score(rr, slope_abs, rsi_now):
    base = 55
    base += min(18, max(0.0, (rr-1.4)*10))
    base += max(0, min(8, slope_abs/0.05))
    base += 4 if (45 <= rsi_now <= 65) else 0
    return int(max(0, min(100, round(base))))

# ================== Telegram ==================
def send_tg(text):
    if not SEND_TO_TELEGRAM:
        print("[DRY-RUN]", text.replace("\n"," ")[:200]); return
    token = os.getenv("TELEGRAM_TOKEN",""); chat_id = os.getenv("TELEGRAM_CHAT_ID","")
    if not token or not chat_id:
        print("Telegram yapılandırılmamış. TOKEN/CHAT_ID eksik."); return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
        print("[TG SEND]", r.status_code, r.text[:160])
        if r.status_code != 200:
            print("Telegram hata:", r.text[:200])
    except Exception as e:
        print("Telegram istisna:", e)

def render_message(sym, tf, mode, side, entry, tp, sl, rr, conf, est_minutes):
    payload = {
        "PARITE": sym,
        "TF": tf,
        "MOD": mode,
        "YON": ("📈 LONG" if side == "LONG" else "📉 SHORT"),
        "GIRIS": _fmt_price(entry),
        "TP": _fmt_price(tp),
        "SL": _fmt_price(sl),
        "RR": f"{rr:.2f}",
        "GUVEN": int(conf),
        "SURE": f"{int(est_minutes)} dk",
    }
    return TEMPLATE_MINIMAL.format(**payload)   # şablon seninle bire bir aynı:contentReference[oaicite:4]{index=4}

# ================== TOP-50 Futures Evreni ==================
_universe = []
_last_universe_ts = 0

def refresh_top_futures(n=TOP_N):
    global _universe, _last_universe_ts, _fapi_idx
    base = _fapi_base()
    url = f"{base}/fapi/v1/ticker/24hr"
    r = http_get(url, params=None, timeout=12)
    if not r or r.status_code != 200:
        if DEBUG: print("[UNIVERSE] fetch failed; keeping previous or majors fallback")
        if not _universe:
            _universe = ["BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","AVAXUSDT","ADAUSDT","DOGEUSDT"]
        _last_universe_ts = time.time()
        return _universe
    arr = r.json()
    usdt = [it for it in arr if it.get("symbol","").endswith("USDT")]
    usdt_sorted = sorted(usdt, key=lambda x: float(x.get("quoteVolume",0.0)), reverse=True)
    top = [it["symbol"] for it in usdt_sorted if float(it.get("quoteVolume",0.0))>=MIN_24H_USDT_VOL][:n]
    if top:
        _universe = top
        _last_universe_ts = time.time()
        if DEBUG: print("[UNIVERSE]", " ".join(top[:10]), "…")
    return _universe

# ================== KLINES CACHE ==================
_KLINES_CACHE = {}  # (symbol, interval) -> (ts, df)

def get_klines_cached(symbol, interval, limit=250):
    key = (symbol, interval)
    ts_df = _KLINES_CACHE.get(key)
    now = time.time()
    if ts_df and (now - ts_df[0] <= KLINES_CACHE_TTL):
        return ts_df[1]
    _sleep(REQ_SLEEP_SEC)
    url = f"{_fapi_base()}/fapi/v1/klines"
    r = http_get(url, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=10)
    if not r or r.status_code != 200: return None
    arr = r.json()
    cols = ["open_time","open","high","low","close","volume","close_time","qav","nt","tb","tq","i"]
    df = pd.DataFrame(arr, columns=cols)
    for c in ["open","high","low","close","volume"]:
        df[c] = df[c].astype(float)
    df["open_time"]  = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    _KLINES_CACHE[key] = (now, df)
    return df

# ================== SİNYAL İNŞASI ==================
def build_signal(df, tf, sym):
    out = []
    if df is None or len(df) < 60: return out
    df = df.copy()
    df["ema_fast"] = ema(df["close"], EMA_FAST)
    df["ema_slow"] = ema(df["close"], EMA_SLOW)
    df["ema_base"] = ema(df["close"], EMA_BASE)
    df["rsi"] = rsi(df["close"], RSI_LEN)
    df["atr"] = atr(df, ATR_LEN)
    bb_mid, bb_up, bb_lo = bollinger_mid(df["close"], BB_LEN)

    c = df["close"].iloc[-2]                      # son kapanan mum
    atr_now = df["atr"].iloc[-2]
    base_now = df["ema_base"].iloc[-2]
    slope_base = slope(df["ema_base"], 15)

    long_trend  = (c > base_now) and (slope_base >= BASE_SLOPE_MIN)
    short_trend = (c < base_now) and (slope_base <= -BASE_SLOPE_MIN)

    bars_ago, dir_cross = last_cross_bars(df["ema_fast"], df["ema_slow"])
    recent_cross_ok = (bars_ago is not None) and (bars_ago <= 20)
    wick_ok = (not WICK_FILTER) or wick_filter_ok(df, 2)
    r_now = df["rsi"].iloc[-2]

    bbm = bb_mid.iloc[-2]; bbu = bb_up.iloc[-2]; bbl = bb_lo.iloc[-2]
    not_extreme_long  = (c <= bbu - 0.4*atr_now)
    not_extreme_short = (c >= bbl + 0.4*atr_now)
    above_mid = c > bbm
    below_mid = c < bbm

    def push(side, entry, tp, sl, rr, conf):
        min_rr   = MIN_RR_BY_TF.get(tf, 1.6)
        min_conf = MIN_CONFIDENCE_BY_TF.get(tf, 70)
        if rr >= min_rr and conf >= min_conf:
            out.append({
                "sym":sym,"tf":tf,"mode": ("Vurkaç" if tf in ["1m","5m","15m"] else "Orta Vade"),
                "side":side,"entry":entry,"tp":tp,"sl":sl,
                "rr":rr,"conf":int(conf),
                "est_min": est_minutes_to_tp(tf, atr_now, abs(tp-entry))
            })

    # LONG
    if long_trend and recent_cross_ok and dir_cross==1 and wick_ok and (r_now >= RSI_LONG_MIN) and above_mid and not_extreme_long:
        entry = c; sl = entry - max(atr_now*ATR_MULT_SL, 1e-9*entry)
        tp = entry + atr_now*ATR_MULT_TP
        rr = (tp-entry)/max((entry-sl),1e-9)
        conf = confidence_score(rr, abs(slope_base), r_now)
        push("LONG", entry, tp, sl, rr, conf)

    # SHORT
    if short_trend and recent_cross_ok and dir_cross==-1 and wick_ok and (r_now <= RSI_SHORT_MAX) and below_mid and not_extreme_short:
        entry = c; sl = entry + max(atr_now*ATR_MULT_SL, 1e-9*entry)
        tp = entry - atr_now*ATR_MULT_TP
        rr = (entry-tp)/max((sl-entry),1e-9)
        conf = confidence_score(rr, abs(slope_base), r_now)
        push("SHORT", entry, tp, sl, rr, conf)

    if DEBUG and not out:
        print(f"[NO-SIGNAL] {sym} {tf} — gevşek modda bile elendi.")
    return out

# ================== Cooldown (coin başına 60 dk) ==================
_last_sent_coin = {}  # sym -> ts

def cooldown_ok(sym):
    now = time.time()
    last = _last_sent_coin.get(sym, 0)
    return (now - last) >= (COOLDOWN_MINUTES_PER_SYMBOL * 60)

def mark_sent(sym):
    _last_sent_coin[sym] = time.time()

# ================== HEARTBEAT / RAPOR ==================
def _fmt_min(seconds): return int(round(seconds/60))

def heartbeat_text():
    parts = ["🟢 KriptoAlper (Futures TOP-50 — High Signal) çalışıyor."]
    if HEARTBEAT_SUMMARY:
        avg_rr = (sum(perf_rr_last)/len(perf_rr_last)) if perf_rr_last else 0.0
        top_syms = sorted(perf_sent_by_sym.items(), key=lambda x: x[1], reverse=True)[:3]
        top_txt = ", ".join([f"{s}:{c}" for s,c in top_syms]) if top_syms else "—"
        parts.append(f"Son {HEARTBEAT_EVERY_MIN} dk: ~{_scanned_counter} tarama, {perf_sent_total} sinyal.")
        parts.append(f"En çok sinyal: {top_txt}. Ortalama R:R: {avg_rr:.2f}")
        if _last_signal_ts:
            parts.append(f"Son sinyal: {_fmt_min(time.time() - _last_signal_ts)} dk önce.")
        else:
            parts.append("Henüz sinyal yok.")
    return " ".join(parts)

def maybe_heartbeat():
    global _last_heartbeat_ts, _scanned_counter
    now = time.time()
    if (now - _last_heartbeat_ts) >= HEARTBEAT_EVERY_MIN * 60:
        send_tg(heartbeat_text()); _last_heartbeat_ts = now; _scanned_counter = 0

def maybe_silence_alert():
    global _last_signal_ts
    if _last_signal_ts is None: return
    now = time.time()
    if (now - _last_signal_ts) >= SILENCE_ALERT_MIN * 60:
        send_tg(f"🟡 {SILENCE_ALERT_MIN}+ dk sinyal yok (High Signal).")
        _last_signal_ts = now

# ================== ANA DÖNGÜ ==================
def loop_once():
    global _scanned_counter, perf_sent_total, _last_signal_ts

    tf_list = []
    if ENABLE_SCALP: tf_list += TIMEFRAMES_SCALP
    if ENABLE_SWING: tf_list += TIMEFRAMES_SWING

    # Evreni 2 dakikada bir yenile
    global _last_universe_ts
    if (time.time() - _last_universe_ts) >= 120 or not _universe:
        refresh_top_futures(TOP_N)

    for idx, sym in enumerate(list(_universe)):
        if idx % BATCH_PAUSE_EVERY == 0 and idx > 0: _sleep(BATCH_PAUSE_SEC)

        # Sadece cooldown uygunsa bak
        if not cooldown_ok(sym):
            if DEBUG and PRINT_REASONS: print(f"[SKIP cooldown] {sym}")
            continue

        for tf in tf_list:
            _scanned_counter += 1
            try:
                df = get_klines_cached(sym, tf, limit=250)
                sigs = build_signal(df, tf, sym)
                if not sigs: continue

                # Aynı sembolden gelen tüm sinyalleri gönder ama tek sefer cooldown işaretle
                sent_any = False
                for s in sigs:
                    msg = render_message(
                        s["sym"], s["tf"], s["mode"], s["side"],
                        s["entry"], s["tp"], s["sl"], s["rr"], s["conf"], s["est_min"]
                    )
                    send_tg(msg)
                    perf_sent_total += 1
                    perf_sent_by_sym[sym] += 1
                    perf_rr_last.append(float(s["rr"]))
                    _last_signal_ts = time.time()
                    sent_any = True
                    print(f"[SENT] {sym} {s['tf']} {s['side']} conf={s['conf']} rr={s['rr']:.2f}")

                if sent_any:
                    mark_sent(sym)  # 60 dk kilit

            except Exception as e:
                print(f"{sym} {tf} hata:", e)
                if DEBUG:
                    traceback.print_exc()

def telegram_diag():
    token = os.getenv("TELEGRAM_TOKEN", ""); chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    base = f"https://api.telegram.org/bot{token}"
    try:
        r1 = requests.get(f"{base}/getMe", timeout=10)
        print("[TG DIAG] getMe:", r1.status_code, r1.text[:140])
        r2 = requests.get(f"{base}/getChat", params={"chat_id": chat_id}, timeout=10)
        print("[TG DIAG] getChat:", r2.status_code, r2.text[:140])
    except Exception as e:
        print("[TG DIAG] Exception:", e)

def main_loop():
    global _last_heartbeat_ts
    print(f"{BOT_NAME} (Futures TOP-50 — High Signal) tarama başladı…")
    print(f"ENV → TT: {os.getenv('TELEGRAM_TOKEN','')[:10]}***  CID: {os.getenv('TELEGRAM_CHAT_ID','')}")
    telegram_diag()

    if HEARTBEAT_FORCE_ON_START:
        send_tg("✅ Heartbeat: hayattayım (Futures TOP-50 — High Signal)")
    send_tg("🟢 KriptoAlper (Futures TOP-50 — High Signal) açıldı. Tarama başladı.")
    _last_heartbeat_ts = time.time()

    while True:
        t0 = time.time()
        try:
            loop_once()
            maybe_heartbeat()
            maybe_silence_alert()
        except Exception as e:
            print("Döngü istisna:", e); traceback.print_exc()
        _sleep(max(1, 12 - (time.time()-t0)))

# --- import uyumluluğu: app.py 'from scanner import main' diyorsa çalışır ---
def main():
    return main_loop()

if __name__ == "__main__":
    main_loop()
