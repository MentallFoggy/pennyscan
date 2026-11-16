# pennyscan

Scans penny stocks for news, SEC filings and Reddit/StockTwits chatter. Each stock gets:

- a **lean**: BULLISH, BEARISH or NEUTRAL
- a **strength** score from 1 to 10
- **reasons**: the headlines, volume spike, chatter and short interest behind the score
- **red flags**: dilution filings, pump patterns, delisting risk

It sends strong signals to your phone. It also **grades itself**: each call is checked 1 day and 5 days later, so the confidence it shows is its real hit rate, not a guess.

> Not financial advice. This is a heuristic scanner. Assume it's unproven until the track record in `REPORT.md` says otherwise over many calls.

## What it watches for (the BYND-style setup)

| Signal | Where it comes from |
|---|---|
| Good/bad news (FDA, contracts, offerings, reverse splits…) | Yahoo Finance news (+ Finnhub if you add a key) |
| Dilution (company selling new shares) | SEC EDGAR filings: S-1, S-3, 424B… |
| Unusual volume | Yahoo prices (adjusted for time of day) |
| Hype building | Reddit mention spikes (ApeWisdom), StockTwits bull/bear tags |
| Squeeze fuel | Short interest % of float |

---

## Setup (about 15 minutes, easiest on a computer)

### 1. Put it on GitHub (runs free in the cloud, no computer needed after)
1. Make a free account at github.com.
2. Create a **New repository** named `pennyscan`. Choose **Public** to get unlimited free run time. A private repo gets 2,000 free minutes a month, which is roughly enough for a scan every 30 minutes (see step 5).
3. Click **uploading an existing file**. Drag in everything from this folder, **including the hidden `.github` folder** (on Mac press Cmd+Shift+. in Finder to show it). Then commit.

### 2. Phone alerts (free)
1. Install the **ntfy** app (iOS / Android).
2. Tap **+** and subscribe to a topic with a random, hard-to-guess name, such as `pennyscan-k7x92q`. Anyone who knows the name can read the alerts.
3. In the GitHub repo, go to **Settings → Secrets and variables → Actions → New repository secret**. Add:
   - `NTFY_TOPIC` = your topic name
   - `SEC_USER_AGENT` = `Your Name you@email.com` (the SEC requires this to read filings)
   - `FINNHUB_KEY` = optional, free key from finnhub.io for more news
   - `ANTHROPIC_API_KEY` = turns on **AI news reading** (see below)

### 3. Turn it on
**Actions** tab → enable workflows → **pennyscan** → **Run workflow**. In about 2 minutes, `REPORT.md` appears in the repo with the current signals and track record. From then on it runs every 15 minutes on weekdays.

### 4. Check it from your phone
- Strong signals (7/10+) show up as ntfy notifications.
- Open `REPORT.md` in the GitHub app for the full list and the track record.

### 5. Tune it
Edit `config.yaml` right on GitHub:
- `max_price`: what counts as a penny stock (default $5)
- `watchlist`: tickers to always check
- `min_strength`: how strong a signal must be to ping you
- `weights`: once you have a few weeks of results, raise whatever is working

To scan less often (private repo), change `*/15` to `*/30` in `.github/workflows/scan.yml`.

---

## AI news reading

With an `ANTHROPIC_API_KEY` secret, Claude reads each stock's headlines like a skeptical small-cap trader. It judges whether the news is material, which way it pushes the price, and whether it's really dilution or a paid promotion. Without the key, the scanner falls back to keyword rules.

1. Create an account at **console.anthropic.com** and add a small amount of credit.
2. Create an API key and add it as the `ANTHROPIC_API_KEY` repo secret.
3. **Set a monthly spend limit** in the console so the cost can't surprise you.

Each set of headlines is read once and then cached, and `ai.max_calls_per_run` in `config.yaml` caps the calls per scan.

## Running on your own computer instead (or as well)

Needs Python 3.10 or newer.

```bash
pip install -r requirements.txt
python -m pennyscan test-alert   # check phone alerts
python -m pennyscan run          # one scan, prints a table
python -m pennyscan loop         # scan every 10 min while the market is open
```

---

## Reading the confidence number

The strength score (1–10) is the scanner's opinion. The **track record** shows whether that opinion is worth anything:

- A **hit** means the stock moved at least 5% in the called direction (next day / 5 days).
- With fewer than about 20 graded calls in a strength bucket, it shows **"unproven"**.
- A bucket is only worth trusting if its hit rate stays above about 50% **and** the average move points the right way, over a lot of calls.

Expect the first version to be wrong a lot. The point of the tracker is to find out which signals actually work, then adjust the weights in `config.yaml` to match.

## Honest limits
- **Not truly live.** GitHub runs every 15 minutes or so (sometimes a few minutes late). Second-by-second scanning needs a paid real-time data feed.
- **Free data can break or lag.** Yahoo, StockTwits and ApeWisdom can change or block access without notice. The scanner skips any source that fails and keeps going.
- **No X/Twitter.** Its API is expensive.
- **Short interest is old.** It's only reported twice a month.
- **Shorting penny stocks** is often impossible or very costly. Treat BEARISH mostly as "avoid / get out."
