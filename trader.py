"""
NSE Momentum Paper Trader
-------------------------
Paper-trades (FAKE money) mid- and small-cap NSE stocks. Nothing is
hand-picked: the bot builds its own stock list from every NSE-listed share.

Rules (all set in CONFIG below):
  1. Stock list: rank every NSE stock by how much money trades in it per day
     (6-month median). Skip the 100 most traded (large caps) and keep the
     next 400. Rebuilt every month from past data only.
  2. Candidates: stocks with a positive 6-month return (ignoring the latest
     month) that trade above their own 50-day average.
  3. Selection ("select"): how the final top N are chosen from candidates.
  4. Exposure ("exposure"): how much of the money is invested at all, based
     on market conditions. The rest sits in cash. Re-checked weekly.
  5. Stocks are re-picked monthly.

State lives in docs/state.json, which the dashboard (docs/index.html) reads.

Usage:
  python trader.py                    # live prices
  python trader.py --demo             # synthetic prices (offline testing)
  python trader.py --reset            # start over with fresh capital
  python trader.py --refresh-symbols  # re-download the list of NSE stocks
"""
import csv
import io
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "docs" / "state.json"
SYMBOLS_FILE = ROOT / "data" / "symbols.txt"
IST = timezone(timedelta(hours=5, minutes=30))

STARTING_CAPITAL = 1_000_000.0  # ₹10 lakh fake money
COST = 0.002                    # 0.2% per trade (brokerage + STT + mid/small-cap spreads)
REGIME_INDEX = "^NSEI"          # Nifty 50, used for the market check
# First benchmark that Yahoo returns data for is used.
BENCHMARKS = {"NIFTY_MIDCAP_100.NS": "Nifty Midcap 100", "^NSEI": "Nifty 50"}
INDEX_SYMBOLS = list(dict.fromkeys(list(BENCHMARKS) + [REGIME_INDEX]))

CONFIG = {
    # --- stock list (built by the bot, no hand-picking)
    "universe_skip": 100,   # ignore the N most-traded stocks (large caps)
    "universe_size": 400,   # then take this many
    "min_price": 20,        # ignore penny stocks
    # --- picking
    "top_n": 20,            # stocks held at once
    "lookback": 126,        # return window in trading days (126 = 6 months)
    "skip": 21,             # ignore the most recent N days when measuring return
    "trend_sma": 50,        # stock must be above this moving average
    "select": "mom",        # "mom"     = highest return
                            # "mom_vol" = highest return per unit of volatility
                            # "lowvol"  = calmest stocks among the top third by return
    "weighting": "equal",   # "equal" or "inv_vol" (calmer stocks get more money)
    "buffer": 2.0,          # keep a held stock while it ranks in top_n x buffer
    # --- how much to invest
    "exposure": ["regime"], # any of: "regime"    = all cash if Nifty 50 < its 200-day average
                            #         "breadth"   = invest less when under half of stocks are in uptrends
                            #         "voltarget" = invest less when the market is jumpy
    "vol_target": 0.15,     # for "voltarget": aim for this yearly volatility
    # --- timing
    "rebalance": "monthly", # when stocks are re-picked
    "risk_check": "weekly", # when exposure is re-checked (None = only at rebalance)
    "drift": 0.25,          # only resize a holding if it is >25% away from its target size
}


# ---------------------------------------------------------------- data
def refresh_symbols() -> int:
    """Download the official list of NSE-listed shares (series EQ)."""
    import requests
    hdr = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
           "Accept": "text/csv,*/*", "Referer": "https://www.nseindia.com/"}
    for url in ("https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
                "https://archives.nseindia.com/content/equities/EQUITY_L.csv"):
        try:
            r = requests.get(url, headers=hdr, timeout=30)
            r.raise_for_status()
            rows = [{k.strip(): v.strip() for k, v in row.items()} for row in csv.DictReader(io.StringIO(r.text))]
            syms = sorted({row["SYMBOL"] for row in rows if row.get("SERIES") == "EQ"})
            if len(syms) > 500:
                SYMBOLS_FILE.parent.mkdir(exist_ok=True)
                SYMBOLS_FILE.write_text("\n".join(syms) + "\n")
                print(f"Saved {len(syms)} symbols from {url}")
                return len(syms)
        except Exception as e:  # noqa: BLE001
            print(f"Could not fetch {url}: {e}")
    return 0


def all_tickers(demo=False) -> list[str]:
    if SYMBOLS_FILE.exists():
        return [s + ".NS" for s in SYMBOLS_FILE.read_text().split()]
    if demo:
        return [f"DEMO{i:03d}.NS" for i in range(600)]
    raise SystemExit("data/symbols.txt is missing - run: python trader.py --refresh-symbols")


