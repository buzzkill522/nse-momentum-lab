"""
Backtest: how would different versions of the bot have done historically?

No hindsight in the stock list: at each month start the bot builds its list
from every NSE share using only data up to the previous day (see
trader.choose_universe). The only remaining gap is companies that have since
been delisted, which Yahoo Finance no longer carries.

Each rebalance morning the bot sees closes up to yesterday plus today's OPEN,
trades at the open, and the portfolio is valued at every close.

Usage:  python backtest.py [years]       (default 5)
        python backtest.py --demo        (synthetic prices, offline)
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import trader as T

OUT = Path(__file__).parent / "docs"


def variants():
    base = {**T.CONFIG, "select": "mom", "weighting": "equal", "exposure": [], "risk_check": None, "top_n": 20}
    wk = {"risk_check": "weekly"}
    v = {
        "mom, always invested": base,
        "mom +regime monthly": {**base, "exposure": ["regime"]},
        "mom +regime": {**base, **wk, "exposure": ["regime"]},
        "mom +breadth": {**base, **wk, "exposure": ["breadth"]},
        "mom +voltarget": {**base, **wk, "exposure": ["voltarget"]},
        "mom +regime+voltarget": {**base, **wk, "exposure": ["regime", "voltarget"]},
        "mom +breadth+voltarget": {**base, **wk, "exposure": ["breadth", "voltarget"]},
        "mom/vol +breadth": {**base, **wk, "select": "mom_vol", "exposure": ["breadth"]},
        "lowvol, always invested": {**base, "select": "lowvol"},
        "lowvol +regime": {**base, **wk, "select": "lowvol", "exposure": ["regime"]},
        "lowvol +breadth": {**base, **wk, "select": "lowvol", "exposure": ["breadth"]},
        "lowvol +voltarget": {**base, **wk, "select": "lowvol", "exposure": ["voltarget"]},
        "lowvol +breadth+voltarget": {**base, **wk, "select": "lowvol", "exposure": ["breadth", "voltarget"]},
        "lowvol +regime+voltarget": {**base, **wk, "select": "lowvol", "exposure": ["regime", "voltarget"]},
        "lowvol invvol +breadth": {**base, **wk, "select": "lowvol", "weighting": "inv_vol", "exposure": ["breadth"]},
        "lowvol top10 +breadth": {**base, **wk, "select": "lowvol", "exposure": ["breadth"], "top_n": 10},
        "lowvol top30 +breadth": {**base, **wk, "select": "lowvol", "exposure": ["breadth"], "top_n": 30},
        "lowvol 12m +breadth": {**base, **wk, "select": "lowvol", "exposure": ["breadth"], "lookback": 252},
        "LIVE CONFIG": dict(T.CONFIG),
    }
    return v


class Sim:
    def __init__(self, opn, close, volume, days):
        self.opn, self.close, self.volume, self.days = opn, close, volume, days
        self.pos = {d: i for i, d in enumerate(close.index)}
        self._universes = {}

    def universe(self, d, cfg):
        key = (d, cfg["universe_skip"], cfg["universe_size"], cfg["min_price"])
        if key not in self._universes:
            i = self.pos[d]
            self._universes[key] = T.choose_universe(self.close.iloc[max(0, i - 126): i],
                                                     self.volume.iloc[max(0, i - 126): i], cfg)
        return self._universes[key]

    def view(self, d, cols):
        """Prices as the bot sees them on the morning of d."""
        i = self.pos[d]
        hist = self.close[cols].iloc[max(0, i - 330): i + 1].copy()
        hist.iloc[-1] = self.opn.loc[d, cols].fillna(hist.iloc[-2])
        return hist

    def run(self, cfg):
        state, values = T.new_state(), []
        for d in self.days:
            mkey = T.period_key(d.date(), cfg["rebalance"])
            rkey = T.period_key(d.date(), cfg["risk_check"]) if cfg["risk_check"] else mkey
            if state["last_rebalance"] != mkey or state["last_risk"] != rkey:
                uni = self.universe(d, cfg) if state["last_rebalance"] != mkey else state["universe"]
                cols = list(dict.fromkeys(uni + list(state["positions"]) + [T.REGIME_INDEX]))
                T.step(state, self.view(d, cols), d.date(), d.date().isoformat(), cfg, lambda: uni)
            values.append(T.portfolio_value(state, self.close.loc[d]))
        return pd.Series(values, index=self.days), state

    def hold_universe(self, cfg):
        """Equal money in every stock on the bot's list, refreshed monthly, no costs."""
        rets = self.close.pct_change(fill_method=None)
        out, uni, last = [], [], None
        for d in self.days:
            key = T.period_key(d.date(), "monthly")
            if key != last:
                uni, last = self.universe(d, cfg), key
            r = rets.loc[d, uni]
            out.append(float(r[r.abs() < 0.4].mean()))
        return T.STARTING_CAPITAL * (1 + pd.Series(out, index=self.days).fillna(0)).cumprod()


