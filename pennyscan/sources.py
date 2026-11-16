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
import email.utils
import logging
import math
import re
import time
import xml.etree.ElementTree as ET_XML
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

log = logging.getLogger("pennyscan")
ET = ZoneInfo("America/New_York")
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


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
            "_daily_last_is_today": last_day == today,
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


def history_closes_batch(tickers, start_date):
    """{ticker: [(date, close)]} daily closes from start_date, one download for all."""
    try:
        df = yf.download(list(tickers), start=start_date, interval="1d", group_by="ticker",
                         auto_adjust=False, progress=False, threads=True)
    except Exception as e:  # noqa: BLE001
        log.warning("history download failed: %s", e)
        return {}
    return {t: [(pd.Timestamp(i).date(), float(v)) for i, v in f["Close"].items()]
            for t, f in _split_frames(df, list(tickers)).items()}


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


# ---------------------------------------------------------------- intraday / extended hours
def intraday(tickers):
    """Latest price incl. pre/after-hours, today's volume so far, and a mini chart.
    {ticker: {last, session, day_volume, spark:[...], last_ts}}"""
    tickers = sorted(set(tickers))
    if not tickers:
        return {}
    try:
        df = yf.download(tickers, period="5d", interval="5m", prepost=True, group_by="ticker",
                         auto_adjust=False, progress=False, threads=True)
    except Exception as e:  # noqa: BLE001
        log.warning("intraday download failed: %s", e)
        return {}
    out = {}
    for t, sub in _split_frames(df, tickers).items():
        try:
            idx = sub.index.tz_convert(ET) if sub.index.tz is not None else sub.index.tz_localize("UTC").tz_convert(ET)
            sub = sub.set_axis(idx)
            last_day = idx[-1].date()
            day = sub[idx.date == last_day]
            closes = [float(x) for x in day["Close"].tolist()]
            if len(closes) > 48:  # keep the chart light: ~48 points
                step = len(closes) / 48
                closes = [closes[int(i * step)] for i in range(48)] + [closes[-1]]
            tm = idx[-1].time()
            session = ("pre" if tm < dt.time(9, 30) else "post" if tm >= dt.time(16, 0) else "regular")
            out[t] = {
                "last": float(day["Close"].iloc[-1]),
                "last_date": last_day,
                "session": session,
                "day_volume": float(day["Volume"].fillna(0).sum()),
                "spark": [round(c, 4) for c in closes],
            }
        except Exception:  # noqa: BLE001
            continue
    return out


def merge_intraday(snaps, intra):
    """Use the freshest price (pre-market / after-hours included) for price + % change."""
    today = now_et().date()
    for t, s in snaps.items():
        i = intra.get(t)
        if not i:
            s.update(session="regular", spark=[])
            continue
        s["spark"] = i["spark"]
        s["session"] = i["session"]
        if i["last_date"] == today:
            # prev close = last daily close before today
            prev = s["prev_close"] if s.get("_daily_last_is_today") else s["price"]
            s["prev_close"] = prev
            s["price"] = i["last"]
            s["chg_pct"] = (i["last"] / prev - 1) * 100 if prev else 0.0
            vol = max(i["day_volume"], s.get("volume", 0) if s.get("_daily_last_is_today") else 0)
            s["volume"] = vol
            expected = s["avg_volume"] * session_fraction()
            s["rel_volume"] = vol / expected if expected else 0.0
        else:
            s["session"] = "closed"
    return snaps


# ---------------------------------------------------------------- press-release wires
_TICKER_RE = re.compile(
    r"\((?:NASDAQ|Nasdaq|NYSE|NYSE American|NYSE MKT|NYSE Arca|OTC|OTCQB|OTCQX|OTC Markets|Cboe|CBOE)"
    r"(?:\s+[A-Za-z]+){0,3}\s*:\s*\$?([A-Z]{1,5})(?:\.[A-Z])?\b")


def _rss_items(url, headers=None, timeout=30):
    try:
        r = requests.get(url, headers=headers or {"User-Agent": BROWSER_UA,
                                                  "Accept": "application/rss+xml, application/xml, text/xml, */*"},
                         timeout=timeout)
        r.raise_for_status()
        root = ET_XML.fromstring(r.content)
    except Exception as e:  # noqa: BLE001
        log.warning("feed %s failed: %s", url.split("/")[2], e)
        return []
    items = []
    for it in root.iter():
        tag = it.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        get = lambda name: next((c for c in it if c.tag.split("}")[-1] == name), None)  # noqa: E731
        title = (get("title").text if get("title") is not None else "") or ""
        link_el = get("link")
        link = (link_el.text or link_el.get("href") or "") if link_el is not None else ""
        desc_el = get("description") if get("description") is not None else get("summary")
        desc = re.sub(r"<[^>]+>", " ", (desc_el.text or "") if desc_el is not None else "")
        when_el = get("pubDate") if get("pubDate") is not None else get("updated")
        ts = None
        if when_el is not None and when_el.text:
            try:
                ts = email.utils.parsedate_to_datetime(when_el.text).timestamp()
            except Exception:  # noqa: BLE001
                ts = _iso_to_ts(when_el.text)
        items.append({"title": title.strip(), "url": link.strip(), "summary": desc.strip()[:600],
                      "ts": ts or time.time()})
    return items


def wire_releases(feeds, lookback_hours):
    """{ticker: [news items]} from press-release wire RSS feeds."""
    cutoff = time.time() - lookback_hours * 3600
    out = {}
    for name, url in feeds.items():
        for it in _rss_items(url):
            if it["ts"] < cutoff:
                continue
            tickers = set(_TICKER_RE.findall(it["title"] + " " + it["summary"]))
            for t in tickers:
                out.setdefault(t, []).append({**it, "source": name, "wire": True})
    return out


def sec_current_8k(user_agent):
    """{ticker: filed_ts} for 8-Ks in EDGAR's live feed (last ~100 filings)."""
    global _cik_map
    if not user_agent:
        return {}
    headers = {"User-Agent": user_agent}
    if _cik_map is None:
        data = _get_json("https://www.sec.gov/files/company_tickers.json", headers=headers)
        _cik_map = {v["ticker"].upper(): int(v["cik_str"]) for v in (data or {}).values()}
    by_cik = {}
    for t, c in _cik_map.items():
        by_cik.setdefault(c, t)
    out = {}
    url = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company=&dateb="
           "&owner=include&start=0&count=100&output=atom")
    for it in _rss_items(url, headers=headers, timeout=10):
        m = re.search(r"\((\d{6,10})\)", it["title"])
        if m and int(m.group(1)) in by_cik:
            out[by_cik[int(m.group(1))]] = it["ts"]
    return out
