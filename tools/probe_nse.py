"""Check which NSE archive files can be downloaded, and what columns they have."""
import io, zipfile, time, requests
HDR = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
       "Accept": "*/*", "Referer": "https://www.nseindia.com/"}
S = requests.Session(); S.headers.update(HDR)
import datetime as dt
DATES = [dt.date(2019, 1, 2), dt.date(2022, 6, 1), dt.date(2024, 7, 5), dt.date(2024, 7, 8), dt.date(2026, 11, 12)]
def urls(d):
    M = d.strftime("%b").upper()
    for host in ("https://nsearchives.nseindia.com", "https://archives.nseindia.com"):
        yield "old bhavcopy", f"{host}/content/historical/EQUITIES/{d.year}/{M}/cm{d:%d}{M}{d.year}bhav.csv.zip"
        yield "udiff bhavcopy", f"{host}/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
        yield "sec_bhavdata_full", f"{host}/products/content/sec_bhavdata_full_{d:%d%m%Y}.csv"
        yield "PR zip", f"{host}/archives/equities/bhavcopy/pr/PR{d:%d%m%y}.zip"
def show(name, raw):
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
        names = z.namelist()
        print("    zip members:", names[:30])
        for n in names:
            if n.lower().endswith(".csv") and any(k in n.lower() for k in ("bhav", "mcap", "pd", "cm")):
                t = z.read(n).decode("latin1").splitlines()
                print(f"    {n}: {len(t)} lines | {t[0][:300]}")
                print(f"      {t[1][:300] if len(t)>1 else ''}")
    except zipfile.BadZipFile:
        t = raw.decode("latin1").splitlines()
        print(f"    csv {len(t)} lines | {t[0][:300]}"); print(f"      {t[1][:300] if len(t)>1 else ''}")
for d in DATES:
    print("=====", d)
    for name, u in urls(d):
        try:
            r = S.get(u, timeout=30)
            print(f"  [{r.status_code}] {name}: {u} ({len(r.content)} bytes)")
            if r.status_code == 200 and len(r.content) > 200:
                show(name, r.content)
        except Exception as e:
            print(f"  [ERR] {name}: {u}: {e}")
        time.sleep(0.5)
# a couple of index-history candidates
for u in ["https://nsearchives.nseindia.com/content/indices/ind_close_all_12112026.csv",
          "https://www.niftyindices.com/IndexConstituent/ind_niftymidsmallcap400list.csv"]:
    try:
        r = S.get(u, timeout=30); print(f"[{r.status_code}] {u} ({len(r.content)} bytes)")
        if r.status_code == 200: print("   ", r.text.splitlines()[0][:300])
    except Exception as e: print("[ERR]", u, e)
