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


POOLS = {"index": {"universe": "index"},                                              # today's index members
         "nohind": {"universe": "traded", "universe_skip": 100, "universe_size": 400},     # rebuilt monthly from past data
         "large": {"universe": "traded", "universe_skip": 0, "universe_size": 100},       # 100 most traded
         "mcap": {"universe": "mcap", "universe_skip": 100, "universe_size": 400}}        # market-value ranks 101-500


def nse_variants():
    """For official NSE data: both bots on every stock list."""
    v = {}
    for pool in ("nohind", "mcap", "index", "large"):
        for mid, m in T.MODELS.items():
            v[f"MODEL {mid} [{pool}]"] = {**m["config"], **POOLS[pool]}
    g = T.MODELS["growth"]["config"]
    for pool in ("nohind", "mcap"):
        one = {**g, **POOLS[pool], "tranches": 1}
        v[f"CHECK growth all monthly [{pool}]"] = one
        for off in (0, 1, 2):
            v[f"CHECK growth all quarterly, start month +{off} [{pool}]"] = {**one, "rebalance": "quarterly", "rebalance_offset": off}
    return v


def variants():
    w = {**T.CONFIG, "select": "mom", "weighting": "equal", "exposure": [], "risk_check": "weekly", "top_n": 20,
         "lookback": 126, "lookback2": 0, "near_high": 0.0, "cut_losers": False}
    kinds = {
        "winners": w,
        "winners 12m": {**w, "lookback": 252},
        "winners 6m+12m": {**w, "lookback2": 252},
        "winners near high": {**w, "near_high": 0.85},
        "winners cut losers": {**w, "cut_losers": True},
        "winners near high + cut": {**w, "near_high": 0.85, "cut_losers": True},
        "winners 6m+12m near high + cut": {**w, "lookback2": 252, "near_high": 0.85, "cut_losers": True},
        "winners top10": {**w, "top_n": 10},
        "winners + rough-market cash": {**w, "exposure": ["voltarget"]},
        "winners near high + cut + cash": {**w, "near_high": 0.85, "cut_losers": True, "exposure": ["voltarget"]},
        "calm risers (previous bot)": {**w, "select": "lowvol", "exposure": ["voltarget"]},
        "calm risers, always invested": {**w, "select": "lowvol"},
    }
    v = {}
    for pool in ("index", "nohind"):
        for name, cfg in kinds.items():
            v[f"{name} [{pool}]"] = {**cfg, **POOLS[pool]}
    for name in ("winners", "winners 12m", "winners near high + cut", "calm risers (previous bot)"):
        v[f"{name} [large]"] = {**kinds[name], **POOLS["large"]}
    # robustness: do the adopted changes hold up when varied?
    g, s = T.MODELS["growth"]["config"], T.MODELS["steady"]["config"]
    for pool in ("index", "nohind"):
        one = {**g, **POOLS[pool], "tranches": 1}
        v[f"CHECK growth all monthly [{pool}]"] = one
        for off in (0, 1, 2):
            v[f"CHECK growth all quarterly, start month +{off} [{pool}]"] = {**one, "rebalance": "quarterly", "rebalance_offset": off}
        v[f"CHECK steady, thirds [{pool}]"] = {**s, **POOLS[pool], "tranches": 3}
        v[f"CHECK steady, no sector cap [{pool}]"] = {**s, **POOLS[pool], "sector_cap": 0}
        v[f"CHECK steady, no cash interest [{pool}]"] = {**s, **POOLS[pool], "cash_yield": 0.0}
    for mid, m in T.MODELS.items():
        v[f"MODEL {mid}"] = dict(m["config"])
        v[f"MODEL {mid} [nohind]"] = {**m["config"], **POOLS["nohind"]}
    return v


def experiments():
    """One change at a time to each bot, on both stock lists, to see which decisions matter."""
    both = {
        "base": {},
        "keep-zone 1.5x": {"buffer": 1.5}, "keep-zone 3x": {"buffer": 3.0},
        "re-pick quarterly": {"rebalance": "quarterly"},
        "sector cap 4": {"sector_cap": 4}, "sector cap 6": {"sector_cap": 6},
        "calmer stocks get more money": {"weighting": "inv_vol"},
        "resize only if 50% off": {"drift": 0.5},
        "hold 15": {"top_n": 15}, "hold 30": {"top_n": 30},
        "no skip month": {"skip": 0},
        "trend 100d": {"trend_sma": 100}, "trend 200d": {"trend_sma": 200},
        "cash interest 6%": {"cash_yield": 0.06},
    }
    only = {
        "growth": {"rise 6m": {"lookback": 126}, "rise 9m": {"lookback": 189},
                   "rough-market cash, step 10": {"exposure": ["voltarget"], "risk_check": "weekly", "exposure_step": 0.1}},
        "steady": {"rise 9m": {"lookback": 189}, "rise 12m": {"lookback": 252},
                   "cash step 10": {"exposure_step": 0.1}, "cash step 20": {"exposure_step": 0.2},
                   "cash checked monthly": {"risk_check": None},
                   "target swing 12%": {"vol_target": 0.12}, "target swing 20%": {"vol_target": 0.20},
                   "cash step 10 + interest 6%": {"exposure_step": 0.1, "cash_yield": 0.06}},
    }
    v = {}
    for mid, m in T.MODELS.items():
        for pool in ("nohind", "index"):
            for name, ch in {**both, **only[mid]}.items():
                v[f"{mid:6s} | {pool:6s} | {name}"] = {**m["config"], **POOLS[pool], **ch}
    return v


