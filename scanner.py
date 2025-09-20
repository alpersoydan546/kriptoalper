#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KriptoAlper — Futures TOP-50 (High-Signal), 60dk cooldown, 1 saatlik Heartbeat
- Sinyal mesaj şablonu: KULLANICININ ORİJİNAL TEMPLATE_MINIMAL (emoji/metin aynen) :contentReference[oaicite:7]{index=7}
- Dinamik evren: Binance Futures (fapi) 24h quoteVolume TOP-N (varsayılan 50)
- Coin başına 60 dk cooldown (aynı coinden 1 saat içinde tekrar sinyal yok)
- Heartbeat: 1 saatte bir (format yenilendi)
- Daha üretken filtre: gevşetilmiş trend/RSI/wick/RR; güven puanı ile birlikte
- MTF teyit ENV ile aç/kapa (varsayılan kapalı → daha çok sinyal)
- Import uyumluluğu: main()
"""
import os, time, sys, traceback
from collections import defaultdict, deque

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

BOT_NAME = "KriptoAlper"

# ================== TELEGRAM / HEARTBEAT ==================
SEND_TO_TELEGRAM = True
HEARTBEAT_EVERY_MIN = int(os.getenv("HEARTBEAT_MIN", "60"))     # 1 saat
HEARTBEAT_FORCE_ON_START = os.getenv("HEARTBEAT_FORCE", "1") == "1"
SILENCE_ALERT_MIN = int(os.getenv("SILENCE_ALERT_MIN", "180"))  # 3 saat sessizlik uyarısı

_last_heartbeat_ts = 0.0
_last_signal_ts = None
_scanned_counter = 0

perf_sent_total = 0
perf_sent_by_sym = defaultdict(int)
perf_rr_last = deque(maxlen=500)

# ================== EVREN / TF ==================
TOP_N = int(os.getenv("TOP_N", "50"))  # Futures TOP-50
TIMEFRAMES = ["1m","5m","15m","1h"]    # üretken
MIN_24H_USDT_VOL = float(os.getenv("MIN_24H_USDT_VOL", "5000000"))  # 5M: likid ama kapsayıcı

# ================== FİLTRE AYARLARI (gevşek ama temkinli) ==================
EMA_FAST = 12
EMA_SLOW = 26
EMA_BASE = 200
BASE_SLOPE_MIN = 0.005     # %0.5 → daha fazla sinyal

RSI_LEN = 14
RSI_LONG_MIN = 40
RSI_SHORT_MAX = 60

ATR_LEN = 14
ATR_MULT_SL = 1.10
ATR_MULT_TP = 2.40
MIN_RR_BY_TF = {"1m":1.4,"5m":1.5,"15m":1.6,"1h":1.8}  # üretken
MIN_CONFIDENCE_BY_TF = {"1m":65,"5m":68,"15m":72,"1h":78}

WICK_FILTER = True
WICK_BODY_MAX = 0.9
BB_LEN = 20

# Cooldown (coin başına 60 dk)
COOLDOWN_MINUTES_PER_SYMBOL = int(os.getenv("COOLDOWN_MINUTES_PER_SYMBOL","60"))

# MTF teyit (opsiyonel, varsayılan kapalı → daha çok sinyal)
MTF_CONFIRM_ENABLE = os.getenv("MTF_CONFIRM","0") == "1"
MTF_RELAX_MAP = {"1m":["5m","15m"], "5m":["15m","1h"], "15m":["1h","4h"], "1h":["4h"]}

# ================== RATE LIMIT / CACHE / HOST ==================
REQ_SLEEP_SEC = float(os.getenv("REQ_SLEEP_SEC","0.18"))
KLINES_CACHE_TTL = int(os.getenv("KLINES_CACHE_TTL","20"))
MAX_RETRY_429 = 3
BACKOFF_BASE = 0.8

FAPI_BASES = ["https://fapi.binance.com", "https://fapi.binance.us"]
_fapi_idx = 0

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": f"{BOT_NAME}/high-signal-1.1"})

# --- SİNYAL MESAJ ŞABLONU (KULLANICININ ORİJİNALİ) ---
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
)  # kaynak: senin mevcut dosyan:contentReference[oaicite:8]{index=8}

# --- Heartbeat formatı (yenilendi) ---
def heartbeat_text():
    avg_rr = (sum(perf_rr_last)/len(perf_rr_last)) if perf_rr_last else 0.0
    top_syms = sorted(perf_sent_by_sym.items(), key=lambda x: x[1], reverse=True)[:3]
    top_txt = ", ".join([f"{s}:{c}" for s,c in top_syms]) if top_syms else "—"
    last_sig = ("yok" if not _last_signal_ts else f"{int((time.time()-_last_signal_ts)//60)} dk önce")
    return (
        "💓 KriptoAlper — Heartbeat (1 saat)\n"
        f"• Tarama: ~{_scanned_counter}  • Sinyal: {perf_sent_total}\n"
        f"• En çok sinyal: {top_txt}\n"
        f"• Ortalama R:R: {avg_rr:.2f}\n"
        f"• Son sinyal: {last_sig}"
    )

# ================== Yardımcılar ==================
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
            _sleep((BACKOFF_BASE**i)+0.5); continue
        if r.status_code in (418,451):
            _fapi_idx += 1; _sleep((i+1)*0.8); continue
        return r
    return None

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
def bollinger_mid(series, n=20):
    ma = series.rolling(n).mean(); std = series.rolling(n).std(ddof=0)
    return ma, ma + 2*std, ma - 2*std
def last_cross_bars(fast, slow):
    sign = np.sign((fast - slow).values); d = np.diff(sign)
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
def _tf_min(tf): return {"1m":1,"3m":3,"5m":5,"15m":15,"30m":30,"1h":60}.get(tf,15)

# ================== 24h TOP-50 Futures ==================
_universe = []; _last_universe_ts = 0
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

# ================== Klines cache ==================
_KLINES_CACHE = {}  # (symbol, interval) -> (ts, df)
def get_klines_cached(symbol, interval, limit=250):
    key = (symbol, interval); now = time.time()
    ts_df = _KLINES_CACHE.get(key)
    if ts_df and (now - ts_df[0] <= KLINES_CACHE_TTL): return ts_df[1]
    _sleep(REQ_SLEEP_SEC)
    r = http_get(f"{_fapi_base()}/fapi/v1/klines", params={"symbol":symbol,"interval":interval,"limit":limit})
    if not r or r.status_code != 200: return None
    arr = r.json()
    cols = ["open_time","open","high","low","close","volume","close_time","qav","nt","tb","tq","i"]
    df = pd.DataFrame(arr, columns=cols)
    for c in ["open","high","low","close","volume"]: df[c] = df[c].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    _KLINES_CACHE[key] = (now, df)
    return df

# ================== Sinyal inşası ==================
def est_minutes_to_tp(tf, atr_val, distance):
    if atr_val <= 0: return _tf_min(tf)
    bars = max(1.0, distance/atr_val) * 1.05
    return int(round(bars * _tf_min(tf)))
def _fmt_price(x: float):
    if x >= 100: return f"{x:,.3f}".replace(","," ")
    if x >= 1: return f"{x:,.5f}".replace(","," ")
    return f"{x:.8f}".rstrip("0").rstrip(".")
def confidence_score(rr, slope_abs, rsi_now):
    base = 55
    base += min(20, max(0.0, (rr-1.3)*10))
    base += min(10, slope_abs/0.05)
    base += 4 if 45 <= rsi_now <= 65 else 0
    return int(max(0, min(100, round(base))))

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

    # son kapanan mum
    c   = df["close"].iloc[-2]
    atr_now = df["atr"].iloc[-2]
    base_now = df["ema_base"].iloc[-2]
    slope_b  = slope(df["ema_base"], 15)

    long_trend  = (c > base_now) and (slope_b >= BASE_SLOPE_MIN)
    short_trend = (c < base_now) and (slope_b <= -BASE_SLOPE_MIN)

    bars_ago, dir_cross = last_cross_bars(df["ema_fast"], df["ema_slow"])
    recent_cross_ok = (bars_ago is not None) and (bars_ago <= 25)
    wick_ok = (not WICK_FILTER) or wick_filter_ok(df, 2)
    r_now = df["rsi"].iloc[-2]

    bbm = bb_mid.iloc[-2]; bbu = bb_up.iloc[-2]; bbl = bb_lo.iloc[-2]
    not_extreme_long  = (c <= bbu)          # aşırı üst bant zorunlu değil
    not_extreme_short = (c >= bbl)

    def push(side, entry, tp, sl, rr, conf):
        min_rr   = MIN_RR_BY_TF.get(tf, 1.5)
        min_conf = MIN_CONFIDENCE_BY_TF.get(tf, 70)
        if rr >= min_rr and conf >= min_conf:
            out.append({
                "sym":sym,"tf":tf,"mode": ("Vurkaç" if tf in ["1m","5m","15m"] else "Orta Vade"),
                "side":side,"entry":entry,"tp":tp,"sl":sl,
                "rr":rr,"conf":int(conf),
                "est_min": est_minutes_to_tp(tf, atr_now, abs(tp-entry))
            })

    # LONG
    if long_trend and recent_cross_ok and dir_cross==1 and wick_ok and (r_now >= RSI_LONG_MIN) and not_extreme_long:
        entry = c; sl = entry - max(atr_now*ATR_MULT_SL, 1e-9*entry)
        tp = entry + atr_now*ATR_MULT_TP
        rr = (tp-entry)/max((entry-sl),1e-9)
        conf = confidence_score(rr, abs(slope_b), r_now)
        push("LONG", entry, tp, sl, rr, conf)

    # SHORT
    if short_trend and recent_cross_ok and dir_cross==-1 and wick_ok and (r_now <= RSI_SHORT_MAX) and not_extreme_short:
        entry = c; sl = entry + max(atr_now*ATR_MULT_SL, 1e-9*entry)
        tp = entry - atr_now*ATR_MULT_TP
        rr = (entry-tp)/max((sl-entry),1e-9)
        conf = confidence_score(rr, abs(slope_b), r_now)
        push("SHORT", entry, tp, sl, rr, conf)

    return out

# ================== MTF teyit (opsiyonel) ==================
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

# ================== Cooldown ==================
_last_sent_coin = {}  # sym -> ts
def cooldown_ok(sym):
    last = _last_sent_coin.get(sym, 0)
    return (time.time() - last) >= (COOLDOWN_MINUTES_PER_SYMBOL * 60)
def mark_sent(sym):
    _last_sent_coin[sym] = time.time()

# ================== Telegram ==================
def send_tg(text):
    token = os.getenv("TELEGRAM_TOKEN",""); chat_id = os.getenv("TELEGRAM_CHAT_ID","")
    if not token or not chat_id:
        print("[TG] TOKEN/CHAT_ID eksik."); return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
        print("[TG SEND]", r.status_code, r.text[:180])
        return r.status_code == 200
    except Exception as e:
        print("[TG EX]", e); return False

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
    return TEMPLATE_MINIMAL.format(**payload)  # orijinal format:contentReference[oaicite:9]{index=9}

# ================== Döngü ==================
def maybe_heartbeat():
    global _last_heartbeat_ts, _scanned_counter
    if (time.time() - _last_heartbeat_ts) >= HEARTBEAT_EVERY_MIN * 60:
        send_tg(heartbeat_text()); _last_heartbeat_ts = time.time(); _scanned_counter = 0
def maybe_silence_alert():
    global _last_signal_ts
    if _last_signal_ts and (time.time() - _last_signal_ts) >= SILENCE_ALERT_MIN*60:
        send_tg(f"🟡 {SILENCE_ALERT_MIN}+ dk sinyal yok."); _last_signal_ts = time.time()

def loop_once():
    global _scanned_counter, perf_sent_total, _last_signal_ts, _last_universe_ts
    # evren güncelle
    if (time.time() - _last_universe_ts) >= 120 or not _universe:
        refresh_top_futures(TOP_N)

    for sym in list(_universe):
        if not cooldown_ok(sym): continue
        for tf in TIMEFRAMES:
            _scanned_counter += 1
            df = get_klines_cached(sym, tf, limit=250)
            sigs = build_signal(df, tf, sym)
            if not sigs: continue
            sent_any = False
            for s in sigs:
                if not mtf_confirm_if_enabled(sym, tf, s["side"]): continue
                msg = render_message(s["sym"], s["tf"], s["mode"], s["side"],
                                     s["entry"], s["tp"], s["sl"], s["rr"], s["conf"], s["est_min"])
                send_tg(msg)
                perf_sent_total += 1; perf_sent_by_sym[sym] += 1; perf_rr_last.append(float(s["rr"]))
                _last_signal_ts = time.time(); sent_any = True
            if sent_any: mark_sent(sym)  # 60 dk kilit

def main_loop():
    global _last_heartbeat_ts
    print("KriptoAlper — Futures TOP-50 High-Signal başlıyor…")
    if HEARTBEAT_FORCE_ON_START:
        send_tg("✅ Heartbeat: hayattayım (Futures TOP-50)")
    send_tg("🟢 KriptoAlper (Futures TOP-50) açıldı. Tarama başladı.")
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

# Import uyumu (app.py: from scanner import main) :contentReference[oaicite:10]{index=10}
def main():
    return main_loop()

if __name__ == "__main__":
    main_loop()
