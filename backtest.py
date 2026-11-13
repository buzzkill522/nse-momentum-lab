"""
Backtest: how would different versions of the bot have done historically?

Replays trader.py's exact functions day by day on Yahoo Finance data:
  - on each rebalance morning, rank stocks using closes up to yesterday plus
    today's OPEN (the live bot decides shortly after the open)
  - trade at today's open, pay the same costs
  - value the portfolio at every day's close
Nothing from the future is used when making each day's decision.

Usage:  python backtest.py [years]       (default 5)
        python backtest.py --demo        (synthetic prices, offline)
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import trader as T

OUT = Path(__file__).parent / "docs" / "backtest.json"

OLD = dict(top_n=8, lookback=20, skip=0, trend_sma=50, rebalance="daily",
           buffer=1.0, regime=False, vol_adjust=False, resize=True)


def variants():
    v = {"old bot (20d, daily)": OLD,
         "old, monthly": {**OLD, "rebalance": "monthly", "resize": False}}
    for lb, name in ((126, "6m"), (252, "12m")):
        for freq in ("weekly", "monthly"):
            for regime in (False, True):
                v[f"{name} {freq}{' +mkt' if regime else ''}"] = {
                    **T.CONFIG, "lookback": lb, "rebalance": freq, "regime": regime, "vol_adjust": False}
        v[f"{name} monthly +mkt +vol"] = {**T.CONFIG, "lookback": lb, "rebalance": "monthly",
                                          "regime": True, "vol_adjust": True}
    for n in (5, 20):
        v[f"6m monthly +mkt top{n}"] = {**T.CONFIG, "lookback": 126, "rebalance": "monthly",
                                        "regime": True, "vol_adjust": False, "top_n": n}
    v["LIVE CONFIG"] = dict(T.CONFIG)
    return v


def load(demo):
    if demo:
        rng = np.random.default_rng(7)
        idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=1700)
        n = len(T.ALL_SYMBOLS)
        rets = rng.normal(rng.normal(0.0004, 0.0008, n), 0.018, (len(idx), n))
        close = pd.DataFrame(1000 * np.exp(np.cumsum(rets, axis=0)), index=idx, columns=T.ALL_SYMBOLS)
        opn = close.shift(1).fillna(1000) * (1 + rng.normal(0, 0.004, close.shape))
        return opn, close
    import yfinance as yf
    df = yf.download(T.ALL_SYMBOLS, period="10y", interval="1d", auto_adjust=True, progress=False)
    close = df["Close"].dropna(how="all").ffill()
    opn = df["Open"].reindex(close.index)
    return opn.fillna(close), close           # indices have no reliable open


def simulate(opn, close, days, cfg):
    """Returns (daily portfolio value Series, state)."""
    state, values, last_key = T.new_state(), [], None
    for d in days:
        key = T.period_key(d.date(), cfg["rebalance"])
        if key != last_key:
            last_key = key
            hist = close.loc[:d].copy()
            hist.loc[d] = opn.loc[d].fillna(hist.iloc[-2])      # today's row = open price only
            targets = T.pick_stocks(hist, cfg, held=list(state["positions"]))
            T.rebalance(state, hist.iloc[-1], targets, d.date().isoformat(), cfg)
        values.append(T.portfolio_value(state, close.loc[d]))
    return pd.Series(values, index=days), state


def stats(v, start_value, years, state=None):
    """v = daily value series."""
    total = v.iloc[-1] / start_value
    daily = v.pct_change().dropna()
    out = {
        "total %": (total - 1) * 100,
        "per yr %": (total ** (1 / years) - 1) * 100,
        "worst fall %": float((v / v.cummax() - 1).min() * 100),
        "sharpe": float(daily.mean() / daily.std() * np.sqrt(252)) if daily.std() else 0.0,
    }
    yr_end = v.groupby(v.index.year).last()
    prev = start_value
    for y, val in yr_end.items():
        out[str(y)] = (val / prev - 1) * 100
        prev = val
    for label, n in (("last 12m", 252), ("last 6m", 126)):
        out[label] = (v.iloc[-1] / v.iloc[-n - 1] - 1) * 100 if len(v) > n else np.nan
    if state is not None:
        out["trades/yr"] = len(state["trades"]) / years
        out["fees %/yr"] = sum(t["fee"] for t in state["trades"]) / start_value * 100 / years
    return out


def main():
    demo = "--demo" in sys.argv
    years = next((int(a) for a in sys.argv[1:] if a.isdigit()), 5)
    opn, close = load(demo)
    have = [t for t in T.TICKERS if t in close and close[t].notna().sum() > 0]
    missing = [t.replace(".NS", "") for t in T.TICKERS if t not in have]
    days = close.index[close.index > close.index[-1] - pd.DateOffset(years=years)]
    yrs = (days[-1] - days[0]).days / 365.25
    cap = T.STARTING_CAPITAL
    print(f"Period: {days[0].date()} to {days[-1].date()}  ({len(days)} trading days)")
    print(f"Stocks with data: {len(have)}/{len(T.TICKERS)}   no data: {missing}")
    at_start = [t for t in have if pd.notna(close.loc[days[0], t])]
    print(f"Stocks already listed at start: {len(at_start)}")

    rows, curves = {}, {}
    # comparison 1: an index
    bsym = next((s for s in T.BENCHMARKS if s in close and pd.notna(close[s].loc[:days[0]].tail(1)).all()
                 and close[s].loc[:days[0]].notna().sum() > 1), T.REGIME_INDEX)
    b = close[bsym].loc[days] / close[bsym].loc[:days[0]].iloc[-2] * cap
    rows[f"INDEX: {T.BENCHMARKS.get(bsym, bsym)}"] = stats(b, cap, yrs)
    # comparison 2: buy every stock in the list on day one and hold
    shares = (cap * (1 - T.COST) / len(at_start)) / opn.loc[days[0], at_start]
    h = (close.loc[days, at_start] * shares).sum(axis=1)
    rows["HOLD ALL STOCKS"] = stats(h, cap, yrs)
    curves["index"], curves["hold_all"] = b, h

    for name, cfg in variants().items():
        v, state = simulate(opn, close, days, cfg)
        rows[name] = stats(v, cap, yrs, state)
        curves[name] = v

    table = pd.DataFrame(rows).T
    pd.set_option("display.width", 250)
    print("=== RESULTS (all figures after costs) ===")
    print(table.round(1).to_string())

    live = curves["LIVE CONFIG"]
    weekly = pd.DataFrame({"live": live, "index": b, "hold_all": h}).iloc[::5]
    OUT.write_text(json.dumps({
        "period": f"{days[0].date()} to {days[-1].date()}", "demo": demo,
        "config": T.CONFIG, "description": T.describe(),
        "table": json.loads(table.round(2).to_json(orient="index")),
        "curve": [{"date": d.date().isoformat(), **{k: round((x / cap - 1) * 100, 2) for k, x in r.items()}}
                  for d, r in weekly.iterrows()],
    }, indent=1))


if __name__ == "__main__":
    main()