class Sim:
    def __init__(self, opn, close, volume, days):
        self.opn, self.close, self.volume, self.days = opn, close, volume, days
        self.pos = {d: i for i, d in enumerate(close.index)}
        self._universes = {}

    def universe(self, d, cfg):
        key = (d, cfg["universe"], cfg["universe_skip"], cfg["universe_size"], cfg["min_price"])
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
            mkey = T.period_key(d.date(), cfg["rebalance"], cfg.get("rebalance_offset", 0))
            rkey = T.period_key(d.date(), cfg["risk_check"]) if cfg["risk_check"] else mkey
            T.accrue_interest(state, d.date(), cfg)
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


def nse_main(years):
    """Backtest on official NSE prices for every listed share, including delisted ones."""
    sys.path.insert(0, str(Path(__file__).parent / "tools"))
    import nse_data as N
    panel = N.load_panel()
    opn, close, volume, mcap = N.as_matrices(panel)
    # index levels (Nifty 50 and benchmarks) still come from Yahoo
    _, iclose, _ = T.download(T.INDEX_SYMBOLS, f"{years + 3}y")
    iclose = iclose.reindex(close.index).ffill()
    for c in iclose.columns:
        close[c] = iclose[c]
        opn[c] = iclose[c]
        volume[c] = 0.0
    members = [m for m in T.index_members() if m in close.columns]
    T.CONFIG["_members"] = members
    for m in T.MODELS.values():
        m["config"]["_members"] = members
    POOLS["mcap"]["_mcap"] = mcap
    days = close.index[close.index > close.index[-1] - pd.DateOffset(years=years)]
    cap = T.STARTING_CAPITAL
    sim = Sim(opn, close, volume, days)
    last_trade = panel["last"].rename(lambda c: c + ".NS")
    lines = [f"Source: official NSE bhavcopy, every listed company including delisted ones",
             f"Period: {days[0].date()} to {days[-1].date()}  ({len(days)} trading days)",
             f"Companies in the data: {panel['meta']['companies']}, of which stopped trading before the end: "
             f"{panel['meta']['stopped_trading']}; with share counts: {panel['meta']['with_shares']}",
             f"Index members found in NSE data: {len(members)}",
             "Stock list size at start / end: " + ", ".join(
                 f"{k} {len(sim.universe(days[0], {**T.CONFIG, **p}))} / {len(sim.universe(days[-1], {**T.CONFIG, **p}))}"
                 for k, p in POOLS.items())]
    rows, curves = {}, {}
    idx = close[T.REGIME_INDEX]
    curves["Nifty 50"] = idx.loc[days] / idx.loc[:days[0]].iloc[-2] * cap
    for k in ("nohind", "mcap", "index", "large"):
        curves[f"ALL [{k}] (no costs)"] = sim.hold_universe({**T.CONFIG, **POOLS[k]})
    for k in list(curves):
        rows[k] = stats(curves[k], cap)
    for name, cfg in nse_variants().items():
        v, state = sim.run(cfg)
        r = stats(v, cap, state)
        # sales of shares that had already stopped trading (delisted or suspended)
        stuck = [t for t in state["trades"] if t["side"] == "SELL"
                 and last_trade.get(t["ticker"] + ".NS", pd.Timestamp.max) < pd.Timestamp(t["time"]) - pd.Timedelta(days=14)]
        r["stuck sales"] = len(stuck)
        rows[name], curves[name] = r, v
    table = pd.DataFrame(rows).T
    pd.set_option("display.width", 400)
    pd.set_option("display.max_columns", 50)
    lines += ["=== RESULTS ON NSE DATA (bot figures are after costs) ===", table.round(1).to_string()]
    text = "\n".join(lines)
    print(text)
    OUT.mkdir(exist_ok=True)
    (OUT / "backtest_nse.txt").write_text(text + "\n")
    weekly = pd.DataFrame({k: v for k, v in curves.items()}).iloc[::5]
    (OUT / "backtest_nse.json").write_text(json.dumps({
        "period": f"{days[0].date()} to {days[-1].date()}", "source": "nse", "meta": panel["meta"],
        "table": json.loads(table.round(2).to_json(orient="index")),
        "curve": [{"date": d.date().isoformat(), **{k: round((x / cap - 1) * 100, 2) for k, x in r.items() if pd.notna(x)}}
                  for d, r in weekly.iterrows()],
    }, indent=1))
    # data check: NSE against Yahoo for today's index members
    _, yclose, _ = T.download(members, f"{years + 2}y")
    (OUT / "data_report.txt").write_text(N.report(yclose) + "\n")


