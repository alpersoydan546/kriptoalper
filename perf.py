# perf.py — KriptoAlper sinyal performans takibi (state.db uyumlu)
# - Saatlik detay mesajında sadece KAPANANLARI (TP/SL/AMB/EXPIRED) listeler
# - Açık (NEW) sinyaller için sadece SAYI verir

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
  horizon_min INT  -- çözümleme ufku (dk)
)""")
DB.commit()

HORIZON_MIN_DEFAULT = 240  # 4 saat
EVAL_BAR_TF = "1m"         # değerlendirme 1m üzerinden

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
    """scanner.get_klines_cached fonksiyonu DI ile verilir."""
    now = time.time()
    rows = DB.execute("""SELECT id,ts,sym,side,entry,tp,sl,horizon_min
                         FROM signals
                         WHERE status='NEW'""").fetchall()
    if not rows: return 0,0,0,0
    tp_c=sl_c=amb_c=exp_c=0

    for _id, ts, sym, side, entry, tp, sl, horizon_min in rows:
        # süre dolmuşsa expire
        if now - ts > horizon_min*60:
            DB.execute("UPDATE signals SET status='EXPIRED', outcome_ts=? WHERE id=?", (now,_id))
            exp_c += 1
            continue

        # sinyalden sonra oluşan 1m mumları çek
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

        if outcome == "TP":
            tp_c += 1
        elif outcome == "SL":
            sl_c += 1
        elif outcome == "AMB":
            amb_c += 1

        DB.execute("UPDATE signals SET status=?, outcome_ts=? WHERE id=?", (outcome, now, _id))
        DB.commit()

    return tp_c, sl_c, amb_c, exp_c

def summary_last_minutes(minutes: int = 180):
    now = time.time(); t0 = now - minutes*60
    tot = DB.execute("SELECT COUNT(*) FROM signals WHERE ts>=?", (t0,)).fetchone()[0]
    tp  = DB.execute("SELECT COUNT(*) FROM signals WHERE ts>=? AND status='TP'", (t0,)).fetchone()[0]
    sl  = DB.execute("SELECT COUNT(*) FROM signals WHERE ts>=? AND status='SL'", (t0,)).fetchone()[0]
    amb = DB.execute("SELECT COUNT(*) FROM signals WHERE ts>=? AND status='AMB'",(t0,)).fetchone()[0]
    exp = DB.execute("SELECT COUNT(*) FROM signals WHERE ts>=? AND status='EXPIRED'",(t0,)).fetchone()[0]
    open_ = DB.execute("SELECT COUNT(*) FROM signals WHERE ts>=? AND status='NEW'",(t0,)).fetchone()[0]
    succ_rate = (tp / max(1, (tp+sl))) * 100.0
    return dict(total=tot, tp=tp, sl=sl, amb=amb, exp=exp, open=open_, succ=succ_rate)

def render_summary_text(minutes=180):
    s = summary_last_minutes(minutes)
    return (
        f"📈 Performans — Son {minutes} dk\n"
        f"• Gönderilen: {s['total']}\n"
        f"• 🎯 TP: {s['tp']}   🛑 SL: {s['sl']}\n"
        f"• ❔ AMB: {s['amb']}  💤 EXP: {s['exp']}\n"
        f"• ⏳ Açık: {s['open']}\n"
        f"• Başarı: {s['succ']:.0f}%"
    )

def render_detail_text(minutes=60, max_rows=40):
    """
    Son 'minutes' içinde atılan sinyallerin detaylı listesi.
    • KAPALI sinyaller (TP/SL/AMB/EXPIRED) tek tek listelenir.
    • AÇIK sinyaller için sadece adet verilir.
    """
    now = time.time(); t0 = now - minutes*60

    # Kapalılar (detay listelenecek)
    rows = DB.execute("""
        SELECT ts, sym, side, tf, entry, tp, sl, status
        FROM signals
        WHERE ts>=? AND status IN ('TP','SL','AMB','EXPIRED')
        ORDER BY ts DESC
        LIMIT ?
    """, (t0, int(max_rows))).fetchall()

    # Açık sayısı (detay yok)
    open_count = DB.execute("""
        SELECT COUNT(*) FROM signals WHERE ts>=? AND status='NEW'
    """, (t0,)).fetchone()[0]

    header = [
        f"📋 Detay — Son {minutes} dk",
    ]
    if open_count > 0:
        header.append(f"⏳ Açık (detay verilmez): {open_count} adet")

    if not rows:
        # Kapalı yoksa, sadece açık sayısı/boş mesaj
        return "\n".join(header if header else [f"📋 Detay — Son {minutes} dk\nKayıt yok."])

    lines = header
    # Tatlı emojili satırlar
    for ts, sym, side, tf, entry, tp, sl, status in rows:
        icon = "🎯" if status=="TP" else "🛑" if status=="SL" else "❔" if status=="AMB" else "💤"
        side_txt = "LONG" if side=="LONG" else "SHORT"
        lines.append(
            f"{icon} {sym} {side_txt} [{tf}] • "
            f"💵 {entry:.6f} → 🎯 {tp:.6f} | 🛑 {sl:.6f} • {status}"
        )

    if len(rows) == max_rows:
        lines.append(f"… (ilk {max_rows} satır gösterildi)")
    return "\n".join(lines)
