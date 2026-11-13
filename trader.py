"""
NSE Momentum Paper Trader
-------------------------
Paper-trades (FAKE money) mid- and small-cap NSE stocks with two bots side
by side: a Growth bot and a Steady bot (see MODELS). Each has its own
₹10 lakh, its own state file (docs/state-<bot>.json) and trade log
(docs/trades-<bot>.csv).

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

The dashboard (docs/index.html) reads the state files.

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
SYMBOLS_FILE = ROOT / "data" / "symbols.txt"
NAMES_FILE = ROOT / "data" / "names.json"      # ticker -> company name
INDEX_FILE = ROOT / "data" / "midsmallcap400.txt"   # current Nifty MidSmallcap 400 members
SECTORS_FILE = ROOT / "data" / "sectors.json"       # ticker -> industry, from the index file
IST = timezone(timedelta(hours=5, minutes=30))

STARTING_CAPITAL = 1_000_000.0  # ₹10 lakh fake money
COST = 0.002                    # 0.2% per trade (brokerage + STT + mid/small-cap spreads)
REGIME_INDEX = "^NSEI"          # Nifty 50, used for the market check
# First benchmark that Yahoo returns data for is used.
BENCHMARKS = {"NIFTYMIDSML400.NS": "Nifty MidSmallcap 400", "NIFTY_MIDCAP_100.NS": "Nifty Midcap 100",
              "^NSEI": "Nifty 50"}
INDEX_SYMBOLS = list(dict.fromkeys(list(BENCHMARKS) + [REGIME_INDEX]))

CONFIG = {
    # --- stock list
    "universe": "index",    # "index"  = members of the Nifty MidSmallcap 400
                            # "traded" = bot's own list by trading activity (see next two)
    "universe_skip": 100,   # "traded" only: ignore the N most-traded stocks (large caps)
    "universe_size": 400,   # "traded" only: then take this many
    "min_price": 20,        # ignore penny stocks
    # --- picking
    "top_n": 20,            # stocks held at once
    "lookback": 252,        # return window in trading days (252 = 12 months)
    "lookback2": 0,         # optional second window (e.g. 252); stocks are ranked on both
    "near_high": 0.0,       # e.g. 0.85 = price must be within 15% of its 52-week high
    "cut_losers": False,    # weekly: sell holdings that fall below their average, buy new winners
    "skip": 21,             # ignore the most recent N days when measuring return
    "trend_sma": 50,        # stock must be above this moving average
    "select": "mom",        # "mom"     = highest return
                            # "mom_vol" = highest return per unit of volatility
                            # "lowvol"  = calmest stocks among the top third by return
    "weighting": "equal",   # "equal" or "inv_vol" (calmer stocks get more money)
    "buffer": 2.0,          # keep a held stock while it ranks in top_n x buffer
    "sector_cap": 0,        # most stocks allowed from one industry (0 = no limit)
    # --- how much to invest
    "exposure": [],         # any of: "regime"    = all cash if Nifty 50 < its 200-day average
                            #         "breadth"   = invest less when under half of stocks are in uptrends
                            #         "voltarget" = invest less when the market is jumpy
    "vol_target": 0.15,     # for "voltarget": aim for this yearly volatility
    "exposure_step": 0.0,   # ignore changes in the invested share smaller than this (e.g. 0.1)
    "cash_yield": 0.0,      # yearly interest assumed on idle cash (e.g. 0.06 for a liquid fund)
    # --- timing
    "rebalance": "monthly", # when stocks are re-picked: "monthly" or "quarterly"
    "risk_check": None,     # when exposure is re-checked (None = only at rebalance)
    "drift": 0.25,          # only resize a holding if it is >25% away from its target size
}

# Two bots run side by side, each with its own ₹10 lakh of practice money.
# Each one's settings are CONFIG with the changes listed here.
MODELS = {
    "growth": {
        "name": "Growth bot",
        "tagline": "Buys the 20 biggest 12-month winners and re-picks every quarter. Aims for higher returns and accepts bigger drops.",
        "config": {**CONFIG, "rebalance": "quarterly", "cash_yield": 0.06},
    },
    "steady": {
        "name": "Steady bot",
        "tagline": "Buys 20 calm, steadily rising stocks and holds back cash when the market gets rough. "
                   "Aims for smoother returns and accepts lower gains.",
        "config": {**CONFIG, "lookback": 126, "select": "lowvol", "exposure": ["voltarget"], "risk_check": "weekly",
                   "sector_cap": 4, "cash_yield": 0.06},
    },
}


def state_file(model):
    return ROOT / "docs" / f"state-{model}.json"


def trades_csv(model):
    return ROOT / "docs" / f"trades-{model}.csv"   # permanent log of every buy and sell


# ---------------------------------------------------------------- data
HDR = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
       "Accept": "text/csv,*/*", "Referer": "https://www.nseindia.com/"}


def _index_csv(name):
    """{symbol: industry} from one NSE index constituent file, or {} if it cannot be fetched."""
    import requests
    for base in ("https://nsearchives.nseindia.com/content/indices/", "https://archives.nseindia.com/content/indices/",
                 "https://www.niftyindices.com/IndexConstituent/"):
        try:
            r = requests.get(base + name, headers=HDR, timeout=30)
            r.raise_for_status()
            rows = [{k.strip(): v.strip() for k, v in row.items()} for row in csv.DictReader(io.StringIO(r.text))]
            syms = {row["Symbol"]: row.get("Industry", "") for row in rows if row.get("Symbol")}
            if syms:
                print(f"{name}: {len(syms)} symbols from {base}")
                return syms
        except Exception as e:  # noqa: BLE001
            print(f"Could not fetch {base + name}: {e}")
    return {}


def refresh_index() -> int:
    """Download the current Nifty MidSmallcap 400 members."""
    syms = _index_csv("ind_niftymidsmallcap400list.csv")
    if len(syms) < 350:   # fall back to its two halves
        syms = {**_index_csv("ind_niftymidcap150list.csv"), **_index_csv("ind_niftysmallcap250list.csv")}
    if len(syms) >= 350:
        INDEX_FILE.parent.mkdir(exist_ok=True)
        INDEX_FILE.write_text("\n".join(sorted(syms)) + "\n")
        SECTORS_FILE.write_text(json.dumps(syms, indent=0, sort_keys=True))
        print(f"Saved {len(syms)} Nifty MidSmallcap 400 members")
        return len(syms)
    return 0


_SECTORS = None


def sectors() -> dict:
    """{ticker.NS: industry}. Stocks missing from the file have no industry and are never capped."""
    global _SECTORS
    if _SECTORS is None:
        raw = json.loads(SECTORS_FILE.read_text()) if SECTORS_FILE.exists() else {}
        _SECTORS = {k + ".NS": v for k, v in raw.items() if v}
    return _SECTORS


def index_members() -> list[str]:
    if not INDEX_FILE.exists():
        raise SystemExit("data/midsmallcap400.txt is missing - run: python trader.py --refresh-symbols")
    return [s + ".NS" for s in INDEX_FILE.read_text().split()]


def refresh_symbols() -> int:
    """Download the official list of NSE-listed shares (series EQ)."""
    import requests
    hdr = HDR
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
                names = {row["SYMBOL"]: row.get("NAME OF COMPANY", "") for row in rows if row.get("SERIES") == "EQ"}
                NAMES_FILE.write_text(json.dumps(names, indent=0, sort_keys=True))
                print(f"Saved {len(syms)} symbols from {url}")
                return len(syms)
        except Exception as e:  # noqa: BLE001
            print(f"Could not fetch {url}: {e}")
    return 0


def company_names() -> dict:
    return json.loads(NAMES_FILE.read_text()) if NAMES_FILE.exists() else {}


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
    """The stock list the bot may pick from, using the rows given (past data only)."""
    stocks = [c for c in close.columns if c not in INDEX_SYMBOLS]
    if cfg["universe"] == "index":
        members = set(cfg.get("_members") or index_members())
        stocks = [c for c in stocks if c in members]
    c, v = close[stocks].iloc[-126:], volume[stocks].iloc[-126:]
    traded = (c * v).median()
    ok = (c.notna().sum() >= 120) & (c.iloc[-1] >= cfg["min_price"]) & (traded > 0)
    if cfg["universe"] == "index":
        return traded[ok].sort_values(ascending=False).index.tolist()
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


def pick_stocks(prices, universe, cfg=CONFIG, held=(), locked=False) -> dict:
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
    if cfg.get("lookback2"):                     # also rank on a second, longer window
        w2 = px.iloc[max(0, len(px) - 1 - skip - cfg["lookback2"]): len(px) - skip]
        mom2 = w2.iloc[-1] / w2.iloc[0] - 1
        ok &= (mom2 > 0) & (len(w2) > cfg["lookback2"])
        mom = (mom[ok].rank(pct=True) + mom2[ok].rank(pct=True)).reindex(mom.index)
    if cfg.get("near_high"):
        ok &= last >= cfg["near_high"] * px.iloc[-252:].max()
    if cfg["select"] == "mom_vol":
        score = (mom / (vol * np.sqrt(lb)))[ok]
    elif cfg["select"] == "lowvol":
        cands = mom[ok].sort_values(ascending=False)
        cands = cands.iloc[: max(n * 2, len(cands) // 3)]
        score = -vol[cands.index]
    else:
        score = mom[ok]
    ranked = score.sort_values(ascending=False).index.tolist()
    keep = [t for t in held if locked or t in ranked[: int(n * cfg["buffer"])]][:n]
    cap = cfg.get("sector_cap") or 0
    if cap:                                      # limit how many stocks come from one industry
        sec = cfg.get("_sectors") or sectors()
        count = {}
        for t in keep:
            count[sec.get(t)] = count.get(sec.get(t), 0) + 1
        chosen = list(keep)
        for t in ranked:
            if len(chosen) >= n:
                break
            g = sec.get(t)
            if t in chosen or (g and count.get(g, 0) >= cap):
                continue
            chosen.append(t)
            count[g] = count.get(g, 0) + 1
    else:
        chosen = keep + [t for t in ranked if t not in keep][: n - len(keep)]
    if not chosen:
        return {}
    if cfg["weighting"] == "inv_vol":
        inv = 1 / vol[chosen]
        w = (inv / inv.sum() * len(chosen) / n).clip(upper=2 / n)
    else:
        w = pd.Series(1 / n, index=chosen)       # unused slots stay in cash
    return {t: float(x) for t, x in w.items()}


def period_key(day, freq, offset=0) -> str:
    """offset shifts which months a quarter starts in (0 = Jan/Apr/Jul/Oct)."""
    if freq == "weekly":
        y, w, _ = day.isocalendar()
        return f"{y}-W{w:02d}"
    if freq == "monthly":
        return day.strftime("%Y-%m")
    if freq == "quarterly":
        m = day.year * 12 + day.month - 1 - offset
        return f"Q{m // 3}"
    return day.isoformat()


def describe(cfg=CONFIG) -> str:
    months = round(cfg["lookback"] / 21)
    rise = f"{months}-month" + (f" and {round(cfg['lookback2'] / 21)}-month" if cfg.get("lookback2") else "")
    how = {"mom": f"the {cfg['top_n']} with the biggest {rise} rise",
           "mom_vol": f"the {cfg['top_n']} with the biggest {rise} rise relative to their volatility",
           "lowvol": f"the {cfg['top_n']} calmest stocks among the top third by {rise} rise"}[cfg["select"]]
    pool = ("the Nifty MidSmallcap 400 index" if cfg["universe"] == "index" else
            f"its own list of {cfg['universe_size']} mid- and small-cap NSE stocks (by trading activity)")
    s = f"The bot picks from {pool}. From the stocks in an uptrend"
    if cfg.get("near_high"):
        s += f" and within {round((1 - cfg['near_high']) * 100)}% of their 52-week high"
    s += f" it holds {how}. Stocks are re-picked {cfg['rebalance']}."
    if cfg.get("sector_cap"):
        s += f" At most {cfg['sector_cap']} stocks may come from one industry."
    if cfg.get("cut_losers"):
        s += " Each week, holdings that fall below their average price are sold and replaced."
    ex = []
    if "regime" in cfg["exposure"]:
        ex.append("moves fully to cash when the Nifty 50 is below its 200-day average")
    if "breadth" in cfg["exposure"]:
        ex.append("invests less when fewer than half of the stocks are in uptrends")
    if "voltarget" in cfg["exposure"]:
        ex.append("invests less when the market is unusually jumpy")
    if ex:
        s += " It " + ", and ".join(ex) + (f" (checked {cfg['risk_check']})." if cfg["risk_check"] else ".")
    if cfg.get("cash_yield"):
        s += f" Idle cash is assumed to earn {cfg['cash_yield'] * 100:.0f}% a year, as in a liquid fund."
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


def load_state(model) -> dict:
    f = state_file(model)
    return json.loads(f.read_text()) if f.exists() else new_state()


def save_state(state: dict, model) -> None:
    state_file(model).parent.mkdir(exist_ok=True)
    state_file(model).write_text(json.dumps(state, indent=1))
    # every trade ever made, one per line, with its exact timestamp (IST)
    with trades_csv(model).open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time_ist", "side", "ticker", "company", "qty", "price", "value", "fee"])
        for t in state["trades"]:
            w.writerow([t["time"], t["side"], t["ticker"], t.get("name", ""), t["qty"], t["price"],
                        t.get("value", round(t["qty"] * t["price"], 2)), t["fee"]])


# ---------------------------------------------------------------- execution
def trade(state, ticker, qty, price, ts):
    """qty > 0 buys, qty < 0 sells. Applies cost to cash."""
    value = qty * price
    fee = abs(value) * COST
    state["cash"] -= value + fee
    pos = state["positions"].get(ticker, {"qty": 0, "avg_price": 0.0})
    if qty > 0:
        pos["avg_price"] = (pos["qty"] * pos["avg_price"] + value) / (pos["qty"] + qty)
        pos.setdefault("bought_at", ts)   # when this holding was first bought
        pos["last_buy"] = ts              # most recent top-up
    pos["qty"] += qty
    if pos["qty"] == 0:
        state["positions"].pop(ticker, None)
    else:
        state["positions"][ticker] = pos
    state["trades"].append({"time": ts, "ticker": ticker.replace(".NS", ""),
                            "side": "BUY" if qty > 0 else "SELL", "qty": abs(qty),
                            "price": round(price, 2), "value": round(abs(value), 2),
                            "fee": round(fee, 2)})


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


def accrue_interest(state, day, cfg):
    """Idle cash earns cfg['cash_yield'] a year (as if parked in a liquid fund)."""
    prev, state["interest_day"] = state.get("interest_day"), day.isoformat()
    if cfg.get("cash_yield") and prev and state["cash"] > 0:
        days = (day - datetime.fromisoformat(prev).date()).days
        gain = state["cash"] * ((1 + cfg["cash_yield"]) ** (days / 365) - 1)
        state["cash"] += gain
        state["interest_earned"] = state.get("interest_earned", 0.0) + gain


def step(state, prices, day, ts, cfg, universe_fn):
    """One decision point. `prices` last row = now. Returns True if it acted.
    universe_fn() is only called when the stock list needs rebuilding."""
    mkey = period_key(day, cfg["rebalance"], cfg.get("rebalance_offset", 0))
    rkey = period_key(day, cfg["risk_check"]) if cfg["risk_check"] else mkey
    new_month = state["last_rebalance"] != mkey or state["universe"] is None
    if not new_month and state["last_risk"] == rkey:
        return False
    was_in_cash = not state["positions"]
    if new_month:
        state["universe"] = universe_fn()
    e = exposure(prices, state["universe"], cfg)
    old = state.get("exposure")
    if cfg.get("exposure_step") and old and e > 0 and abs(e - old) < cfg["exposure_step"]:
        e = old                                    # too small a change to be worth trading on
    if new_month or (was_in_cash and e > 0):
        state["base_weights"] = pick_stocks(prices, state["universe"], cfg, held=list(state["positions"]))
    elif cfg.get("cut_losers") and state["base_weights"]:
        ok = [t for t in state["base_weights"] if t in prices
              and prices[t].iloc[-1] > prices[t].iloc[-cfg["trend_sma"]:].mean()]
        if len(ok) < len(state["base_weights"]):    # replace the fallers, leave the rest alone
            state["base_weights"] = pick_stocks(prices, state["universe"], cfg, held=ok, locked=True)
    rebalance(state, prices.iloc[-1], {t: w * e for t, w in state["base_weights"].items()}, ts, cfg)
    state.update(last_rebalance=mkey, last_risk=rkey, exposure=e)
    return True


def finish(state, model, prices, ts, demo, names):
    """Record the portfolio's value and save it."""
    m, cfg = MODELS[model], MODELS[model]["config"]
    last = prices.iloc[-1]
    if state.get("benchmark") is None:
        for sym, name in BENCHMARKS.items():
            if pd.notna(last.get(sym)):
                state.update(benchmark=sym, benchmark_name=name, benchmark_start=float(last[sym]))
                break
    bsym = state.get("benchmark")
    for t in state["trades"]:
        t.setdefault("name", names.get(t["ticker"], ""))
    value = portfolio_value(state, last)
    bench = float(last[bsym]) if bsym and pd.notna(last.get(bsym)) else None
    bench_pct = (bench / state["benchmark_start"] - 1) * 100 if bench and state["benchmark_start"] else None
    state["history"].append({
        "time": ts, "value": round(value, 2),
        "pct": round((value / state["starting_capital"] - 1) * 100, 3),
        "benchmark_pct": round(bench_pct, 3) if bench_pct is not None else None,
    })
    # keep every reading for the last 7 days, then one per day, so the file stays small
    cutoff = (datetime.fromisoformat(ts) - timedelta(days=7)).isoformat()
    thin = {}
    for h in state["history"]:
        thin[h["time"] if h["time"] >= cutoff else h["time"][:10]] = h
    state["history"] = list(thin.values())
    state["holdings"] = [{
        "ticker": t.replace(".NS", ""), "name": names.get(t.replace(".NS", ""), ""), "qty": p["qty"],
        "bought_at": p.get("bought_at"), "last_buy": p.get("last_buy"),
        "avg_price": round(p["avg_price"], 2), "price": round(float(last[t]), 2),
        "value": round(p["qty"] * float(last[t]), 2),
        "pnl_pct": round((float(last[t]) / p["avg_price"] - 1) * 100, 2),
    } for t, p in state["positions"].items()]
    state["model"] = {"id": model, "name": m["name"], "tagline": m["tagline"]}
    state["params"] = {**{k: v for k, v in cfg.items() if not k.startswith("_")}, "cost": COST,
                       "description": describe(cfg), "listed": len(all_tickers(demo))}
    state["updated_at"] = ts
    state["demo"] = demo
    save_state(state, model)
    print(f"{m['name']:12s} {ts}  value ₹{value:,.0f}  ({state['history'][-1]['pct']:+.2f}%)")


