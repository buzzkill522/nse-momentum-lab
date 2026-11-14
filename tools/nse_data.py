"""
Official NSE end-of-day data for every listed share, including companies that
were later delisted or suspended (no survivorship bias).

Sources (NSE archives, free):
  - Daily bhavcopy: open, high, low, close, previous close, volume, traded value,
    ISIN for every security traded that day.
      up to 2024-07-05: /content/historical/EQUITIES/YYYY/MON/cmDDMONYYYYbhav.csv.zip
      from 2024-07-08:  /content/cm/BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip
  - Monthly market-cap file (inside the daily PR zip, from February 2024):
    issue size (shares) per company.

Price adjustment: NSE sets each day's "previous close" to the adjusted price after
a split, bonus or rights issue. Where previous close differs from the actual
close of the last trading day, the ratio is a corporate-action factor; earlier
prices are multiplied by it. Ordinary dividends are not adjusted (price return).

Renames: a company keeps its ISIN when it changes symbol, and keeps its symbol
when a split changes its ISIN, so symbols and ISINs that ever appear together are
merged into one company, named by its most recent symbol.

Usage (needs network access to NSE):
  python tools/nse_data.py build [start YYYY-MM-DD]   # download (cached) and build
  python tools/nse_data.py report                     # data-quality report
"""
import concurrent.futures as cf
import datetime as dt
import io
import sys
import time
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache" / "nse"
RAW = CACHE / "raw"
MCAP = CACHE / "mcap"
PANEL = CACHE / "panel.pkl"
SERIES = {"EQ", "BE", "BZ"}          # normal, trade-for-trade, non-compliant (falling stocks end up here)
HOSTS = ("https://nsearchives.nseindia.com", "https://archives.nseindia.com")
UDIFF_FROM = dt.date(2024, 7, 8)
MCAP_FROM = dt.date(2024, 2, 1)
ADJ_TOL = 0.005                      # previous-close mismatch treated as a corporate action

_S = requests.Session()
_S.headers.update({"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
                   "Accept": "*/*", "Referer": "https://www.nseindia.com/"})


def _get(path):
    """Bytes of an archive file, None if NSE has no file for that day (holiday)."""
    for attempt in range(4):
        for host in HOSTS:
            try:
                r = _S.get(host + path, timeout=30)
                if r.status_code == 200 and len(r.content) > 500:
                    return r.content
                if r.status_code == 404:
                    continue
            except requests.RequestException:
                pass
        time.sleep(1 + 2 * attempt)
        if attempt >= 1:
            return None
    return None


def _one_zip_csv(raw):
    z = zipfile.ZipFile(io.BytesIO(raw))
    return pd.read_csv(z.open(z.namelist()[0]), dtype=str)


