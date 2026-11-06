"""Second-by-second bead populations for CDP calibration dates, plus laser health for all CDP sessions."""
import io
import os
import pickle
import re
import sys
import zipfile

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
pd.set_option("display.width", 220, "display.max_rows", 220, "display.max_columns", 30, "display.max_colwidth", 40, "display.float_format", lambda x: f"{x:.4g}")
D = os.path.dirname(os.path.abspath(__file__))
hists = pickle.load(open(os.path.join(D, "cal_hists.pkl"), "rb"))
idx = pickle.load(open(os.path.join(D, "idx.pkl"), "rb"))
DATES = {"20240905": "標示未寫粒徑", "20250312": "標示未寫粒徑", "20250321": "15 µm", "20250813": "標示未寫粒徑", "20261018": "32 µm"}
MODES = {"large": (20.0, 36.0), "small": (11.0, 18.0)}
MIN_COUNTS = 30

rows = []
seconds = []
for (sid, kind, folder), h in sorted(hists.items()):
    if kind != "cdp" or sid[:8] not in DATES:
        continue
    sizes = np.array(h["sizes"], float)
    lower = np.r_[2.0, sizes[:-1]]
    mid = (lower + sizes) / 2
    counts = h["counts"]
    per = counts.sum(axis=1)
    for name, (lo, hi) in MODES.items():
        mask = (mid >= lo) & (mid <= hi)
        share = np.divide(counts[:, mask].sum(axis=1), per, out=np.zeros_like(per, dtype=float), where=per > 0)
        take = (per >= MIN_COUNTS) & (share >= 0.4)
        if name == "small":
            # A droplet spectrum also fills this range, so require little water above and low totals.
            above = counts[:, mid > 18].sum(axis=1) / np.maximum(per, 1)
            take &= above < 0.1
        if not take.any():
            continue
        pool = counts[take].sum(axis=0)
        mean = (mid[mask] * pool[mask]).sum() / pool[mask].sum()
        sd = np.sqrt((pool[mask] * (mid[mask] - mean) ** 2).sum() / pool[mask].sum())
        cum = np.cumsum(pool[mask]) / pool[mask].sum()
        rows.append(dict(sid=sid, date=sid[:8], label=DATES[sid[:8]], mode=name, seconds=int(take.sum()), counts=int(pool.sum()), in_mode=int(pool[mask].sum()), share=pool[mask].sum() / pool.sum(), mean=mean, sd=sd, se=sd / np.sqrt(pool[mask].sum()), median_bin=mid[mask][np.searchsorted(cum, 0.5)], below8=pool[mid < 8].sum() / pool.sum(), max_per_sec=int(per[take].max())))
t = pd.DataFrame(rows)
print(t.to_string())
t.to_csv(os.path.join(D, "cal_beads_by_session.csv"), index=False, encoding="utf-8-sig")

print("\n=== large-mode pooled by date")
agg = []
for date, g in t[t["mode"] == "large"].groupby("date"):
    w = g["in_mode"]
    mean = (g["mean"] * w).sum() / w.sum()
    agg.append(dict(date=date, label=g["label"].iloc[0], sessions=len(g), seconds=g["seconds"].sum(), in_mode=w.sum(), mean=mean, session_min=g["mean"].min(), session_max=g["mean"].max(), sd=(g["sd"] * w).sum() / w.sum(), share=(g["share"] * g["counts"]).sum() / g["counts"].sum(), below8=(g["below8"] * g["counts"]).sum() / g["counts"].sum()))
a = pd.DataFrame(agg)
print(a.to_string())
a.to_csv(os.path.join(D, "cal_beads_by_date.csv"), index=False, encoding="utf-8-sig")
x = pd.to_datetime(t[t["mode"] == "large"]["date"]).map(lambda d: d.toordinal()).to_numpy()
y = t[t["mode"] == "large"]["mean"].to_numpy()
if len(x) >= 3:
    slope, intercept = np.polyfit((x - x.min()) / 365.25, y, 1)
    print("session-level trend um/yr", slope, "r", np.corrcoef(x, y)[0, 1], "n", len(x))

print("\n=== laser and baseline health for every CDP session")


def op(p):
    return zipfile.ZipFile(idx[p][0]).open(p)


rows = []
for p in sorted(idx):
    m = re.match(r"^Instruments/.*/\d\d(CDP PBP)(\d{14})\.csv$", p)
    if not m:
        continue
    stream = io.TextIOWrapper(op(p), encoding="cp950", errors="replace", newline="")
    while True:
        line = stream.readline()
        if not line or line.startswith("****"):
            break
    want = {"Laser Current (mA)": "i", "Laser Temp (C)": "t", "Dump Spot Monitor (V)": "dump", "Sizer Baseline (V)": "sb", "Qualifier Baseline (V)": "qb", "Wingboard Temp (C)": "wing"}
    df = pd.read_csv(stream, usecols=lambda c: c.strip() in want, on_bad_lines="skip", low_memory=False, skipinitialspace=True)
    if df.empty:
        continue
    df.columns = [want[c.strip()] for c in df.columns]
    df = df.apply(pd.to_numeric, errors="coerce")
    run = df[df["i"] > 40]
    if len(run) < 30:
        continue
    rows.append(dict(sid=m.group(2), camp=p.split("/")[3], secs=len(run), i=run["i"].median(), t=run["t"].median(), dump=run["dump"].median(), sb=run["sb"].median(), qb=run["qb"].median()))
h = pd.DataFrame(rows).sort_values("sid")
h.to_pickle(os.path.join(D, "cal_laser.pkl"))
h["date"] = pd.to_datetime(h["sid"].str[:8])
h["year"] = h["date"].dt.year + (h["date"].dt.month - 1) // 6 * 0.5
print(len(h))
print(h.groupby("year")[["i", "t", "dump", "sb", "qb"]].agg(["median", "min", "max"]).round(2).to_string())
X = np.column_stack([np.ones(len(h)), h["t"], (h["date"] - h["date"].min()).dt.days / 365.25])
ok = ~np.isnan(X).any(axis=1) & h["i"].notna().to_numpy()
beta, *_ = np.linalg.lstsq(X[ok], h["i"].to_numpy()[ok], rcond=None)
res = h["i"].to_numpy()[ok] - X[ok] @ beta
print("laser current = a + b*T + c*years: ", beta, " resid sd", res.std(), " R2", 1 - res.var() / h["i"][ok].var())