def stats(v, cap, state=None):
    yrs = (v.index[-1] - v.index[0]).days / 365.25
    daily = v.pct_change().dropna()
    m = pd.concat([pd.Series([cap], index=[v.index[0] - pd.Timedelta(days=1)]), v]).resample("ME").last().pct_change().dropna()
    r6 = v.pct_change(126).dropna()
    half = len(v) // 2
    ann = lambda a, b, n: ((b / a) ** (252 / n) - 1) * 100  # noqa: E731
    yr_end = v.groupby(v.index.year).last()
    yr = (yr_end / yr_end.shift(1).fillna(cap) - 1) * 100
    out = {
        "per yr %": ((v.iloc[-1] / cap) ** (1 / yrs) - 1) * 100,
        "swing %": float(daily.std() * np.sqrt(252) * 100),
        "sharpe": float(daily.mean() / daily.std() * np.sqrt(252)) if daily.std() else 0.0,
        "worst fall %": float((v / v.cummax() - 1).min() * 100),
        "up months %": float((m > 0).mean() * 100),
        "worst month %": float(m.min() * 100),
        "worst 6m %": float(r6.min() * 100) if len(r6) else np.nan,
        "6m windows up %": float((r6 > 0).mean() * 100) if len(r6) else np.nan,
        "worst yr %": float(yr.min()),
        "1st half /yr": ann(cap, v.iloc[half], half),
        "2nd half /yr": ann(v.iloc[half], v.iloc[-1], len(v) - half),
        "last 6m %": (v.iloc[-1] / v.iloc[-127] - 1) * 100 if len(v) > 127 else np.nan,
    }
    for y, x in yr.items():
        out[str(y)] = float(x)
    if state is not None:
        out["trades/yr"] = len(state["trades"]) / yrs
        out["fees %/yr"] = sum(t["fee"] for t in state["trades"]) / cap * 100 / yrs
    return out


def main():
    demo = "--demo" in sys.argv
    years = next((int(a) for a in sys.argv[1:] if a.isdigit()), 5)
    syms = T.all_tickers(demo) + T.INDEX_SYMBOLS
    opn, close, volume = T.demo_data(syms, 252 * (years + 2), seed=7) if demo else T.download(syms, f"{years + 2}y")
    days = close.index[close.index > close.index[-1] - pd.DateOffset(years=years)]
    cap = T.STARTING_CAPITAL
    sim = Sim(opn, close, volume, days)
    lines = [f"Period: {days[0].date()} to {days[-1].date()}  ({len(days)} trading days)",
             f"NSE shares requested: {len(syms) - len(T.INDEX_SYMBOLS)}, with price data: {close.shape[1] - len(T.INDEX_SYMBOLS)}",
             f"Stock list size at start / end: {len(sim.universe(days[0], T.CONFIG))} / {len(sim.universe(days[-1], T.CONFIG))}"]

    rows, curves = {}, {}
    idx = close[T.REGIME_INDEX]
    curves["Nifty 50"] = idx.loc[days] / idx.loc[:days[0]].iloc[-2] * cap
    curves["ALL 400 LISTED (no costs)"] = sim.hold_universe(T.CONFIG)
    for k in list(curves):
        rows[k] = stats(curves[k], cap)
    for name, cfg in variants().items():
        v, state = sim.run(cfg)
        rows[name], curves[name] = stats(v, cap, state), v

    table = pd.DataFrame(rows).T
    pd.set_option("display.width", 400)
    pd.set_option("display.max_columns", 50)
    lines += ["=== RESULTS (bot figures are after costs) ===", table.round(1).to_string()]
    text = "\n".join(lines)
    print(text)
    OUT.mkdir(exist_ok=True)
    (OUT / "backtest.txt").write_text(text + "\n")
    weekly = pd.DataFrame({"live": curves["LIVE CONFIG"], "nifty50": curves["Nifty 50"],
                           "all_listed": curves["ALL 400 LISTED (no costs)"]}).iloc[::5]
    (OUT / "backtest.json").write_text(json.dumps({
        "period": f"{days[0].date()} to {days[-1].date()}", "demo": demo,
        "config": T.CONFIG, "description": T.describe(),
        "table": json.loads(table.round(2).to_json(orient="index")),
        "curve": [{"date": d.date().isoformat(), **{k: round((x / cap - 1) * 100, 2) for k, x in r.items()}}
                  for d, r in weekly.iterrows()],
    }, indent=1))


if __name__ == "__main__":
    main()
