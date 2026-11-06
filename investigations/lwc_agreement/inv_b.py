import os
import pickle
import sys

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
pd.set_option("display.width", 230, "display.max_rows", 120, "display.max_columns", 40, "display.float_format", lambda x: f"{x:.3g}")
D = os.path.dirname(os.path.abspath(__file__))
store = pickle.load(open(os.path.join(D, "inv_1hz.pkl"), "rb"))

L_ = 2.0e-2  # m, manual section 2.3
d_ = 0.18e-2  # m


def w_per_gm3(v, ta_c=10.0):
    """Watts of evaporative power per g/m3 from the manual's formula (Lv ~ 2500 J/g, c ~ 4.18 J/g/K)."""
    energy = 2500 - 2.4 * ta_c + 4.18 * (92 - ta_c)
    return L_ * d_ * v * energy


print("=== 1. Sensitivity from the manual formula at PAS 7 m/s")
s = w_per_gm3(7)
print(f"evaporative power per 1 g/m3: {s:.3f} W; 1 W = {1/s:.2f} g/m3")

# Validate the formula against PADS's own output
vals = []
for sid, j in list(store.items()):
    c = j[["power", "dap_calc", "hotcalc", "lpas", "tamb"]].dropna().sample(min(2000, len(j)), random_state=0)
    k = (c["power"] - c["dap_calc"]) / c["hotcalc"].replace(0, np.nan)
    vals.append((sid, c["lpas"].median(), k.median(), c["dap_calc"].median(), c["power"].median(), j.loc[j["n"] < 1, "power"].median(), j["wire"].median(), c["tamb"].median(), j["p"].median()))
v = pd.DataFrame(vals, columns=["sid", "pas", "w_per_g", "dap_calc", "power_med", "power_clear", "wire", "tamb", "p"])
print("PADS W per g/m3 by PAS:", v.groupby("pas").w_per_g.median().to_dict(), " formula:", {p: round(w_per_gm3(p), 3) for p in [7, 8, 8.45]})

print("\n=== 2. Sensitivity to true air speed and temperature (dry power ~ v^0.5, (Ts-Ta))")
for col, val in [("dap", v[v.pas == 7].dap_calc.median()), ("clearP", v[v.pas == 7].power_clear.median())]:
    print(col, val)
P = v[v.pas == 7].dap_calc.median()
print(f"at PAS 7, DAP={P:.2f} W: +1 m/s -> +{P*0.5/7:.3f} W = {P*0.5/7/s:.3f} g/m3 apparent; 1 C ambient -> {P/140:.4f} W = {P/140/s:.3f} g/m3")

