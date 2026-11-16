"""Writes the JSON files the website reads (docs/data/*.json)."""
import json
import math
import os

from . import sources


def _clean(o):
    """JSON-safe: turn NaN/inf into null."""
    if isinstance(o, float):
        return None if math.isnan(o) or math.isinf(o) else round(o, 4)
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def write(out_dir, signals, store, cfg):
    t = cfg["tracking"]
    os.makedirs(out_dir, exist_ok=True)
    rows = []
    for s in sorted(signals, key=lambda x: (-x["strength"], -abs(x["chg_pct"]))):
        s = dict(s)
        if s["lean"] != "NEUTRAL":
            s["confidence"] = store.confidence(s["lean"], s["strength"], t["min_samples_for_confidence"])
        rows.append(s)
    latest = {
        "generated_at": sources.now_et().isoformat(),
        "checked": len(signals),
        "calls_logged": store.total_calls(),
        "settings": {"max_price": cfg["universe"]["max_price"],
                     "win_threshold_pct": t["win_threshold_pct"],
                     "min_samples": t["min_samples_for_confidence"],
                     "alert_min_strength": cfg["alerts"]["min_strength"],
                     "ai_enabled": bool(cfg["ai"].get("api_key"))},
        "signals": rows,
    }
    track = {
        "generated_at": latest["generated_at"],
        "win_threshold_pct": t["win_threshold_pct"],
        "buckets": store.track_record(),
        "baseline": store.baseline_stats(t["win_threshold_pct"]),
        "recent": store.recent_calls(80),
    }
    for name, obj in (("latest.json", latest), ("track.json", track)):
        tmp = os.path.join(out_dir, name + ".tmp")
        with open(tmp, "w") as f:
            json.dump(_clean(obj), f, separators=(",", ":"))
        os.replace(tmp, os.path.join(out_dir, name))
