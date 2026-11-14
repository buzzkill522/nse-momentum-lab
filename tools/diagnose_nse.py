"""Why does the price adjustment find so many corporate actions? And do 12-month returns match Yahoo?"""
import sys
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nse_data as N
import trader as T

files = sorted(N.RAW.glob("*.csv.gz"))
raw = pd.concat((pd.read_csv(f, dtype={"symbol": str, "isin": str, "series": str}, parse_dates=["date"]) for f in files), ignore_index=True)
raw = raw.dropna(subset=["close"]).sort_values(["symbol", "date"])
# per raw symbol, no merging: previous-close vs actual previous close
raw["last_close"] = raw.groupby("symbol")["close"].shift(1)
raw["last_date"] = raw.groupby("symbol")["date"].shift(1)
raw["last_series"] = raw.groupby("symbol")["series"].shift(1)
raw["r"] = raw["prev"] / raw["last_close"]
ev = raw[(raw["r"] - 1).abs() > N.ADJ_TOL].copy()
print("raw-symbol mismatches:", len(ev), "of", raw["r"].notna().sum())
ev["era"] = np.where(ev["date"] < pd.Timestamp(N.UDIFF_FROM), "old", "udiff")
ev["gapdays"] = (ev["date"] - ev["last_date"]).dt.days
ev["series_change"] = ev["series"] != ev["last_series"]
print(ev.groupby([ev["date"].dt.year, "era"]).size().to_string())
print("share with series change:", ev["series_change"].mean().round(3), " share with gap > 5 days:", (ev["gapdays"] > 5).mean().round(3))
b = pd.cut(ev["r"], [0, 0.5, 0.9, 0.97, 0.995, 1.005, 1.03, 1.1, 2, 100])
print(b.value_counts().sort_index().to_string())
small = ev[(ev["r"] > 0.97) & (ev["r"] < 1.03)]
print("sample small mismatches:")
print(small.sample(min(25, len(small)), random_state=1)[["date", "symbol", "series", "last_series", "prev", "last_close", "close", "gapdays", "r"]].to_string())
big = ev[(ev["r"] < 0.9)]
print("sample big (split/bonus-like):")
print(big.sample(min(15, len(big)), random_state=1)[["date", "symbol", "series", "prev", "last_close", "r"]].to_string())

# does the merge (ISIN union) create false events?
p = N.load_panel()
print("panel events:", len(p["events"]), " raw-symbol events:", len(ev))

# 12-month return check against Yahoo for current index members
members = T.index_members()
_, y, _ = T.download(members, "7y")
nse = p["close"].rename(columns=lambda c: c + ".NS")
common = [c for c in y.columns if c in nse.columns]
idx = nse.index.intersection(y.index)
months = [d for d in idx[300:] if d.month != (idx[idx.get_loc(d) - 1]).month]
rows = []
for d in months:
    i = idx.get_loc(d)
    a = nse.loc[idx[i - 21], common] / nse.loc[idx[i - 273], common] - 1
    b2 = y.loc[idx[i - 21], common] / y.loc[idx[i - 273], common] - 1
    diff = (a - b2).abs().dropna()
    rows.append((d.date(), len(diff), diff.median(), (diff > 0.10).mean(), (diff > 0.30).mean()))
df = pd.DataFrame(rows, columns=["month", "n", "median abs diff", "share >10pts", "share >30pts"])
print(df.round(3).to_string())
last = (nse.loc[idx[-22], common] / nse.loc[idx[-274], common] - 1) - (y.loc[idx[-22], common] / y.loc[idx[-274], common] - 1)
print("worst 12-month mismatches at the end:")
print(last.abs().sort_values(ascending=False).head(20).round(3).to_string())
