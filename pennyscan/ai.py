"""AI news reading with Claude.

For each stock with headlines, Claude reads them the way a penny-stock trader
would: is this actually material? Which way will it push THIS stock over the
next few days? (A "strategic financing" is usually dilution; a "most active
stocks" list is noise; a paid promo isn't news.)

Results are cached by the exact set of headlines, so each batch is only
analyzed once, no matter how many scans see it. Without an ANTHROPIC_API_KEY
the scanner falls back to keyword scoring.
"""
import hashlib
import json
import logging
import time

import requests

log = logging.getLogger("pennyscan")

CATEGORIES = [
    "clinical/FDA", "contract/partnership", "M&A/buyout", "earnings/guidance",
    "offering/dilution", "reverse split", "listing/compliance", "legal/regulatory",
    "bankruptcy/restructuring", "management change", "product/business update",
    "paid promotion", "analyst/price target", "market recap/noise", "other",
]

SYSTEM = f"""You are a sharp, skeptical penny-stock news analyst. For one stock you get its
price action and recent headlines (with short summaries). Judge each headline by its
likely effect on THIS stock's price over the next 1-5 trading days, the way an
experienced small-cap trader would:

- Offerings, registered directs, private placements, ATMs, warrant inducements,
  "strategic financing" = dilution, usually negative even when worded positively.
- Reverse splits, Nasdaq deficiency notices, going-concern warnings, bankruptcy = negative.
- FDA approvals, strong trial data, big contracts, buyouts (at a premium) = positive.
- "Most active stocks" lists, market recaps, generic "is it a buy?" articles,
  and articles that merely describe a price move = NOT material (impact near 0).
- Paid promotion / sponsored content / "investor awareness" pieces = not material,
  and a warning sign.
- Old news rehashed = low impact.

Return ONLY a JSON object, no other text:
{{"takeaway": "<=25 words: what is actually driving this stock and whether the news supports the move",
  "items": [{{"i": <headline number>, "impact": <-1.0 to 1.0>, "material": <true|false>,
             "category": "<one of: {', '.join(CATEGORIES)}>",
             "note": "<=12 words: why"}}]}}"""


def _key(ticker, news):
    titles = "\n".join(sorted(n["title"] for n in news))
    return hashlib.sha1(f"{ticker}\n{titles}".encode()).hexdigest()


def _prompt(ticker, name, snap, news):
    lines = [f"Stock: {ticker} ({name})",
             f"Price ${snap.get('price', 0):.4g}, {snap.get('chg_pct', 0):+.1f}% today, "
             f"volume {snap.get('rel_volume', 0):.1f}x normal, session: {snap.get('session', 'regular')}",
             "", "Headlines:"]
    now = time.time()
    for i, n in enumerate(news):
        age = max(0, (now - n["ts"]) / 3600)
        lines.append(f"[{i}] ({n.get('source', '')}, {age:.0f}h ago) {n['title']}")
        if n.get("summary"):
            lines.append(f"    {n['summary'][:300]}")
    return "\n".join(lines)


def _parse(text):
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b < 0:
        raise ValueError("no JSON in reply")
    return json.loads(text[a:b + 1])


def analyze(ticker, name, snap, news, cfg, store, budget):
    """Returns {"takeaway": str, "items": {title: {...}}} or None.
    `budget` is a one-element list counting remaining API calls this run."""
    ai = cfg["ai"]
    if not ai.get("api_key") or not news:
        return None
    news = news[:8]
    key = _key(ticker, news)
    cached = store.ai_get(key)
    if cached is None:
        if budget[0] <= 0:
            return None
        budget[0] -= 1
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ai["api_key"], "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": ai["model"], "max_tokens": 900, "temperature": 0,
                      "system": SYSTEM,
                      "messages": [{"role": "user", "content": _prompt(ticker, name, snap, news)}]},
                timeout=45,
            )
            r.raise_for_status()
            text = "".join(c.get("text", "") for c in r.json().get("content", []))
            cached = _parse(text)
            store.ai_put(key, cached)
        except Exception as e:  # noqa: BLE001
            code = getattr(getattr(e, "response", None), "status_code", None)
            log.warning("AI read for %s failed (%s)", ticker, f"HTTP {code}" if code else type(e).__name__)
            return None
    items = {}
    for it in cached.get("items", []):
        try:
            i = int(it["i"])
            if 0 <= i < len(news):
                items[news[i]["title"]] = {
                    "impact": max(-1.0, min(1.0, float(it.get("impact", 0)))),
                    "material": bool(it.get("material", True)),
                    "category": str(it.get("category", "other"))[:40],
                    "note": str(it.get("note", ""))[:120],
                }
        except (KeyError, ValueError, TypeError):
            continue
    return {"takeaway": str(cached.get("takeaway", ""))[:240], "items": items}
