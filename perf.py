# perf.py — EOD rapor + değerlendirme + sticky sig_key desteği
import sqlite3, time

DB = sqlite3.connect("state.db", check_same_thread=False)
DB.execute("""
CREATE TABLE IF NOT EXISTS signals(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL, sym TEXT, side TEXT, tf TEXT,
  entry REAL, tp REAL, sl REAL,
  rr REAL, conf INT,
  status TEXT,     -- NEW | TP | SL | AMB | EXPIRED
  outcome_ts REAL,
  horizon_min INT,
  sig_key TEXT
)
""")
DB.commit()

# Eski tabloda sig_key yoksa ekle (idempotent)
try:
    DB.execute("ALTER TABLE signals ADD COLUMN sig_key TEXT")
    DB.commit()
except Exception:
    pass

HORIZON_MIN_DEFAULT = 240  # 4 saat izleme
EVAL_BAR_TF = "1m"
IST_OFFSET = 3 * 3600  # Europe/Istanbul UTC+3

def record_signal(sig: dict, horizon_min: int = HORIZON_MIN_DEFAULT):
    tf_joined = "/".join(sig.get("tf_list",[sig.get('tf','?')]))
    sig_key = f"{sig['sym']}|{sig['side']}|{tf_joined}|{round(float(sig['entry']),5)}|{round(float(sig['tp']),5)}|{round(float(sig['sl']),5)}"
    DB.execute("""INSERT INTO signals(ts,sym,side,tf,entry,tp,sl,rr,conf,status,outcome_ts,horizon_min,sig_key)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (time.time(), sig["sym"], sig["side"], tf_joined,
                float(sig["entry"]), float(sig["tp"]), float(sig["sl"]),
                float(sig.get("rr",0)), int(sig.get("conf",0)),
                "NEW", None, int(horizon_min), sig_key))
    DB.commit()

def _touch_order_long(row, tp, sl):
    hit_tp = row["high"] >= tp
    hit_sl = row["low"]  <= sl
    if hit_tp and not hit_sl: return "TP"
    if hit_sl and not hit_tp: return "SL"
    if hit_tp and hit_sl:     return "AMB"
    return None

def _touch_order_short(row, tp, sl):
    hit_tp = row["low"]  <= tp
    hit_sl = row["high"] >= sl
    if hit_tp and not hit_sl: return "TP"
    if hit_sl and not hit_tp: return "SL"
    if hit_tp and hit_sl:     return "AMB"
    return None

def evaluate_pending(get_klines_cached, return_closed_sigkeys: bool = False):
    """Açık sinyalleri 1m bar üzerinden değerlendirir. İstenirse kapananların sig_key listesini döndürür."""
    now = time.time()
    rows = DB.execute("""SELECT id,ts,sym,side,entry,tp,sl,horizon_min,sig_key
                         FROM signals
                         WHERE status='NEW'""").fetchall()
    if not rows:
        if return_closed_sigkeys:
            return 0,0,0,0,[]
        return 0,0,0,0

    tp_c=sl_c=amb_c=exp_c=0
    closed_keys = []

    for _id, ts, sym, side, entry, tp, sl, horizon_min, sig_key in rows:
        if now - ts > horizon_min*60:
            DB.execute("UPDATE signals SET status='EXPIRED', outcome_ts=? WHERE id=?", (now,_id))
            DB.commit()
            exp_c += 1
            if sig_key: closed_keys.append(sig_key)
            continue

        df = get_klines_cached(sym, EVAL_BAR_TF, 300)
        if df is None or len(df)==0:
            continue

        df2 = df[df["open_time"].astype("int64")/1e9 > ts]
        if df2.empty: 
            continue

        outcome = None
        for _, row in df2.iterrows():
            if side == "LONG":
                outcome = _touch_order_long(row, tp, sl)
            else:
                outcome = _touch_order_short(row, tp, sl)
            if outcome:
                break

        if not outcome:
            continue

        if outcome == "TP": tp_c += 1
        elif outcome == "SL": sl_c += 1
        elif outcome == "AMB": amb_c += 1

        DB.execute("UPDATE signals SET status=?, outcome_ts=? WHERE id=?", (outcome, now, _id))
        DB.commit()
        if sig_key: closed_keys.append(sig_key)

    if return_closed_sigkeys:
        return tp_c, sl_c, amb_c, exp_c, closed_keys
    return tp_c, sl_c, amb_c, exp_c

# ---------- Gün aralığı (Istanbul) ----------
def _ist_day_range_from_ts(any_ts: float):
    loc = any_ts + IST_OFFSET
    t = time.gmtime(loc)
    start_loc = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0,0,0, 0,0,0))
    start_utc = start_loc - IST_OFFSET
    end_utc = start_utc + 86400
    return start_utc, end_utc, f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"

# ---------- Günlük rapor ----------
def render_detail_text_daily(yesterday_ts: float = None):
    now = time.time()
    if yesterday_ts is None:
        yesterday_ts = now - 86400
    day_start, day_end, day_key = _ist_day_range_from_ts(yesterday_ts)

    opened = DB.execute("""
        SELECT id, sym, side, tf, entry, tp, sl, status, outcome_ts, ts
        FROM signals
        WHERE ts >= ? AND ts < ?
        ORDER BY ts ASC
    """, (day_start, day_end)).fetchall()

    if not opened:
        return f"📅 Gün Sonu — {day_key}\nKayıt yok."

    # Özet listeleri
    from collections import Counter
    tp_list, sl_list = [], []
    amb_count = exp_count = open_count = 0

    for _id, sym, side, tf, entry, tp, sl, status, outcome_ts, ts in opened:
        if status == "TP": tp_list.append(sym)
        elif status == "SL": sl_list.append(sym)
        elif status == "AMB": amb_count += 1
        elif status == "EXPIRED": exp_count += 1
        else: open_count += 1

    tp_c = Counter(tp_list); sl_c = Counter(sl_list)
    lines = [f"📅 Gün Sonu — {day_key}"]
    if tp_c:
        lines.append("🎯 TP olanlar:")
        for sym, cnt in tp_c.most_common():
            lines.append(f"🎯 {sym} ×{cnt}" if cnt>1 else f"🎯 {sym}")
    if sl_c:
        lines.append("🛑 SL olanlar:")
        for sym, cnt in sl_c.most_common():
            lines.append(f"🛑 {sym} ×{cnt}" if cnt>1 else f"🛑 {sym}")
    lines.append(f"⏳ Açık: {open_count} | ❔ AMB: {amb_count} | 💤 EXP: {exp_count}")
    return "\n".join(lines)
