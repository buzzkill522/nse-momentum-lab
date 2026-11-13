"""
Backtest: how would the bot have done over the last N months?

Replays trader.py's exact rules day by day on historical Yahoo Finance data:
  - each morning, rank stocks using closes up to yesterday + today's OPEN
    (the live bot decides shortly after the open, using the current price)
  - trade at today's open, pay the same costs
  - value the portfolio at today's close
Nothing from the future is used when making each day's decision.

Usage:  python backtest.py [months]      (default 6)
        python backtest.py --demo        (synthetic prices, offline)
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import trader as T

OUT = Path(__file__).parent / "docs" / "backtest.json"


def load(demo):
    cols = T.TICKERS + list(T.BENCHMARKS)
    if demo:
        rng = np.random.default_rng(7)
        idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=330)
        rets = rng.normal(rng.normal(0.0004, 0.001, len(cols)), 0.018, (len(idx), len(cols)))
        close = pd.DataFrame(1000 * np.exp(np.cumsum(rets, axis=0)), index=idx, columns=cols)
        opn = close.shift(1).fillna(1000) * (1 + rng.normal(0, 0.004, close.shape))
        return opn, close
    import yfinance as yf
    df = yf.download(cols, period="2y", interval="1d", auto_adjust=True, progress=False)
    close = df["Close"].dropna(how="all")
    opn = df["Open"].reindex(close.index)
    return opn.fillna(close), close.ffill()   # indices have no reliable open


def max_drawdown(series):
    s = pd.Series(series)
    return float((s / s.cummax() - 1).min() * 100)


def run(opn, close, months, every=1, demo=False, verbose=True):
    """every = rebalance every N trading days (1 = daily, like the live bot)."""
    missing = [t for t in T.TICKERS if t not in close or close[t].notna().sum() < 100]
    start = close.index[-1] - pd.DateOffset(months=months)
    days = close.index[close.index > start]

    state = T.new_state()
    bsym = next(s for s in T.BENCHMARKS if s in close and close[s].notna().sum() > 100)
    prev = close.index[close.index.get_loc(days[0]) - 1]   # day before the test starts
    bench0 = float(close[bsym][prev])
    # "just hold everything" comparison: equal money in every stock, bought at first open
    live = [t for t in T.TICKERS if t not in missing]
    hold_shares = (T.STARTING_CAPITAL * (1 - T.COST) / len(live)) / opn.loc[days[0], live]

    rows, n_hold = [], []
    for i, d in enumerate(days):
        if i % every == 0:
            hist = close.loc[:d].copy()
            hist.loc[d] = opn.loc[d].fillna(hist.iloc[-2])     # today's row = open price only
            targets = T.pick_stocks(hist)
            T.rebalance(state, hist.iloc[-1], targets, d.date().isoformat())
        value = T.portfolio_value(state, close.loc[d])
        n_hold.append(len(state["positions"]))
        rows.append({
            "date": d.date().isoformat(),
            "strategy": round((value / T.STARTING_CAPITAL - 1) * 100, 2),
            "benchmark": round((float(close[bsym][d]) / bench0 - 1) * 100, 2),
            "hold_all": round((float((hold_shares * close.loc[d, live]).sum()) / T.STARTING_CAPITAL - 1) * 100, 2),
            "value": round(value, 2),
        })

    res = pd.DataFrame(rows).set_index("date")
    values = res["value"]
    fees = sum(t["fee"] for t in state["trades"])
    daily = values.pct_change().dropna()
    monthly = {}
    for col in ("strategy", "benchmark", "hold_all"):
        lvl = 1 + res[col] / 100
        lvl.index = pd.to_datetime(lvl.index)
        m = lvl.resample("ME").last()
        monthly[col] = ((m / m.shift(1).fillna(1) - 1) * 100).round(2)
    summary = {
        "period": f"{rows[0]['date']} to {rows[-1]['date']}", "trading_days": len(rows),
        "benchmark_name": T.BENCHMARKS[bsym], "demo": demo, "rebalance_every_days": every,
        "final_value": round(float(values.iloc[-1])),
        "strategy_return_pct": rows[-1]["strategy"],
        "benchmark_return_pct": rows[-1]["benchmark"],
        "hold_all_return_pct": rows[-1]["hold_all"],
        "max_drawdown_pct": round(max_drawdown(values), 2),
        "benchmark_max_drawdown_pct": round(max_drawdown(1 + res["benchmark"] / 100), 2),
        "worst_day_pct": round(float(daily.min() * 100), 2),
        "best_day_pct": round(float(daily.max() * 100), 2),
        "up_days_pct": round(float((daily > 0).mean() * 100), 1),
        "trades": len(state["trades"]), "fees_paid": round(fees),
        "fees_pct_of_capital": round(fees / T.STARTING_CAPITAL * 100, 2),
        "avg_stocks_held": round(float(np.mean(n_hold)), 1),
        "days_all_cash": int(sum(1 for n in n_hold if n == 0)),
        "missing_tickers": [t.replace(".NS", "") for t in missing],
    }
    pnl = {}
    for t in state["trades"]:
        pnl[t["ticker"]] = pnl.get(t["ticker"], 0) + (-1 if t["side"] == "BUY" else 1) * t["qty"] * t["price"] - t["fee"]
    for tk, p in state["positions"].items():
        k = tk.replace(".NS", "")
        pnl[k] = pnl.get(k, 0) + p["qty"] * float(close[tk].iloc[-1])
    by_stock = sorted(((k, round(v)) for k, v in pnl.items()), key=lambda x: -x[1])

    if not verbose:
        return summary
    print("=== SUMMARY ===")
    print(json.dumps(summary, indent=1))
    print("=== MONTHLY RETURNS % ===")
    print(pd.DataFrame(monthly).to_string())
    print("=== PROFIT/LOSS BY STOCK (Rs) ===")
    print("best :", by_stock[:6])
    print("worst:", by_stock[-6:])
    print("=== EQUITY CURVE (weekly) ===")
    print(res[["strategy", "benchmark", "hold_all"]].iloc[::5].to_string())
    OUT.write_text(json.dumps({"summary": summary, "curve": rows, "by_stock": by_stock}, indent=1))
    return summary


def main():
    demo = "--demo" in sys.argv
    months = next((int(a) for a in sys.argv[1:] if a.isdigit()), 6)
    opn, close = load(demo)
    run(opn, close, months, 1, demo)
    print("=== VARIANTS (same rules, different period / rebalance frequency) ===")
    keys = ["strategy_return_pct", "benchmark_return_pct", "hold_all_return_pct",
            "max_drawdown_pct", "trades", "fees_pct_of_capital"]
    table = {}
    for m in (3, 6, 12):
        for every in (1, 5, 20):
            r = run(opn, close, m, every, demo, verbose=False)
            table[f"{m}mo, rebalance every {every}d"] = [r[k] for k in keys]
    print(pd.DataFrame(table, index=keys).T.to_string())


if __name__ == "__main__":
    main()