def main():
    if "--refresh-symbols" in sys.argv:
        ok = [refresh_symbols(), refresh_index()]
        sys.exit(0 if all(ok) else 1)
    demo = "--demo" in sys.argv
    reset = "--reset" in sys.argv
    states = {k: new_state() if reset else load_state(k) for k in MODELS}
    now = datetime.now(IST)
    ts = now.isoformat(timespec="seconds")
    held = sorted({t for st in states.values() for t in st["positions"]})

    def due(model):
        """'month' = re-pick stocks, 'week' = re-check risk, None = just record value."""
        cfg, st = MODELS[model]["config"], states[model]
        mkey = period_key(now.date(), cfg["rebalance"], cfg.get("rebalance_offset", 0))
        rkey = period_key(now.date(), cfg["risk_check"]) if cfg["risk_check"] else mkey
        if st["universe"] is None or st["last_rebalance"] != mkey:
            return "month"
        return "week" if st["last_risk"] != rkey else None

    if demo:
        syms = all_tickers(demo=True)[:600] + INDEX_SYMBOLS
        _, prices, volume = demo_data(syms, 420)
        for m in MODELS.values():
            m["config"]["_members"] = syms[:400]
    else:
        # cheap first look: is the market open today?
        _, prices, volume = download(INDEX_SYMBOLS + held, "5d")
        if prices.index[-1].date() != now.date():
            print(f"No prices for today yet (latest: {prices.index[-1].date()}) - market closed or holiday.")
            if reset:
                for k, st in states.items():
                    save_state(st, k)
            return
        needs = {k: due(k) for k in MODELS}
        if "month" in needs.values():                       # re-pick: whole stock pool
            pool = set(held)
            for k, m in MODELS.items():
                if needs[k] == "month":
                    pool |= set(index_members() if m["config"]["universe"] == "index" else all_tickers())
                else:
                    pool |= set(states[k]["universe"] or [])
            _, prices, volume = download(sorted(pool) + INDEX_SYMBOLS, "2y")
        elif "week" in needs.values():                      # risk check: current lists only
            pool = set(held) | {t for st in states.values() for t in (st["universe"] or [])}
            _, prices, volume = download(sorted(pool) + INDEX_SYMBOLS, "2y")

    names = company_names()
    for k, m in MODELS.items():
        st, cfg = states[k], m["config"]
        accrue_interest(st, now.date(), cfg)
        if step(st, prices, now.date(), ts, cfg, lambda cfg=cfg: choose_universe(prices, volume, cfg)):
            print(f"{m['name']}: rebalanced. Exposure {st['exposure']:.0%} ->",
                  [t.replace('.NS', '') for t in st["positions"]] or "all cash")
        finish(st, k, prices, ts, demo, names)


if __name__ == "__main__":
    main()
