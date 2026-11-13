"""When do the MCAP (market value) files start inside NSE's daily PR archive?"""
import io, zipfile, time, datetime as dt, requests
S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36", "Referer": "https://www.nseindia.com/"})
def pr(d):
    r = S.get(f"https://nsearchives.nseindia.com/archives/equities/bhavcopy/pr/PR{d:%d%m%y}.zip", timeout=30)
    if r.status_code != 200: return None
    return [n for n in zipfile.ZipFile(io.BytesIO(r.content)).namelist()]
d = dt.date(2019, 1, 1)
while d < dt.date(2024, 9, 1):
    for k in range(7):                       # first weekday of the month that has a file
        x = d + dt.timedelta(days=k)
        if x.weekday() < 5:
            n = pr(x)
            if n is not None:
                print(x, "MCAP" if any("mcap" in m.lower() for m in n) else "-", [m for m in n if "mcap" in m.lower()])
                break
        time.sleep(0.3)
    d = (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
