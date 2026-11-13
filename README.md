# NSE Momentum Lab

A simple quant bot that paper-trades (fake money) mid- and small-cap NSE stocks, plus a live dashboard.

## Strategy
Set in `CONFIG` at the top of `trader.py`:

1. **Market check:** if the Nifty 50 is below its 200-day average, hold cash.
2. **Rank** 111 mid- and small-cap stocks by 6-month return, ignoring the most recent 21 trading days.
3. **Filter:** the return must be positive and the stock must be above its own 50-day average.
4. **Hold the top 10**, one tenth of the portfolio each. A held stock is kept while it stays in the top 20.
5. **Re-pick monthly**, on the first trading day of the month. Each trade is charged 0.2%.

It starts with ₹10,00,000 and is compared against the Nifty Midcap 100.

## Backtest (15 Nov 2021 to 13 Nov 2026, after costs)

| | Total | Per year | Worst fall |
|---|---|---|---|
| This strategy | +152% | 20.4% | −20.8% |
| Same, without the market check | +169% | 21.9% | −36.5% |
| Buy all 111 stocks and hold | +127% | 17.9% | −25.3% |
| Nifty 50 | +39% | 6.8% | −16.5% |
| Original version (20-day return, daily trading) | −60% | −16.7% | −62.3% |

The stock list was hand-picked in 2026, so every row except the Nifty 50 is flattered by hindsight. Compare the strategy against "buy all and hold", not against the index. Re-run from the Actions tab → *backtest*.

## Files
- `trader.py`: fetches prices (Yahoo Finance), re-picks on schedule, and records portfolio value on every run.
- `backtest.py`: replays the same rules on 5 years of history for several variants.
- `.github/workflows/trade.yml`: runs the bot every 30 min during NSE hours (Mon–Fri) and commits `docs/state.json`.
- `docs/index.html`: the dashboard. It shows portfolio value, % return vs the index, holdings and trades.

## Setup (one time)
1. Settings → Pages → Source: *Deploy from a branch* → this repo's default branch, `/docs` folder.
2. Actions tab → *paper-trade* → **Run workflow** (tick *reset*) to start.
3. Open `https://<your-user>.github.io/nse-momentum-lab/`.

Local test without internet: `pip install -r requirements.txt && python trader.py --demo --reset`.

*Paper trading only. This is not investment advice.*
