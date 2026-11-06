"""載入每分鐘快取,並換算成可直接比較的粒徑分布。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np


@dataclass
class Series:
    """依時間排序、已合併的表。time 是 Unix 秒(當地時間視為 UTC 數字,不做時區換算)。"""

    time: np.ndarray
    values: Dict[str, np.ndarray]
    spectrum: np.ndarray  # 每列是 dN/dlogD(cm⁻³)
    edges: np.ndarray  # 粒徑區間邊界(µm),長度為 欄數 + 1
    labels: np.ndarray  # 每列所屬的量測或檔案編號
    meta: List[dict]


def cdp_spectrum(counts: np.ndarray, seconds: np.ndarray, area_mm2: float, pas: np.ndarray, edges: np.ndarray) -> np.ndarray:
    """由計數換算 dN/dlogD。取樣體積(cm³/s)= 面積(mm²)× 流速(m/s)。"""
    volume = area_mm2 * np.where(pas > 0, pas, np.nan) * seconds
    return counts / volume[:, None] / np.log10(edges[1:] / edges[:-1])


def d11_spectrum(per_liter: np.ndarray, edges: np.ndarray) -> np.ndarray:
    """11-D 的 C 檔是每通道 counts/L,轉為 cm⁻³ 再除以通道寬度。"""
    return per_liter / 1000.0 / np.log10(edges[1:] / edges[:-1])


def d11_edges(channels: List[float]) -> np.ndarray:
    """欄名是各通道下限;最後一個通道的上限沿用前一通道的比例。"""
    low = np.array(channels, float)
    return np.r_[low, low[-1] * low[-1] / low[-2]]


def _combine(parts: List[Tuple[np.ndarray, Dict[str, np.ndarray], np.ndarray, np.ndarray]], edges: np.ndarray, meta: List[dict]) -> Optional[Series]:
    if not parts:
        return None
    time = np.concatenate([p[0] for p in parts])
    order = np.argsort(time, kind="stable")
    names: List[str] = []
    for part in parts:
        names += [n for n in part[1] if n not in names]
    # 各檔案的欄位可能不同,缺的部分補 NaN
    values = {n: np.concatenate([p[1].get(n, np.full(len(p[0]), np.nan)) for p in parts])[order] for n in names}
    return Series(
        time=time[order],
        values=values,
        spectrum=np.concatenate([p[2] for p in parts])[order],
        edges=edges,
        labels=np.concatenate([p[3] for p in parts])[order],
        meta=meta,
    )


@dataclass
class Channels:
    """某一型儀器所有參數的每分鐘值。"""

    time: np.ndarray
    values: Dict[str, np.ndarray]


def load_channels(cache: Path) -> Dict[str, Channels]:
    index = json.loads((cache / "index.json").read_text(encoding="utf-8"))
    parts: Dict[str, List[Tuple[np.ndarray, Dict[str, np.ndarray]]]] = {}
    for key, meta in sorted(index.get("channels", {}).items(), key=lambda kv: kv[1]["sid"]):
        path = cache / f"chan_{key}.npz"
        if not path.exists():
            continue
        with np.load(path) as data:
            names = [str(c) for c in data["columns"]]
            table = data["values"]
            parts.setdefault(meta["kind"], []).append((data["time"], {n: table[:, i] for i, n in enumerate(names)}))
    out: Dict[str, Channels] = {}
    for kind, items in parts.items():
        time = np.concatenate([p[0] for p in items])
        order = np.argsort(time, kind="stable")
        names: List[str] = []
        for item in items:
            names += [n for n in item[1] if n not in names]
        values = {n: np.concatenate([i[1].get(n, np.full(len(i[0]), np.nan, "float32")) for i in items])[order] for n in names}
        out[kind] = Channels(time[order], values)
    return out


def load(cache: Path) -> Tuple[Optional[Series], Optional[Series]]:
    index = json.loads((cache / "index.json").read_text(encoding="utf-8"))
    cdp_parts, cdp_meta = [], []
    cdp_edges = None
    for key, meta in sorted(index["cdp"].items(), key=lambda kv: kv[1]["sid"]):
        path = cache / f"cdp_{key}.npz"
        if not path.exists() or not meta.get("sizes") or not meta.get("sample_area"):
            continue
        sizes = np.array(meta["sizes"], float)
        edges = np.r_[2.0, sizes]
        if cdp_edges is None:
            cdp_edges = edges
        elif len(edges) != len(cdp_edges) or not np.allclose(edges, cdp_edges):
            continue
        with np.load(path) as data:
            names = [str(c) for c in data["columns"]]
            table = data["values"].astype("float64")
            values = {n: table[:, i] for i, n in enumerate(names)}
            spectrum = cdp_spectrum(data["bins"].astype("float64"), values["seconds"], meta["sample_area"], values["pas_ms"], edges)
            cdp_parts.append((data["time"], values, spectrum, np.full(len(table), len(cdp_meta))))
        cdp_meta.append(meta)
    d11_parts, d11_meta = [], []
    edges_d = None
    for key, meta in sorted(index["d11"].items(), key=lambda kv: kv[1]["start"]):
        path = cache / f"d11_{key}.npz"
        if not path.exists():
            continue
        edges = d11_edges(meta["channels"])
        if edges_d is None:
            edges_d = edges
        elif len(edges) != len(edges_d):
            continue
        with np.load(path) as data:
            names = [str(c) for c in data["columns"]]
            table = data["values"].astype("float64")
            values = {n: table[:, i] for i, n in enumerate(names)}
            spectrum = d11_spectrum(data["bins"].astype("float64"), edges)
            d11_parts.append((data["time"], values, spectrum, np.full(len(table), len(d11_meta))))
        d11_meta.append(meta)
    return (
        _combine(cdp_parts, cdp_edges, cdp_meta) if cdp_parts else None,
        _combine(d11_parts, edges_d, d11_meta) if d11_parts else None,
    )
