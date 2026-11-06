"""11-D 氣膠儀匯出的 .dat 檔(-C / -M / -dM / -L)解析。"""
from __future__ import annotations

import io
import re
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .sources import DataFile, DataSet

KINDS = ("C", "M", "dM", "L")
_NAME = re.compile(r"^(.*)-(C|M|dM|L)\.dat$")
_STAMP = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})\s+(上午|下午|AM|PM)\s+(\d{1,2}):(\d{2}):(\d{2})$")


def parse_timestamp(text: str) -> Optional[pd.Timestamp]:
    """解析 '2025/3/29 上午 08:35:20'。上午 12 點 = 00 時,下午 12 點 = 12 時。"""
    match = _STAMP.match(text.strip())
    if not match:
        return None
    year, month, day, period, hour, minute, second = match.groups()
    hour_value = int(hour) % 12
    if period in ("下午", "PM"):
        hour_value += 12
    try:
        return pd.Timestamp(int(year), int(month), int(day), hour_value, int(minute), int(second))
    except ValueError:
        return None


def read_table(dataset: DataSet, item: DataFile) -> pd.DataFrame:
    """回傳第一欄為 time_local 的表;其餘欄維持原名。"""
    with dataset.open(item) as raw:
        text = raw.read().decode("cp950", errors="replace")
    start = text.find("<Data>")
    if start < 0:
        raise ValueError("找不到 <Data>")
    frame = pd.read_csv(
        io.StringIO(text[start + len("<Data>"):].lstrip()),
        sep="\t",
        index_col=False,  # 資料列結尾多一個 tab,否則第一欄會被當成索引
        on_bad_lines="skip",
        low_memory=False,
    )
    frame = frame.loc[:, [c for c in frame.columns if not str(c).startswith("Unnamed")]]
    stamps = frame.iloc[:, 0].astype(str).map(parse_timestamp)
    frame = frame.iloc[:, 1:].copy()
    frame.insert(0, "time_local", pd.to_datetime(stamps))
    return frame.dropna(subset=["time_local"]).reset_index(drop=True)


def discover(dataset: DataSet) -> Dict[str, Dict[str, DataFile]]:
    groups: Dict[str, Dict[str, DataFile]] = {}
    for rel, item in dataset.files.items():
        if not rel.startswith("Instruments/11D/"):
            continue
        match = _NAME.match(rel)
        if match:
            groups.setdefault(match.group(1), {})[match.group(2)] = item
    return groups


def analyse(dataset: DataSet, stem: str, members: Dict[str, DataFile]) -> Dict[str, object]:
    parts = stem.split("/")
    row: Dict[str, object] = {
        "file": stem,
        "campaign": parts[2] if len(parts) > 3 else "(11D 根目錄)",
        "kinds": ",".join(k for k in KINDS if k in members),
    }
    log = members.get("L")
    if log is None:
        return row
    if log.size < 100:  # 有些匯出的 -L 檔只有一個位元組,視為沒有資料
        row["rows"] = 0
        return row
    frame = read_table(dataset, log)
    row["rows"] = len(frame)
    if frame.empty:
        return row
    times = frame["time_local"]
    steps = times.diff().dt.total_seconds()
    row.update(
        start=times.iloc[0],
        end=times.iloc[-1],
        hours=(times.iloc[-1] - times.iloc[0]).total_seconds() / 3600,
        median_step_s=steps.median(),
        gaps=int((steps > 30).sum()),
        backward=int((steps < 0).sum()),
    )
    if "Err" in frame:
        errors = pd.to_numeric(frame["Err"], errors="coerce").fillna(0)
        row["err_nonzero_frac"] = float((errors != 0).mean())
    if "Pres" in frame:
        row["pressure_mb"] = float(pd.to_numeric(frame["Pres"], errors="coerce").median())
    needed = ["Year", "Mon", "Day", "Hr", "Min", "Sec"]
    if all(c in frame for c in needed):
        clock = frame[needed].apply(pd.to_numeric, errors="coerce")
        internal = pd.to_datetime(
            dict(
                year=clock["Year"] + 2000,
                month=clock["Mon"],
                day=clock["Day"],
                hour=clock["Hr"],
                minute=clock["Min"],
                second=clock["Sec"],
            ),
            errors="coerce",
        )
        offset = (times - internal).dt.total_seconds()
        if offset.notna().any():
            row["clock_offset_s"] = float(np.nanmedian(offset))
    return row


def flags_for(row: Dict[str, object], limits) -> List[Dict[str, str]]:
    flags: List[Dict[str, str]] = []

    def add(severity: str, code: str, message: str) -> None:
        flags.append({"severity": severity, "code": code, "message": message})

    if row.get("rows") == 0:
        add("error", "D11_NO_ROWS", "-L 檔讀不到任何資料列")
    present = str(row.get("kinds", "")).split(",")
    missing = [k for k in KINDS if k not in present]
    if missing:
        add("info", "D11_MISSING_KIND", f"缺少 {','.join(missing)} 檔")
    if row.get("backward"):
        add("error", "D11_TIME_BACKWARD", f"時間倒退 {row['backward']} 次")
    if row.get("gaps"):
        add("warn", "D11_GAPS", f"超過 30 秒的中斷 {row['gaps']} 次")
    offset = row.get("clock_offset_s")
    if offset is not None and abs(offset) > limits.d11_clock_offset_s:
        add("warn", "D11_CLOCK_OFFSET", f"電腦時間與儀器時鐘相差 {offset:.0f} 秒")
    err = row.get("err_nonzero_frac")
    if err is not None and err > limits.d11_err_frac:
        add("warn", "D11_ERR", f"Err 欄非 0 佔 {err:.0%}")
    return flags
