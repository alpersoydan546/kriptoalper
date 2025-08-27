# ============================ KriptoAlper — SCANNER (15 Coin • ConfBadge • Dynamic Leverage) ============================
# Gereksinimler: requests, pandas, numpy, python-dotenv
# ENV (zorunlu): TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
# ENV (opsiyonel):
#   GEO_FORCE_POOL=spot|vision|fapi   (varsayılan: spot; 451 gelirse otomatik vision→fapi fallback)
#   USE_FAPI_FALLBACK=1               (451/418 geldiğinde futures'a düş)
#   KLINES_CACHE_TTL=25               (sn)
#   REQ_SLEEP_SEC=0.22                (istek arası taban gecikme)
#   MAX_TRIES_PER_CALL=5
# Not: Auto-trade YOK; sadece sinyal gönderir.

import os, time, traceback, random, math
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

HEARTBEAT_EVERY_MIN = 15
SILENCE_ALERT_MIN = 120
STATS_EVERY_HR = 12

_last_heartbeat_ts = 0.0
_last_stats_ts = 0.0
_last_signal_ts = None
_scanned_counter = 0

perf_sent_total = 0
perf_sent_by_sym = defaultdict(int)
perf_rr_last = deque(maxlen=300)

# ================== TELEGRAM ==================
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "KriptoAlper/1.0 (+render) python-requests",
    "Accept": "application/json",
    "Connection": "keep-alive",
})

def send_tg(text):
    if not SEND_TO_TELEGRAM:
        print("[DRY-RUN]", text.replace("\n"," ")[:280]); return
    token = os.getenv("TELEGRAM_TOKEN","")
    chat_id = os.getenv("TELEGRAM_CHAT_ID","")
    if not token or not chat_id:
        print("Telegram yapılandırılmamış."); return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = SESSION.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
        print("[TG SEND]", r.status_code, r.text[:120])
    except Exception as e:
        print("[TG ERROR]", repr(e))

def _fmt_price(x: float) -> str:
    if x >= 1:   return f"{x:,.3f}".replace(","," ")
    return f"{x:.6f}".rstrip("0").rstrip(".")

# ================== Coin Listesi (15 güçlü coin) ==================
COINS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
    "ADAUSDT","AVAXUSDT","LINKUSDT","DOGEUSDT","TRXUSDT",
    "MATICUSDT","DOTUSDT","ARBUSDT","OPUSDT","RNDRUSDT"
]

# === TF’ler: 5m + 15m (scalp) ve 1h + 4h (swing) ===
TIMEFRAMES_SCALP = ["5m","15m"]
TIMEFRAMES_SWING = ["1h","4h"]
SCALP_TFS = set(TIMEFRAMES_SCALP)

# ================== Göstergeler ==================
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

# ================== Sinyal Mantığı ==================
EMA_FAST, EMA_SLOW, EMA_BASE = 12, 26, 200
RSI_LEN = 14
ATR_LEN = 14

MIN_RR_SCALP, MIN_RR_SWING = 1.30, 1.80
MIN_CONF_SCALP, MIN_CONF_SWING = 60, 80    # algoritmik kabul eşiği (gönderim eşiği ayrıca var)
SLOPE_MIN_BY_TF = {"5m": 0.009, "15m": 0.009, "1h": 0.012, "4h": 0.015}
DEFAULT_SLOPE_MIN = 0.009

ATR_MULT_SL, ATR_MULT_TP = 1.05, 2.0

# >>> Telegram'a gönderim minimumu
MIN_SEND_CONF = 60

def confidence_score(rr, slope_abs, rsi_now, tf):
    base = 50
    base += min(15, max(0.0, (rr-1.2)*10))
    base += max(0, min(10, slope_abs/0.05))
    if 45 <= rsi_now <= 62: base += 3
    if tf in SCALP_TFS: base += 2
    return int(max(0, min(100, round(base))))

# ====== Trafik ışığı ve Dinamik Kaldıraç ======
def conf_badge(conf:int) -> str:
    if conf >= 85: return "🟢"
    if conf >= 75: return "🟡"
    return "🔴"

def choose_leverage(conf:int, tf:str) -> int | None:
    if conf < 75: return None
    if conf < 85: return 12 if tf in SCALP_TFS else 10
    if conf < 90: return 18 if tf in SCALP_TFS else 12
    return 25 if tf in SCALP_TFS else 15

