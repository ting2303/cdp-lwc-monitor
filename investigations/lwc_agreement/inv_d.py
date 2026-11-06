"""Does the hot wire follow CDP once hour-scale excursions are filtered out?"""
import os
import pickle
import sys

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
pd.set_option("display.width", 200, "display.max_rows", 80, "display.float_format", lambda x: f"{x:.3g}")
D = os.path.dirname(os.path.abspath(__file__))
store = pickle.load(open(os.path.join(D, "inv_1hz.pkl"), "rb"))
rows = []
for sid, j in store.items():
    if j["lpas"].median() != 7:
        continue
    x = j[~j.index.duplicated()]
    idx = np.arange(int(x.index.min()), int(x.index.max()) + 1, 60)
    grouped = x[["clwc", "hot", "power", "n"]].groupby((x.index.to_numpy() // 60).astype(int) * 60).mean()
    m = grouped.reindex((idx // 60) * 60)
    cloud = (m["n"] > 50) & (m["clwc"] > 0.02)
    if cloud.sum() < 60:
        continue
    row = dict(sid=sid, camp=j["camp"].iloc[0], cloud_min=int(cloud.sum()))
    for w in (5, 15, 30, 60, 180):
        a = m["clwc"] - m["clwc"].rolling(w, center=True, min_periods=max(3, w // 3)).mean()
        b = m["hot"] - m["hot"].rolling(w, center=True, min_periods=max(3, w // 3)).mean()
        ok = cloud & a.notna() & b.notna()
        if ok.sum() >= 30 and a[ok].std() > 0 and b[ok].std() > 0:
            row[f"r{w}"] = a[ok].corr(b[ok])
            row[f"slope{w}"] = np.polyfit(a[ok], b[ok], 1)[0]
            row[f"cdpstd{w}"] = a[ok].std()
            row[f"hotstd{w}"] = b[ok].std()
    ok = cloud
    row["r_raw"] = m.loc[ok, "clwc"].corr(m.loc[ok, "hot"])
    row["slope_raw"] = np.polyfit(m.loc[ok, "clwc"].fillna(0), m.loc[ok, "hot"].fillna(0), 1)[0]
    rows.append(row)
t = pd.DataFrame(rows)
print(len(t), "sessions, LWC PAS 7, >=60 cloud minutes")
out = []
for w in (5, 15, 30, 60, 180):
    out.append(dict(remove_slower_than_min=w, r_median=t[f"r{w}"].median(), r_p25=t[f"r{w}"].quantile(0.25), r_p75=t[f"r{w}"].quantile(0.75), frac_r_pos=(t[f"r{w}"] > 0).mean(), slope_median=t[f"slope{w}"].median(), cdp_std=t[f"cdpstd{w}"].median(), hot_std=t[f"hotstd{w}"].median()))
print(pd.DataFrame(out).to_string(index=False))
print("raw: r", t.r_raw.median(), " slope", t.slope_raw.median())
print(t.sort_values("r15", ascending=False)[["sid", "camp", "cloud_min", "r_raw", "r15", "slope15", "r60", "slope60"]].head(8).to_string())
print(t.sort_values("r15")[["sid", "camp", "cloud_min", "r_raw", "r15", "slope15", "r60", "slope60"]].head(5).to_string())
