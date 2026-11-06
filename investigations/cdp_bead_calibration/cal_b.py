import os
import pickle
import re
import sys
import zipfile

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
pd.set_option("display.width", 240, "display.max_rows", 200, "display.max_columns", 30, "display.max_colwidth", 50, "display.float_format", lambda x: f"{x:.3g}")
D = os.path.dirname(os.path.abspath(__file__))
hists = pickle.load(open(os.path.join(D, "cal_hists.pkl"), "rb"))
idx = pickle.load(open(os.path.join(D, "idx.pkl"), "rb"))

# Export the labelled screenshots so they can be read.
target = os.path.join(D, "cal_png")
os.makedirs(target, exist_ok=True)
for p in idx:
    if p.endswith(".png") and "玻璃球" in p:
        name = p.split("/")[1] + "_" + os.path.basename(p)
        with zipfile.ZipFile(idx[p][0]) as z, open(os.path.join(target, name), "wb") as out:
            out.write(z.read(p))
print(sorted(os.listdir(target)))


def describe(counts, sizes):
    """Mode from dN/dD, then mean and spread of the population around it."""
    sizes = np.array(sizes, float)
    lower = np.r_[max(0.0, sizes[0] - 1), sizes[:-1]]
    width = sizes - lower
    mid = (lower + sizes) / 2
    density = counts / width
    # ignore the lowest channels when looking for the bead mode: they hold a noise tail
    mask = mid >= 8
    if counts[mask].sum() == 0:
        return None
    peak = int(np.argmax(np.where(mask, density, -1)))
    keep = np.abs(mid - mid[peak]) <= np.maximum(0.25 * mid[peak], 3)
    if counts[keep].sum() == 0:
        return None
    mean = (mid[keep] * counts[keep]).sum() / counts[keep].sum()
    sd = np.sqrt((counts[keep] * (mid[keep] - mean) ** 2).sum() / counts[keep].sum())
    return dict(peak_lo=lower[peak], peak_hi=sizes[peak], mean=mean, sd=sd, share=counts[keep].sum() / counts.sum(), below8=counts[mid < 8].sum() / counts.sum())


rows = []
for (sid, kind, folder), h in sorted(hists.items()):
    per = h["per_sec"]
    if per.sum() < 50:
        continue
    d = describe(h["sum"], h["sizes"])
    if d is None:
        continue
    rows.append(dict(sid=sid, kind=kind, camp=folder.split("/")[-3] if "OBS" in folder else folder.split("/")[1], secs=len(per), active=int((per > 0).sum()), total=int(per.sum()), top_sec_share=per.max() / per.sum(), **d))
t = pd.DataFrame(rows)
t.to_pickle(os.path.join(D, "cal_table.pkl"))
print(t.to_string())