# ================== Mesaj Formatı ==================
def fmt_signal_card(parite, tf, yon, price, tp, sl, rr, conf, slope_val, slope_min, lev, est_min):
    badge = conf_badge(conf)
    header = f"{'📈' if yon=='LONG' else '📉'} {parite} {tf} {yon} {badge}"
    conf_line = f"🧠 Güven: {conf}/100 {badge}"
    if lev is None:
        lev_line = f"⚠️ Güven düşük (kaldıraç yok)"
    else:
        lev_line = f"⚡ Kaldıraç: {lev}x"
    return (
        f"{header}\n"
        f"💵 Giriş: {_fmt_price(price)}\n"
        f"🎯 TP: {_fmt_price(tp)}\n"
        f"🛑 SL: {_fmt_price(sl)}\n"
        f"⚖️ R:R {rr:.2f}\n"
        f"{conf_line}\n"
        f"📐 Slope {slope_val:.3f} / min {slope_min:.3f}\n"
        f"{lev_line} | ⏳ ~{est_min} dk"
    )

# ================== (YENİ) Gerçekçi Süre Tahmini ==================
TF_MINUTES = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}

def estimate_minutes(entry: float, tp: float, atr_now: float, tf: str) -> int:
    """
    Hedef mesafesini ATR'e böler → yaklaşık kaç bar gerekir?
    TF dakikası ile çarp → süre (dk). Volatilite artarsa süre kısalır.
    """
    dist = abs(tp - entry)
    bars = max(1.0, dist / max(atr_now, 1e-9))
    return int(math.ceil(bars * TF_MINUTES.get(tf, 5)))

# ================== Ağ Katmanı (Anti 451/429) ==================
SPOT_HOSTS  = ["https://api.binance.com","https://api1.binance.com","https://api2.binance.com","https://api3.binance.com","https://api4.binance.com","https://api-gcp.binance.com","https://data-api.binance.vision","https://api.binance.vision"]
FAPI_HOSTS  = ["https://fapi.binance.com","https://fapi1.binance.com","https://fapi2.binance.com","https://fapi3.binance.com"]
POOL_CURSOR = {"spot":0, "fapi":0}
GEO_FORCE_POOL = os.getenv("GEO_FORCE_POOL","spot").lower()   # spot|vision|fapi
USE_FAPI_FALLBACK = os.getenv("USE_FAPI_FALLBACK","1") == "1"
REQ_SLEEP_SEC = float(os.getenv("REQ_SLEEP_SEC","0.22"))
MAX_TRIES_PER_CALL = int(os.getenv("MAX_TRIES_PER_CALL","5"))
CACHE_TTL = int(os.getenv("KLINES_CACHE_TTL","25"))

_kl_cache = {}  # (sym,tf) -> (ts, df)

def _pick_host(pool: str) -> str:
    hosts = SPOT_HOSTS if pool=="spot" else FAPI_HOSTS
    i = POOL_CURSOR[pool] % len(hosts)
    POOL_CURSOR[pool] += 1
    return hosts[i]

def _make_url(pool: str, host: str, symbol: str, interval: str) -> str:
    if pool == "fapi":
        return f"{host}/fapi/v1/klines?symbol={symbol}&interval={interval}&limit=210"
    # spot/vision
    return f"{host}/api/v3/klines?symbol={symbol}&interval={interval}&limit=210"

