"""SQLite log of every call + grading of how each call actually played out."""
import datetime as dt
import json
import os
import sqlite3
import time

from . import sources

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY,
    ts REAL, day TEXT, ticker TEXT, lean TEXT, strength INTEGER,
    direction REAL, price REAL, reasons TEXT, red_flags TEXT,
    ret_1d REAL, hit_1d INTEGER, ret_5d REAL, hit_5d INTEGER
);
CREATE TABLE IF NOT EXISTS alerts (ticker TEXT, ts REAL, lean TEXT, strength INTEGER);
"""

BUCKETS = [(1, 3), (4, 6), (7, 8), (9, 10)]


def bucket_of(strength):
    for lo, hi in BUCKETS:
        if lo <= strength <= hi:
            return lo, hi
    return BUCKETS[0]


class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    # ------------------------------------------------------------ logging calls
    def record_call(self, sig, now=None):
        """One call per ticker per lean per ~day, so hourly scans don't skew stats.
        Returns True if logged."""
        now = now or time.time()
        row = self.db.execute(
            "SELECT id FROM calls WHERE ticker=? AND lean=? AND ts>?",
            (sig["ticker"], sig["lean"], now - 20 * 3600)).fetchone()
        if row:
            return False
        self.db.execute(
            "INSERT INTO calls (ts, day, ticker, lean, strength, direction, price, reasons, red_flags)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (now, sources.now_et().date().isoformat(), sig["ticker"], sig["lean"],
             sig["strength"], sig["direction"], sig["price"],
             json.dumps(sig["reasons"]), json.dumps(sig["red_flags"])))
        self.db.commit()
        return True

    # ------------------------------------------------------------ alerts
    def recently_alerted(self, ticker, lean, strength, hours):
        row = self.db.execute(
            "SELECT MAX(strength) FROM alerts WHERE ticker=? AND lean=? AND ts>?",
            (ticker, lean, time.time() - hours * 3600)).fetchone()
        # Re-alert only if the signal got clearly stronger.
        return row[0] is not None and strength <= row[0] + 1

    def mark_alerted(self, ticker, lean, strength):
        self.db.execute("INSERT INTO alerts VALUES (?,?,?,?)", (ticker, time.time(), lean, strength))
        self.db.commit()

    # ------------------------------------------------------------ grading
    def grade(self, win_pct, history_fn=None):
        """Fill in 1-day and 5-day returns for calls old enough to judge."""
        history_fn = history_fn or sources.history_closes
        today = sources.now_et().date().isoformat()
        rows = self.db.execute(
            "SELECT id, day, ticker, lean, price, ret_1d FROM calls"
            " WHERE ret_5d IS NULL AND day < ?", (today,)).fetchall()
        cache, graded = {}, 0
        for cid, day, ticker, lean, entry, ret1 in rows:
            key = (ticker, day)
            if key not in cache:
                cache[key] = history_fn(ticker, day)
            after = [(d, c) for d, c in cache[key] if d.isoformat() > day]
            if not after or not entry:
                continue
            sign = 1 if lean == "BULLISH" else -1

            def judge(close):
                r = (close / entry - 1) * 100
                return r, int(sign * r >= win_pct)

            if ret1 is None:
                r, h = judge(after[0][1])
                self.db.execute("UPDATE calls SET ret_1d=?, hit_1d=? WHERE id=?", (r, h, cid))
                graded += 1
            if len(after) >= 5:
                r, h = judge(after[4][1])
                self.db.execute("UPDATE calls SET ret_5d=?, hit_5d=? WHERE id=?", (r, h, cid))
        self.db.commit()
        return graded

    # ------------------------------------------------------------ track record
    def track_record(self):
        """[{lean, lo, hi, n1, hits1, avg1, n5, hits5, avg5}] per strength bucket."""
        out = []
        for lean in ("BULLISH", "BEARISH"):
            for lo, hi in BUCKETS:
                r = self.db.execute(
                    "SELECT COUNT(ret_1d), SUM(hit_1d), AVG(ret_1d),"
                    "       COUNT(ret_5d), SUM(hit_5d), AVG(ret_5d)"
                    " FROM calls WHERE lean=? AND strength BETWEEN ? AND ?",
                    (lean, lo, hi)).fetchone()
                out.append({"lean": lean, "lo": lo, "hi": hi,
                            "n1": r[0], "hits1": r[1] or 0, "avg1": r[2],
                            "n5": r[3], "hits5": r[4] or 0, "avg5": r[5]})
        return out

    def confidence(self, lean, strength, min_samples):
        """Real-world hit rate for this kind of call, or None if not enough history."""
        lo, hi = bucket_of(strength)
        r = self.db.execute(
            "SELECT COUNT(ret_1d), SUM(hit_1d), COUNT(ret_5d), SUM(hit_5d) FROM calls"
            " WHERE lean=? AND strength BETWEEN ? AND ?", (lean, lo, hi)).fetchone()
        n1, h1, n5, h5 = r[0], r[1] or 0, r[2], r[3] or 0
        return {
            "n1": n1, "rate1": h1 / n1 if n1 else None,
            "n5": n5, "rate5": h5 / n5 if n5 else None,
            "proven": n1 >= min_samples,
        }

    def recent_calls(self, limit=60):
        cols = ["ts", "day", "ticker", "lean", "strength", "price", "ret_1d", "hit_1d", "ret_5d", "hit_5d"]
        rows = self.db.execute(
            f"SELECT {', '.join(cols)} FROM calls ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def total_calls(self):
        return self.db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
