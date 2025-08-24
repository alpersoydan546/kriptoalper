# =============== KriptoAlper — SCANNER v6 (Futures-only • DynTop100 • noVision • JSON-hardening) ===============
# Çalıştırma: app.py içinden main() çağrılıyor. Render'da WEB_CONCURRENCY=1 olmalı.
# ENV: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
# Bağımlılıklar: requests, pandas, python-dotenv

import os, time, math, random, traceback, threading
from collections import defaultdict, deque

import pandas as pd
import requests
from requests.exceptions import RequestException, SSLError, ConnectionError, Timeout
from dotenv import load_dotenv
load_dotenv()

VERSION = "v6-fapi-hardening"
BOT_NAME = "KriptoAlper"

# ---------- EVREN ----------
USE_DYNAMIC_UNIVERSE = True
TOP_N = 100
MIN_QUOTE_VOL_USDT = 150_000_000
FALLBACK_FAVORITES = [
    "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT","DOGEUSDT","ADAUSDT","TRXUSDT","LINKUSDT","LTCUSDT",
    "MATICUSDT","DOTUSDT","AVAXUSDT","NEARUSDT","ATOMUSDT","FILUSDT","APTUSDT","ARBUSDT","OPUSDT","SUIUSDT",
    "TIAUSDT","INJUSDT","RNDRUSDT","AAVEUSDT","FTMUSDT","ARUSDT","SEIUSDT","ENSUSDT","ENAUSDT","BLURUSDT",
    "TONUSDT","ORDIUSDT","PEPEUSDT","WIFUSDT","BCHUSDT","ETCUSDT","XLMUSDT","SANDUSDT","THETAUSDT","CHRUSDT",
    "RUNEUSDT","KASUSDT","NEOUSDT","STXUSDT","IMXUSDT","DYDXUSDT","GALAUSDT","FLOWUSDT","APEUSDT","CELOUSDT"
]
STATIC_SYMBOLS = []  # sabitlemek istersen buraya yaz

# ---------- ZAMAN DİLİMLERİ ----------
INTERVAL   = "15m"
CONFIRM_TF = "1h"

# ---------- RİSK / TP-SL ----------
ATR_MULT_SL = 1.3
ATR_MULT_TP = 2.0
MIN_SEND_RR = 1.50
RISK_PER_TRADE_PCT   = 5.0
FUTURES_BALANCE_USDT = 20.0
MAX_LEVERAGE_CAP     = 10

# ---------- FİLTRELER ----------
ATRP_LOW         = 0.008
ATRP_HIGH        = 0.028
BREAK_BUFFER_ATR = 0.05
RETEST_TOL_ATR   = 0.15
VOL_BOOST_MIN    = 1.30
TAKER_LONG_MIN   = 0.55
TAKER_SHORT_MAX  = 0.45

# ---------- SKOR / KAPILAR ----------
MIN_CONF_SEND      = 70
HIGH_CONF_FOR_10X  = 80
EMASLOPE_ATR1H_EPS = 0.02

# ---------- HEARTBEAT ----------
HEARTBEAT_MIN  = 30
HEARTBEAT_TEXT = "🟢 KriptoAlper yaşıyor"

# ---------- GÜVENLİK / PACING ----------
TIMEOUT_HOURS        = 6.0
GLOBAL_SPIKE_PCT_5M  = 0.008
COOLDOWN_BARS        = 1
MAX_OPEN_SIGNALS     = 1
SCAN_INTERVAL_SEC    = 120
UNIVERSE_REFRESH_SEC = 1800
KLINES_CACHE_TTL     = 60
REQ_SLEEP_SEC        = 0.30
MAX_TRIES_PER_CALL   = 5
MSG_INCLUDE_REASONS  = 0  # 0=sade mesaj

# ---------- TELEGRAM ----------
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ---------- FUTURES ENDPOINTLER (binance-vision YOK) ----------
BINANCE_FAPI_ENDPOINTS = [
    "https://fapi.binance.com",
    "https://fapi1.binance.com",
    "https://fapi2.binance.com",
    "https://fapi3.binance.com",
]
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
})
SESSION.trust_env = True

