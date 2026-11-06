"""由過去的野外資料統計「典型範圍」。這是過去資料的樣子,不代表一定正確。

python -m cpd_lwc_qc.normal --cache cache --out cache/normal_reference.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List

import numpy as np

from .store import Channels, Series, load, load_channels

QUANTILES = [0.05, 0.25, 0.5, 0.75, 0.95]
CLOUD_NUMBER = 50.0
CLOUD_LWC = 0.02
CLEAR_NUMBER = 1.0
SITES = {
    "鹿林": r"KAPAS|Lulin",
    "東眼山": r"(?i)don(y|g)an",
    "seman": r"(?i)seman",
}
LAB = re.compile(r"(?i)\blab\b|lab_|fog test|PADS Data")


def site_of(campaign: str) -> str:
    if LAB.search(campaign):
        return "實驗室"
    for name, pattern in SITES.items():
        if re.search(pattern, campaign):
            return name
    return "其他"


def quantiles(values: np.ndarray) -> Dict[str, float]:
    values = values[np.isfinite(values)]
    if not len(values):
        return {}
    return {f"p{int(q * 100)}": float(np.quantile(values, q)) for q in QUANTILES} | {"n": int(len(values))}


def session_spectra(series: Series, mask: np.ndarray, min_minutes: int = 30) -> np.ndarray:
    """每次量測先平均,避免長時間的量測主導結果。"""
    out = []
    for label in np.unique(series.labels[mask]):
        chosen = mask & (series.labels == label)
        if chosen.sum() >= min_minutes:
            out.append(np.nanmean(series.spectrum[chosen], axis=0))
    return np.array(out) if out else np.empty((0, series.spectrum.shape[1]))


def band(spectra: np.ndarray) -> Dict[str, List[float]]:
    if not len(spectra):
        return {}
    return {name: np.nanquantile(spectra, q, axis=0).tolist() for name, q in (("p10", 0.1), ("p50", 0.5), ("p90", 0.9))} | {"sessions": int(len(spectra))}


def reference_entry(values: np.ndarray, edges: List[float], log: bool) -> dict:
    values = values[np.isfinite(values)]
    counts, _ = np.histogram(values, bins=edges)
    return {
        "edges": [float(e) for e in edges],
        "log": log,
        "hist": (counts / max(len(values), 1)).round(4).tolist(),
        "percentiles": np.quantile(values, np.arange(1, 100) / 100).round(4).tolist(),
        "n": int(len(values)),
    }


def laser_model(channel: Channels, field: np.ndarray) -> dict:
    """雷射電流隨溫度和時間的變化,用來判斷某天是否偏離常態。"""
    current, temperature = channel.values["Laser Current (mA)"], channel.values["Laser Temp (C)"]
    good = field & (current > 40) & np.isfinite(temperature)
    t0 = float(channel.time.min())
    years = (channel.time[good] - t0) / (365.25 * 86400)
    matrix = np.column_stack([np.ones(good.sum()), temperature[good], years])
    beta, *_ = np.linalg.lstsq(matrix, current[good], rcond=None)
    residual = current[good] - matrix @ beta
    return {"a": float(beta[0]), "b": float(beta[1]), "c": float(beta[2]), "t0": t0, "sd": float(residual.std())}


def summarise(cdp: Series, d11: Series) -> dict:
    campaign = np.array([site_of(m["campaign"]) for m in cdp.meta])[cdp.labels]
    field = campaign != "實驗室"
    number, lwc = cdp.values["number_cm3"], cdp.values["lwc_gm3"]
    cloud = (number > CLOUD_NUMBER) & (lwc > CLOUD_LWC)
    clear = number < CLEAR_NUMBER
    out: dict = {"cdp_edges": cdp.edges.tolist(), "d11_edges": d11.edges.tolist(), "definition": {"cloud": f"每分鐘 CDP 濃度 > {CLOUD_NUMBER:g} /cm³ 且 LWC > {CLOUD_LWC:g} g/m³", "clear": f"CDP 濃度 < {CLEAR_NUMBER:g} /cm³"}}

    out["minutes"] = {name: {"all": int((campaign == name).sum()), "cloud": int(((campaign == name) & cloud).sum())} for name in np.unique(campaign)}
    out["cloud_metrics"] = {}
    for name in list(SITES) + ["全部野外"]:
        chosen = (campaign == name if name != "全部野外" else field) & cloud
        out["cloud_metrics"][name] = {
            "number_cm3": quantiles(number[chosen]),
            "lwc_gm3": quantiles(lwc[chosen]),
            "mvd_um": quantiles(cdp.values["mvd_um"][chosen]),
            "ed_um": quantiles(cdp.values["ed_um"][chosen]),
        }
    out["cdp_cloud_spectrum"] = band(session_spectra(cdp, field & cloud))
    out["cdp_cloud_spectrum_by_site"] = {n: band(session_spectra(cdp, (campaign == n) & cloud)) for n in SITES}
    lab_spec = session_spectra(cdp, (campaign == "實驗室") & (number > CLOUD_NUMBER), min_minutes=3)
    out["cdp_lab_spectrum"] = band(lab_spec)
    # 雲外仍看得到幾顆大粒子的背景
    out["cdp_clear_spectrum"] = band(session_spectra(cdp, field & clear))
    out["clear_metrics"] = {
        "hotwire_gm3": quantiles(cdp.values["hotwire_gm3"][field & clear]),
        "number_cm3": quantiles(number[field & clear]),
    }
    out["housekeeping"] = {k: quantiles(cdp.values[k][field & (cdp.values["laser_ma"] > 40)]) for k in ("laser_ma", "dump_spot_v")}

    # 11-D 與同時間 CDP 配對,分成有雲和沒雲
    labels = np.array([m["campaign"] for m in d11.meta])[d11.labels]
    d11_site = np.array([site_of(c) for c in labels])
    d11_field = d11_site != "實驗室"
    order = np.argsort(cdp.time)
    position = np.searchsorted(cdp.time[order], d11.time)
    position = np.clip(position, 0, len(order) - 1)
    matched = cdp.time[order][position] == d11.time
    nearest_number = np.where(matched, number[order][position], np.nan)
    d11_cloud = d11_field & (nearest_number > CLOUD_NUMBER)
    d11_clear = d11_field & (nearest_number < CLEAR_NUMBER)
    out["d11_metrics"] = {}
    for name, chosen in (("全部野外", d11_field), ("CDP 同時有雲", d11_cloud), ("CDP 同時無雲", d11_clear)):
        out["d11_metrics"][name] = {k: quantiles(d11.values[k][chosen]) for k in ("PM1", "PM2.5", "PM10", "TSP")}
    out["d11_metrics_by_site"] = {n: {k: quantiles(d11.values[k][d11_site == n]) for k in ("PM1", "PM2.5", "PM10")} for n in SITES}
    out["d11_spectrum_clear"] = band(session_spectra(d11, d11_clear))
    out["d11_spectrum_cloud"] = band(session_spectra(d11, d11_cloud))
    out["d11_spectrum_all"] = band(session_spectra(d11, d11_field))
    out["d11_pressure_by_site"] = {n: quantiles(d11.values["Pres"][d11_site == n]) for n in SITES}
    out["d11_overlap_minutes"] = int(matched.sum())
    out["reference"] = {
        "number": reference_entry(number[field & cloud], np.logspace(np.log10(10), np.log10(5000), 26).tolist(), True),
        "lwc": reference_entry(lwc[field & cloud], np.logspace(np.log10(0.01), np.log10(3), 26).tolist(), True),
        "mvd": reference_entry(cdp.values["mvd_um"][field & cloud], list(range(2, 42)), False),
        "pm25": reference_entry(d11.values["PM2.5"][d11_field], np.logspace(np.log10(0.1), np.log10(100), 26).tolist(), True),
    }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="統計過去資料的典型範圍")
    parser.add_argument("--cache", default="cache")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    cache = Path(args.cache).resolve()
    cdp, d11 = load(cache)
    if cdp is None or d11 is None:
        print("快取裡沒有足夠資料,請先執行 cpd_lwc_qc.minute", file=sys.stderr)
        return 2
    result = summarise(cdp, d11)
    channels = load_channels(cache)
    if "cdp" in channels:
        index = json.loads((cache / "index.json").read_text(encoding="utf-8"))
        lab_times = [
            (np.datetime64(m["start"].replace(" ", "T"), "s").astype("int64"), np.datetime64(m["end"].replace(" ", "T"), "s").astype("int64"))
            for m in index["channels"].values()
            if m["kind"] == "cdp" and site_of(m["campaign"]) == "實驗室"
        ]
        field = np.ones(len(channels["cdp"].time), bool)
        for first, last in lab_times:
            field[np.searchsorted(channels["cdp"].time, first): np.searchsorted(channels["cdp"].time, last, side="right")] = False
        result["laser_model"] = laser_model(channels["cdp"], field)
    target = Path(args.out) if args.out else cache / "normal_reference.json"
    target.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print("完成:", target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