print("\n=== 3. Air speed implied by clear-air power, v=PAS*(P/DAPcalc)^2")
rows = []
for sid, j in store.items():
    clear = j[j["n"] < 1]
    if len(clear) < 300:
        continue
    implied = clear["lpas"] * (clear["power"] / clear["dap_calc"]) ** 2
    bins = (clear.index.to_numpy() // 60).astype(int)
    per_min = implied.groupby(bins).mean()
    rows.append(dict(sid=sid, camp=j["camp"].iloc[0], pas=clear["lpas"].median(), wire=j["wire"].median(), tamb=j["tamb"].median(), v_med=implied.median(), v_p10=per_min.quantile(0.1), v_p90=per_min.quantile(0.9), pow_std1=clear["power"].std(), pow_std60=clear["power"].groupby(bins).mean().std(), power=clear["power"].median(), dap=clear["dap_calc"].median()))
t = pd.DataFrame(rows)
print(len(t))
print(t[["v_med", "v_p10", "v_p90", "pow_std1", "pow_std60", "power", "dap"]].describe().T[["min", "25%", "50%", "75%", "max"]].to_string())
print(t.groupby(["camp", "wire"])[["v_med", "v_p10", "v_p90", "pow_std1", "pow_std60"]].median().to_string())
print("apparent LWC from clear-air power variation: 1 s", t.pow_std1.median() / s, " 60 s", t.pow_std60.median() / s)

print("\n=== 4. Does CDP transit time track total power in cloud? (1/transit ~ true speed)")
rows = []
pool = []
for sid, j in store.items():
    cloud = j[(j["n"] > 50) & (j["clwc"] > 0.02) & (j["transit"] > 0)]
    if len(cloud) < 300:
        continue
    bins = (cloud.index.to_numpy() // 60).astype(int)
    m = cloud[["power", "transit", "clwc", "mvd", "n", "wing", "dap_calc", "lpas", "cpas"]].groupby(bins).mean()
    m = m[cloud["power"].groupby(bins).count() >= 30]
    if len(m) < 10:
        continue
    m["speed"] = 1 / m["transit"]
    m["sqrt_speed"] = np.sqrt(m["speed"])
    m["sid"] = sid
    m["camp"] = j["camp"].iloc[0]
    pool.append(m)
    X = np.column_stack([np.ones(len(m)), m["sqrt_speed"], m["clwc"], m["wing"]])
    if np.isnan(X).any() or m["power"].isna().any():
        ok = ~(np.isnan(X).any(axis=1) | m["power"].isna())
        X, y = X[ok], m["power"][ok]
    else:
        y = m["power"]
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    pred = X @ beta
    r2 = 1 - ((y - pred) ** 2).sum() / ((y - y.mean()) ** 2).sum()
    Xb = X[:, [0, 2]]
    b2, *_ = np.linalg.lstsq(Xb, y, rcond=None)
    r2b = 1 - ((y - Xb @ b2) ** 2).sum() / ((y - y.mean()) ** 2).sum()
    rows.append(dict(sid=sid, camp=j["camp"].iloc[0], n=len(m), r_transit_power=np.corrcoef(1 / np.sqrt(m["transit"]), m["power"])[0, 1], r_transit_mvd=np.corrcoef(m["transit"], m["mvd"])[0, 1], r2_full=r2, r2_lwc_only=r2b, beta_lwc_full=beta[2], beta_lwc_only=b2[1], transit=m["transit"].median(), transit_cv=m["transit"].std() / m["transit"].mean(), power_std=m["power"].std()))
r = pd.DataFrame(rows)
print(len(r))
print(r[["r_transit_power", "r_transit_mvd", "r2_lwc_only", "r2_full", "beta_lwc_only", "beta_lwc_full", "transit", "transit_cv", "power_std"]].describe().T[["min", "25%", "50%", "75%", "max"]].to_string())
print("expected W per g/m3 if wire sees all CDP water:", round(s, 3))
print(r.groupby("camp")[["r_transit_power", "r2_lwc_only", "r2_full", "beta_lwc_full", "transit"]].median().to_string())
pickle.dump(pd.concat(pool), open(os.path.join(D, "inv_cloud60.pkl"), "wb"))

print("\n=== 5. Step response at cloud edges (immune to slow drift)")
ev = []
for sid, j in store.items():
    # Reindex to continuous seconds so edges are real time
    x = j[["n", "clwc", "power", "hot", "mvd"]].copy()
    x = x[~x.index.duplicated()]
    idx = np.arange(int(x.index.min()), int(x.index.max()) + 1)
    x = x.reindex(idx)
    n = x["n"].rolling(30, center=True, min_periods=20).median()
    state = np.where(n > 50, 1, np.where(n < 1, -1, 0))
    i = 0
    W = 120
    while i < len(state) - 2 * W:
        a = x.iloc[i: i + W]
        b = x.iloc[i + W + 60: i + 2 * W + 60]
        if len(b) == W and (a["n"] < 1).mean() > 0.9 and (b["n"] > 50).mean() > 0.9:
            kind = "enter"
        elif len(b) == W and (a["n"] > 50).mean() > 0.9 and (b["n"] < 1).mean() > 0.9:
            kind = "leave"
        else:
            i += 15
            continue
        cl = b if kind == "enter" else a
        cr = a if kind == "enter" else b
        ev.append(dict(sid=sid, camp=j["camp"].iloc[0], kind=kind, dcdp=cl["clwc"].mean() - cr["clwc"].mean(), dpower=cl["power"].mean() - cr["power"].mean(), dhot=cl["hot"].mean() - cr["hot"].mean(), mvd=cl["mvd"].mean(), n=cl["n"].mean(), pas=7))
        i += 2 * W + 60
ev = pd.DataFrame(ev)
print("events", len(ev), ev.kind.value_counts().to_dict())
if len(ev):
    print(ev[["dcdp", "dpower", "dhot", "mvd", "n"]].describe().T[["25%", "50%", "75%"]].to_string())
    ev["ratio_power"] = (ev["dpower"] / s) / ev["dcdp"]
    print("median ratio hot-wire/CDP step (power based):", ev["ratio_power"].median(), " dhot/dcdp:", (ev["dhot"] / ev["dcdp"]).median())
    print("r(dcdp, dhot)", ev["dcdp"].corr(ev["dhot"]), " Spearman", ev["dcdp"].rank().corr(ev["dhot"].rank()))
    fit = np.polyfit(ev["dcdp"], ev["dhot"], 1)
    print("OLS dhot vs dcdp slope, intercept", fit)
    print("frac dhot>0", (ev["dhot"] > 0).mean())
    ev["mvdbin"] = pd.cut(ev["mvd"], [0, 8, 12, 16, 20, 60])
    print(ev.groupby("mvdbin", observed=True).agg(n=("dhot", "size"), dcdp=("dcdp", "median"), dhot=("dhot", "median"), ratio=("ratio_power", "median")).to_string())
    ev.to_pickle(os.path.join(D, "inv_events.pkl"))
