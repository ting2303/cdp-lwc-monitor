"""把 CDP / LWC-300 / 11-D 整理成每分鐘的快取,供視覺化與統計使用。

python -m cpd_lwc_qc.minute --data "資料夾" --cache cache
已處理且大小沒變的檔案不會重算,所以新增資料後再跑一次就好。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from . import d11
from .pads import discover_sessions, parse_sizes, read_channels, read_instrument
from .sources import DataFile, DataSet

CDP_VALUES = ["number_cm3", "lwc_gm3", "mvd_um", "ed_um", "pas_ms", "dump_spot_v", "laser_ma"]
D11_VALUES = ["PM1", "PM2.5", "PM4", "PM10", "TSP"]
D11_LOG = ["Pres", "rH_i", "Temp_i"]
_D11_CHANNEL = re.compile(r"^([\d.]+)\s*um$")
_D11_LOG_SKIP = {"Year", "Mon", "Day", "Hr", "Min", "Sec"}
CACHE_VERSION = "2"  # 內容格式改變時加一,舊快取會重建


def _key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _stamp(items: List[DataFile]) -> str:
    return _key(CACHE_VERSION + "|" + "|".join(sorted(f"{i.rel}:{i.size}" for i in items)))


def _write(path: Path, times: pd.DatetimeIndex, values: pd.DataFrame, bins: Optional[np.ndarray]) -> None:
    arrays = {
        "time": times.values.astype("datetime64[s]").astype("int64"),
        "values": values.to_numpy(dtype="float32"),
        "columns": np.array(list(values.columns)),
    }
    if bins is not None:
        arrays["bins"] = bins.astype("float32")
    temporary = path.with_suffix(".tmp.npz")
    np.savez_compressed(temporary, **arrays)
    temporary.replace(path)


def build_cdp(dataset: DataSet, session, path: Path) -> Optional[dict]:
    cdp = read_instrument(dataset, session.instruments["cdp"], "cdp", bins=True)
    if cdp.data.empty:
        return None
    sizes = parse_sizes(cdp.header)
    data = cdp.data
    columns = [f"bin_{i}" for i in range(1, len(sizes) + 1)]
    minute = data["time_local"].dt.floor("min")
    grouped = data.groupby(minute)
    values = grouped[[c for c in CDP_VALUES if c in data and c not in ("mvd_um", "ed_um")]].mean()
    droplets = data[data["number_cm3"] > 0]
    for column in ("mvd_um", "ed_um"):
        if column in data:
            # 沒有粒子的秒數是 0,不應拉低平均粒徑。
            values[column] = droplets.groupby(droplets["time_local"].dt.floor("min"))[column].mean()
    values["seconds"] = grouped.size()
    if "lwc" in session.instruments:
        hot = read_instrument(dataset, session.instruments["lwc"], "lwc")
        if not hot.data.empty:
            frame = hot.data
            values["hotwire_gm3"] = frame.groupby(frame["time_local"].dt.floor("min"))["lwc_gm3"].mean()
    if "hotwire_gm3" not in values:
        values["hotwire_gm3"] = np.nan
    bins = grouped[columns].sum().reindex(values.index).to_numpy()
    _write(path, values.index, values, bins)
    return {
        "sizes": sizes,
        "sample_area": cdp.sample_area,
        "rows": len(values),
        "start": str(values.index[0]),
        "end": str(values.index[-1]),
    }


def read_c_factor(dataset: DataSet, item: DataFile) -> Optional[float]:
    with dataset.open(item) as raw:
        head = raw.read(4096).decode("cp950", errors="replace")
    match = re.search(r"C-factor:\s*([\d.]+)", head)
    return float(match.group(1)) if match else None


def build_d11(dataset: DataSet, members: Dict[str, DataFile], path: Path) -> Optional[dict]:
    if "C" not in members or members["C"].size < 100:
        return None
    counts = d11.read_table(dataset, members["C"])
    if counts.empty:
        return None
    channels = [c for c in counts.columns if _D11_CHANNEL.match(str(c).strip())]
    numeric = counts[channels].apply(pd.to_numeric, errors="coerce")
    minute = counts["time_local"].dt.floor("min")
    spectrum = numeric.groupby(minute).mean()
    values = pd.DataFrame(index=spectrum.index)
    values["seconds"] = minute.groupby(minute).size() * 6.0
    if "M" in members and members["M"].size >= 100:
        mass = d11.read_table(dataset, members["M"])
        rename = {c: str(c).split(" [")[0].strip() for c in mass.columns}
        mass = mass.rename(columns=rename)
        block = mass.drop(columns="time_local").apply(pd.to_numeric, errors="coerce")
        means = block.groupby(mass["time_local"].dt.floor("min")).mean()
        values = values.join(means)
    if "L" in members and members["L"].size >= 100:
        log = d11.read_table(dataset, members["L"])
        names = [c for c in log.columns if c != "time_local" and str(c) not in _D11_LOG_SKIP]
        renamed = {c: str(c).split(" [")[0].strip() for c in names}
        block = log[names].rename(columns=renamed).apply(lambda s: pd.to_numeric(s.astype(str).str.rstrip("A"), errors="coerce"))
        block = block.dropna(axis=1, how="all")
        block = block[[c for c in block.columns if c not in values.columns]]
        values = values.join(block.groupby(log["time_local"].dt.floor("min")).mean())
    for column in D11_VALUES + D11_LOG:
        if column not in values:
            values[column] = np.nan
    _write(path, values.index, values, spectrum.to_numpy())
    return {
        "c_factor": read_c_factor(dataset, members["C"]),
        "channels": [float(_D11_CHANNEL.match(str(c).strip()).group(1)) for c in channels],
        "rows": len(values),
        "start": str(values.index[0]),
        "end": str(values.index[-1]),
    }


def build_channels(dataset: DataSet, item: DataFile, path: Path) -> Optional[dict]:
    """每分鐘平均的所有參數,給歷史瀏覽用。"""
    table = read_channels(dataset, item)
    if table.empty:
        return None
    minute = table["time_local"].dt.floor("min")
    values = table.drop(columns=["time_local", "end_seconds", "day_of_year", "year"], errors="ignore").groupby(minute).mean()
    values = values.dropna(axis=1, how="all")
    _write(path, values.index, values, None)
    return {"rows": len(values), "start": str(values.index[0]), "end": str(values.index[-1]), "columns": list(values.columns)}


def build(data_root: str, cache: Path, match: Optional[str] = None) -> dict:
    cache.mkdir(parents=True, exist_ok=True)
    index_path = cache / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {"cdp": {}, "d11": {}}
    index.setdefault("channels", {})
    pattern = re.compile(match) if match else None
    started = time.time()
    errors: List[str] = []
    with DataSet(data_root, exclude=[str(cache)]) as dataset:
        all_sessions = discover_sessions(dataset)
        if pattern:
            all_sessions = [s for s in all_sessions if pattern.search(s.rel_dir)]
        sessions = [s for s in all_sessions if "cdp" in s.instruments]
        for number, session in enumerate(sessions, 1):
            items = [session.instruments["cdp"]] + ([session.instruments["lwc"]] if "lwc" in session.instruments else [])
            stamp = _stamp(items)
            key = _key(session.rel_dir)
            old = index["cdp"].get(key)
            path = cache / f"cdp_{key}.npz"
            if old and old.get("stamp") == stamp and path.exists():
                continue
            try:
                meta = build_cdp(dataset, session, path)
            except Exception as error:  # 單一壞檔不應中斷全部
                errors.append(f"{session.rel_dir}: {error}")
                continue
            if meta:
                meta.update(stamp=stamp, sid=session.sid, campaign=session.campaign, subcampaign=session.subcampaign, path=session.rel_dir, hotwire="lwc" in session.instruments)
                index["cdp"][key] = meta
            if number % 20 == 0:
                print(f"  CDP {number}/{len(sessions)},{time.time() - started:.0f} 秒", flush=True)
                index_path.write_text(json.dumps(index, ensure_ascii=False, indent=0), encoding="utf-8")
        for number, session in enumerate(all_sessions, 1):
            for kind, item in session.instruments.items():
                stamp = _stamp([item])
                key = _key(item.rel)
                old = index["channels"].get(key)
                path = cache / f"chan_{key}.npz"
                if old and old.get("stamp") == stamp and path.exists():
                    continue
                try:
                    meta = build_channels(dataset, item, path)
                except Exception as error:
                    errors.append(f"{item.rel}: {error}")
                    continue
                if meta:
                    meta.update(stamp=stamp, kind=kind, sid=session.sid, campaign=session.campaign, subcampaign=session.subcampaign, path=session.rel_dir)
                    index["channels"][key] = meta
            if number % 20 == 0:
                print(f"  參數 {number}/{len(all_sessions)},{time.time() - started:.0f} 秒", flush=True)
                index_path.write_text(json.dumps(index, ensure_ascii=False, indent=0), encoding="utf-8")
        groups = d11.discover(dataset)
        for stem, members in sorted(groups.items()):
            if pattern and not pattern.search(stem):
                continue
            stamp = _stamp(list(members.values()))
            key = _key(stem)
            old = index["d11"].get(key)
            path = cache / f"d11_{key}.npz"
            if old and old.get("stamp") == stamp and path.exists():
                if "c_factor" not in old and "C" in members:  # 舊快取補上設定值,不必重算
                    old["c_factor"] = read_c_factor(dataset, members["C"])
                continue
            try:
                meta = build_d11(dataset, members, path)
            except Exception as error:
                errors.append(f"{stem}: {error}")
                continue
            if meta:
                parts = stem.split("/")
                meta.update(stamp=stamp, file=stem, campaign=parts[2] if len(parts) > 3 else "(11D 根目錄)")
                index["d11"][key] = meta
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"完成:CDP {len(index['cdp'])} 次、參數檔 {len(index['channels'])} 個、11-D {len(index['d11'])} 組,{time.time() - started:.0f} 秒")
    for message in errors:
        print("讀取失敗:", message)
    return index


def main() -> int:
    parser = argparse.ArgumentParser(description="建立每分鐘快取")
    parser.add_argument("--data", required=True)
    parser.add_argument("--cache", default="cache")
    parser.add_argument("--match", default=None)
    args = parser.parse_args()
    build(args.data, Path(args.cache).resolve(), args.match)
    return 0


if __name__ == "__main__":
    sys.exit(main())
