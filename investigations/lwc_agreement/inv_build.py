"""Build a pooled table for the LWC agreement investigation. Reads zips only."""
import io
import os
import pickle
import re
import sys
import time
import zipfile

import numpy as np
import pandas as pd

D = os.path.dirname(os.path.abspath(__file__))
idx = pickle.load(open(os.path.join(D, "idx.pkl"), "rb"))
zc = {}


def op(p):
    z = idx[p][0]
    if z not in zc:
        zc[z] = zipfile.ZipFile(z)
    return zc[z].open(p)


def read(p):
    stream = io.TextIOWrapper(op(p), encoding="latin-1", newline="")
    while True:
        line = stream.readline()
        if not line:
            return None
        if line.startswith("****"):
            break
    df = pd.read_csv(stream, on_bad_lines="skip", low_memory=False, skipinitialspace=True)
    df.columns = [c.strip() for c in df.columns]
    return df


LW = {
    "End_Seconds": "t", "LWC-AveragedDAP": "hot", "LWC-CalculatedDAP": "hotcalc", "DAP-Averaged": "dap_avg",
    "DAP-Calculated": "dap_calc", "TotalPower(W)": "power", "EffectiveWireTemp(C)": "wire",
    "ElectronicsTemp(C)": "elec_t", "PAS(m/s)": "lpas", "Static Pressure (mB)": "p",
    "Ambient Temperature (C)": "tamb", "Master_Voltage(V)": "mv", "Master_Current(A)": "mc",
}
CW = {
    "End Seconds": "t", "Number Conc (#/cm^3)": "n", "LWC (g/m^3)": "clwc", "MVD (um)": "mvd", "ED (um)": "ed",
    "Applied PAS (m/s)": "cpas", "Wingboard Temp (C)": "wing", "Control Board T (C)": "ctrl", "Oversize Particles": "oversize", "Avg Transit Time": "transit", "DOF Reject Count": "dofrej", "PBP_Avg_IPT (msec)": "ipt",
}
pairs = {}
for p in idx:
    m = re.match(r"^(Instruments/CDP/OBS_data/.*/(\d{14})[^/]*/)(\d\d)(LWC 300_|CDP PBP)\d+\.csv$", p)
    if m:
        pairs.setdefault(m.group(1), {})[m.group(4)[:3]] = p

rows60 = []
store = {}
t0 = time.time()
for key, v in sorted(pairs.items(), key=lambda x: re.search(r"(\d{14})", x[0]).group(1)):
    if "LWC" not in v or "CDP" not in v:
        continue
    sid = re.search(r"(\d{14})", key).group(1)
    camp = key.split("/")[3]
    L = read(v["LWC"])
    C = read(v["CDP"])
    if L is None or C is None or len(L) < 600 or len(C) < 600:
        continue
    L = L[[c for c in LW if c in L]].rename(columns=LW).apply(pd.to_numeric, errors="coerce")
    C = C[[c for c in CW if c in C]].rename(columns=CW).apply(pd.to_numeric, errors="coerce")
    for frame in (L, C):
        frame["t"] = frame["t"].round()
    j = C.drop_duplicates("t").set_index("t").join(L.drop_duplicates("t").set_index("t"), how="inner").sort_index()
    if len(j) < 600:
        continue
    j.insert(0, "sid", sid)
    j.insert(1, "camp", camp)
    store[sid] = j.astype({c: "float32" for c in j.columns if c not in ("sid", "camp")})
    print(len(store), sid, round(time.time() - t0), flush=True)
pickle.dump(store, open(os.path.join(D, "inv_1hz.pkl"), "wb"))
print("DONE", len(store), sum(len(v) for v in store.values()) / 3600)
