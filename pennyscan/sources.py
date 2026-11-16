"""Data sources. Every function fails soft: if a site is down or blocks us,
we log a warning and return empty data so the rest of the scan keeps going.

Sources (all free):
  - Yahoo Finance via yfinance: prices, volume, float, short interest, news, screeners
  - ApeWisdom: trending tickers + mention counts across Reddit stock subs
  - StockTwits: recent messages tagged Bullish / Bearish
  - SEC EDGAR: recent filings (catches dilution: S-1, S-3, 424B...)
  - Finnhub (optional, free key): extra company news
"""
import datetime as dt
import logging
import math
import time
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

log = logging.getLogger("pennyscan")
ET = ZoneInfo("America/New_York")
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) pennyscan/1.0"


def _get_json(url, headers=None, params=None, timeout=15):
    try:
        r = requests.get(url, headers=headers or {"User-Agent": BROWSER_UA},
                         params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("GET %s failed: %s", url.split("?")[0], e)
        return None


# ---------------------------------------------------------------- market clock
def now_et():
    return dt.datetime.now(ET)


def market_is_open(t=None):
    t = t or now_et()
    if t.weekday() >= 5:
        return False
    return dt.time(4, 0) <= t.time() <= dt.time(20, 0)  # include pre/after-hours


def session_fraction(t=None):
    """How much of the regular session (9:30-16:00 ET) has elapsed, 0.1..1."""
    t = t or now_et()
    if t.weekday() >= 5:
        return 1.0
    start = t.replace(hour=9, minute=30, second=0, microsecond=0)
    frac = (t - start).total_seconds() / (390 * 60)
    return max(0.1, min(1.0, frac))


# ---------------------------------------------------------------- universe
def reddit_trending(pages=2):
    """{ticker: {mentions, mentions_24h_ago, upvotes}} from ApeWisdom."""
    out = {}
    for p in range(1, pages + 1):
        data = _get_json(f"https://apewisdom.io/api/v1.0/filter/all-stocks/page/{p}")
        if not data:
            break
        for r in data.get("results", []):
            t = (r.get("ticker") or "").upper()
            if t:
                out[t] = {
                    "mentions": int(r.get("mentions") or 0),
                    "mentions_24h_ago": int(r.get("mentions_24h_ago") or 0),
                    "upvotes": int(r.get("upvotes") or 0),
                }
    return out


def market_movers(screeners, count=100):
    syms = set()
    for name in screeners:
        try:
            res = yf.screen(name, count=count)
            for q in res.get("quotes", []):
                s = (q.get("symbol") or "").upper()
                if s and "." not in s and "^" not in s:
                    syms.add(s)
        except Exception as e:  # noqa: BLE001
            log.warning("screener %s failed: %s", name, e)
    return syms


# ---------------------------------------------------------------- prices
def _split_frames(df, tickers):
    frames = {}
    if df is None or df.empty:
        return frames
    multi = isinstance(df.columns, pd.MultiIndex)
    for t in tickers:
        try:
            if multi:
                if t not in df.columns.get_level_values(0):
                    continue
                sub = df[t]
            else:
                sub = df
            sub = sub.dropna(subset=["Close"])
            if not sub.empty:
                frames[t] = sub
        except Exception:  # noqa: BLE001
            continue
    return frames


def price_snapshot(tickers):
    """{ticker: {price, prev_close, chg_pct, volume, avg_volume, rel_volume}}"""
    tickers = sorted(set(tickers))
    if not tickers:
        return {}
    try:
        df = yf.download(tickers, period="2mo", interval="1d", group_by="ticker",
                         auto_adjust=False, progress=False, threads=True)
    except Exception as e:  # noqa: BLE001
        log.warning("price download failed: %s", e)
        return {}
    frac = session_fraction()
    today = now_et().date()
    out = {}
    for t, sub in _split_frames(df, tickers).items():
        if len(sub) < 6:
            continue
        close, vol = sub["Close"], sub["Volume"].fillna(0)
        price, prev = float(close.iloc[-1]), float(close.iloc[-2])
        avg_vol = float(vol.iloc[-21:-1].mean() or 0)
        v_today = float(vol.iloc[-1])
        # If today's bar is still forming, compare against the volume expected so far.
        last_day = pd.Timestamp(sub.index[-1]).date()
        expected = avg_vol * (frac if last_day == today else 1.0)
        out[t] = {
            "price": price,
            "prev_close": prev,
            "chg_pct": (price / prev - 1) * 100 if prev else 0.0,
            "volume": v_today,
            "avg_volume": avg_vol,
            "rel_volume": v_today / expected if expected else 0.0,
        }
    return out


def ticker_details(t):
    """Float and short interest (can be weeks old: exchanges report twice a month)."""
    try:
        info = yf.Ticker(t).info or {}
    except Exception as e:  # noqa: BLE001
        log.warning("info %s failed: %s", t, e)
        return {}
    return {
        "name": info.get("shortName") or info.get("longName") or t,
        "short_pct_float": info.get("shortPercentOfFloat"),
        "float_shares": info.get("floatShares"),
        "market_cap": info.get("marketCap"),
    }


def history_closes(ticker, start_date):
    """Daily closes from start_date onward (used to grade past calls)."""
    try:
        df = yf.download(ticker, start=start_date, interval="1d", auto_adjust=False,
                         progress=False)
    except Exception as e:  # noqa: BLE001
        log.warning("history %s failed: %s", ticker, e)
        return []
    frames = _split_frames(df, [ticker])
    if ticker not in frames:
        return []
    s = frames[ticker]["Close"]
    return [(pd.Timestamp(i).date(), float(v)) for i, v in s.items()]


# ---------------------------------------------------------------- news
def _iso_to_ts(s):
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:  # noqa: BLE001
        return None


def news_yahoo(t, lookback_hours):
    try:
        raw = yf.Ticker(t).news or []
    except Exception as e:  # noqa: BLE001
        log.warning("news %s failed: %s", t, e)
        return []
    cutoff = time.time() - lookback_hours * 3600
    items = []
    for n in raw:
        c = n.get("content") or n
        title = c.get("title")
        if not title:
            continue
        ts = n.get("providerPublishTime") or _iso_to_ts(c.get("pubDate") or "")
        if ts and ts < cutoff:
            continue
        items.append({
            "title": title,
            "summary": c.get("summary") or c.get("description") or "",
            "ts": ts or time.time(),
            "url": (c.get("canonicalUrl") or {}).get("url") or c.get("link") or "",
            "source": (c.get("provider") or {}).get("displayName") or c.get("publisher") or "Yahoo",
        })
    return items


def news_finnhub(t, key, lookback_hours):
    if not key:
        return []
    today = dt.date.today()
    frm = today - dt.timedelta(days=max(1, math.ceil(lookback_hours / 24)))
    data = _get_json("https://finnhub.io/api/v1/company-news",
                     params={"symbol": t, "from": frm.isoformat(), "to": today.isoformat(),
                             "token": key})
    cutoff = time.time() - lookback_hours * 3600
    return [{
        "title": d.get("headline", ""), "summary": d.get("summary", ""),
        "ts": d.get("datetime") or time.time(), "url": d.get("url", ""),
        "source": d.get("source", "Finnhub"),
    } for d in (data or []) if d.get("headline") and (d.get("datetime") or 0) >= cutoff]


def dedupe_news(items):
    seen, out = set(), []
    for it in sorted(items, key=lambda x: -x["ts"]):
        key = it["title"].lower()[:80]
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out


# ---------------------------------------------------------------- StockTwits
_stocktwits_ok = True


def stocktwits(t):
    """{bull, bear, messages} from the latest ~30 messages; None if unavailable."""
    global _stocktwits_ok
    if not _stocktwits_ok:
        return None
    data = _get_json(f"https://api.stocktwits.com/api/2/streams/symbol/{t}.json")
    if data is None:
        _stocktwits_ok = False  # blocked or down: don't hammer it for every ticker
        return None
    bull = bear = 0
    msgs = data.get("messages", [])
    for m in msgs:
        s = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
        bull += s == "Bullish"
        bear += s == "Bearish"
    return {"bull": bull, "bear": bear, "messages": len(msgs)}


# ---------------------------------------------------------------- SEC EDGAR
_cik_map = None


def sec_recent_filings(t, user_agent, days):
    """[{form, date}] filed in the last `days` days, newest first."""
    global _cik_map
    if not user_agent:
        return []
    headers = {"User-Agent": user_agent}
    if _cik_map is None:
        data = _get_json("https://www.sec.gov/files/company_tickers.json", headers=headers)
        _cik_map = {v["ticker"].upper(): int(v["cik_str"]) for v in (data or {}).values()}
    cik = _cik_map.get(t)
    if not cik:
        return []
    time.sleep(0.15)  # SEC asks for <10 requests/second
    data = _get_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", headers=headers)
    if not data:
        return []
    recent = data.get("filings", {}).get("recent", {})
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    out = []
    for form, date in zip(recent.get("form", []), recent.get("filingDate", [])):
        if date < cutoff:
            break
        out.append({"form": form, "date": date})
    return out
