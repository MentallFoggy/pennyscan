"""Writes REPORT.md: current signals + the scanner's real track record.
On GitHub this renders as a page you can check from your phone."""
from .alerts import confidence_line
from . import sources


def _pct(x):
    return "-" if x is None else f"{x:+.1f}%"


def write(path, signals, store, cfg):
    t = cfg["tracking"]
    now = sources.now_et().strftime("%a %b %d, %I:%M %p ET")
    live = sorted([s for s in signals if s["lean"] != "NEUTRAL"], key=lambda s: -s["strength"])
    L = [f"# Penny stock scanner", f"_Last scan: {now} · {len(signals)} stocks checked · "
         f"{store.total_calls()} calls logged so far_", ""]

    L += ["## Current signals", ""]
    if not live:
        L.append("Nothing with a clear lean right now.")
    else:
        L += ["| Ticker | Lean | Strength | Price | Today | Volume |", "|---|---|---|---|---|---|"]
        for s in live:
            L.append(f"| **{s['ticker']}** | {s['lean']} | {s['strength']}/10 | ${s['price']:.2f} | "
                     f"{s['chg_pct']:+.1f}% | {s['rel_volume']:.1f}x |")
        L.append("")
        for s in live[:15]:
            conf = store.confidence(s["lean"], s["strength"], t["min_samples_for_confidence"])
            L.append(f"### {s['ticker']} · {s['lean']} {s['strength']}/10")
            L += [f"- {r}" for r in s["reasons"]]
            L += [f"- ⚠️ {f}" for f in s["red_flags"]]
            L += [f"- _{confidence_line(conf)}_", ""]

    L += ["## Track record (does it actually work?)", "",
          f"A **hit** = price moved at least {t['win_threshold_pct']:.0f}% in the called direction. "
          "Graded from the price at the time of the call.", "",
          "| Lean | Strength | Next-day hits | Avg next-day move | 5-day hits | Avg 5-day move |",
          "|---|---|---|---|---|---|"]
    for r in store.track_record():
        h1 = f"{r['hits1']}/{r['n1']} ({100 * r['hits1'] / r['n1']:.0f}%)" if r["n1"] else "-"
        h5 = f"{r['hits5']}/{r['n5']} ({100 * r['hits5'] / r['n5']:.0f}%)" if r["n5"] else "-"
        L.append(f"| {r['lean']} | {r['lo']}-{r['hi']} | {h1} | {_pct(r['avg1'])} | {h5} | {_pct(r['avg5'])} |")
    L += ["", "> Rough guide: under ~20 calls in a row, the numbers are mostly noise. "
          "A bucket is only worth trusting if it beats 50% hits **and** the average move "
          "points the right way over many calls.", "",
          "_Not financial advice. Signals are automated and can be wrong; penny stocks can lose most of their value fast._"]
    with open(path, "w") as f:
        f.write("\n".join(L) + "\n")