def get_klines(symbol, interval="5m", limit=210):
    # ---- cache
    key = (symbol, interval)
    now = time.time()
    hit = _kl_cache.get(key)
    if hit and (now - hit[0]) <= CACHE_TTL:
        if DEBUG: print(f"[CACHE] {symbol} {interval} hit")
        return hit[1].copy()

    pools_try = []
    force = GEO_FORCE_POOL
    if force in ("vision","spot"):
        pools_try = ["spot","fapi"] if USE_FAPI_FALLBACK else ["spot"]
    elif force == "fapi":
        pools_try = ["fapi"]
    else:
        pools_try = ["spot","fapi"]

    last_err = None
    for pool in pools_try:
        tries = 0
        while tries < MAX_TRIES_PER_CALL:
            host = _pick_host(pool)
            url = _make_url(pool, host, symbol, interval)
            try:
                if DEBUG: print(f"[RATE/GEO] {symbol} {interval} via {host.split('//')[1]} ...", end="")
                r = SESSION.get(url, timeout=10)
                code = r.status_code
                if code == 200:
                    arr = r.json()
                    cols = ["open_time","open","high","low","close","volume","ct","qv","trades","tb","tq","ig"]
                    df = pd.DataFrame(arr, columns=cols)
                    for c in ["open","high","low","close","volume"]:
                        df[c] = df[c].astype(float)
                    _kl_cache[key] = (now, df)
                    if DEBUG: print(" 200")
                    time.sleep(REQ_SLEEP_SEC + random.random()*0.05)
                    return df.copy()
                elif code in (429, 418):
                    if DEBUG: print(f" {code} (rate)"); time.sleep(REQ_SLEEP_SEC*1.6 + tries*0.15)
                elif code in (451, 403):
                    if DEBUG: print(f" {code} (geo)")
                    # bu hostu bırak, diğer hosta dene
                    time.sleep(REQ_SLEEP_SEC*1.2)
                else:
                    if DEBUG: print(f" {code}")
                    time.sleep(REQ_SLEEP_SEC)
            except Exception as e:
                last_err = e
                if DEBUG: print(f" EXC {type(e).__name__}")
                time.sleep(REQ_SLEEP_SEC)
            tries += 1
        # pool bitti, bir sonrakine geç
    raise RuntimeError("klines failed") from last_err

# ================== Sinyal Üretimi ==================
def build_signal(df, tf, sym):
    df = df.copy()
    df["ema_fast"] = ema(df["close"], EMA_FAST)
    df["ema_slow"] = ema(df["close"], EMA_SLOW)
    df["ema_base"] = ema(df["close"], EMA_BASE)
    df["rsi"] = rsi(df["close"], RSI_LEN)
    df["atr"] = atr(df, ATR_LEN)

    c = float(df["close"].iloc[-1])
    base_val = float(df["ema_base"].iloc[-1])
    slope_b = float(slope(df["ema_base"], 15))
    r_now = float(df["rsi"].iloc[-1])
    atr_now = float(df["atr"].iloc[-1])

    slope_min = SLOPE_MIN_BY_TF.get(tf, DEFAULT_SLOPE_MIN)

    long_trend  = (c > base_val) and (slope_b >=  slope_min)
    short_trend = (c < base_val) and (slope_b <= -slope_min)

    out = []
    def push(side, entry, tp, sl, reason_ok):
        rr = (tp-entry)/max((entry-sl),1e-9) if side=="LONG" else (entry-tp)/max((sl-entry),1e-9)
        conf = confidence_score(rr, abs(slope_b), r_now, tf)
        rr_min   = MIN_RR_SCALP if tf in SCALP_TFS else MIN_RR_SWING
        conf_min = MIN_CONF_SCALP if tf in SCALP_TFS else MIN_CONF_SWING

        if rr >= rr_min and conf >= conf_min:
            lev = choose_leverage(conf, tf)
            out.append({
                "sym":sym,"tf":tf,"side":side,"entry":entry,"tp":tp,"sl":sl,
                "rr":rr,"conf":conf,"lev": lev,
                # >>> (YENİ) ATR+TF bazlı süre:
                "est_min": estimate_minutes(entry, tp, atr_now, tf),
                "slope_b":slope_b,"slope_min":slope_min
            })
            if DEBUG: print(f"[SCAN] {sym} {tf} {side} rr={rr:.2f} conf={conf} lev={lev} {reason_ok}")
        else:
            if PRINT_REASONS:
                print(f"[INFO] Yakın sinyal: {sym} {tf} rr={rr:.2f} conf={conf} slope={slope_b:.3f}/{slope_min:.3f}")

    if long_trend and r_now >= 42:
        push("LONG", c, c+atr_now*ATR_MULT_TP, c-atr_now*ATR_MULT_SL,
             f"slope {slope_b:.3f}>=min {slope_min:.3f} rsi {r_now:.1f}")
    else:
        if PRINT_REASONS:
            print(f"[REJECT] {sym} {tf} LONG trend yok | slope {slope_b:.3f}/min {slope_min:.3f} rsi {r_now:.1f}")

    if short_trend and r_now <= 58:
        push("SHORT", c, c-atr_now*ATR_MULT_TP, c+atr_now*ATR_MULT_SL,
             f"slope {slope_b:.3f}<=-min {-slope_min:.3f} rsi {r_now:.1f}")
    else:
        if PRINT_REASONS:
            print(f"[REJECT] {sym} {tf} SHORT trend yok | slope {slope_b:.3f}/-min {-slope_min:.3f} rsi {r_now:.1f}")

    return out

