"""Offline end-to-end test: fakes every network source, runs a full scan,
then fakes later prices and checks grading + the track record.
Run:  python -m pytest tests -q   (or: python tests/test_offline.py)"""
import datetime as dt
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pennyscan import __main__ as app, config, sources  # noqa: E402
from pennyscan.sentiment import score_text  # noqa: E402
from pennyscan.store import Store  # noqa: E402

NOW = time.time()

FAKE = {
    # BYND-style: heavy short interest, retail hype, real catalyst, volume explosion
    "BYND": dict(snap={"price": 1.45, "prev_close": 0.98, "chg_pct": 48, "volume": 9e8,
                       "avg_volume": 4e7, "rel_volume": 22},
                 details={"name": "Beyond Meat", "short_pct_float": 0.63},
                 news=[{"title": "Beyond Meat announces expansion with Walmart to 2,000 stores",
                        "summary": "", "ts": NOW - 3600, "url": "", "source": "x"}],
                 social={"mentions": 900, "mentions_24h_ago": 40, "upvotes": 5000},
                 twits={"bull": 25, "bear": 3, "messages": 30}, filings=[]),
    # Dilution: offering priced, stock dumping
    "DILU": dict(snap={"price": 0.62, "prev_close": 0.81, "chg_pct": -23, "volume": 3e7,
                       "avg_volume": 5e6, "rel_volume": 6},
                 details={"name": "Dilutive Co", "short_pct_float": 0.05},
                 news=[{"title": "Dilutive Co announces pricing of $8 million registered direct offering",
                        "summary": "", "ts": NOW - 7200, "url": "", "source": "x"}],
                 social=None, twits={"bull": 2, "bear": 9, "messages": 30},
                 filings=[{"form": "424B5", "date": dt.date.today().isoformat()}]),
    # Pump: huge move, hype, no news at all
    "PUMP": dict(snap={"price": 2.10, "prev_close": 1.20, "chg_pct": 75, "volume": 5e7,
                       "avg_volume": 2e6, "rel_volume": 25},
                 details={"name": "Pump Inc"}, news=[],
                 social={"mentions": 300, "mentions_24h_ago": 5, "upvotes": 100},
                 twits=None, filings=[]),
    # Quiet: nothing happening
    "QUIET": dict(snap={"price": 3.00, "prev_close": 3.01, "chg_pct": -0.3, "volume": 3e5,
                        "avg_volume": 3e5, "rel_volume": 1.0},
                  details={"name": "Quiet Corp"}, news=[], social=None, twits=None, filings=[]),
    # Too expensive: should be filtered out
    "BIG": dict(snap={"price": 150.0, "prev_close": 140.0, "chg_pct": 7, "volume": 1e7,
                      "avg_volume": 1e7, "rel_volume": 1.0},
                details={}, news=[], social=None, twits=None, filings=[]),
}


def patch_sources(sent):
    sources.reddit_trending = lambda pages=2: {k: v["social"] for k, v in FAKE.items() if v["social"]}
    sources.market_movers = lambda screeners, count=100: set(FAKE)
    sources.price_snapshot = lambda ts: {k: FAKE[k]["snap"] for k in ts if k in FAKE}
    sources.ticker_details = lambda t: FAKE[t]["details"]
    sources.news_yahoo = lambda t, h: list(FAKE[t]["news"])
    sources.news_finnhub = lambda t, key, h: []
    sources.sec_recent_filings = lambda t, ua, d: FAKE[t]["filings"]
    sources.stocktwits = lambda t: FAKE[t]["twits"]
    app.alerts.send = lambda cfg, title, body, bullish=True: sent.append((title, body)) or True


def test_sentiment_keywords():
    assert score_text("XYZ announces pricing of $5M public offering")[0] < -0.5
    assert score_text("XYZ receives FDA approval for lead drug")[0] > 0.5
    s, hits = score_text("FDA approval granted")
    assert "fda approval" in hits and "approval" not in hits  # no double count


def test_full_scan_and_grading():
    tmp = tempfile.mkdtemp()
    cfg = config.load("/nonexistent.yaml")
    cfg["tracking"]["db_path"] = os.path.join(tmp, "t.db")
    cfg["report_path"] = os.path.join(tmp, "REPORT.md")
    sent = []
    patch_sources(sent)

    sigs = {s["ticker"]: s for s in app.run_once(cfg)}
    assert "BIG" not in sigs
    assert sigs["BYND"]["lean"] == "BULLISH" and sigs["BYND"]["strength"] >= 7
    assert sigs["DILU"]["lean"] == "BEARISH"
    assert any("Dilution" in f for f in sigs["DILU"]["red_flags"])
    assert any("pump" in f.lower() for f in sigs["PUMP"]["red_flags"])
    assert sigs["QUIET"]["lean"] == "NEUTRAL"
    assert any(t.startswith("BYND") for t, _ in sent)

    # Second scan right away: no duplicate alerts or duplicate logged calls
    n_alerts, st = len(sent), Store(cfg["tracking"]["db_path"])
    n_calls = st.total_calls()
    app.run_once(cfg)
    assert len(sent) == n_alerts and st.total_calls() == n_calls

    # Pretend the calls were made a week ago, then fake what prices did after.
    week_ago = (dt.date.today() - dt.timedelta(days=8))
    st.db.execute("UPDATE calls SET day=?", (week_ago.isoformat(),))
    st.db.commit()
    paths = {"BYND": [1.9, 2.5, 3.0, 2.2, 1.8], "DILU": [0.50, 0.45, 0.44, 0.40, 0.39],
             "PUMP": [1.3, 1.1, 1.0, 0.9, 0.8]}

    def fake_hist(t, start):
        return [(week_ago + dt.timedelta(days=i + 1), c) for i, c in enumerate(paths.get(t, []))]

    st.grade(5.0, history_fn=fake_hist)
    rows = {r[0]: r[1:] for r in st.db.execute("SELECT ticker, ret_1d, hit_1d, ret_5d, hit_5d FROM calls")}
    assert rows["BYND"][1] == 1 and rows["BYND"][2] > 0      # bullish call that went up
    assert rows["DILU"][1] == 1 and rows["DILU"][2] < 0      # bearish call that went down

    from pennyscan import report
    report.write(cfg["report_path"], list(sigs.values()), st, cfg)
    text = open(cfg["report_path"]).read()
    assert "Track record" in text and "BYND" in text
    print(text)
    print("\n--- sample alert ---")
    print(sent[0][0]); print(sent[0][1])


if __name__ == "__main__":
    test_sentiment_keywords()
    test_full_scan_and_grading()
    print("\nALL TESTS PASSED")
