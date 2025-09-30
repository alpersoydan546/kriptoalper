# perf.py — Gün sonu raporu (00:00), Seçenek 7 günlük format uyarlaması
# - evaluate_pending: 1m bar ile TP/SL/AMB/EXPIRED tespit eder
# - render_detail_text_daily(yesterday_ts): Europe/Istanbul gününe göre rapor üretir

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
  horizon_min INT
)""")
DB.commit()

HORIZON_MIN_DEFAULT = 240  # 4 saat
EVAL_BAR_TF = "1m"
IST_OFFSET = 3 * 3600  # Europe/Istanbul UTC+3 (kalıcı)

def record_signal(sig: dict, horizon_min: int = HORIZON_MIN_DEFAULT):
    DB.execute("""INSERT INTO signals(ts,sym,side,tf,entry,tp,sl,rr,conf,status,outcome_ts,horizon_min)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
               (time.time(), sig["sym"], sig["side"],
                "/".join(sig.get("tf_list",[sig.get('tf','?')])),
                float(sig["entry"]), float(sig["tp"]), float(sig["sl"]),
                float(sig.get("rr",0)), int(sig.get("conf",0)),
                "NEW", None, int(horizon_min)))
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

def evaluate_pending(get_klines_cached):
    now = time.time()
    rows = DB.execute("""SELECT id,ts,sym,side,entry,tp,sl,horizon_min
                         FROM signals
                         WHERE status='NEW'""").fetchall()
    if not rows: return 0,0,0,0
    tp_c=sl_c=amb_c=exp_c=0
    for _id, ts, sym, side, entry, tp, sl, horizon_min in rows:
        if now - ts > horizon_min*60:
            DB.execute("UPDATE signals SET status='EXPIRED', outcome_ts=? WHERE id=?", (now,_id))
            exp_c += 1
            continue
        df = get_klines_cached(sym, EVAL_BAR_TF, 300)
        if df is None or len(df)==0:
            continue
        df2 = df[df["open_time"].astype("int64")/1e9 > ts]
        if df2.empty: continue
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
    return tp_c, sl_c, amb_c, exp_c

# ---------- Gün sonu penceresi ----------
def _ist_day_range_from_ts(any_ts: float):
    """Verilen timestamp'in ait olduğu Istanbul gününün [start,end) aralığını döndür."""
    loc = any_ts + IST_OFFSET
    t = time.gmtime(loc)
    start_loc = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0,0,0, 0,0,0))  # bu local sayar ama offset uygulanacak
    start_utc = start_loc - IST_OFFSET
    end_utc = start_utc + 86400
    return start_utc, end_utc, f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"

# ---------- Günlük rapor (Seçenek 7 uyumlu, ama günlük kapsam) ----------
def render_detail_text_daily(yesterday_ts: float = None):
    """
    Dün açılan ve dünde sonuçlanan/sonuçlanmayanları raporlar.
    Format:
    📅 Gün Sonu — YYYY-MM-DD
    🎯 COIN
    🛑 COIN
    ⏳ Açık: N | ❔ AMB: x | 💤 EXP: y
    """
    now = time.time()
    if yesterday_ts is None:
        yesterday_ts = now - 86400
    day_start, day_end, day_key = _ist_day_range_from_ts(yesterday_ts)

    # Gün içinde AÇILAN sinyaller
    opened = DB.execute("""
        SELECT id, sym, side, tf, entry, tp, sl, status, outcome_ts, ts
        FROM signals
        WHERE ts >= ? AND ts < ?
        ORDER BY ts ASC
    """, (day_start, day_end)).fetchall()

    if not opened:
        return f"📅 Gün Sonu — {day_key}\nKayıt yok."

    # Kapanış durumlarına göre ayır
    tp_list = []
    sl_list = []
    amb_count = 0
    exp_count = 0
    open_count = 0

    # En son statüsü dikkate al
    for _id, sym, side, tf, entry, tp, sl, status, outcome_ts, ts in opened:
        if status == "TP":
            tp_list.append(sym)
        elif status == "SL":
            sl_list.append(sym)
        elif status == "AMB":
            amb_count += 1
        elif status == "EXPIRED":
            exp_count += 1
        else:  # NEW
            open_count += 1

    # Listeyi sadeleştir (coin başına tekrarları birleştirme: çok istersen kaldırırım)
    from collections import Counter
    tp_c = Counter(tp_list)
    sl_c = Counter(sl_list)

    lines = [f"📅 Gün Sonu — {day_key}"]
    if tp_c:
        lines.append("🎯 TP olanlar:")
        # emoji başta — ancak günlükte coin + adet
        for sym, cnt in tp_c.most_common():
            if cnt == 1:
                lines.append(f"🎯 {sym}")
            else:
                lines.append(f"🎯 {sym} ×{cnt}")
    if sl_c:
        lines.append("🛑 SL olanlar:")
        for sym, cnt in sl_c.most_common():
            if cnt == 1:
                lines.append(f"🛑 {sym}")
            else:
                lines.append(f"🛑 {sym} ×{cnt}")

    # Kalan sayılar
    lines.append(f"⏳ Açık: {open_count} | ❔ AMB: {amb_count} | 💤 EXP: {exp_count}")

    return "\n".join(lines)
