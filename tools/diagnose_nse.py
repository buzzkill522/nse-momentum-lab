"""Why does the price adjustment find so many corporate actions? And do 12-month returns match Yahoo?"""
import sys
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nse_data as N
import trader as T

p0 = N.load_panel()
for k, v in p0["meta"].items():
    print(f"{k}: {v}")
close = p0["close"]
lc = close.ffill().shift(1)
mv = (close / lc).stack()
print("adjusted one-day moves below -38% or above +60%:", int(((mv < 0.62) | (mv > 1.6)).sum()))
print(mv[(mv < 0.62) | (mv > 1.6)].sort_values().head(25).round(3).to_string())
# does the merge (ISIN union) create false events?
p = N.load_panel()
print("panel events:", len(p["events"]))

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
