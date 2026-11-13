# NSE Momentum Lab

A simple quant bot that paper-trades (fake money) mid- and small-cap NSE stocks, plus a live dashboard.

**Strategy:** every trading day it holds the top 8 of 40 mid- and small-cap NSE stocks ranked by 20-day return. A stock only qualifies if it trades above its 50-day moving average. Positions are equal weight, and each trade is charged a 0.2% cost (mid/small caps have wider spreads). It starts with ₹10,00,000 and is compared against the Nifty Midcap 100 (falls back to Nifty 50 if Yahoo has no data for it).

## How it runs
- `trader.py`: fetches prices (Yahoo Finance), rebalances once a day, and records portfolio value on every run.
- `.github/workflows/trade.yml`: runs the bot every 30 min during NSE hours (Mon–Fri) and commits `docs/state.json`.
- `docs/index.html`: the dashboard. It shows portfolio value, % return vs Nifty, holdings and trades.

## Setup (one time)
1. Merge this into `main`. GitHub only runs scheduled workflows on the default branch.
2. Settings → Pages → Source: *Deploy from a branch* → `main` / `/docs`.
3. Actions tab → *paper-trade* → **Run workflow** (tick *reset*) to start the week.
4. Open `https://<your-user>.github.io/nse-momentum-lab/`.

Local test without internet: `pip install -r requirements.txt && python trader.py --demo --reset`.

*Paper trading only. This is not investment advice.*
