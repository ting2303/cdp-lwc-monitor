import os
import pickle
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))
from cpd_lwc_qc.pads import align_seconds, discover_sessions, read_instrument  # noqa: E402
from cpd_lwc_qc.sources import DataSet  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
pd.set_option("display.width", 230, "display.max_rows", 120, "display.max_columns", 40, "display.float_format", lambda x: f"{x:.3g}")
D = os.path.dirname(os.path.abspath(__file__))
store = pickle.load(open(os.path.join(D, "inv_1hz.pkl"), "rb"))
W_PER_G = 0.6636  # PADS value at PAS 7, measured above


def r2(y, X):
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    pred = X @ beta
    return 1 - ((y - pred) ** 2).sum() / ((y - y.mean()) ** 2).sum(), beta


print("=== A. Clear-air power vs board temperature; can it predict dry power inside cloud?")
rows = []
for sid, j in store.items():
    if abs(j["lpas"].median() - 7) > 0.01:
        continue
    bins = (j.index.to_numpy() // 60).astype(int)
    m = j[["power", "wing", "ctrl", "elec_t", "clwc", "n", "hot", "dap_calc"]].groupby(bins).mean().dropna()
    clear = m[m["n"] < 1]
    cloud = m[(m["n"] > 50) & (m["clwc"] > 0.02)]
    if len(clear) < 30:
        continue
    X = np.column_stack([np.ones(len(clear)), clear["wing"]])
    rw, bw = r2(clear["power"].to_numpy(), X)
    X2 = np.column_stack([np.ones(len(clear)), clear["wing"], clear["ctrl"], clear["elec_t"]])
    rall, ball = r2(clear["power"].to_numpy(), X2)
    row = dict(sid=sid, camp=j["camp"].iloc[0], clear_min=len(clear), cloud_min=len(cloud), r2_wing=rw, slope_wing=bw[1], r2_temps=rall, clear_power_std=clear["power"].std(), wing_range=clear["wing"].quantile(0.95) - clear["wing"].quantile(0.05), resid_std_gm3=(clear["power"] - np.column_stack([np.ones(len(clear)), clear["wing"], clear["ctrl"], clear["elec_t"]]) @ ball).std() / W_PER_G)
    if len(cloud) >= 20:
        pred = np.column_stack([np.ones(len(cloud)), cloud["wing"], cloud["ctrl"], cloud["elec_t"]]) @ ball
        est = (cloud["power"] - pred) / W_PER_G
        row["corr_raw"] = cloud["hot"].corr(cloud["clwc"])
        row["corr_tempfix"] = est.corr(cloud["clwc"])
        row["ratio_tempfix"] = est.mean() / cloud["clwc"].mean()
        row["cdp_mean"] = cloud["clwc"].mean()
    rows.append(row)
a = pd.DataFrame(rows)
print(len(a), "sessions at LWC PAS 7 with >=30 clear minutes")
print(a[["r2_wing", "slope_wing", "r2_temps", "clear_power_std", "wing_range", "resid_std_gm3", "corr_raw", "corr_tempfix", "ratio_tempfix"]].describe().T[["count", "25%", "50%", "75%"]].to_string())
print("expected slope if power follows ambient T:", -2.32 / 140, "W/C")

print("\n=== B. Cloud minus clear LWC per session (slow drift mostly cancelled by same-session comparison)")
rows = []
for sid, j in store.items():
    cloud = j[(j["n"] > 50) & (j["clwc"] > 0.02)]
    clear = j[j["n"] < 1]
    if len(cloud) < 300 or len(clear) < 300:
        continue
    w = 0.6636 * j["lpas"].median() / 7
    rows.append(dict(sid=sid, camp=j["camp"].iloc[0], kind="lab" if "lab" in j["camp"].iloc[0].lower() else "field", cdp=cloud["clwc"].mean(), hot_diff=cloud["hot"].mean() - clear["hot"].mean(), power_diff_gm3=(cloud["power"].mean() - clear["power"].mean()) / w, mvd=cloud["mvd"].mean(), n=cloud["n"].mean(), cloud_min=len(cloud) / 60, cpas=j["cpas"].median(), lpas=j["lpas"].median(), wire=j["wire"].median()))
b = pd.DataFrame(rows)
b["ratio"] = b["power_diff_gm3"] / b["cdp"]
print(b.groupby("kind").ratio.describe()[["count", "25%", "50%", "75%"]].to_string())
print(b.sort_values("sid")[["sid", "camp", "cdp", "power_diff_gm3", "ratio", "mvd", "n", "cloud_min", "cpas", "lpas", "wire"]].to_string())
print("cdp-weighted field ratio:", b[b.kind == "field"].power_diff_gm3.sum() / b[b.kind == "field"].cdp.sum())
print("r(ratio, mvd)", b.ratio.corr(b.mvd), " r(ratio,n)", b.ratio.corr(b.n))

print("\n=== C. Sessions with FM-100 alongside CDP")
with DataSet(sys.argv[1]) as ds:
    sess = [s for s in discover_sessions(ds) if {"fm", "cdp"} <= set(s.instruments)]
    print([(s.sid, s.campaign, sorted(s.instruments)) for s in sess])
    for s in sess:
        tables = {k: read_instrument(ds, f, k) for k, f in s.instruments.items()}
        j = align_seconds(tables)
        print(s.sid, "rows", len(j))
        cols = [c for c in j.columns if c.endswith(("number_cm3", "lwc_gm3", "mvd_um", "pas_ms"))]
        print(j[cols].describe().T[["count", "mean", "50%", "max"]].to_string())
        act = j[(j["cdp_number_cm3"] > 10) | (j["fm_number_cm3"] > 10)]
        print("active rows", len(act))
        if len(act) > 30:
            print("corr number", act["cdp_number_cm3"].corr(act["fm_number_cm3"]), " corr lwc", act["cdp_lwc_gm3"].corr(act["fm_lwc_gm3"]))
            print(act[cols].mean().to_string())

print("\n=== D. Lab fog test sessions")
for sid, j in store.items():
    if "fog" in j["camp"].iloc[0]:
        cloud = j[(j["n"] > 50) & (j["clwc"] > 0.02)]
        print(sid, j["camp"].iloc[0], "rows", len(j), "cloud", len(cloud))
        if len(cloud):
            print(cloud[["n", "clwc", "mvd", "hot", "power", "cpas", "lpas", "p", "tamb", "wire"]].describe().T[["mean", "min", "50%", "max"]].to_string())
            clear = j[j["n"] < 1]
            print("clear rows", len(clear), clear[["hot", "power", "wing"]].describe().T[["mean", "std"]].to_string())
