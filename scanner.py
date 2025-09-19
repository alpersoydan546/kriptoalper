
# ====================== KriptoAlper — SCANNER (Gevşek ama Güvenli, MANUEL — Auto-Trade YOK) ======================
# Gereksinimler: requests, pandas, numpy, python-dotenv
# ENV: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
# Opsiyonel: HEARTBEAT_MIN=60, HEARTBEAT_FORCE=1, USE_FAPI_24H=1
# ================================================================================================================
import os, time, sys, traceback
from collections import defaultdict, deque
from urllib.parse import urlencode

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

HEARTBEAT_EVERY_MIN = int(os.getenv("HEARTBEAT_MIN", "60"))
HEARTBEAT_FORCE_ON_START = os.getenv("HEARTBEAT_FORCE", "0") == "1"
SILENCE_ALERT_MIN = 120
HEARTBEAT_SUMMARY = True

_last_heartbeat_ts = 0.0
_last_signal_ts = None
_scanned_counter = 0

perf_sent_total = 0
perf_sent_by_sym = defaultdict(int)
perf_rr_last = deque(maxlen=200)

# ================== EVREN / ZAMAN DİLİMLERİ ==================
TIMEFRAMES_SCALP = ["1m","5m","15m"]
TIMEFRAMES_SWING = ["1h","4h"]
ENABLE_SCALP = True
ENABLE_SWING = True

# MTF teyit (gevşek): listelenen HTF'lerden en az (len-1) aynı yönde olsun
MTF_CONFIRM_RELAXED = {
    "1m":  ["5m","15m"],
    "5m":  ["15m","1h"],
    "15m": ["1h","4h"],
    "1h":  ["4h"],
    "4h":  []
}

# “En güvenilir 30” (majörler)
SYMBOLS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","DOGEUSDT","TONUSDT","AVAXUSDT",
    "LINKUSDT","MATICUSDT","LTCUSDT","NEARUSDT","ADAUSDT","DOTUSDT","TRXUSDT","APTUSDT",
    "ARBUSDT","OPUSDT","SUIUSDT","ATOMUSDT","INJUSDT","RUNEUSDT","AAVEUSDT","UNIUSDT",
    "FILUSDT","ETCUSDT","XLMUSDT","ALGOUSDT","FTMUSDT","ICPUSDT"
]

# ================== FİLTRE AYARLARI (gevşek ama güvenli) ==================
EMA_FAST = 12
EMA_SLOW = 26
EMA_BASE = 200
BASE_SLOPE_MIN = 0.02     # %2

RSI_LEN = 14
RSI_LONG_MIN = 45
RSI_SHORT_MAX = 55

ATR_LEN = 14
ATR_MULT_SL = 1.25
ATR_MULT_TP = 2.80
MIN_RR_BY_TF = {"1m":2.0,"5m":2.1,"15m":2.2,"1h":2.3,"4h":2.4}
MIN_CONFIDENCE_BY_TF = {"1m":85,"5m":86,"15m":88,"1h":90,"4h":92}

WICK_FILTER = True
WICK_BODY_MAX = 0.45
BB_LEN = 20

# Hacim (24h USDT)
MIN_24H_USDT_VOL = 30_000_000

# Cooldown (aynı Parite, TF, Yön) + global yön kilidi → 6 saat
COOLDOWN_BY_TF_MIN = {"1m":360,"5m":360,"15m":360,"1h":360,"4h":360}
GLOBAL_SIDE_COOLDOWN_MIN = 360

# ================== RATE LIMIT / CACHE / HOST ROTASYONU ==================
REQ_SLEEP_SEC = 0.18
BATCH_PAUSE_EVERY = 12
BATCH_PAUSE_SEC = 0.6
KLINES_CACHE_TTL = 20

MAX_RETRY_429 = 3
BACKOFF_BASE = 0.8

SPOT_BASES = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
]
_spot_idx = 0

USE_FAPI_24H = os.getenv("USE_FAPI_24H","0") == "1"
VOL_CACHE_TTL = 300
_VOL_SNAPSHOT = {"ts": 0, "map": {}}
_VOL_BACKOFF_TS = 0.0

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": f"{BOT_NAME}/manual-only-1.0"})

# --- Telegram Mesaj Şablonu (ORİJİNAL DOSYADAKİ FORMAT) ---
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

# ================== yardımcılar ==================
def _sleep(s):
    try: time.sleep(s)
    except: pass

