"""
NSE Momentum Paper Trader
-------------------------
A simple cross-sectional momentum strategy on mid- and small-cap NSE stocks, traded
with FAKE money. Each run:
  1. Downloads recent daily prices (Yahoo Finance, ".NS" tickers).
  2. Once per trading day, rebalances: buys the TOP_N stocks with the best
     20-day return that are also above their 50-day moving average
     (trend filter). Equal weight. Stocks that drop out are sold.
  3. Marks the portfolio to market and appends a point to the equity curve.
State lives in docs/state.json, which the dashboard (docs/index.html) reads.

Usage:
  python trader.py            # live prices
  python trader.py --demo     # synthetic prices (offline testing)
  python trader.py --reset    # start over with fresh capital
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

STATE_FILE = Path(__file__).parent / "docs" / "state.json"
IST = timezone(timedelta(hours=5, minutes=30))

STARTING_CAPITAL = 1_000_000.0  # ₹10 lakh fake money
TOP_N = 8                        # stocks held at once (more names = less single-stock risk)
LOOKBACK = 20                    # momentum window (trading days)
TREND_SMA = 50                   # trend filter window
COST = 0.002                     # 0.2% per trade (brokerage + STT + wider mid/small-cap spreads)
# First benchmark that Yahoo returns data for is used.
BENCHMARKS = {"NIFTY_MIDCAP_100.NS": "Nifty Midcap 100", "^NSEI": "Nifty 50"}

UNIVERSE = [
    # Mid caps
    "PERSISTENT", "DIXON", "POLYCAB", "COFORGE", "MPHASIS", "CUMMINSIND",
    "FEDERALBNK", "IDFCFIRSTB", "AUBANK", "PIIND", "LUPIN", "AUROPHARMA",
    "ASHOKLEY", "TATACOMM", "VOLTAS", "MAXHEALTH", "KPITTECH", "SUPREMEIND",
    "ASTRAL", "PHOENIXLTD", "BHARATFORG", "OBEROIRLTY", "JUBLFOOD", "NMDC",
    # Small caps
    "CDSL", "ANGELONE", "KEI", "BSOFT", "CYIENT", "AMBER", "RADICO", "CAMS",
    "IEX", "KAYNES", "NATIONALUM", "CROMPTON", "BLUESTARCO", "SONACOMS",
    "MANAPPURAM", "GLENMARK",
]
TICKERS = [t + ".NS" for t in UNIVERSE]


# ---------------------------------------------------------------- data
def fetch_prices(demo: bool) -> pd.DataFrame:
    """Daily close prices, columns = tickers + benchmark. Last row = latest price."""
    cols = TICKERS + list(BENCHMARKS)
    if demo:
        rng = np.random.default_rng(int(datetime.now().timestamp()) // 600)
        idx = pd.bdate_range(end=datetime.now(IST).date(), periods=120)
        drift = rng.normal(0.0005, 0.001, len(cols))
        rets = rng.normal(drift, 0.015, (len(idx), len(cols)))
        return pd.DataFrame(1000 * np.exp(np.cumsum(rets, axis=0)), index=idx, columns=cols)

    import yfinance as yf
    df = yf.download(cols, period="6mo", interval="1d", auto_adjust=True,
                     progress=False, threads=True)["Close"]
    return df.dropna(how="all").ffill()


# ---------------------------------------------------------------- strategy
def pick_stocks(prices: pd.DataFrame) -> list[str]:
    px = prices[TICKERS].dropna(axis=1, thresh=TREND_SMA + 1).ffill()
    px = px.loc[:, px.iloc[-1].notna()]
    momentum = px.iloc[-1] / px.iloc[-LOOKBACK - 1] - 1
    above_trend = px.iloc[-1] > px.rolling(TREND_SMA).mean().iloc[-1]
    candidates = momentum[above_trend & (momentum > 0)]
    return candidates.sort_values(ascending=False).head(TOP_N).index.tolist()


# ---------------------------------------------------------------- state
def new_state() -> dict:
    now = datetime.now(IST).isoformat(timespec="seconds")
    return {
        "started_at": now, "starting_capital": STARTING_CAPITAL,
        "cash": STARTING_CAPITAL, "positions": {}, "trades": [],
        "history": [], "last_rebalance": None, "benchmark_start": None,
        "benchmark": None, "benchmark_name": None,
        "params": {"top_n": TOP_N, "lookback": LOOKBACK, "trend_sma": TREND_SMA,
                   "cost": COST, "universe_size": len(UNIVERSE)},
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


def rebalance(state, last, targets, ts):
    # sell anything not in targets
    for t in list(state["positions"]):
        if t not in targets:
            trade(state, t, -state["positions"][t]["qty"], float(last[t]), ts)
    if not targets:
        return
    # resize to equal weight (sells first so cash is available)
    target_value = portfolio_value(state, last) * (1 - 2 * COST) / len(targets)
    orders = []
    for t in targets:
        price = float(last[t])
        want = int(target_value // price)
        have = state["positions"].get(t, {"qty": 0})["qty"]
        if want != have:
            orders.append((want - have, t, price))
    for qty, t, price in sorted(orders):  # negative qty (sells) first
        if qty > 0:
            qty = min(qty, int(state["cash"] // (price * (1 + COST))))
        if qty:
            trade(state, t, qty, price, ts)


def main():
    demo = "--demo" in sys.argv
    state = new_state() if "--reset" in sys.argv else load_state()

    prices = fetch_prices(demo)
    last = prices.iloc[-1]
    now = datetime.now(IST)
    ts = now.isoformat(timespec="seconds")
    today = now.date().isoformat()

    if state.get("benchmark") is None:
        for sym, name in BENCHMARKS.items():
            if pd.notna(last.get(sym)):
                state.update(benchmark=sym, benchmark_name=name, benchmark_start=float(last[sym]))
                break
    bsym = state.get("benchmark")

    if state["last_rebalance"] != today:
        targets = pick_stocks(prices)
        rebalance(state, last, targets, ts)
        state["last_rebalance"] = today
        state["last_signal"] = [t.replace(".NS", "") for t in targets]
        print("Rebalanced ->", state["last_signal"])

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
    state["updated_at"] = ts
    state["demo"] = demo
    save_state(state)
    print(f"{ts}  value ₹{value:,.0f}  ({state['history'][-1]['pct']:+.2f}%)")


if __name__ == "__main__":
    main()
