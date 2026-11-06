import os
import pickle
import sys

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
pd.set_option("display.width", 230, "display.max_rows", 120, "display.max_columns", 40, "display.float_format", lambda x: f"{x:.3g}")
D = os.path.dirname(os.path.abspath(__file__))
store = pickle.load(open(os.path.join(D, "inv_1hz.pkl"), "rb"))
print("sessions", len(store), "hours", sum(len(v) for v in store.values()) / 3600)


def corr(a, b):
    ok = a.notna() & b.notna()
    if ok.sum() < 10 or a[ok].std() == 0 or b[ok].std() == 0:
        return np.nan
    return float(a[ok].corr(b[ok]))


def spearman(a, b):
    ok = a.notna() & b.notna()
    if ok.sum() < 10:
        return np.nan
    return float(a[ok].rank().corr(b[ok].rank()))


def clear_baseline(j, window=600, min_count=60):
    """Interpolate clear-air medians from fixed windows over the whole session."""
    clear = j["n"] < 1
    idx = (j.index.to_numpy() // window).astype(int)
    med = j.loc[clear, "hot"].groupby(idx[clear.to_numpy()]).agg(["median", "count"])
    med = med[med["count"] >= min_count]
    if len(med) < 2:
        return None, len(med)
    centers = (med.index.to_numpy() + 0.5) * window
    return np.interp(j.index.to_numpy(), centers, med["median"].to_numpy()), len(med)


rows = []
pool = []
for sid, j in store.items():
    j = j.copy()
    cloud = (j["n"] > 50) & (j["clwc"] > 0.02)
    if cloud.sum() < 300:
        continue
    base, nb = clear_baseline(j)
    r = dict(sid=sid, camp=j["camp"].iloc[0], cloud_s=int(cloud.sum()), clear_s=int((j["n"] < 1).sum()), base_bins=nb)
    r["hot_mean"] = j.loc[cloud, "hot"].mean()
    r["cdp_mean"] = j.loc[cloud, "clwc"].mean()
    r["cdp_std"] = j.loc[cloud, "clwc"].std()
    r["hot_std"] = j.loc[cloud, "hot"].std()
    r["c_all_1s"] = corr(j["clwc"], j["hot"])
    r["c_cloud_1s"] = corr(j.loc[cloud, "clwc"], j.loc[cloud, "hot"])
    r["c_calc_all"] = corr(j["clwc"], j["hotcalc"])
    r["c_power_all"] = corr(j["clwc"], j["power"])
    bins = (j.index.to_numpy() // 60).astype(int)
    m = j[["clwc", "hot", "hotcalc", "power", "n", "mvd", "wing", "elec_t", "ctrl", "dap_avg", "dap_calc"]].groupby(bins).mean()
    m["cloud_frac"] = cloud.groupby(bins).mean().to_numpy()
    r["c_all_60"] = corr(m["clwc"], m["hot"])
    r["c_power_60"] = corr(m["clwc"], m["power"])
    r["c_wing_hot_60"] = corr(m["wing"], m["hot"])
    r["c_ctrl_hot_60"] = corr(m["ctrl"], m["hot"])
    r["c_dap_hot_60"] = corr(m["dap_avg"], m["hot"])
    full = m["cloud_frac"] >= 0.5
    r["c_cloud_60"] = corr(m.loc[full, "clwc"], m.loc[full, "hot"])
    r["dap_unique"] = j["dap_avg"].round(3).nunique()
    if base is not None:
        j["hotbc"] = j["hot"] - base
        r["hotbc_cloud_mean"] = j.loc[cloud, "hotbc"].mean()
        r["c_bc_cloud_1s"] = corr(j.loc[cloud, "clwc"], j.loc[cloud, "hotbc"])
        mb = j["hotbc"].groupby(bins).mean()
        r["c_bc_all_60"] = corr(m["clwc"], mb)
        r["c_bc_cloud_60"] = corr(m.loc[full, "clwc"], mb[full])
        clear = j["n"] < 1
        r["res_clear_std"] = j.loc[clear, "hotbc"].std()
        r["clear_60_std"] = mb[(m["n"] < 1)].std()
    rows.append(r)

t = pd.DataFrame(rows)
t.to_pickle(os.path.join(D, "inv_sessions.pkl"))
print(len(t), "sessions with >=300 s cloud")
cols = ["c_cloud_1s", "c_cloud_60", "c_all_1s", "c_all_60", "c_bc_cloud_1s", "c_bc_cloud_60", "c_bc_all_60", "c_calc_all", "c_power_all", "c_power_60", "c_wing_hot_60", "c_ctrl_hot_60", "c_dap_hot_60"]
print(t[cols].describe().T[["count", "min", "25%", "50%", "75%", "max"]].to_string())
print("\nstds: cdp", t.cdp_std.median(), "hot", t.hot_std.median(), " clear residual 1s", t.res_clear_std.median(), " clear residual 60s", t.clear_60_std.median())
print("mean: cdp", t.cdp_mean.median(), "hot", t.hot_mean.median(), "hot bc", t.hotbc_cloud_mean.median())
print("base bins", t.base_bins.describe()[["min", "50%", "max"]].to_dict(), " dap unique", t.dap_unique.describe()[["min", "50%", "max"]].to_dict())
print("\nby campaign"); print(t.groupby("camp")[["c_cloud_1s", "c_cloud_60", "c_all_60", "c_bc_cloud_60", "c_bc_all_60", "c_power_60", "c_wing_hot_60", "cdp_mean", "hotbc_cloud_mean"]].median().to_string())
print("\nsessions with best all_60"); print(t.sort_values("c_all_60", ascending=False)[["sid", "camp", "cloud_s", "c_all_60", "c_cloud_60", "c_bc_all_60", "c_power_60", "cdp_mean", "hot_mean"]].head(10).to_string())
print("\nworst"); print(t.sort_values("c_all_60")[["sid", "camp", "cloud_s", "c_all_60", "c_cloud_60", "c_bc_all_60", "c_power_60", "cdp_mean", "hot_mean"]].head(8).to_string())
