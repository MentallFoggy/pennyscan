"""Loads config.yaml (and environment-variable overrides for secrets)."""
import copy
import os

import yaml

DEFAULTS = {
    "universe": {
        "min_price": 0.10,
        "max_price": 5.00,
        "min_avg_volume": 200_000,
        "max_tickers_per_run": 40,
        "watchlist": [],
        "use_reddit_trending": True,
        "use_market_movers": True,
        "screeners": ["small_cap_gainers", "aggressive_small_caps", "most_actives",
                      "day_gainers", "day_losers", "most_shorted_stocks"],
    },
    "signals": {
        "news_lookback_hours": 24,
        "dilution_lookback_days": 30,
        "neutral_band": 0.15,
        "weights": {"news": 0.45, "stocktwits": 0.20, "momentum": 0.20, "dilution": 0.50},
        "squeeze_short_pct": 0.20,
    },
    "alerts": {
        "ntfy_topic": "",
        "discord_webhook": "",
        "min_strength": 7,
        "cooldown_hours": 6,
    },
    "tracking": {
        "db_path": "data/pennyscan.db",
        "log_min_strength": 4,
        "win_threshold_pct": 5.0,
        "min_samples_for_confidence": 20,
    },
    "sources": {
        "finnhub_key": "",
        "sec_user_agent": "",
        "use_stocktwits": True,
        "use_sec": True,
        "use_wires": True,
        "wire_feeds": {
            "PR Newswire": "https://www.prnewswire.com/rss/news-releases-list.rss",
            "PR Newswire (finance)": "https://www.prnewswire.com/rss/financial-services-latest-news.rss",
        },
    },
    "ai": {
        "api_key": "",
        "model": "claude-haiku-5-5",
        "max_calls_per_run": 15,
    },
    "schedule": {"loop_minutes": 10, "run_outside_market_hours": False},
    "report_path": "REPORT.md",
    "site_data_dir": "docs/data",
}

ENV_OVERRIDES = {
    "NTFY_TOPIC": ("alerts", "ntfy_topic"),
    "DISCORD_WEBHOOK": ("alerts", "discord_webhook"),
    "FINNHUB_KEY": ("sources", "finnhub_key"),
    "SEC_USER_AGENT": ("sources", "sec_user_agent"),
    "ANTHROPIC_API_KEY": ("ai", "api_key"),
}


def _merge(base, extra):
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v


def load(path="config.yaml"):
    cfg = copy.deepcopy(DEFAULTS)
    if os.path.exists(path):
        with open(path) as f:
            _merge(cfg, yaml.safe_load(f))
    for env, (section, key) in ENV_OVERRIDES.items():
        if os.getenv(env):
            cfg[section][key] = os.getenv(env)
    cfg["universe"]["watchlist"] = [t.upper().lstrip("$") for t in cfg["universe"]["watchlist"] or []]
    return cfg