# ---------- DURUM ----------
_exchange_info = None
_exchange_info_time = 0
_klines_cache = {}
_last_signal_bar = {}
_open_signals = {}
_history = deque(maxlen=500)
_symbol_locks_until = {}
_global_lock_until = 0
_last_hb = 0
_daily_R = 0.0
_losses_lookback = defaultdict(lambda: deque(maxlen=3))
_fapi_fail_count = 0
_last_fapi_alert = 0
_last_http_err_log = 0
lock = threading.RLock()

# ---------- TELEGRAM ----------
def tg_send(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TG SKIP]", text[:100]); return
    try:
        SESSION.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                     json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    except Exception as e:
        print("[TG ERR]", repr(e))

# ---------- HTTP (JSON-hardening + ayrıntılı hata logu) ----------
def _short(s: str, n: int = 180) -> str:
    return (s or "")[:n].replace("\n", " ").replace("\r", " ")

def http_get(path: str, params: dict | None = None):
    """Futures-only GET; JSON değilse/redirectse/boşsa endpoint değiştirir.
       Her denemede hata tipini ve kısa gövde snippeti loglar.
       5+ ardışık fail'de TG'ye uyarı atar (30dk'da 1 kez)."""
    global _fapi_fail_count, _last_fapi_alert, _last_http_err_log
    params = params or {}
    last_exc = None

    for base in BINANCE_FAPI_ENDPOINTS:
        url = base + path
        try:
            r = SESSION.get(url, params=params, timeout=15, allow_redirects=False)

            if r.status_code in (451, 418):
                print(f"[HTTP WARN] blocked status={r.status_code} url={url}")
                continue
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("Location", "")
                print(f"[HTTP WARN] redirect status={r.status_code} url={url} -> {loc}")
                raise ValueError("redirect to non-json")

            if r.status_code == 429:
                time.sleep(1.0 + random.random()); continue

            r.raise_for_status()

            ct = (r.headers.get("Content-Type") or "").lower()
            if "json" not in ct:
                print(f"[HTTP WARN] non-json ct='{ct}' url={url} body[:180]={_short(r.text)}")
                raise ValueError("non-json response")

            try:
                data = r.json()
            except ValueError as je:
                print(f"[HTTP WARN] json-decode-fail url={url} body[:180]={_short(r.text)}")
                raise

            time.sleep(REQ_SLEEP_SEC)
            _fapi_fail_count = 0  # success → reset
            return data

        except (Timeout, SSLError, ConnectionError, RequestException, ValueError) as e:
            last_exc = e
            now = time.time()
            if now - _last_http_err_log > 10:
                print(f"[HTTP ERR] url={url} type={type(e).__name__} msg={repr(e)}")
                _last_http_err_log = now
            time.sleep(0.35 + random.random()*0.5)
            continue

    _fapi_fail_count += 1
    if _fapi_fail_count >= 5 and (time.time() - _last_fapi_alert > 1800):
        tg_send("🔴 Binance FAPI erişilemiyor/yanıt geçersiz. Tarama beklemede (ağ engeli olası).")
        _last_fapi_alert = time.time()
    raise last_exc or RuntimeError("HTTP GET failed")

# ---------- EXCHANGE INFO / YUVARLAMA ----------
def get_exchange_info():
    global _exchange_info, _exchange_info_time
    if _exchange_info and time.time() - _exchange_info_time < 3600:
        return _exchange_info
    data = http_get("/fapi/v1/exchangeInfo")
    _exchange_info = {s["symbol"]: s for s in data.get("symbols", [])}
    _exchange_info_time = time.time()
    return _exchange_info

def _get_filter(symbol: str, ftype: str):
    info = get_exchange_info().get(symbol)
    if not info: return None
    for f in info.get("filters", []):
        if f.get("filterType") == ftype: return f
    return None

def round_price(symbol: str, price: float) -> float:
    f = _get_filter(symbol, "PRICE_FILTER")
    if not f: return float(f"{price:.6f}")
    tick = float(f.get("tickSize", 0.0001))
    return math.floor(price / tick) * tick

