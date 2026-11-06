"""畫出過去資料的典型粒徑分布。python make_figure.py <cache 資料夾> <輸出 png>"""
import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

cache, out = Path(sys.argv[1]), sys.argv[2]
n = json.loads((cache / "normal_reference.json").read_text(encoding="utf-8"))
plt.rcParams.update({"font.family": ["Microsoft JhengHei", "sans-serif"], "axes.unicode_minus": False, "font.size": 11})
BLUE, ORANGE, MUTED, INK, GRID, SURFACE = "#2a78d6", "#eb6834", "#52514e", "#0b0b0b", "#e4e3de", "#fcfcfb"


def steps(edges, values):
    return np.repeat(edges, 2)[1:-1], np.repeat(values, 2)


fig, ax = plt.subplots(figsize=(9.5, 5.2), facecolor=SURFACE)
ax.set_facecolor(SURFACE)
for edges, band, color, label in (
    (n["d11_edges"], n["d11_spectrum_all"], ORANGE, f"11-D 氣膠(全部時間,{n['d11_spectrum_all']['sessions']} 組)"),
    (n["cdp_edges"], n["cdp_cloud_spectrum"], BLUE, f"CDP 雲滴(雲中,{n['cdp_cloud_spectrum']['sessions']} 次量測)"),
):
    edges = np.array(edges)
    x, low = steps(edges, np.maximum(band["p10"], 1e-9))
    _, high = steps(edges, np.maximum(band["p90"], 1e-9))
    _, mid = steps(edges, np.maximum(band["p50"], 1e-9))
    ax.fill_between(x, low, high, color=color, alpha=0.18, lw=0)
    ax.plot(x, mid, color=color, lw=1.8, label=label)
ax.set_xscale("log")
ax.set_yscale("log")
ax.set_xlim(0.2, 55)
ax.set_ylim(1e-3, 3e4)
ax.set_xticks([0.3, 1, 3, 10, 30], ["0.3", "1", "3", "10", "30"])
SUPERSCRIPT = str.maketrans("-0123456789", "⁻⁰¹²³⁴⁵⁶⁷⁸⁹")
ax.set_yticks([10.0**e for e in range(-3, 5)], ["1" if e == 0 else "10" + str(e).translate(SUPERSCRIPT) for e in range(-3, 5)])
ax.minorticks_off()
ax.set_xlabel("光學粒徑(µm)", color=MUTED)
ax.set_ylabel("dN/dlogD(cm⁻³)", color=MUTED)
ax.set_title("過去野外資料的典型粒徑分布:粗線為中位數,色帶為 10–90% 範圍", loc="left", color=INK, fontsize=12)
ax.grid(color=GRID, lw=0.8)
ax.set_axisbelow(True)
for side in ("top", "right", "left", "bottom"):
    ax.spines[side].set_visible(False)
ax.tick_params(length=0, colors=MUTED, which="both")
ax.legend(frameon=False, loc="upper right")
fig.tight_layout()
fig.savefig(out, dpi=130)
