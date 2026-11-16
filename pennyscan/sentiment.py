"""Scores a headline from -1 (bad for the stock) to +1 (good for the stock).

General-purpose sentiment (VADER) is weak on finance news: "XYZ announces
pricing of $5M public offering" sounds neutral but usually tanks a penny
stock. So keyword rules for known penny-stock catalysts carry most of the weight.
"""
import re

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

_vader = SentimentIntensityAnalyzer()

BULLISH = {
    "fda approval": 0.9, "fda approves": 0.9, "fda clearance": 0.7, "approval": 0.5,
    "breakthrough therapy": 0.6, "fast track": 0.4, "positive topline": 0.7,
    "positive results": 0.6, "met primary endpoint": 0.7, "strategic partnership": 0.5,
    "partnership": 0.4, "collaboration": 0.3, "awarded": 0.5, "contract": 0.4,
    "purchase order": 0.4, "definitive agreement": 0.5, "to be acquired": 0.7,
    "acquisition": 0.3, "merger": 0.3, "record revenue": 0.6, "beats": 0.4,
    "raises guidance": 0.6, "profitable": 0.4, "uplist": 0.5, "patent": 0.3,
    "share repurchase": 0.5, "buyback": 0.5, "expansion": 0.3, "expands": 0.3,
    "launch": 0.2, "regains compliance": 0.5, "insider buying": 0.5,
    "insider purchase": 0.5, "eliminates debt": 0.5, "short squeeze": 0.3,
}

BEARISH = {
    "public offering": -0.8, "registered direct": -0.8, "private placement": -0.6,
    "at-the-market": -0.6, "announces pricing": -0.6, "pricing of": -0.5,
    "dilution": -0.5, "warrant": -0.3, "reverse stock split": -0.7,
    "reverse split": -0.7, "delist": -0.7, "deficiency": -0.5, "non-compliance": -0.5,
    "noncompliance": -0.5, "bankruptcy": -0.9, "chapter 11": -0.9,
    "going concern": -0.7, "misses": -0.4, "class action": -0.4, "lawsuit": -0.3,
    "investigation": -0.4, "sec charges": -0.8, "halted": -0.3, "resign": -0.3,
    "downgrade": -0.4, "default": -0.5, "recall": -0.4, "terminat": -0.4,
    "complete response letter": -0.8, "failed to meet": -0.6, "did not meet": -0.6,
}

# Longest phrases first so "fda approval" wins over "approval".
_RULES = sorted(list(BULLISH.items()) + list(BEARISH.items()), key=lambda kv: -len(kv[0]))
_PATTERNS = [(re.compile(r"\b" + re.escape(p)), p, w) for p, w in _RULES]


def score_text(text):
    """Returns (score in [-1, 1], [matched phrases])."""
    low = text.lower()
    total, hits = 0.0, []
    for pat, phrase, w in _PATTERNS:
        if pat.search(low):
            total += w
            hits.append(phrase)
            low = pat.sub(" ", low)  # don't double-count inside a longer phrase
    vader = _vader.polarity_scores(text)["compound"]
    score = max(-1.0, min(1.0, total + 0.4 * vader))
    return score, hits


def score_item(item):
    # Headline counts double: it's what moves people.
    text = f"{item['title']}. {item['title']}. {item.get('summary', '')[:400]}"
    return score_text(text)