def round_qty(symbol: str, qty: float) -> float:
    f = _get_filter(symbol, "LOT_SIZE")
    if not f: return float(f"{qty:.6f}")
    step = float(f.get("stepSize", 0.001))
    if step <= 0: return float(f"{qty:.6f}")
    return math.floor(qty / step) * step

# ---------- VERİ ----------
def get_top_symbols_usdtm(top_n=TOP_N, min_qv=MIN_QUOTE_VOL_USDT):
    try:
        tickers = http_get("/fapi/v1/ticker/24hr")
        pairs = []
        for t in tickers:
            sym = t.get("symbol", "")
            if not sym.endswith("USDT"): 
                continue
            qv = float(t.get("quoteVolume", 0) or 0.0)
            if qv >= min_qv:
                pairs.append((sym, qv))
        pairs.sort(key=lambda x: x[1], reverse=True)
        got = [s for s, _ in pairs[:top_n]]
        if got: return got
    except Exception as e:
        print("[UNIVERSE WARN] 24hr ticker failed → FALLBACK_FAVORITES. cause:", repr(e))
    return FALLBACK_FAVORITES[:min(top_n, len(FALLBACK_FAVORITES))] if top_n > 0 else FALLBACK_FAVORITES

def get_klines(symbol: str, interval: str, limit: int = 500) -> pd.DataFrame:
    key = (symbol, interval, limit); now = time.time()
    if key in _klines_cache:
        ts, df = _klines_cache[key]
        if now - ts < KLINES_CACHE_TTL:
            return df.copy()
    raw = http_get("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    cols = ["open_time","open","high","low","close","volume","close_time","qav","trades","taker_base","taker_quote","ignore"]
    df = pd.DataFrame(raw, columns=cols)
    for c in ("open","high","low","close","volume","qav","taker_base","taker_quote"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["open_time"]  = pd.to_numeric(df["open_time"],  errors="coerce")
    df["close_time"] = pd.to_numeric(df["close_time"], errors="coerce")
    _klines_cache[key] = (now, df)
    return df.copy()

# ---------- GÖSTERGELER ----------
def ema(s: pd.Series, n: int) -> pd.Series: return s.ewm(span=n, adjust=False).mean()
def rsi(s: pd.Series, p: int = 14) -> pd.Series:
    d = s.diff(); up = d.clip(lower=0); dn = -d.clip(upper=0)
    ma_up = up.ewm(com=p-1, adjust=False).mean()
    ma_dn = dn.ewm(com=p-1, adjust=False).mean()
    rs = ma_up / (ma_dn + 1e-12)
    return 100 - (100 / (1 + rs))
def atr(h: pd.Series, l: pd.Series, c: pd.Series, p: int = 14) -> pd.Series:
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/p, adjust=False).mean()
def donchian(h: pd.Series, l: pd.Series, n: int = 20):
    up = h.rolling(n).max(); lo = l.rolling(n).min(); mid = (up + lo) / 2.0
    return up, lo, mid
def ema_slope(series: pd.Series, window=5) -> float:
    if len(series) < window + 1: return 0.0
    return float(series.iloc[-1] - series.iloc[-1-window]) / window

# ---------- REJİM & GUARD ----------
def btc_regime():
    try:
        df1h = get_klines("BTCUSDT", "1h", 320)
        close = df1h["close"]; high = df1h["high"]; low = df1h["low"]
        ema200 = ema(close, 200)
        slope = ema_slope(ema200, 5)
        atr1h = float(atr(high, low, close, 14).iloc[-1])
        thr = EMASLOPE_ATR1H_EPS * max(1e-9, atr1h)
        if slope >  thr: return "LONG"
        if slope < -thr: return "SHORT"
        return None
    except Exception as e:
        print("[REGIME WARN]", repr(e)); return None

def global_spike_guard():
    try:
        df5 = get_klines("BTCUSDT", "5m", 2)
        o = float(df5.iloc[-1]["open"]); c = float(df5.iloc[-1]["close"])
        return abs(c - o) / max(1e-9, o) >= GLOBAL_SPIKE_PCT_5M
    except Exception:
        return False

def candle_wick_ok(df: pd.DataFrame) -> bool:
    o = df.iloc[-1]["open"]; h = df.iloc[-1]["high"]; l = df.iloc[-1]["low"]; c = df.iloc[-1]["close"]
    body = abs(c - o); rng = max(1e-9, h - l)
    top = h - max(c, o); bot = min(c, o) - l
    return (top <= 0.35*rng) and (bot <= 0.35*rng) and (top <= 0.7*body) and (bot <= 0.7*body)

# ---------- SİNYAL ----------
def build_signal(symbol: str, df15: pd.DataFrame, df1h: pd.DataFrame, allowed_side: str | None):
    close=df15["close"]; high=df15["high"]; low=df15["low"]; vol=df15["volume"]; qav=df15["qav"]
    ema200_15=ema(close,200); ema12=ema(close,12); ema26=ema(close,26); rsi15=rsi(close,14)
    atr15=atr(high,low,close,14); up20,lo20,_=donchian(high,low,20)

    price=float(close.iloc[-1]); atrv=float(atr15.iloc[-1])
    ema200v=float(ema200_15.iloc[-1]); ema12v=float(ema12.iloc[-1]); ema26v=float(ema26.iloc[-1])
    rsi_v=float(rsi15.iloc[-1]); up_v=float(up20.iloc[-1]); lo_v=float(lo20.iloc[-1])
    atrp=atrv/max(1e-9, price)

    if not candle_wick_ok(df15): return None
    if not (ATRP_LOW <= atrp <= ATRP_HIGH): return None

    med_vol=float(vol.tail(20).median())
    if med_vol>0 and float(vol.iloc[-1]) < VOL_BOOST_MIN*med_vol: return None

    taker_ratio = None
    try:
        tq = float(df15.iloc[-1]["taker_quote"]); total_q = float(qav.iloc[-1])
        if total_q > 0 and not math.isnan(tq): taker_ratio = tq / total_q
    except Exception:
        taker_ratio = None

    long_break  = (price >= up_v + BREAK_BUFFER_ATR*atrv) and (price > ema200v) and (ema12v > ema26v) and (52 <= rsi_v <= 68) and ((taker_ratio is None) or (taker_ratio >= TAKER_LONG_MIN))
    short_break = (price <= lo_v - BREAK_BUFFER_ATR*atrv) and (price < ema200v) and (ema12v < ema26v) and (32 <= rsi_v <= 48) and ((taker_ratio is None) or (taker_ratio <= TAKER_SHORT_MAX))
    side = "LONG" if long_break else ("SHORT" if short_break else None)
    if side is None or allowed_side is None or side != allowed_side:
        return None

    if side == "LONG" and price < ema200v + 0.2*atrv: return None
    if side == "SHORT" and price > ema200v - 0.2*atrv: return None

    tol=RETEST_TOL_ATR*atrv; recent=df15.tail(3)
    if side=="LONG":
        if not ((recent["low"]<=up_v+tol).any() and price>=float(df15["open"].iloc[-1])): return None
    else:
        if not ((recent["high"]>=lo_v-tol).any() and price<=float(df15["open"].iloc[-1])): return None

    cl1h=df1h["close"]; ema200_1h=ema(cl1h,200); rsi1h=rsi(cl1h,14)
    slope1h=ema_slope(ema200_1h,5); rsi1h_v=float(rsi1h.iloc[-1])
    if side=="LONG"  and not (slope1h>0 and rsi1h_v>=50): return None
    if side=="SHORT" and not (slope1h<0 and rsi1h_v<=50): return None

    swing_low=float(low.tail(5).min()); swing_high=float(high.tail(5).max())
    if side=="LONG":
        sl=min(price-ATR_MULT_SL*atrv, swing_low-0.1*atrv); tp=price+ATR_MULT_TP*atrv
    else:
        sl=max(price+ATR_MULT_SL*atrv, swing_high+0.1*atrv); tp=price-ATR_MULT_TP*atrv
    entry=round_price(symbol, price); sl=round_price(symbol, sl); tp=round_price(symbol, tp)

    stop_dist=abs(entry-sl)
    rr = abs((tp-entry)/max(1e-9,(entry-sl))) if side=="LONG" else abs((entry-tp)/max(1e-9,(sl-entry)))
    if rr < MIN_SEND_RR: return None

    bars_to_tp=max(1,int(math.ceil(abs(tp-entry)/max(1e-9,atrv)))); est_min=bars_to_tp*15

    conf=0
    conf+=25  # 1h teyit
    conf+=15  # 15m EMA200 ayrışma
    conf+=15  # hacim patlaması
    breakout_strength=abs((price - (up_v if side=="LONG" else lo_v)) / max(1e-9, atrv))
    conf+=min(15, int(7 + 4*breakout_strength))
    conf+=10  # retest
    conf+=10  # wick
    conf+=10 if ((52<=rsi_v<=68 and side=="LONG") or (32<=rsi_v<=48 and side=="SHORT")) else 5
    conf=min(100, conf)
    if conf < MIN_CONF_SEND: return None

    qty = round_qty(symbol, ((RISK_PER_TRADE_PCT/100.0) * FUTURES_BALANCE_USDT) / max(1e-9, stop_dist))
    notional = qty * entry
    lev_bucket = 10 if (conf >= HIGH_CONF_FOR_10X and (atrv/max(1e-9,price)) <= 0.012) else 5

    return {
        "symbol": symbol, "side": side, "tf": INTERVAL,
        "entry": float(entry), "tp": float(tp), "sl": float(sl),
        "rr": float(rr), "eta_min": int(est_min),
        "confidence": int(conf), "lev_bucket": int(lev_bucket)
    }

# ---------- MESAJ (Final format) ----------
def fmt_signal_msg(sig: dict) -> str:
    line1 = f"⚡ {sig['symbol']} {sig['side']} • {sig.get('tf', INTERVAL)}"
    line2 = f"💰 Giriş {sig['entry']}  🎯 TP {sig['tp']}  🛡️ SL {sig['sl']}  ⚖️ R:R {sig['rr']:.2f}  🔧 {sig['lev_bucket']}x  ⏱️ ~{sig['eta_min']} dk  📈 {sig['confidence']}/100"
    return f"{line1}\n{line2}"

# ---------- TARAMA ----------
def scan_once(symbols, allowed_side):
    if global_spike_guard(): time.sleep(2); return
    sent = 0
    for sym in symbols:
        if time.time() < _symbol_locks_until.get(sym, 0): continue
        try:
            df15 = get_klines(sym, INTERVAL, 320)
            df1h = get_klines(sym, CONFIRM_TF, 320)
            if len(df15) < 210 or len(df1h) < 210: continue

            last_bar_open = int(df15.iloc[-1]["open_time"])
            if _last_signal_bar.get(sym, 0) >= last_bar_open and COOLDOWN_BARS > 0: continue
            if len(_open_signals) >= MAX_OPEN_SIGNALS: break

            allowed = allowed_side  # LONG/SHORT/None
            sig = build_signal(sym, df15, df1h, allowed)
            if not sig: continue

            tg_send(fmt_signal_msg(sig))
            _last_signal_bar[sym] = last_bar_open
            _open_signals[f"{sym}:{int(time.time())}"] = {
                "symbol": sym, "side": sig["side"], "entry": sig["entry"],
                "tp": sig["tp"], "sl": sig["sl"], "tf": sig.get("tf", INTERVAL),
                "t_open": time.time(), "rr": sig["rr"]
            }
            sent += 1
        except Exception as e:
            print("[SCAN ERR]", sym, repr(e)); continue
    if sent: print(f"[SCAN] sent {sent} signals")

# ---------- TRACKER ----------
def poll_price(symbol: str):
    try:
        df = get_klines(symbol, "1m", 2)
        return float(df.iloc[-1]["close"])
    except Exception:
        return None

def tracker_loop():
    global _daily_R, _global_lock_until
    timeout_sec = int(TIMEOUT_HOURS * 3600)
    DAY = 24*3600; last_reset = int(time.time())//DAY
    while True:
        try:
            cur_day = int(time.time())//DAY
            if cur_day != last_reset:
                _daily_R = 0.0
                last_reset = cur_day

            for key, pos in list(_open_signals.items()):
                sym = pos["symbol"]; side = pos["side"]; tp = pos["tp"]; sl = pos["sl"]; t_open = pos["t_open"]
                pr = poll_price(sym)
                if pr is None: continue
                win = (pr >= tp) if side=="LONG" else (pr <= tp)
                loss = (pr <= sl) if side=="LONG" else (pr >= sl)
                elapsed = time.time() - t_open
                if win or loss or elapsed >= timeout_sec:
                    result = "TP" if win else ("SL" if loss else "TIMEOUT")
                    rr = pos.get("rr", 1.0)
                    _history.append({"symbol": sym, "side": side, "result": result,
                                     "rr": (rr if win else -1.0), "duration_min": int(elapsed/60),
                                     "t_close": time.time()})
                    _open_signals.pop(key, None)

                    if result == "SL":
                        _daily_R -= 1.0
                        dq = _losses_lookback[sym]; dq.append(time.time())
                        if len(dq) >= 2 and (dq[-1]-dq[-2]) <= 90*60:
                            _symbol_locks_until[sym] = time.time() + 4*3600
                    elif result == "TP":
                        _daily_R += rr

                    if _daily_R <= -3.0:
                        t = time.localtime()
                        secs = (24 - t.tm_hour - 1)*3600 + (60 - t.tm_min - 1)*60 + (60 - t.tm_sec)
                        _global_lock_until = time.time() + secs
            time.sleep(6)
        except Exception as e:
            print("[TRACKER ERR]", repr(e)); time.sleep(3)

# ---------- HEARTBEAT ----------
def heartbeat_loop():
    global _last_hb
    while True:
        try:
            if HEARTBEAT_MIN>0 and time.time()-_last_hb >= HEARTBEAT_MIN*60:
                tg_send(HEARTBEAT_TEXT); _last_hb = time.time()
            time.sleep(5)
        except Exception as e:
            print("[HB ERR]", repr(e)); time.sleep(3)

# ---------- FAPI PING MONITOR ----------
def fapi_monitor_loop():
    fail = 0
    while True:
        try:
            http_get("/fapi/v1/ping")
            if fail > 0:
                print("[FAPI OK] ping recovered")
            fail = 0
        except Exception as e:
            fail += 1
            print("[FAPI DOWN]", repr(e))
        time.sleep(60)

# ---------- MAIN ----------
def main():
    print(f"[{BOT_NAME}] scanner started. build={VERSION} endpoints={BINANCE_FAPI_ENDPOINTS} dyn={USE_DYNAMIC_UNIVERSE} TOP_N={TOP_N}")
    try:
        http_get("/fapi/v1/ping")
    except Exception as e:
        print("[PING WARN]", repr(e))

    threading.Thread(target=tracker_loop, daemon=True).start()
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    threading.Thread(target=fapi_monitor_loop, daemon=True).start()

    last_universe_ts = 0
    symbols = STATIC_SYMBOLS[:] if STATIC_SYMBOLS else (FALLBACK_FAVORITES[:] if not USE_DYNAMIC_UNIVERSE else [])

    while True:
        try:
            if time.time() < _global_lock_until:
                time.sleep(5); continue

            if STATIC_SYMBOLS:
                symbols = STATIC_SYMBOLS[:]
            elif USE_DYNAMIC_UNIVERSE and (time.time()-last_universe_ts > UNIVERSE_REFRESH_SEC or not symbols):
                try:
                    symbols = get_top_symbols_usdtm(TOP_N, MIN_QUOTE_VOL_USDT)
                except Exception as e:
                    print("[UNIVERSE ERR]", repr(e))
                    symbols = FALLBACK_FAVORITES[:min(TOP_N, len(FALLBACK_FAVORITES))]
                last_universe_ts = time.time()

            allowed_side = btc_regime()
            if allowed_side is None: time.sleep(5); continue

            scan_once(symbols, allowed_side)
            time.sleep(SCAN_INTERVAL_SEC)
        except KeyboardInterrupt:
            print("[STOP] keyboard interrupt"); break
        except Exception as e:
            print("[LOOP ERR]", repr(e)); traceback.print_exc(); time.sleep(3)

if __name__ == "__main__":
    main()
