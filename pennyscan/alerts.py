"""Phone alerts via ntfy (free app, no account) and/or a Discord webhook."""
import logging

import requests

log = logging.getLogger("pennyscan")


def _ascii(s):
    return s.encode("ascii", "ignore").decode()


def send(cfg, title, body, bullish=True):
    a = cfg["alerts"]
    sent = False
    if a.get("ntfy_topic"):
        try:
            requests.post(
                f"https://ntfy.sh/{a['ntfy_topic']}",
                data=body.encode("utf-8"),
                headers={"Title": _ascii(title), "Priority": "high",
                         "Tags": "chart_with_upwards_trend" if bullish else "chart_with_downwards_trend"},
                timeout=10,
            ).raise_for_status()
            sent = True
        except Exception as e:  # noqa: BLE001
            log.warning("ntfy failed: %s", e)
    if a.get("discord_webhook"):
        try:
            requests.post(a["discord_webhook"], json={"content": f"**{title}**\n{body}"[:1900]},
                          timeout=10).raise_for_status()
            sent = True
        except Exception as e:  # noqa: BLE001
            log.warning("discord failed: %s", e)
    return sent


def format_alert(sig, conf):
    arrow = "UP" if sig["lean"] == "BULLISH" else "DOWN"
    title = f"{sig['ticker']} {sig['lean']} {sig['strength']}/10 ({arrow}) ${sig['price']:.2f}"
    lines = [f"{sig['name']}: {sig['chg_pct']:+.1f}% today, vol {sig['rel_volume']:.1f}x"]
    lines += [f"- {r}" for r in sig["reasons"][:4]]
    if sig["red_flags"]:
        lines.append("Red flags:")
        lines += [f"! {f}" for f in sig["red_flags"]]
    lines.append(confidence_line(conf))
    return title, "\n".join(lines)


def confidence_line(conf):
    if not conf["proven"]:
        return f"Track record at this strength: not enough data yet ({conf['n1']} graded calls). Treat as unproven."
    s = f"Track record at this strength: {conf['rate1'] * 100:.0f}% hit next day (n={conf['n1']})"
    if conf["rate5"] is not None and conf["n5"]:
        s += f", {conf['rate5'] * 100:.0f}% after 5 days (n={conf['n5']})"
    return s