def http_get(url, params=None, timeout=10):
    global _spot_idx
    for i in range(MAX_RETRY_429 + 1):
        r = SESSION.get(url, params=params, timeout=timeout)
        if r.status_code == 429:
            wait = (BACKOFF_BASE ** i) + 0.5
            print(f"[HTTP429] {url} backoff {wait:.2f}s"); _sleep(wait); continue
        if r.status_code in (418,451):
            print(f"[HTTP {r.status_code}] rotate host"); _spot_idx += 1
        return r

def _spot_base():
    global _spot_idx
    return SPOT_BASES[_spot_idx % len(SPOT_BASES)]

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

def wick_filter_ok(df, lookback=3):
    if len(df) < lookback+1: return True
    for i in range(1, lookback+1):
        o = df["open"].iloc[-i]; c = df["close"].iloc[-i]
        h = df["high"].iloc[-i]; l = df["low"].iloc[-i]
        body = abs(c-o); upper = h - max(o,c); lower = min(o,c) - l
        wick = upper + lower; body = body if body!=0 else 1e-9
        if (wick/body) > WICK_BODY_MAX: return False
    return True

def _tf_minutes(tf: str) -> int:
    return {"1m":1,"3m":3,"5m":5,"15m":15,"30m":30,"1h":60,"2h":120,"4h":240}.get(tf, 15)

def est_minutes_to_tp(tf, atr_val, distance):
    if atr_val <= 0: return _tf_minutes(tf)
    bars = max(1.0, distance/atr_val) * 1.1
    return int(round(bars * _tf_minutes(tf)))

def _fmt_price(x: float) -> str:
    if x >= 100: return f"{x:,.3f}".replace(","," ")
    if x >= 1:   return f"{x:,.3f}".replace(","," ")
    return f"{x:.6f}".rstrip("0").rstrip(".")

def confidence_score(rr, slope_abs, rsi_now):
    base = 55
    base += min(18, max(0.0, (rr-1.5)*10))
    base += max(0, min(8, slope_abs/0.05))
    base += 4 if (45 <= rsi_now <= 65) else 0
    return int(max(0, min(100, round(base))))

# ================== Telegram ==================
def send_tg(text):
    if not SEND_TO_TELEGRAM:
        print("[DRY-RUN]", text.replace("\n"," ")[:180]); return
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
    return TEMPLATE_MINIMAL.format(**payload)

# ================== 24H Hacim (418/451 safe) ==================
def refresh_24h_bulk():
    global _VOL_SNAPSHOT, _VOL_BACKOFF_TS, _spot_idx
    if time.time() < _VOL_BACKOFF_TS: return
    path = "/api/v3/ticker/24hr" if not USE_FAPI_24H else "/fapi/v1/ticker/24hr"
    url = f"{_spot_base()}{path}"
    r = http_get(url, params=None, timeout=12)
    if r.status_code in (418, 451):
        _spot_idx += 1
        _VOL_BACKOFF_TS = time.time() + 90
        raise requests.exceptions.HTTPError(f"binance ban {r.status_code}")
    r.raise_for_status()
    arr = r.json()
    mp = {}
    for it in arr:
        sym = it.get("symbol", "")
        qv = float(it.get("quoteVolume", 0.0) or 0.0)
        mp[sym] = qv
    _VOL_SNAPSHOT = {"ts": time.time(), "map": mp}

def get_24h(symbol):
    if (time.time() - _VOL_SNAPSHOT["ts"] > VOL_CACHE_TTL) or not _VOL_SNAPSHOT["map"]:
        try:
            refresh_24h_bulk()
        except Exception as e:
            print("[VOL] bulk refresh hata; majörleri serbest:", e)
            if not _VOL_SNAPSHOT["map"]:
                return float("inf") if symbol in SYMBOLS else 0.0
    return float(_VOL_SNAPSHOT["map"].get(symbol, 0.0))

# ================== KLINES CACHE + PACING ==================
_KLINES_CACHE = {}  # (symbol, interval) -> (ts, df)

