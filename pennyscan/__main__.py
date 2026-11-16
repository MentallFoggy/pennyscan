"""
python -m pennyscan run            # one scan (what GitHub Actions calls)
python -m pennyscan loop           # keep scanning every N minutes (on your own computer)
python -m pennyscan grade          # just grade past calls
python -m pennyscan test-alert     # send a test notification to your phone
"""
import argparse
import logging
import time

from . import alerts, config, export, report, sources
from .scoring import score_ticker
from .store import Store

log = logging.getLogger("pennyscan")


def build_universe(cfg):
    u = cfg["universe"]
    social = sources.reddit_trending() if u["use_reddit_trending"] else {}
    tickers = set(social) | set(u["watchlist"])
    if u["use_market_movers"]:
        tickers |= sources.market_movers(u["screeners"])
    return tickers, social


def run_once(cfg):
    u, s, t = cfg["universe"], cfg["sources"], cfg["tracking"]
    store = Store(t["db_path"])

    graded = store.grade(t["win_threshold_pct"])
    if graded:
        log.info("graded %d past calls", graded)

    tickers, social = build_universe(cfg)
    log.info("universe: %d tickers", len(tickers))
    snaps = sources.price_snapshot(tickers)

    watch = set(u["watchlist"])
    cands = [k for k, v in snaps.items()
             if k in watch or (u["min_price"] <= v["price"] <= u["max_price"]
                               and v["avg_volume"] >= u["min_avg_volume"])]

    # Look closest at whatever is getting the most unusual attention.
    def heat(k):
        sm = social.get(k, {})
        return (k in watch, snaps[k]["rel_volume"] + sm.get("mentions", 0) / 20 + abs(snaps[k]["chg_pct"]) / 10)

    cands = sorted(cands, key=heat, reverse=True)[: u["max_tickers_per_run"]]
    log.info("scanning %d penny-stock candidates", len(cands))

    signals = []
    for k in cands:
        try:
            details = sources.ticker_details(k)
            news = sources.news_yahoo(k, cfg["signals"]["news_lookback_hours"])
            news += sources.news_finnhub(k, s["finnhub_key"], cfg["signals"]["news_lookback_hours"])
            news = sources.dedupe_news(news)
            filings = (sources.sec_recent_filings(k, s["sec_user_agent"],
                                                  cfg["signals"]["dilution_lookback_days"])
                       if s["use_sec"] else [])
            twits = sources.stocktwits(k) if s["use_stocktwits"] else None
            signals.append(score_ticker(k, snaps[k], details, news, filings,
                                        social.get(k), twits, cfg))
        except Exception as e:  # noqa: BLE001
            log.warning("scoring %s failed: %s", k, e)

    a = cfg["alerts"]
    for sig in sorted(signals, key=lambda x: -x["strength"]):
        if sig["lean"] == "NEUTRAL":
            continue
        if sig["strength"] >= t["log_min_strength"]:
            store.record_call(sig)
        if sig["strength"] >= a["min_strength"] and not store.recently_alerted(
                sig["ticker"], sig["lean"], sig["strength"], a["cooldown_hours"]):
            conf = store.confidence(sig["lean"], sig["strength"], t["min_samples_for_confidence"])
            title, body = alerts.format_alert(sig, conf)
            if alerts.send(cfg, title, body, bullish=sig["lean"] == "BULLISH"):
                store.mark_alerted(sig["ticker"], sig["lean"], sig["strength"])
            log.info("ALERT %s", title)

    report.write(cfg["report_path"], signals, store, cfg)
    export.write(cfg["site_data_dir"], signals, store, cfg)
    print_table(signals)
    return signals


def print_table(signals):
    live = sorted([x for x in signals if x["lean"] != "NEUTRAL"], key=lambda x: -x["strength"])
    print(f"\n{'TICKER':<7}{'LEAN':<9}{'STR':>4}{'PRICE':>9}{'TODAY':>9}{'VOL':>7}  TOP REASON")
    for x in live:
        top = x["reasons"][0] if x["reasons"] else ""
        print(f"{x['ticker']:<7}{x['lean']:<9}{x['strength']:>4}{x['price']:>9.2f}"
              f"{x['chg_pct']:>8.1f}%{x['rel_volume']:>6.1f}x  {top[:70]}")
    if not live:
        print("(no clear signals this scan)")


def main():
    p = argparse.ArgumentParser(prog="pennyscan")
    p.add_argument("command", choices=["run", "loop", "grade", "test-alert"])
    p.add_argument("--config", default="config.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    cfg = config.load(args.config)

    if args.command == "run":
        run_once(cfg)
    elif args.command == "grade":
        st = Store(cfg["tracking"]["db_path"])
        print(f"graded {st.grade(cfg['tracking']['win_threshold_pct'])} calls")
    elif args.command == "test-alert":
        ok = alerts.send(cfg, "pennyscan test", "If you can read this, alerts work.")
        print("sent" if ok else "not sent: set ntfy_topic or discord_webhook in config.yaml")
    elif args.command == "loop":
        mins = cfg["schedule"]["loop_minutes"]
        while True:
            if cfg["schedule"]["run_outside_market_hours"] or sources.market_is_open():
                try:
                    run_once(cfg)
                except Exception as e:  # noqa: BLE001
                    log.exception("scan failed: %s", e)
            else:
                log.info("market closed; sleeping")
            time.sleep(mins * 60)


if __name__ == "__main__":
    main()
