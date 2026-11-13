# NSE Momentum Lab

A simple quant bot that paper-trades (fake money) mid- and small-cap NSE stocks, plus a live dashboard.

## Two bots
Both pick from the ~400 companies in the Nifty MidSmallcap 400 index, start with their own ₹10,00,000 of practice money, re-pick monthly and pay 0.2% per trade. Settings are in `MODELS` in `trader.py`.

| | Growth bot | Steady bot |
|---|---|---|
| Picks | the 20 biggest risers over 12 months | the 20 calmest stocks among the best-rising third over 6 months |
| Must also be | above its 50-day average | above its 50-day average |
| Cash | always fully invested | moves part of the money to cash when the market is jumpy (checked weekly) |
| Aim | higher returns, bigger drops | smoother ride, smaller gains |

Each bot keeps its own files: `docs/state-growth.json` / `docs/state-steady.json` and a full timestamped trade log in `docs/trades-growth.csv` / `docs/trades-steady.csv`.

## Backtest (15 Nov 2021 to 13 Nov 2026, after costs)

| | Per year | Biggest drop | Worst year |
|---|---|---|---|
| Growth bot, today's index members | 35.8% | −35.6% | −12.4% |
| Growth bot, no hindsight | 24.5% | −34.9% | −14.7% |
| Steady bot, today's index members | 20.3% | −22.6% | −7.5% |
| Steady bot, no hindsight | 17.3% | −21.8% | −1.0% |
| Holding every index stock (no costs) | 23.4% | −22.1% | −1.2% |
| Nifty 50 | 6.8% | −16.5% | −4.1% |

"No hindsight" uses a stock list rebuilt each month from past data only; today's index members flatter the results because companies that did badly have since dropped out. Full table: `docs/backtest.txt`. Re-run from the Actions tab → *backtest*.

## Files
- `trader.py`: runs both bots: fetches prices (Yahoo Finance), re-picks on schedule, and records each portfolio's value on every run.
- `backtest.py`: replays the same rules on 5 years of history for several variants.
- `data/symbols.txt`: the official list of NSE shares, refreshed by the backtest workflow.
- `.github/workflows/trade.yml`: runs both bots every 30 min during NSE hours (Mon–Fri) and commits their state files and trade logs.
- `docs/index.html`: the dashboard. It shows portfolio value, % return vs the index, holdings and trades.

## Setup (one time)
1. Settings → Pages → Source: *Deploy from a branch* → this repo's default branch, `/docs` folder.
2. Actions tab → *paper-trade* → **Run workflow** (tick *reset*) to start.
3. Open `https://<your-user>.github.io/nse-momentum-lab/`.

Local test without internet: `pip install -r requirements.txt && python trader.py --demo --reset`.

*Paper trading only. This is not investment advice.*
