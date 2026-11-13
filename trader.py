"""
NSE Momentum Paper Trader
-------------------------
A momentum strategy on mid- and small-cap NSE stocks, traded with FAKE money.

Rules (all set in CONFIG below):
  1. Market check: if the Nifty 50 is below its 200-day average, hold cash.
  2. Rank every stock by its return over the lookback window, ignoring the
     most recent `skip` days (very recent moves tend to reverse).
  3. Only consider stocks with a positive return that trade above their own
     50-day average.
  4. Hold the top N. A stock already held is kept as long as it stays in the
     top N x buffer, which avoids pointless swapping.
  5. Re-pick on a schedule (weekly / monthly), not every day.

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
COST = 0.002                    # 0.2% per trade (brokerage + STT + mid/small-cap spreads)
REGIME_INDEX = "^NSEI"          # Nifty 50, used for the market check
# First benchmark that Yahoo returns data for is used.
BENCHMARKS = {"NIFTY_MIDCAP_100.NS": "Nifty Midcap 100", "^NSEI": "Nifty 50"}

CONFIG = {
    "top_n": 10,            # stocks held at once
    "lookback": 126,        # momentum window in trading days (126 = 6 months)
    "skip": 21,             # ignore the most recent N days when ranking
    "trend_sma": 50,        # stock must be above this moving average
    "rebalance": "monthly", # "daily", "weekly" or "monthly"
    "buffer": 2.0,          # keep a held stock while it ranks in top_n x buffer
    "regime": True,         # go to cash when Nifty 50 < its 200-day average
    "vol_adjust": False,    # rank by return / volatility instead of raw return
    "resize": False,        # True = force equal weights at every rebalance
}

UNIVERSE = [
    # Mid caps
    "PERSISTENT", "DIXON", "POLYCAB", "COFORGE", "MPHASIS", "CUMMINSIND",
    "FEDERALBNK", "IDFCFIRSTB", "AUBANK", "PIIND", "LUPIN", "AUROPHARMA",
    "ASHOKLEY", "TATACOMM", "VOLTAS", "MAXHEALTH", "KPITTECH", "SUPREMEIND",
    "ASTRAL", "PHOENIXLTD", "BHARATFORG", "OBEROIRLTY", "JUBLFOOD", "NMDC",
    "SRF", "PAGEIND", "MFSL", "LTTS", "PETRONET", "CONCOR", "ESCORTS",
    "BALKRISIND", "MRF", "APOLLOTYRE", "EXIDEIND", "IGL", "TORNTPOWER",
    "LAURUSLABS", "IPCALAB", "ALKEM", "BIOCON", "ABCAPITAL", "LICHSGFIN",
    "MUTHOOTFIN", "INDIANB", "BANKINDIA", "UNIONBANK", "NHPC", "SJVN",
    "IRCTC", "BHEL", "NBCC", "RVNL", "SUZLON", "SAIL", "JKCEMENT",
    "DEEPAKNTR", "TATACHEM", "GODREJPROP", "INDHOTEL",
    # Small caps
    "CDSL", "ANGELONE", "KEI", "BSOFT", "CYIENT", "AMBER", "RADICO", "CAMS",
    "IEX", "KAYNES", "NATIONALUM", "CROMPTON", "BLUESTARCO", "SONACOMS",
    "MANAPPURAM", "GLENMARK", "CESC", "NCC", "HFCL", "RBLBANK", "KARURVYSYA",
    "CUB", "FSL", "TANLA", "INTELLECT", "ZENSARTECH", "SONATSOFTW",
    "LALPATHLAB", "METROPOLIS", "GRANULES", "NATCOPHARM", "AARTIIND",
    "NAVINFLUOR", "CHAMBLFERT", "GNFC", "CENTURYPLY", "KAJARIACER",
    "FINCABLES", "GRAPHITE", "HEG", "WELCORP", "RAMCOCEM", "PVRINOX",
    "TRIDENT", "ELGIEQUIP", "TIMKEN", "CARBORUNIV", "GESHIP", "REDINGTON",
    "CASTROLIND", "MGL",
]
TICKERS = [t + ".NS" for t in UNIVERSE]
ALL_SYMBOLS = TICKERS + list(dict.fromkeys(list(BENCHMARKS) + [REGIME_INDEX]))


# ---------------------------------------------------------------- data
def fetch_prices(demo: bool) -> pd.DataFrame:
    """Daily close prices, one column per symbol. Last row = latest price."""
    if demo:
        rng = np.random.default_rng(int(datetime.now().timestamp()) // 600)
        idx = pd.bdate_range(end=datetime.now(IST).date(), periods=400)
        drift = rng.normal(0.0005, 0.001, len(ALL_SYMBOLS))
        rets = rng.normal(drift, 0.015, (len(idx), len(ALL_SYMBOLS)))
        return pd.DataFrame(1000 * np.exp(np.cumsum(rets, axis=0)), index=idx, columns=ALL_SYMBOLS)

    import yfinance as yf
    df = yf.download(ALL_SYMBOLS, period="2y", interval="1d", auto_adjust=True,
                     progress=False, threads=True)["Close"]
    return df.dropna(how="all").ffill()


# ---------------------------------------------------------------- strategy
def market_is_healthy(prices: pd.DataFrame) -> bool:
    if REGIME_INDEX not in prices:
        return True
    idx = prices[REGIME_INDEX].dropna()
    return len(idx) < 200 or idx.iloc[-1] >= idx.iloc[-200:].mean()


def pick_stocks(prices: pd.DataFrame, cfg: dict = CONFIG, held=()) -> list[str]:
    """Stocks to hold, given prices whose last row is 'now'."""
    px = prices[[t for t in TICKERS if t in prices]]
    lb, skip = cfg["lookback"], cfg["skip"]
    if len(px) < lb + skip + 1:
        return []
    if cfg["regime"] and not market_is_healthy(prices):
        return []
    last = px.iloc[-1]
    window = px.iloc[len(px) - 1 - skip - lb: len(px) - skip]
    score = window.iloc[-1] / window.iloc[0] - 1
    positive = score > 0
    if cfg["vol_adjust"]:
        score = score / (window.pct_change(fill_method=None).std() * np.sqrt(lb))
    above_trend = last > px.iloc[-cfg["trend_sma"]:].mean()
    ranked = score[positive & above_trend & score.notna()].sort_values(ascending=False).index.tolist()
    n = cfg["top_n"]
    keep = [t for t in held if t in ranked[: int(n * cfg["buffer"])]][:n]
    new = [t for t in ranked if t not in keep][: n - len(keep)]
    return keep + new


def period_key(day, freq: str) -> str:
    """Rebalancing happens on the first trading day of each new period."""
    if freq == "weekly":
        y, w, _ = day.isocalendar()
        return f"{y}-W{w:02d}"
    if freq == "monthly":
        return day.strftime("%Y-%m")
    return day.isoformat()


def describe(cfg: dict = CONFIG) -> str:
    months = round(cfg["lookback"] / 21)
    s = (f"Re-picks {cfg['rebalance']}. Holds the top {cfg['top_n']} of {len(UNIVERSE)} mid- and small-cap "
         f"NSE stocks ranked by {months}-month return"
         + (" divided by volatility" if cfg["vol_adjust"] else "")
         + (f" (ignoring the latest {cfg['skip']} trading days)" if cfg["skip"] else "")
         + f", only if above their {cfg['trend_sma']}-day average.")
    if cfg["buffer"] > 1:
        s += f" A held stock is kept while it ranks in the top {int(cfg['top_n'] * cfg['buffer'])}."
    if cfg["regime"]:
        s += " Moves to cash when the Nifty 50 is below its 200-day average."
    return s + f" {COST * 100:.1f}% cost per trade. Prices from Yahoo Finance (may be delayed ~15 min)."


# ---------------------------------------------------------------- state
def new_state() -> dict:
    now = datetime.now(IST).isoformat(timespec="seconds")
    return {
        "started_at": now, "starting_capital": STARTING_CAPITAL,
        "cash": STARTING_CAPITAL, "positions": {}, "trades": [],
        "history": [], "last_rebalance": None, "benchmark_start": None,
        "benchmark": None, "benchmark_name": None,
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


def rebalance(state, last, targets, ts, cfg: dict = CONFIG):
    # 1. sell anything that is no longer a target
    for t in list(state["positions"]):
        if t not in targets:
            trade(state, t, -state["positions"][t]["qty"], float(last[t]), ts)
    if not targets:
        return
    # 2. each stock gets one "slot" = 1/top_n of the portfolio.
    #    If fewer than top_n stocks qualify, the spare slots stay in cash.
    slot = portfolio_value(state, last) * (1 - 2 * COST) / cfg["top_n"]
    orders = []
    for t in targets:
        price = float(last[t])
        have = state["positions"].get(t, {"qty": 0})["qty"]
        if have and not cfg["resize"]:
            continue                      # leave existing holdings alone
        want = int(slot // price)
        if want != have:
            orders.append((want - have, t, price))
    for qty, t, price in sorted(orders):  # sells first so cash is available
        if qty > 0:
            qty = min(qty, int(state["cash"] // (price * (1 + COST))))
        if qty:
            trade(state, t, qty, price, ts)


def main():
    demo = "--demo" in sys.argv
    state = new_state() if "--reset" in sys.argv else load_state()

    prices = fetch_prices(demo)
    now = datetime.now(IST)
    if not demo and prices.index[-1].date() != now.date():
        print(f"No prices for today yet (latest: {prices.index[-1].date()}) - market closed or holiday.")
        if "--reset" in sys.argv:
            save_state(state)
        return
    last = prices.iloc[-1]
    ts = now.isoformat(timespec="seconds")

    if state.get("benchmark") is None:
        for sym, name in BENCHMARKS.items():
            if pd.notna(last.get(sym)):
                state.update(benchmark=sym, benchmark_name=name, benchmark_start=float(last[sym]))
                break
    bsym = state.get("benchmark")

    key = period_key(now.date(), CONFIG["rebalance"])
    if state["last_rebalance"] != key:
        targets = pick_stocks(prices, CONFIG, held=list(state["positions"]))
        rebalance(state, last, targets, ts)
        state["last_rebalance"] = key
        state["last_signal"] = [t.replace(".NS", "") for t in targets]
        print("Rebalanced ->", state["last_signal"] or "all cash")

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
    state["market_healthy"] = bool(market_is_healthy(prices))
    state["params"] = {**CONFIG, "cost": COST, "universe_size": len(UNIVERSE), "description": describe()}
    state["updated_at"] = ts
    state["demo"] = demo
    save_state(state)
    print(f"{ts}  value ₹{value:,.0f}  ({state['history'][-1]['pct']:+.2f}%)")


if __name__ == "__main__":
    main()
