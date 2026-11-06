import os
import pickle
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

D = os.path.dirname(os.path.abspath(__file__))
out = sys.argv[1]
store = pickle.load(open(os.path.join(D, "inv_1hz.pkl"), "rb"))
plt.rcParams.update({"font.family": ["Microsoft JhengHei", "sans-serif"], "axes.unicode_minus": False, "font.size": 10})
CDP, HOT, INK, MUTED, GRID = "#2a78d6", "#eb6834", "#0b0b0b", "#52514e", "#e4e3de"

cases = [
    ("20250506100907", "相關最高,但 LWC-300 平穩不跟隨起伏(2025-05-06)"),
    ("20240311081046", "基線在雲中往下漂移數小時(2024-03-11)"),
    ("20250412100121", "CDP 明顯較高(2025-04-12)"),
]
fig, axes = plt.subplots(3, 1, figsize=(10, 8.4), facecolor="#fcfcfb")
for ax, (sid, title) in zip(axes, cases):
    j = store[sid]
    b = ((j.index.to_numpy() - j.index.min()) // 60).astype(int)
    m = j[["clwc", "hot", "n"]].groupby(b).mean()
    hours = m.index.to_numpy() / 60
    cloud = (m["n"] > 50).to_numpy()
    ax.fill_between(hours, 0, 1, where=cloud, transform=ax.get_xaxis_transform(), color="#eeeeea", step="mid", lw=0)
    ax.axhline(0, color=MUTED, lw=0.8)
    ax.plot(hours, m["clwc"], color=CDP, lw=1.3, label="CDP")
    ax.plot(hours, m["hot"], color=HOT, lw=1.3, label="LWC-300")
    ax.set_title(title, loc="left", fontsize=11, color=INK)
    ax.set_ylabel("LWC(g/m³)", color=MUTED)
    ax.set_xlim(hours.min(), hours.max())
    ax.grid(axis="y", color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    ax.set_facecolor("#fcfcfb")
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=MUTED, length=0)
axes[0].legend(loc="upper left", frameon=False, ncol=2)
axes[-1].set_xlabel("量測開始後的小時數(灰底 = CDP 濃度超過 50 /cm³;每點為 60 秒平均)", color=MUTED)
fig.tight_layout()
fig.savefig(out, dpi=130)
