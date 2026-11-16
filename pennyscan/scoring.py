"""Turns raw data for one ticker into a lean (BULLISH / BEARISH / NEUTRAL),
a strength from 1-10, the reasons, and red flags.

direction (-1..1) = what the evidence points to
attention (0..1)  = how much the market is reacting (volume, chatter)
strength          = |direction| scaled up by attention

These weights are a starting guess. The tracker grades every call so you can
see which strengths actually pay off and tune config.yaml from real results.
"""
import math
import time

from .sentiment import score_item

DILUTION_FORMS = {"S-1", "S-1/A", "S-3", "S-3/A", "F-1", "F-1/A", "F-3", "F-3/A",
                  "424B1", "424B2", "424B3", "424B4", "424B5", "424B7"}


def _clamp(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


def score_ticker(t, snap, details, news, filings, social, twits, cfg):
    sig_cfg = cfg["signals"]
    w = sig_cfg["weights"]
    reasons, flags = [], []

    # --- news
    news_score, scored = 0.0, []
    if news:
        num = den = 0.0
        now = time.time()
        for it in news:
            s, hits = score_item(it)
            age_h = max(0.0, (now - it["ts"]) / 3600)
            wt = 0.5 ** (age_h / 12)  # half-life 12h
            num += s * wt
            den += wt
            scored.append((s, hits, it))
        news_score = num / den if den else 0.0
        for s, hits, it in sorted(scored, key=lambda x: -abs(x[0]))[:2]:
            tag = "+" if s > 0.1 else "-" if s < -0.1 else "~"
            why = f" [{', '.join(hits[:3])}]" if hits else ""
            reasons.append(f"News {tag}: {it['title'][:110]}{why}")

    # --- SEC filings
    dilution = [f for f in filings if f["form"] in DILUTION_FORMS]
    if dilution:
        forms = ", ".join(sorted({f["form"] for f in dilution}))
        flags.append(f"Dilution filing(s) in last {sig_cfg['dilution_lookback_days']}d: {forms}")
    recent_8k = [f for f in filings if f["form"].startswith("8-K")][:1]
    if recent_8k:
        reasons.append(f"New 8-K filed {recent_8k[0]['date']} (material event)")

    # --- price action
    chg = snap.get("chg_pct", 0.0)
    rel_vol = snap.get("rel_volume", 0.0)
    mom_dir = math.tanh(chg / 15)
    if abs(chg) >= 5:
        reasons.append(f"{'Up' if chg > 0 else 'Down'} {abs(chg):.0f}% today")
    att_vol = _clamp(math.log(rel_vol) / math.log(10), 0, 1) if rel_vol > 1 else 0.0
    if rel_vol >= 2:
        reasons.append(f"Volume {rel_vol:.1f}x normal")

    # --- social
    att_social, tw_dir = 0.0, 0.0
    if social:
        m, m0 = social["mentions"], social["mentions_24h_ago"]
        growth = m / m0 if m0 else (5.0 if m >= 20 else 1.0)
        att_social = _clamp(math.log(growth) / math.log(5), 0, 1) if growth > 1 else 0.0
        if m >= 10:
            reasons.append(f"Reddit mentions {m} (24h ago: {m0})")
    if twits and twits["bull"] + twits["bear"] >= 5:
        b, s = twits["bull"], twits["bear"]
        tw_dir = (b - s) / (b + s)
        reasons.append(f"StockTwits {100 * b / (b + s):.0f}% bullish ({b + s} tagged posts)")

    # --- combine
    direction = (w["news"] * news_score + w["stocktwits"] * tw_dir
                 + w["momentum"] * mom_dir - w["dilution"] * (1 if dilution else 0))

    si = details.get("short_pct_float")
    if si and si >= sig_cfg["squeeze_short_pct"]:
        reasons.append(f"Short interest {si * 100:.0f}% of float (squeeze fuel)")
        if direction > 0:
            direction += 0.15
    direction = _clamp(direction)

    attention = max(att_vol, att_social)
    strength = round(_clamp(abs(direction) * (0.5 + 0.5 * attention) * 12, 1, 10))

    price = snap.get("price", 0)
    if chg > 30 and not news and att_social > 0.5:
        flags.append("Huge move + hype with no news: classic pump pattern")
        strength = max(1, strength - 2)
    if price < 1:
        flags.append("Under $1: delisting risk, wide spreads")

    band = sig_cfg["neutral_band"]
    lean = "BULLISH" if direction > band else "BEARISH" if direction < -band else "NEUTRAL"
    if lean == "BEARISH" and price < 3:
        flags.append("Shorting sub-$3 stocks is often impossible or very costly to borrow")
    if not news and not social and not twits:
        flags.append("No news or chatter found: signal is price/volume only")

    return {
        "ticker": t,
        "name": details.get("name", t),
        "price": price,
        "chg_pct": chg,
        "rel_volume": rel_vol,
        "lean": lean,
        "strength": int(strength) if lean != "NEUTRAL" else 0,
        "direction": round(direction, 3),
        "attention": round(attention, 3),
        "reasons": reasons,
        "red_flags": flags,
        "news_count": len(news),
        # Extra detail for the website
        "news": [{"title": it["title"], "url": it.get("url", ""), "source": it.get("source", ""),
                  "ts": it["ts"], "score": round(s, 2), "hits": hits[:4]}
                 for s, hits, it in sorted(scored, key=lambda x: -x[2]["ts"])][:10],
        "social": ({"mentions": social["mentions"], "mentions_24h_ago": social["mentions_24h_ago"]}
                   if social else None),
        "stocktwits": ({"bull": twits["bull"], "bear": twits["bear"]} if twits else None),
        "short_pct_float": si,
        "float_shares": details.get("float_shares"),
        "market_cap": details.get("market_cap"),
        "volume": snap.get("volume"),
        "avg_volume": snap.get("avg_volume"),
        "filings": filings[:8],
    }