def download(symbols, period):
    """Returns (open, close, volume) DataFrames of daily data from Yahoo Finance."""
    import yfinance as yf
    symbols = list(dict.fromkeys(symbols))
    parts = {"Open": [], "Close": [], "Volume": []}
    for i in range(0, len(symbols), 200):
        chunk = symbols[i:i + 200]
        for attempt in range(3):
            try:
                df = yf.download(chunk, period=period, interval="1d", auto_adjust=True,
                                 progress=False, threads=True)
                if len(df):
                    for k in parts:
                        parts[k].append(df[k])
                    break
            except Exception as e:  # noqa: BLE001
                print(f"download retry {attempt}: {e}")
            time.sleep(5)
        if len(symbols) > 200:
            time.sleep(1)
    if not parts["Close"]:
        raise SystemExit("No price data received from Yahoo Finance.")
    o, c, v = (pd.concat(parts[k], axis=1).sort_index() for k in ("Open", "Close", "Volume"))
    c = c.dropna(how="all").dropna(axis=1, how="all").ffill()
    o = o.reindex(index=c.index, columns=c.columns).fillna(c)   # indices have no reliable open
    v = v.reindex(index=c.index, columns=c.columns).fillna(0)
    return o, c, v


def demo_data(symbols, periods, seed=None):
    rng = np.random.default_rng(seed if seed is not None else int(time.time()) // 600)
    idx = pd.bdate_range(end=datetime.now(IST).date(), periods=periods)
    n = len(symbols)
    rets = rng.normal(rng.normal(0.0003, 0.0008, n), rng.uniform(0.01, 0.03, n), (len(idx), n))
    c = pd.DataFrame(rng.uniform(50, 3000, n) * np.exp(np.cumsum(rets, axis=0)), index=idx, columns=symbols)
    o = c.shift(1).bfill() * (1 + rng.normal(0, 0.004, c.shape))
    v = pd.DataFrame(rng.uniform(1e4, 5e6, n) * rng.uniform(0.5, 1.5, c.shape), index=idx, columns=symbols)
    return o, c, v


# ---------------------------------------------------------------- strategy
def choose_universe(close, volume, cfg=CONFIG) -> list[str]:
    """The bot's own stock list, from the rows given (use only past data)."""
    stocks = [c for c in close.columns if c not in INDEX_SYMBOLS]
    c, v = close[stocks].iloc[-126:], volume[stocks].iloc[-126:]
    traded = (c * v).median()
    ok = (c.notna().sum() >= 120) & (c.iloc[-1] >= cfg["min_price"]) & (traded > 0)
    ranked = traded[ok].sort_values(ascending=False).index.tolist()
    return ranked[cfg["universe_skip"]: cfg["universe_skip"] + cfg["universe_size"]]


def market_is_healthy(prices) -> bool:
    if REGIME_INDEX not in prices:
        return True
    idx = prices[REGIME_INDEX].dropna()
    return len(idx) < 200 or idx.iloc[-1] >= idx.iloc[-200:].mean()


def exposure(prices, universe, cfg=CONFIG) -> float:
    """Fraction of the portfolio to invest (0 to 1). Last row of prices = now."""
    e = 1.0
    px = prices[[t for t in universe if t in prices]]
    if "regime" in cfg["exposure"] and not market_is_healthy(prices):
        e = 0.0
    if "breadth" in cfg["exposure"] and len(px) >= 200:
        full = px.loc[:, px.iloc[-200:].notna().all()]
        if full.shape[1]:
            share_up = float((full.iloc[-1] > full.iloc[-200:].mean()).mean())
            e *= min(1.0, 2 * share_up)          # fully invested when half or more are in uptrends
    if "voltarget" in cfg["exposure"] and len(px) > 21:
        mkt = px.iloc[-21:].pct_change(fill_method=None).clip(-0.4, 0.4).mean(axis=1).dropna()
        vol = float(mkt.std() * np.sqrt(252))
        if vol > 0:
            e *= min(1.0, cfg["vol_target"] / vol)
    return round(e, 3)


def pick_stocks(prices, universe, cfg=CONFIG, held=()) -> dict:
    """Target weights {ticker: fraction of portfolio} before exposure scaling."""
    px = prices[[t for t in universe if t in prices]]
    lb, skip, n = cfg["lookback"], cfg["skip"], cfg["top_n"]
    if len(px) < lb + skip + 1:
        return {}
    last = px.iloc[-1]
    window = px.iloc[len(px) - 1 - skip - lb: len(px) - skip]
    rets = window.pct_change(fill_method=None)
    mom = window.iloc[-1] / window.iloc[0] - 1
    vol = rets.std()
    ok = (mom > 0) & (last > px.iloc[-cfg["trend_sma"]:].mean()) & (vol > 0)
    ok &= rets.abs().max() < 0.4                 # drop bad data / freak one-day jumps
    ok &= window.notna().all()
    if cfg["select"] == "mom_vol":
        score = (mom / (vol * np.sqrt(lb)))[ok]
    elif cfg["select"] == "lowvol":
        cands = mom[ok].sort_values(ascending=False)
        cands = cands.iloc[: max(n * 2, len(cands) // 3)]
        score = -vol[cands.index]
    else:
        score = mom[ok]
    ranked = score.sort_values(ascending=False).index.tolist()
    keep = [t for t in held if t in ranked[: int(n * cfg["buffer"])]][:n]
    chosen = keep + [t for t in ranked if t not in keep][: n - len(keep)]
    if not chosen:
        return {}
    if cfg["weighting"] == "inv_vol":
        inv = 1 / vol[chosen]
        w = (inv / inv.sum() * len(chosen) / n).clip(upper=2 / n)
    else:
        w = pd.Series(1 / n, index=chosen)       # unused slots stay in cash
    return {t: float(x) for t, x in w.items()}


def period_key(day, freq) -> str:
    if freq == "weekly":
        y, w, _ = day.isocalendar()
        return f"{y}-W{w:02d}"
    if freq == "monthly":
        return day.strftime("%Y-%m")
    return day.isoformat()


def describe(cfg=CONFIG) -> str:
    months = round(cfg["lookback"] / 21)
    how = {"mom": f"the {cfg['top_n']} with the highest {months}-month return",
           "mom_vol": f"the {cfg['top_n']} with the highest {months}-month return relative to their volatility",
           "lowvol": f"the {cfg['top_n']} calmest stocks among the top third by {months}-month return"}[cfg["select"]]
    s = (f"The bot builds its own list of {cfg['universe_size']} mid- and small-cap NSE stocks each month "
         f"(ranked by trading activity, skipping the {cfg['universe_skip']} largest). From those in an uptrend it holds {how}"
         + (", giving calmer stocks more money" if cfg["weighting"] == "inv_vol" else "")
         + f". Stocks are re-picked {cfg['rebalance']}.")
    ex = []
    if "regime" in cfg["exposure"]:
        ex.append("moves fully to cash when the Nifty 50 is below its 200-day average")
    if "breadth" in cfg["exposure"]:
        ex.append("invests less when fewer than half of the stocks are in uptrends")
    if "voltarget" in cfg["exposure"]:
        ex.append("invests less when the market is unusually jumpy")
    if ex:
        s += " It " + ", and ".join(ex) + (f" (checked {cfg['risk_check']})." if cfg["risk_check"] else ".")
    return s + f" {COST * 100:.1f}% cost per trade. Prices from Yahoo Finance (may be delayed ~15 min)."


# ---------------------------------------------------------------- state
def new_state() -> dict:
    now = datetime.now(IST).isoformat(timespec="seconds")
    return {
        "started_at": now, "starting_capital": STARTING_CAPITAL,
        "cash": STARTING_CAPITAL, "positions": {}, "trades": [], "history": [],
        "last_rebalance": None, "last_risk": None, "universe": None, "base_weights": {},
        "exposure": None, "benchmark_start": None, "benchmark": None, "benchmark_name": None,
    }


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return new_state()


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1))


# ---------------------------------------------------------------- execution
def trade(state, ticker, qty, price, ts):
    """qty > 0 buys, qty < 0 sells. Applies cost to cash."""
    value = qty * price
    fee = abs(value) * COST
    state["cash"] -= value + fee
    pos = state["positions"].get(ticker, {"qty": 0, "avg_price": 0.0})
    if qty > 0:
        pos["avg_price"] = (pos["qty"] * pos["avg_price"] + value) / (pos["qty"] + qty)
    pos["qty"] += qty
    if pos["qty"] == 0:
        state["positions"].pop(ticker, None)
    else:
        state["positions"][ticker] = pos
    state["trades"].append({"time": ts, "ticker": ticker.replace(".NS", ""),
                            "side": "BUY" if qty > 0 else "SELL", "qty": abs(qty),
                            "price": round(price, 2), "fee": round(fee, 2)})


def portfolio_value(state, last):
    return state["cash"] + sum(p["qty"] * float(last[t]) for t, p in state["positions"].items())


def rebalance(state, last, weights, ts, cfg=CONFIG):
    """Move the portfolio towards {ticker: fraction of portfolio}."""
    weights = {t: w for t, w in weights.items() if w > 0}
    for t in list(state["positions"]):
        if t not in weights:
            trade(state, t, -state["positions"][t]["qty"], float(last[t]), ts)
    value = portfolio_value(state, last)
    orders = []
    for t, w in weights.items():
        price = float(last[t])
        target = value * w * (1 - 2 * COST)
        have = state["positions"].get(t, {"qty": 0})["qty"]
        if have and abs(have * price / target - 1) <= cfg["drift"]:
            continue                      # close enough - don't pay to fine-tune
        want = int(target // price)
        if want != have:
            orders.append((want - have, t, price))
    for qty, t, price in sorted(orders):  # sells first so cash is available
        if qty > 0:
            qty = min(qty, int(state["cash"] // (price * (1 + COST))))
        if qty:
            trade(state, t, qty, price, ts)


def step(state, prices, day, ts, cfg, universe_fn):
    """One decision point. `prices` last row = now. Returns True if it acted.
    universe_fn() is only called when the stock list needs rebuilding."""
    mkey = period_key(day, cfg["rebalance"])
    rkey = period_key(day, cfg["risk_check"]) if cfg["risk_check"] else mkey
    new_month = state["last_rebalance"] != mkey or state["universe"] is None
    if not new_month and state["last_risk"] == rkey:
        return False
    was_in_cash = not state["positions"]
    if new_month:
        state["universe"] = universe_fn()
    e = exposure(prices, state["universe"], cfg)
    if new_month or (was_in_cash and e > 0):
        state["base_weights"] = pick_stocks(prices, state["universe"], cfg, held=list(state["positions"]))
    rebalance(state, prices.iloc[-1], {t: w * e for t, w in state["base_weights"].items()}, ts, cfg)
    state.update(last_rebalance=mkey, last_risk=rkey, exposure=e)
    return True


def main():
    if "--refresh-symbols" in sys.argv:
        sys.exit(0 if refresh_symbols() else 1)
    demo = "--demo" in sys.argv
    state = new_state() if "--reset" in sys.argv else load_state()
    now = datetime.now(IST)
    ts = now.isoformat(timespec="seconds")
    held = list(state["positions"])

    # cheap first look: is the market open today?
    if demo:
        syms = all_tickers(demo=True)[:600] + INDEX_SYMBOLS
        _, prices, volume = demo_data(syms, 420)
    else:
        _, prices, volume = download(INDEX_SYMBOLS + held, "5d")
        if prices.index[-1].date() != now.date():
            print(f"No prices for today yet (latest: {prices.index[-1].date()}) - market closed or holiday.")
            if "--reset" in sys.argv:
                save_state(state)
            return
        mkey = period_key(now.date(), CONFIG["rebalance"])
        rkey = period_key(now.date(), CONFIG["risk_check"]) if CONFIG["risk_check"] else mkey
        if state["universe"] is None or state["last_rebalance"] != mkey:
            _, prices, volume = download(all_tickers() + INDEX_SYMBOLS, "2y")     # monthly: everything
        elif state["last_risk"] != rkey:
            _, prices, volume = download(state["universe"] + held + INDEX_SYMBOLS, "2y")  # weekly

    if step(state, prices, now.date(), ts, CONFIG, lambda: choose_universe(prices, volume, CONFIG)):
        print(f"Rebalanced. Exposure {state['exposure']:.0%} ->",
              [t.replace('.NS', '') for t in state["positions"]] or "all cash")

    last = prices.iloc[-1]
    if state.get("benchmark") is None:
        for sym, name in BENCHMARKS.items():
            if pd.notna(last.get(sym)):
                state.update(benchmark=sym, benchmark_name=name, benchmark_start=float(last[sym]))
                break
    bsym = state.get("benchmark")
    value = portfolio_value(state, last)
    bench = float(last[bsym]) if bsym and pd.notna(last.get(bsym)) else None
    bench_pct = (bench / state["benchmark_start"] - 1) * 100 if bench and state["benchmark_start"] else None
    state["history"].append({
        "time": ts, "value": round(value, 2),
        "pct": round((value / state["starting_capital"] - 1) * 100, 3),
        "benchmark_pct": round(bench_pct, 3) if bench_pct is not None else None,
    })
    state["holdings"] = [{
        "ticker": t.replace(".NS", ""), "qty": p["qty"],
        "avg_price": round(p["avg_price"], 2), "price": round(float(last[t]), 2),
        "value": round(p["qty"] * float(last[t]), 2),
        "pnl_pct": round((float(last[t]) / p["avg_price"] - 1) * 100, 2),
    } for t, p in state["positions"].items()]
    state["params"] = {**CONFIG, "cost": COST, "description": describe()}
    state["updated_at"] = ts
    state["demo"] = demo
    save_state(state)
    print(f"{ts}  value ₹{value:,.0f}  ({state['history'][-1]['pct']:+.2f}%)")


if __name__ == "__main__":
    main()
