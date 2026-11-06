import os
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

D = os.path.dirname(os.path.abspath(__file__))
out = sys.argv[1]
t = pd.read_csv(os.path.join(D, "cal_beads_by_session.csv"), dtype={"date": str, "sid": str})
t = t[t["mode"] == "large"]
plt.rcParams.update({"font.family": ["Microsoft JhengHei", "sans-serif"], "axes.unicode_minus": False, "font.size": 11})
BLUE, MUTED, INK, GRID, SURFACE = "#2a78d6", "#52514e", "#0b0b0b", "#e4e3de", "#fcfcfb"
dates = sorted(t["date"].unique())
labels = {
    "20240905": "2024-09-05",
    "20250312": "2025-03-12",
    "20250321": "2025-03-21\n(高計數率)",
    "20250813": "2025-08-13",
    "20261018": "2026-10-18\n(標示 32 µm)",
}
fig, ax = plt.subplots(figsize=(9, 4.4), facecolor=SURFACE)
ax.set_facecolor(SURFACE)
ax.plot([3.55, 4.45], [26.6, 26.6], color=MUTED, lw=1.2, ls=(0, (4, 3)))
ax.text(4.45, 22.9, "虛線:32 µm 換算值 26.6 µm\n僅此日標稱粒徑已知", ha="right", va="top", color=MUTED, fontsize=10)
for i, date in enumerate(dates):
    g = t[t["date"] == date].reset_index(drop=True)
    offsets = [(k - (len(g) - 1) / 2) * 0.09 for k in range(len(g))]
    hollow = date == "20250321"
    for off, row in zip(offsets, g.itertuples()):
        ax.errorbar(i + off, row.mean, yerr=row.sd, color=BLUE, lw=1.2, capsize=0, alpha=0.45, zorder=2)
        ax.scatter(i + off, row.mean, s=70, facecolor=SURFACE if hollow else BLUE, edgecolor=BLUE, linewidth=1.6, zorder=3)
    mean = (g["mean"] * g["in_mode"]).sum() / g["in_mode"].sum()
    ax.text(i, g["mean"].max() + g["sd"].max() + 0.35, f"{mean:.1f}", ha="center", color=INK, fontsize=11)
ax.set_xticks(range(len(dates)), [labels[d] for d in dates], color=MUTED)
ax.set_xlim(-0.5, 4.5)
ax.set_ylim(20, 37)
ax.set_ylabel("主峰平均粒徑(µm)", color=MUTED)
ax.set_title("CDP 玻璃珠校正的主峰(20–36 µm),實心點為一次量測,細線為 ±1 標準差", loc="left", color=INK, fontsize=12)
ax.grid(axis="y", color=GRID, lw=0.8)
ax.set_axisbelow(True)
for side in ("top", "right", "left"):
    ax.spines[side].set_visible(False)
ax.spines["bottom"].set_color(GRID)
ax.tick_params(length=0, colors=MUTED)
fig.tight_layout()
fig.savefig(out, dpi=130)
