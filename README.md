# NSE Momentum Lab

A simple quant bot that paper-trades (fake money) mid- and small-cap NSE stocks, plus a live dashboard.

## Strategy
Set in `CONFIG` at the top of `trader.py`. Nothing is hand-picked.

1. **Stock list:** every month the bot ranks all ~2,400 NSE-listed shares by how much money trades in them, skips the 100 most traded (large caps) and keeps the next 400.
2. **Candidates:** stocks with a positive 6-month return (ignoring the latest month) that are above their own 50-day average.
3. **Pick:** from the top third of candidates by return, hold the 20 calmest (lowest volatility), one twentieth of the portfolio each. A held stock is kept while it stays in the top 40.
4. **Exposure:** when the market gets unusually jumpy, part of the money moves to cash. Checked weekly.
5. **Re-pick monthly.** Each trade is charged 0.2%.

It starts with ₹10,00,000 and is compared against the Nifty Midcap 100.

## Backtest (15 Nov 2021 to 13 Nov 2026, after costs, no hindsight in the stock list)

| | Per year | Worst fall | Months up | Worst year |
|---|---|---|---|---|
| This strategy | 17.3% | −21.8% | 68% | −1.0% |
| All 400 stocks on the bot's list (no costs) | 16.4% | −25.2% | 62% | −2.3% |
| Plain momentum (highest return, always invested) | 16.5% | −37.7% | 62% | −20.3% |
| Nifty 50 | 6.8% | −16.5% | 55% | −4.1% |

Most of the gain came from 2023–24: the first half of the test made 33% a year, the second half 4%. Companies delisted since 2021 are missing from Yahoo Finance, which flatters every row a little. Full table of all variants: `docs/backtest.txt`. Re-run from the Actions tab → *backtest*.

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