def main():
    demo = "--demo" in sys.argv
    years = next((int(a) for a in sys.argv[1:] if a.isdigit()), 5)
    if "--source=nse" in sys.argv:
        return nse_main(years)
    syms = T.all_tickers(demo) + T.INDEX_SYMBOLS
    T.CONFIG["_members"] = syms[100:500] if demo else T.index_members()
    for m in T.MODELS.values():
        m["config"]["_members"] = T.CONFIG["_members"]
    cache = Path(__file__).parent / ".cache" / "prices.pkl"
    if demo:
        opn, close, volume = T.demo_data(syms, 252 * (years + 2), seed=7)
    elif cache.exists():
        opn, close, volume = pd.read_pickle(cache)
        print("Using cached prices up to", close.index[-1].date())
    else:
        opn, close, volume = T.download(syms, f"{years + 2}y")
        cache.parent.mkdir(exist_ok=True)
        pd.to_pickle((opn, close, volume), cache)
    days = close.index[close.index > close.index[-1] - pd.DateOffset(years=years)]
    cap = T.STARTING_CAPITAL
    sim = Sim(opn, close, volume, days)
    lines = [f"Period: {days[0].date()} to {days[-1].date()}  ({len(days)} trading days)",
             f"NSE shares requested: {len(syms) - len(T.INDEX_SYMBOLS)}, with price data: {close.shape[1] - len(T.INDEX_SYMBOLS)}",
             "Stock list size at start / end: " + ", ".join(
                 f"{k} {len(sim.universe(days[0], {**T.CONFIG, **p}))} / {len(sim.universe(days[-1], {**T.CONFIG, **p}))}"
                 for k, p in POOLS.items())]

    if "--experiments" in sys.argv:
        cols = ["per yr %", "sharpe", "worst fall %", "worst yr %", "worst month %", "1st half /yr", "2nd half /yr",
                "trades/yr", "fees %/yr"]
        rows = {}
        for name, cfg in experiments().items():
            v, state = sim.run(cfg)
            rows[name] = {k: stats(v, cap, state)[k] for k in cols}
        pd.set_option("display.width", 300)
        text = "\n".join(lines + ["=== EXPERIMENTS: one change at a time (after costs) ===",
                                  pd.DataFrame(rows).T.round(2).to_string()])
        print(text)
        (OUT / "experiments.txt").write_text(text + "\n")
        return

    rows, curves = {}, {}
    idx = close[T.REGIME_INDEX]
    curves["Nifty 50"] = idx.loc[days] / idx.loc[:days[0]].iloc[-2] * cap
    for sym, name in T.BENCHMARKS.items():
        if sym in close and sym != T.REGIME_INDEX and close[sym].loc[:days[0]].notna().sum() > 1:
            curves[name] = close[sym].loc[days] / close[sym].loc[:days[0]].iloc[-2] * cap
    curves["ALL index members (no costs)"] = sim.hold_universe({**T.CONFIG, **POOLS["index"]})
    curves["ALL 400 LISTED (no costs)"] = sim.hold_universe({**T.CONFIG, **POOLS["nohind"]})
    curves["ALL large caps (no costs)"] = sim.hold_universe({**T.CONFIG, **POOLS["large"]})
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
    weekly = pd.DataFrame({**{k: curves[f"MODEL {k}"] for k in T.MODELS},
                           **{k + "_nohind": curves[f"MODEL {k} [nohind]"] for k in T.MODELS},
                           "nifty50": curves["Nifty 50"], "all_index": curves["ALL index members (no costs)"],
                           "all_listed": curves["ALL 400 LISTED (no costs)"]}).iloc[::5]
    (OUT / "backtest.json").write_text(json.dumps({
        "period": f"{days[0].date()} to {days[-1].date()}", "demo": demo,
        "models": {k: {"name": m["name"], "tagline": m["tagline"], "description": T.describe(m["config"]),
                       "config": {**{a: b for a, b in m["config"].items() if not a.startswith("_")}, "cost": T.COST}}
                   for k, m in T.MODELS.items()},
        "table": json.loads(table.round(2).to_json(orient="index")),
        "curve": [{"date": d.date().isoformat(), **{k: round((x / cap - 1) * 100, 2) for k, x in r.items()}}
                  for d, r in weekly.iterrows()],
    }, indent=1))


if __name__ == "__main__":
    main()
