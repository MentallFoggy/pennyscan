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
CREATE TABLE IF NOT EXISTS baseline (
    day TEXT, ticker TEXT, price REAL, ret_1d REAL, ret_5d REAL,
    PRIMARY KEY (day, ticker)
);
CREATE TABLE IF NOT EXISTS ai_cache (key TEXT PRIMARY KEY, ts REAL, result TEXT);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
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

    # ------------------------------------------------------------ AI cache
    def ai_get(self, key):
        r = self.db.execute("SELECT result FROM ai_cache WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else None

    def ai_put(self, key, result):
        self.db.execute("INSERT OR REPLACE INTO ai_cache VALUES (?,?,?)",
                        (key, time.time(), json.dumps(result)))
        # keep the cache from growing forever
        self.db.execute("DELETE FROM ai_cache WHERE ts < ?", (time.time() - 14 * 86400,))
        self.db.commit()

    # ------------------------------------------------------------ random baseline
    def record_baseline(self, ticker, price):
        """Every scanned penny stock, once per day: 'what if you picked at random?'"""
        if not price:
            return
        self.db.execute("INSERT OR IGNORE INTO baseline (day, ticker, price) VALUES (?,?,?)",
                        (sources.now_et().date().isoformat(), ticker, price))

    def commit(self):
        self.db.commit()

    def baseline_stats(self, win_pct):
        r = self.db.execute(
            "SELECT COUNT(ret_1d), SUM(ret_1d >= ?), SUM(ret_1d <= ?), AVG(ret_1d),"
            "       COUNT(ret_5d), SUM(ret_5d >= ?), SUM(ret_5d <= ?), AVG(ret_5d) FROM baseline",
            (win_pct, -win_pct, win_pct, -win_pct)).fetchone()
        n1, up1, dn1, avg1, n5, up5, dn5, avg5 = r
        f = lambda a, n: (a or 0) / n if n else None  # noqa: E731
        return {"n1": n1, "up1": f(up1, n1), "down1": f(dn1, n1), "avg1": avg1,
                "n5": n5, "up5": f(up5, n5), "down5": f(dn5, n5), "avg5": avg5}

    # ------------------------------------------------------------ grading
    def _meta(self, k, v=None):
        if v is None:
            r = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
            return r[0] if r else None
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, str(v)))
        self.db.commit()

    def grade(self, win_pct, history_fn=None, force=False):
        """Fill in 1-day and 5-day returns for calls (and baseline picks) old enough
        to judge. Uses daily closes, so it only needs to run every few hours."""
        last = float(self._meta("last_grade") or 0)
        if not force and time.time() - last < 3 * 3600:
            return 0
        history_fn = history_fn or sources.history_closes_batch
        today = sources.now_et().date().isoformat()
        calls = self.db.execute(
            "SELECT id, day, ticker, lean, price, ret_1d FROM calls"
            " WHERE ret_5d IS NULL AND day < ? AND day > ?",
            (today, (sources.now_et().date() - dt.timedelta(days=30)).isoformat())).fetchall()
        base = self.db.execute(
            "SELECT day, ticker, price, ret_1d FROM baseline WHERE ret_5d IS NULL AND day < ? AND day > ?",
            (today, (sources.now_et().date() - dt.timedelta(days=30)).isoformat())).fetchall()
        tickers = {r[2] for r in calls} | {r[1] for r in base}
        if not tickers:
            self._meta("last_grade", time.time())
            return 0
        start = min([r[1] for r in calls] + [r[0] for r in base])
        hist = history_fn(sorted(tickers), start)

        def after(ticker, day):
            return [c for d, c in hist.get(ticker, []) if d.isoformat() > day]

        graded = 0
        for cid, day, ticker, lean, entry, ret1 in calls:
            a = after(ticker, day)
            if not a or not entry:
                continue
            sign = 1 if lean == "BULLISH" else -1
            if ret1 is None:
                r = (a[0] / entry - 1) * 100
                self.db.execute("UPDATE calls SET ret_1d=?, hit_1d=? WHERE id=?",
                                (r, int(sign * r >= win_pct), cid))
                graded += 1
            if len(a) >= 5:
                r = (a[4] / entry - 1) * 100
                self.db.execute("UPDATE calls SET ret_5d=?, hit_5d=? WHERE id=?",
                                (r, int(sign * r >= win_pct), cid))
        for day, ticker, entry, ret1 in base:
            a = after(ticker, day)
            if not a or not entry:
                continue
            if ret1 is None:
                self.db.execute("UPDATE baseline SET ret_1d=? WHERE day=? AND ticker=?",
                                ((a[0] / entry - 1) * 100, day, ticker))
            if len(a) >= 5:
                self.db.execute("UPDATE baseline SET ret_5d=? WHERE day=? AND ticker=?",
                                ((a[4] / entry - 1) * 100, day, ticker))
        self.db.commit()
        self._meta("last_grade", time.time())
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