def get_klines_cached(symbol, interval, limit=210):
    key = (symbol, interval)
    ts_df = _KLINES_CACHE.get(key)
    now = time.time()
    if ts_df and (now - ts_df[0] <= KLINES_CACHE_TTL):
        return ts_df[1]

    _sleep(REQ_SLEEP_SEC)
    url = f"{_spot_base()}/api/v3/klines"
    r = http_get(url, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=10)
    r.raise_for_status()
    arr = r.json()
    cols = ["open_time","open","high","low","close","volume","close_time",
            "quote_asset_volume","number_of_trades","taker_buy_base","taker_buy_quote","ignore"]
    df = pd.DataFrame(arr, columns=cols)
    for c in ["open","high","low","close","volume"]:
        df[c] = df[c].astype(float)
    df["open_time"]  = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    _KLINES_CACHE[key] = (now, df)
    return df

# ================== SİNYAL İNŞASI ==================
def build_signal(df, tf, sym):
    df = df.copy()
    df["ema_fast"] = ema(df["close"], EMA_FAST)
    df["ema_slow"] = ema(df["close"], EMA_SLOW)
    df["ema_base"] = ema(df["close"], EMA_BASE)
    df["rsi"] = rsi(df["close"], RSI_LEN)
    df["atr"] = atr(df, ATR_LEN)
    bb_mid, bb_up, bb_lo = bollinger_mid(df["close"], BB_LEN)

    c = df["close"].iloc[-1]; atr_now = df["atr"].iloc[-1]
    base_now = df["ema_base"].iloc[-1]; slope_base = slope(df["ema_base"], 15)
    long_trend  = (c > base_now) and (slope_base >= BASE_SLOPE_MIN)
    short_trend = (c < base_now) and (slope_base <= -BASE_SLOPE_MIN)

    bars_ago, dir_cross = last_cross_bars(df["ema_fast"], df["ema_slow"])
    recent_cross_ok = (bars_ago is not None) and (bars_ago >= 2) and (bars_ago <= 14)
    wick_ok = (not WICK_FILTER) or wick_filter_ok(df, 3)
    r_now = df["rsi"].iloc[-1]

    rsi_ok_long  = (r_now >= RSI_LONG_MIN)
    rsi_ok_short = (r_now <= RSI_SHORT_MAX)

    bbm = bb_mid.iloc[-1]; bbu = bb_up.iloc[-1]; bbl = bb_lo.iloc[-1]
    not_extreme_long  = (c <= bbu - 0.4*atr_now)
    not_extreme_short = (c >= bbl + 0.4*atr_now)
    above_mid = c > bbm
    below_mid = c < bbm

    out = []
    def push(side, entry, tp, sl, rr, conf):
        min_rr   = MIN_RR_BY_TF.get(tf, 2.0)
        min_conf = MIN_CONFIDENCE_BY_TF.get(tf, 85)
        if rr >= min_rr and conf >= min_conf:
            out.append({
                "sym":sym,"tf":tf,"mode": ("Vurkaç" if tf in ["1m","5m","15m"] else ("Orta Vade" if tf=="1h" else "Uzun Vade")),
                "side":side,"entry":entry,"tp":tp,"sl":sl,
                "rr":rr,"conf":conf,
                "ttl_min": {"1m":25,"5m":40,"15m":75,"1h":210,"4h":420}.get(tf, 60),
                "est_min": est_minutes_to_tp(tf, atr_now, abs(tp-entry))
            })

    # LONG
    if long_trend and recent_cross_ok and dir_cross==1 and wick_ok and rsi_ok_long and above_mid and not_extreme_long:
        entry = c; sl = entry - max(atr_now*ATR_MULT_SL, 1e-6*entry)
        tp = entry + atr_now*ATR_MULT_TP
        rr = (tp-entry)/max((entry-sl),1e-9)
        conf = confidence_score(rr, abs(slope_base), r_now)
        push("LONG", entry, tp, sl, rr, conf)

    # SHORT
    if short_trend and recent_cross_ok and dir_cross==-1 and wick_ok and rsi_ok_short and below_mid and not_extreme_short:
        entry = c; sl = entry + max(atr_now*ATR_MULT_SL, 1e-6*entry)
        tp = entry - atr_now*ATR_MULT_TP
        rr = (entry-tp)/max((sl-entry),1e-9)
        conf = confidence_score(rr, abs(slope_base), r_now)
        push("SHORT", entry, tp, sl, rr, conf)

    if DEBUG and not out:
        print(f"[NO-SIGNAL] {sym} {tf} — gevşek modda bile elendi.")
    return out

