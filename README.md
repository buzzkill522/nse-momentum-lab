# NSE Momentum Lab

A simple quant bot that paper-trades (fake money) mid- and small-cap NSE stocks, plus a live dashboard.

## Strategy
Set in `CONFIG` at the top of `trader.py`.

1. **Stock pool:** the ~400 companies in the Nifty MidSmallcap 400 index (NSE's mid and small caps).
2. **Candidates:** stocks with a positive 12-month return (ignoring the latest month) that are above their own 50-day average.
3. **Pick:** the 20 biggest risers, one twentieth of the portfolio each. A held stock is kept while it stays in the top 40.
4. **Re-pick monthly.** Each trade is charged 0.2%.

## Backtest (15 Nov 2021 to 13 Nov 2026, after costs)

| | Per year | Worst fall | Worst year |
|---|---|---|---|
| This strategy, on today's index members | 36.4% | −35.7% | −10.0% |
| This strategy, on a list built from past data only | 24.5% | −34.9% | −14.7% |
| Holding every index stock (no costs) | 23.4% | −22.1% | −1.2% |
| Same rules on large caps | 7.3%* | −29.4%* | −10.6%* |
| Nifty 50 | 6.8% | −16.5% | −4.1% |

\* 6-month version; the 12-month version was not run on large caps.

Using today's index members flatters the results, because companies that did badly have since dropped out. The "past data only" row avoids that. Full table: `docs/backtest.txt`. Re-run from the Actions tab → *backtest*.

## Files
- `trader.py`: fetches prices (Yahoo Finance), re-picks on schedule, and records portfolio value on every run.
- `backtest.py`: replays the same rules on 5 years of history for several variants.
- `data/symbols.txt`: the official list of NSE shares, refreshed by the backtest workflow.
- `.github/workflows/trade.yml`: runs the bot every 30 min during NSE hours (Mon–Fri) and commits `docs/state.json`.
- `docs/index.html`: the dashboard. It shows portfolio value, % return vs the index, holdings and trades.

## Setup (one time)
1. Settings → Pages → Source: *Deploy from a branch* → this repo's default branch, `/docs` folder.
2. Actions tab → *paper-trade* → **Run workflow** (tick *reset*) to start.
3. Open `https://<your-user>.github.io/nse-momentum-lab/`.

Local test without internet: `pip install -r requirements.txt && python trader.py --demo --reset`.

*Paper trading only. This is not investment advice.*