# ================== Cooldown ==================
_last_sent = {}
_last_side_sent = {}
COOLDOWN_BY_TF_MIN = {"5m": 90, "15m": 150, "1h": 300, "4h": 360}
GLOBAL_SIDE_COOLDOWN_MIN = 150

def cooldown_ok(sym, tf, side):
    now = time.time()
    cd_tf = COOLDOWN_BY_TF_MIN.get(tf,180)*60
    if (now - _last_sent.get((sym, tf, side), 0)) <= cd_tf:
        if PRINT_REASONS: print(f"[COOLDOWN] {sym} {tf} {side} tf_cd")
        return False
    cd_global = GLOBAL_SIDE_COOLDOWN_MIN*60
    if (now - _last_side_sent.get((sym, side), 0)) <= cd_global:
        if PRINT_REASONS: print(f"[COOLDOWN] {sym} * {side} global_cd")
        return False
    return True

def mark_sent(sym, tf, side):
    ts = time.time()
    _last_sent[(sym, tf, side)] = ts
    _last_side_sent[(sym, side)] = ts

# ================== Döngü ==================
def loop_once():
    global _scanned_counter, perf_sent_total, _last_signal_ts
    tf_list = TIMEFRAMES_SCALP + TIMEFRAMES_SWING
    for sym in COINS:
        for tf in tf_list:
            _scanned_counter += 1
            try:
                df = get_klines(sym, tf, limit=210)
                sigs = build_signal(df, tf, sym)
                for s in sigs:
                    side = s["side"]

                    if s["conf"] < MIN_SEND_CONF:
                        if PRINT_REASONS: print(f"[SKIP SEND] {s['sym']} {s['tf']} conf={s['conf']}<MIN_SEND_CONF")
                        continue

                    if not cooldown_ok(sym, tf, side):
                        continue

                    msg = fmt_signal_card(
                        s["sym"], s["tf"], side,
                        s["entry"], s["tp"], s["sl"],
                        s["rr"], s["conf"], s["slope_b"], s["slope_min"],
                        s["lev"], s["est_min"]
                    )
                    send_tg(msg)
                    mark_sent(sym, tf, side)
                    perf_sent_total += 1
                    perf_sent_by_sym[sym] += 1
                    perf_rr_last.append(float(s["rr"]))
                    _last_signal_ts = time.time()
            except RuntimeError as e:
                # klines failed — geofence/rate sorunları; akışı durdurma.
                print(f"[ERROR] {sym} {tf} -> {e}")
            except Exception as e:
                print(f"[ERROR] {sym} {tf} -> {repr(e)}")

def maybe_heartbeat():
    global _last_heartbeat_ts, _scanned_counter
    now = time.time()
    if (now - _last_heartbeat_ts) >= HEARTBEAT_EVERY_MIN*60:
        send_tg(f"✅ Bot aktif\n⏳ {_scanned_counter} tarama | 📊 {perf_sent_total} sinyal")
        _last_heartbeat_ts = now
        _scanned_counter = 0

def maybe_silence_alert():
    global _last_signal_ts
    if _last_signal_ts and (time.time() - _last_signal_ts) >= SILENCE_ALERT_MIN*60:
        send_tg(f"⚠️ {SILENCE_ALERT_MIN}+ dk sinyal yok")
        _last_signal_ts = time.time()

def maybe_stats():
    global _last_stats_ts
    now = time.time()
    if (now - _last_stats_ts) >= STATS_EVERY_HR*3600:
        avg_rr = np.mean(perf_rr_last) if perf_rr_last else 0.0
        send_tg(f"📊 12h Raporu\nToplam sinyal: {perf_sent_total}\nOrtalama R:R: {avg_rr:.2f}")
        _last_stats_ts = now

def main():
    global _last_heartbeat_ts, _last_stats_ts
    print("[BOOT] scanner VERSION: geo-rot+fallback")
    send_tg("🟢 KriptoAlper başladı.")
    _last_heartbeat_ts = time.time()
    _last_stats_ts = time.time()
    while True:
        t0 = time.time()
        try:
            loop_once()
            maybe_heartbeat()
            maybe_silence_alert()
            maybe_stats()
        except Exception as e:
            print("[LOOP ERROR]", repr(e))
            traceback.print_exc()
        dt = time.time() - t0
        time.sleep(max(1, 12 - dt))

if __name__ == "__main__":
    main()