# ================== MTF teyit (gevşek) ==================
def mtf_confirm_relaxed(sym, tf, side):
    req = MTF_CONFIRM_RELAXED.get(tf, [])
    if not req: return True
    needed = max(1, len(req) - 1)
    ok = 0
    for htf in req:
        try:
            dfh = get_klines_cached(sym, htf, limit=210)
            price = dfh["close"].iloc[-1]
            ema_b = ema(dfh["close"], EMA_BASE).iloc[-1]
            s_b   = slope(ema(dfh["close"], EMA_BASE), 15)
            if side=="LONG" and (price>ema_b and s_b>=BASE_SLOPE_MIN): ok += 1
            if side=="SHORT" and (price<ema_b and s_b<=-BASE_SLOPE_MIN): ok += 1
        except Exception:
            pass
    return ok >= needed

# ================== Cooldown ==================
_last_sent = {}           # (sym, tf, side) -> ts
_last_side_sent = {}      # (sym, side)      -> ts

def cooldown_ok(sym, tf, side):
    now = time.time()
    cd_tf = COOLDOWN_BY_TF_MIN.get(tf, 360)*60
    if (now - _last_sent.get((sym, tf, side), 0)) <= cd_tf: return False
    cd_global = GLOBAL_SIDE_COOLDOWN_MIN*60
    if (now - _last_side_sent.get((sym, side), 0)) <= cd_global: return False
    return True

def mark_sent(sym, tf, side):
    ts = time.time()
    _last_sent[(sym, tf, side)] = ts
    _last_side_sent[(sym, side)] = ts

# ================== HEARTBEAT / RAPOR ==================
def _fmt_min(seconds): return int(round(seconds/60))

def heartbeat_text():
    parts = ["🟢 KriptoAlper (Gevşek ama Güvenli — MANUEL) çalışıyor."]
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
        send_tg(f"🟡 {SILENCE_ALERT_MIN}+ dk sinyal yok (Gevşek mod).")
        _last_signal_ts = now

# ================== ANA DÖNGÜ ==================
def loop_once():
    global _scanned_counter, perf_sent_total, _last_signal_ts

    tf_list = []
    if ENABLE_SCALP: tf_list += TIMEFRAMES_SCALP
    if ENABLE_SWING: tf_list += TIMEFRAMES_SWING

    for idx, sym in enumerate(SYMBOLS):
        if idx % BATCH_PAUSE_EVERY == 0 and idx > 0: _sleep(BATCH_PAUSE_SEC)

        # 24h hacim
        try:
            q = get_24h(sym)
            if q < MIN_24H_USDT_VOL:
                if PRINT_REASONS and DEBUG: print(f"[SKIP vol] {sym} düşük 24h ({q:,.0f})")
                continue
        except Exception as e:
            print("[VOL] kontrol atlandı:", e)

        for tf in tf_list:
            _scanned_counter += 1
            try:
                df = get_klines_cached(sym, tf, limit=210)
                sigs = build_signal(df, tf, sym)
                for s in sigs:
                    side = s["side"]
                    if not cooldown_ok(sym, tf, side):
                        if PRINT_REASONS and DEBUG: print(f"[SKIP cooldown] {sym} {tf} {side}")
                        continue
                    if not mtf_confirm_relaxed(sym, tf, side):
                        if PRINT_REASONS and DEBUG: print(f"[SKIP mtf] {sym} {tf} {side}")
                        continue

                    msg = render_message(
                        s["sym"], s["tf"], s["mode"], side,
                        s["entry"], s["tp"], s["sl"], s["rr"], s["conf"], s["est_min"]
                    )
                    send_tg(msg)
                    mark_sent(sym, tf, side)

                    perf_sent_total += 1
                    perf_sent_by_sym[sym] += 1
                    perf_rr_last.append(float(s["rr"]))
                    _last_signal_ts = time.time()

            except Exception as e:
                print(f"{sym} {tf} hata:", e)

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

def main():
    global _last_heartbeat_ts
    print(f"{BOT_NAME} (Gevşek ama Güvenli — MANUEL) tarama başladı…")
    print(f"ENV → TT: {os.getenv('TELEGRAM_TOKEN','')[:10]}***  CID: {os.getenv('TELEGRAM_CHAT_ID','')}")
    telegram_diag()

    if HEARTBEAT_FORCE_ON_START: send_tg("✅ Heartbeat: hayattayım (Gevşek ama Güvenli — MANUEL)")
    send_tg("🟢 KriptoAlper (Gevşek ama Güvenli — MANUEL) açıldı. Tarama başladı.")
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

if __name__ == "__main__":
    main()
