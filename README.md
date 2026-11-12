# almanac-watch

Background watcher for Almanac. Every five minutes it checks the sources that
matter for the Anthropic (ANT) IPO and the AI market around it, writes anything
new to `feed/feed.json` (Almanac reads this) and sends an alert through
[ntfy](https://ntfy.sh).

| Source | What it watches | Badge |
|---|---|---|
| SEC EDGAR | New Anthropic filings: amended S-1 (range change), final prospectus 424B4, effectiveness | Filed |
| Nasdaq IPO calendar | ANT status, price range, share count, date; flips to priced | Exchange |
| Polymarket | Day-one closing market cap and IPO date odds (alerts on 7-point moves or a new favourite) | Odds |
| Google News | Anthropic IPO, Nvidia, CoreWeave headlines from named outlets, last 6 hours only | Headline |
| Yahoo Finance | Nvidia, CoreWeave, Microsoft, Nasdaq-100 moves; VIX levels; ANT first trade and moves vs offer | Market |

A headline is a single report until a filing or a second outlet confirms it.

## Setup

1. **Settings → Secrets and variables → Actions → New repository secret**
   - `NTFY_TOPIC`: the private channel name (keep it secret; anyone with it can read the alerts).
   - `SEC_USER_AGENT` (optional): `Your Name you@yourdomain`. The SEC asks automated clients for a contact.
2. Subscribe to the same channel in the ntfy app on each phone.
3. **Actions → watch → Run workflow** to start it straight away. After that it runs on its own.

Tune thresholds, outlets and search terms in `config.json`. GitHub's scheduler
can run a few minutes late at busy times.

## Files

- `watch.py`: the checks (Python standard library plus curl, no installs)
- `config.json`: what to watch and when to alert
- `feed/feed.json`: newest-first alert feed for the app
- `state/state.json`: what has already been seen, so nothing alerts twice