def fetch_day(d):
    """Equity rows for one trading day, standard columns, or None for a holiday."""
    if d < UDIFF_FROM:
        m = d.strftime("%b").upper()
        raw = _get(f"/content/historical/EQUITIES/{d.year}/{m}/cm{d:%d}{m}{d.year}bhav.csv.zip")
        if raw is None:
            return None
        df = _one_zip_csv(raw)
        df.columns = [c.strip() for c in df.columns]
        df = df.rename(columns={"SYMBOL": "symbol", "SERIES": "series", "OPEN": "open", "HIGH": "high",
                                "LOW": "low", "CLOSE": "close", "PREVCLOSE": "prev", "TOTTRDQTY": "volume",
                                "TOTTRDVAL": "value", "ISIN": "isin"})
    else:
        raw = _get(f"/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip")
        if raw is None:
            return None
        df = _one_zip_csv(raw)
        df = df.rename(columns={"TckrSymb": "symbol", "SctySrs": "series", "OpnPric": "open", "HghPric": "high",
                                "LwPric": "low", "ClsPric": "close", "PrvsClsgPric": "prev", "TtlTradgVol": "volume",
                                "TtlTrfVal": "value", "ISIN": "isin"})
    df = df[[c for c in ("symbol", "series", "open", "high", "low", "close", "prev", "volume", "value", "isin") if c in df]]
    df["series"] = df["series"].str.strip()
    df = df[df["series"].isin(SERIES)].copy()
    for c in ("open", "high", "low", "close", "prev", "volume", "value"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["symbol"] = df["symbol"].str.strip()
    df["isin"] = df["isin"].str.strip()
    df["date"] = pd.Timestamp(d)
    return df


def fetch_mcap(d):
    """{symbol: issue size} from the market-cap file in that day's PR zip, or None."""
    raw = _get(f"/archives/equities/bhavcopy/pr/PR{d:%d%m%y}.zip")
    if raw is None:
        return None
    z = zipfile.ZipFile(io.BytesIO(raw))
    name = next((n for n in z.namelist() if n.lower().startswith("mcap")), None)
    if name is None:
        return None
    df = pd.read_csv(z.open(name), dtype=str)
    df.columns = [c.strip() for c in df.columns]
    df = df[df["Series"].str.strip().isin(SERIES)]
    out = pd.DataFrame({"symbol": df["Symbol"].str.strip(),
                        "shares": pd.to_numeric(df["Issue Size"].str.strip(), errors="coerce")})
    out["date"] = pd.Timestamp(d)
    return out.dropna()


def download(start, end):
    """Download every missing day into the cache (holidays leave an empty marker)."""
    RAW.mkdir(parents=True, exist_ok=True)
    MCAP.mkdir(parents=True, exist_ok=True)
    days = [start + dt.timedelta(n) for n in range((end - start).days + 1)]
    days = [d for d in days if d.weekday() < 5]
    todo = [d for d in days if not (RAW / f"{d:%Y%m%d}.csv.gz").exists() and not (RAW / f"{d:%Y%m%d}.none").exists()]
    print(f"bhavcopy: {len(days)} weekdays, {len(todo)} to download", flush=True)

    def job(d):
        df = fetch_day(d)
        if df is None:
            if d < dt.date.today() - dt.timedelta(days=3):     # only trust "holiday" for past days
                (RAW / f"{d:%Y%m%d}.none").touch()
            return 0
        df.to_csv(RAW / f"{d:%Y%m%d}.csv.gz", index=False)
        return 1

    got = 0
    with cf.ThreadPoolExecutor(4) as ex:
        for i, n in enumerate(ex.map(job, todo)):
            got += n
            if i % 200 == 199:
                print(f"  {i + 1}/{len(todo)} done", flush=True)
    print(f"bhavcopy: downloaded {got} trading days", flush=True)

    # market cap: first available trading day of each month from Feb 2024
    m = max(start, MCAP_FROM).replace(day=1)
    while m <= end:
        if not (MCAP / f"{m:%Y%m}.csv").exists():
            for k in range(10):
                d = m + dt.timedelta(k)
                if d.weekday() < 5 and (RAW / f"{d:%Y%m%d}.csv.gz").exists():
                    mc = fetch_mcap(d)
                    if mc is not None:
                        mc.to_csv(MCAP / f"{m:%Y%m}.csv", index=False)
                        break
        m = (m.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    print(f"market cap: {len(list(MCAP.glob('*.csv')))} monthly files", flush=True)


def _canonical(raw):
    """Map every symbol to one name per company, joining symbols that share an ISIN."""
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for s, i in raw[["symbol", "isin"]].drop_duplicates().itertuples(index=False):
        if isinstance(i, str) and i:
            a, b = find("S:" + s), find("I:" + i)
            if a != b:
                parent[a] = b
    last = raw.groupby("symbol")["date"].max()
    groups = {}
    for s in last.index:
        groups.setdefault(find("S:" + s), []).append(s)
    mapping = {}
    for members in groups.values():
        name = max(members, key=lambda s: last[s])          # most recent symbol names the company
        for s in members:
            mapping[s] = name
    return mapping


def build():
    files = sorted(RAW.glob("*.csv.gz"))
    raw = pd.concat((pd.read_csv(f, dtype={"symbol": str, "isin": str, "series": str}, parse_dates=["date"]) for f in files),
                    ignore_index=True)
    raw = raw.dropna(subset=["close"])
    mapping = _canonical(raw)
    raw["company"] = raw["symbol"].map(mapping)
    renamed = int((raw["symbol"] != raw["company"]).groupby(raw["symbol"]).any().sum())
    # one row per company per day: the series with the most traded value
    raw = raw.sort_values("value").drop_duplicates(["company", "date"], keep="last")
    wide = {c: raw.pivot(index="date", columns="company", values=c).sort_index()
            for c in ("open", "close", "prev", "volume", "value")}
    close, prev = wide["close"], wide["prev"]

    # corporate-action factors from previous-close mismatches
    last_close = close.ffill().shift(1)
    ratio = (prev / last_close).where(close.notna())
    events = ratio.where((ratio - 1).abs() > ADJ_TOL)
    factor = events.fillna(1.0)
    # adjustment for day t = product of factors strictly after t
    after = factor[::-1].cumprod()[::-1].shift(-1).fillna(1.0)
    adj_close = close * after
    adj_open = wide["open"] * after

    # shares outstanding, expressed on today's share basis (see module docstring)
    shares_end = {}
    for f in sorted(MCAP.glob("*.csv")):
        mc = pd.read_csv(f, parse_dates=["date"])
        d = mc["date"].iloc[0]
        if d not in after.index:
            continue
        mc["company"] = mc["symbol"].map(mapping).fillna(mc["symbol"])
        for c, sh in mc.groupby("company")["shares"].last().items():
            if c in after.columns:
                shares_end[c] = sh / after.at[d, c]          # later files overwrite earlier ones
    mcap = adj_close * pd.Series(shares_end).reindex(adj_close.columns)

    first = close.apply(lambda s: s.first_valid_index())
    lastd = close.apply(lambda s: s.last_valid_index())
    meta = {
        "days": int(len(close)), "companies": int(close.shape[1]), "renamed_symbols": renamed,
        "corporate_actions": int(events.notna().sum().sum()),
        "stopped_trading": int((lastd < close.index[-1] - pd.Timedelta(days=30)).sum()),
        "with_shares": int(len(shares_end)),
        "first_day": str(close.index[0].date()), "last_day": str(close.index[-1].date()),
    }
    panel = {"open": adj_open, "close": adj_close, "value": wide["value"].fillna(0.0),
             "mcap": mcap, "first": first, "last": lastd, "events": events.stack().dropna(), "meta": meta}
    pd.to_pickle(panel, PANEL)
    print("panel:", meta, flush=True)
    return panel


def load_panel():
    return pd.read_pickle(PANEL)


def as_matrices(panel):
    """(open, close, volume) shaped like trader.download(): '.NS' columns, close forward-filled,
    and a volume chosen so that close x volume = actual traded value in rupees."""
    close = panel["close"].ffill()
    opn = panel["open"].reindex_like(close).fillna(close)
    volume = (panel["value"].reindex_like(close).fillna(0.0) / close).fillna(0.0)
    ren = lambda df: df.rename(columns=lambda c: c + ".NS")  # noqa: E731
    return ren(opn), ren(close), ren(volume), ren(panel["mcap"].reindex_like(close))


def report(yahoo_close=None):
    """Data-quality report; with Yahoo prices, compare daily returns company by company."""
    p = load_panel()
    lines = ["=== NSE DATA REPORT ===", *(f"{k}: {v}" for k, v in p["meta"].items())]
    ev = p["events"]
    lines.append(f"corporate-action factors: median {ev.median():.3f}, below 0.95: {(ev < 0.95).sum()}, "
                 f"0.95 to 1.05: {((ev >= 0.95) & (ev <= 1.05)).sum()}")
    stopped = p["last"][p["last"] < p["close"].index[-1] - pd.Timedelta(days=30)].sort_values()
    lines.append(f"companies that stopped trading before the end: {len(stopped)}; examples: "
                 + ", ".join(f"{c} ({d.date()})" for c, d in list(stopped.items())[:15]))
    if yahoo_close is not None:
        nse = p["close"].rename(columns=lambda c: c + ".NS")
        common = [c for c in yahoo_close.columns if c in nse.columns]
        idx = nse.index.intersection(yahoo_close.index)
        a = nse.loc[idx, common].pct_change(fill_method=None)
        b = yahoo_close.loc[idx, common].pct_change(fill_method=None)
        diff = (a - b).abs()
        valid = diff.notna().sum().sum()
        big = (diff > 0.02).sum().sum()
        per = (diff > 0.02).sum().sort_values(ascending=False)
        corr = a.stack().corr(b.stack())
        lines += [f"Yahoo comparison: {len(common)} companies, {valid} company-days",
                  f"  daily-return correlation {corr:.4f}; days differing by more than 2 points: {big} ({big / max(valid, 1):.3%})",
                  "  most mismatched: " + ", ".join(f"{c[:-3]} {n}" for c, n in per.head(10).items())]
    text = "\n".join(lines)
    print(text, flush=True)
    return text


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    if cmd == "build":
        start = dt.date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else dt.date(2019, 1, 1)
        download(start, dt.date.today())
        build()
    elif cmd == "report":
        report()
