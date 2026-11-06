"""PADS 輸出檔的解析:檔頭設定、數值表、統一時間與欄位名稱。"""
from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd

from .formats import (
    BIN_COLUMN,
    CDP_COLUMNS,
    ENCODING,
    LWC_COLUMNS,
    MARKER,
    as_float as _as_float,
    instrument_kind,
    parse_key_values,
    parse_sizes,
    parse_utc_offset,
)
from .sources import DataFile, DataSet

MAX_HEADER_LINES = 5000
_SESSION_DIR = re.compile(r"^(\d{14})")


@dataclass
class Session:
    sid: str
    rel_dir: str
    campaign: str
    subcampaign: str
    files: Dict[str, DataFile] = field(default_factory=dict)
    instruments: Dict[str, DataFile] = field(default_factory=dict)

    @property
    def start_from_id(self) -> Optional[datetime]:
        try:
            return datetime.strptime(self.sid, "%Y%m%d%H%M%S")
        except ValueError:
            return None


def campaign_of(rel_dir: str) -> Tuple[str, str]:
    """由路徑判斷批次,例如 CDP/OBS_data/KAPAS/IOP_1_0213-0216/... -> KAPAS, IOP_1_0213-0216。"""
    parts = [p for p in rel_dir.split("/") if p]
    if parts[:2] == ["DMT", "PADS Data"]:
        return "DMT/PADS Data", ""
    if parts and parts[0] == "Instruments":
        parts = parts[2:]  # 去掉 Instruments/<儀器>
    parts = [p for p in parts if p.lower() != "obs_data"]
    if not parts:
        return "(未分類)", ""
    sub = ""
    if len(parts) >= 3 and not _SESSION_DIR.match(parts[1]) and not re.fullmatch(r"\d{8}", parts[1]):
        sub = parts[1]
    return parts[0], sub


def discover_sessions(dataset: DataSet) -> List[Session]:
    """依每次量測資料夾(YYYYMMDDhhmmss)分組。"""
    sessions: Dict[str, Session] = {}
    for rel, item in dataset.files.items():
        folder, _, name = rel.rpartition("/")
        base = folder.rpartition("/")[2]
        match = _SESSION_DIR.match(base)
        if not match:
            continue
        session = sessions.get(folder)
        if session is None:
            campaign, sub = campaign_of(folder)
            session = Session(match.group(1), folder, campaign, sub)
            sessions[folder] = session
        session.files[name] = item
        kind = instrument_kind(name)
        if kind:
            session.instruments[kind] = item
    return sorted(sessions.values(), key=lambda s: (s.sid, s.rel_dir))


def _text(dataset: DataSet, item: DataFile) -> io.TextIOWrapper:
    return io.TextIOWrapper(dataset.open(item), encoding=ENCODING, errors="replace", newline="")


@dataclass
class InstrumentTable:
    kind: str
    data: pd.DataFrame
    header: Dict[str, str]

    @property
    def sample_area(self) -> Optional[float]:
        return _as_float(self.header.get("Sample Area (mm^2)"))

    @property
    def thresholds_hash(self) -> Optional[str]:
        value = self.header.get("Thresholds")
        return hashlib.md5(value.encode()).hexdigest()[:8] if value else None


def _column_name(raw: str, mapping: Dict[str, str], bins: bool) -> Optional[str]:
    name = raw.strip()
    if name in mapping:
        return mapping[name]
    match = BIN_COLUMN.match(name) if bins else None
    return f"bin_{match.group(1)}" if match else None


def read_instrument(dataset: DataSet, item: DataFile, kind: str, bins: bool = False) -> InstrumentTable:
    """讀取一個儀器的 1 Hz 檔,回傳統一欄名且帶本地時間與 UTC 的表。bins=True 時連粒徑區間一起讀。"""
    mapping = LWC_COLUMNS if kind == "lwc" else CDP_COLUMNS
    with _text(dataset, item) as stream:
        header_lines: List[str] = []
        for _ in range(MAX_HEADER_LINES):
            line = stream.readline()
            if not line:
                raise ValueError(f"找不到資料起點 {MARKER}")
            line = line.rstrip("\r\n")
            if line.startswith(MARKER):
                break
            header_lines.append(line)
        else:
            raise ValueError(f"檔頭超過 {MAX_HEADER_LINES} 行")
        table = pd.read_csv(
            stream,
            usecols=lambda c: _column_name(c, mapping, bins) is not None,
            skipinitialspace=True,
            on_bad_lines="skip",
            low_memory=False,
        )
    table.columns = [_column_name(c, mapping, bins) for c in table.columns]
    table = table.apply(pd.to_numeric, errors="coerce")
    header = parse_key_values(header_lines)
    return InstrumentTable(kind, add_time(table, parse_utc_offset(header.get("Offset from UTC"))), header)


TIME_COLUMNS = {"End Seconds": "end_seconds", "End_Seconds": "end_seconds", "Day of Year": "day_of_year", "Day_of_Year": "day_of_year", "Year": "year"}
_SKIP_CHANNEL = re.compile(r"^(?:CDP|Fog Monitor|CDP IPT) Bin \d+$|^UTC Seconds$|^Unnamed")


def read_channels(dataset: DataSet, item: DataFile) -> pd.DataFrame:
    """讀取一個 1 Hz 檔的所有數值欄(粒徑區間除外),欄名保持 PADS 原名。全空的欄位不保留。"""
    with _text(dataset, item) as stream:
        for _ in range(MAX_HEADER_LINES):
            line = stream.readline()
            if not line:
                raise ValueError(f"找不到資料起點 {MARKER}")
            if line.startswith(MARKER):
                break
        else:
            raise ValueError(f"檔頭超過 {MAX_HEADER_LINES} 行")
        table = pd.read_csv(
            stream,
            usecols=lambda c: not _SKIP_CHANNEL.match(c.strip()),
            skipinitialspace=True,
            on_bad_lines="skip",
            low_memory=False,
        )
    table.columns = [TIME_COLUMNS.get(str(c).strip(), str(c).strip()) for c in table.columns]
    table = table.apply(pd.to_numeric, errors="coerce")
    table = add_time(table, parse_utc_offset(None))
    keep = [c for c in table.columns if c not in ("time_utc",) and (c == "time_local" or table[c].notna().any())]
    return table[keep]


def add_time(table: pd.DataFrame, utc_offset: timedelta) -> pd.DataFrame:
    """由 Year + Day of Year + End Seconds 算出時間。

    PADS 跨午夜後 End Seconds 會超過 86400 且 Day of Year 不變,逐列相加即可正確處理。
    """
    table = table.dropna(subset=["end_seconds", "day_of_year", "year"]).reset_index(drop=True)
    if table.empty:
        table["time_local"] = pd.Series(dtype="datetime64[ns]")
        table["time_utc"] = pd.Series(dtype="datetime64[ns]")
        return table
    stamp = (table["year"].astype(int) * 1000 + table["day_of_year"].astype(int)).astype(str)
    base = pd.to_datetime(stamp, format="%Y%j", errors="coerce")
    table["time_local"] = base + pd.to_timedelta(table["end_seconds"], unit="s")
    table["time_utc"] = table["time_local"] - utc_offset
    return table.dropna(subset=["time_local"]).reset_index(drop=True)


def read_config(dataset: DataSet, session: Session) -> Dict[str, Optional[float]]:
    """讀 Original*.ini(沒有時讀 PADS.ini)裡的全域手動值。"""
    item = next(
        (f for n, f in session.files.items() if n.startswith("Original") and n.endswith(".ini")),
        session.files.get("PADS.ini"),
    )
    if item is None:
        return {}
    with _text(dataset, item) as stream:
        values = parse_key_values(stream.read().splitlines())
    return {
        "config_pressure_mb": _as_float(values.get("Pressure(mb)")),
        "config_temperature_c": _as_float(values.get("Temp (C)")),
    }


def summarise_log(text: str) -> Dict[str, object]:
    """由紀錄檔判斷這次記錄怎麼結束。紀錄只寫事件,不寫原因,所以不下斷電或當機的結論。"""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        ending = "empty"
    elif lines[-1].endswith("Recording started."):
        ending = "restarted"  # 同一秒停止又開始,下一筆資料寫到新資料夾
    elif lines[-1].endswith("Recording stopped."):
        ending = "stopped"
    elif any(line.endswith("Recording stopped.") for line in lines):
        ending = "stopped_then_errors"
    else:
        ending = "no_stop"
    return {"log_ending": ending, "comm_errors": sum("Error 5902" in line or "Error 5903" in line for line in lines)}


def read_log_counts(dataset: DataSet, session: Session) -> Dict[str, int]:
    item = session.files.get(f"Log{session.sid}.txt")
    if item is None:
        return {}
    with _text(dataset, item) as stream:
        text = stream.read()
    return {"log_resets": text.count("PADS program RESET"), **summarise_log(text)}


def align_seconds(tables: Dict[str, InstrumentTable], how: str = "inner") -> pd.DataFrame:
    """把各儀器對齊到整秒,欄名加上儀器前綴。"""
    frames = []
    for kind, table in tables.items():
        frame = table.data.copy()
        frame["time_local"] = frame["time_local"].dt.round("s")
        frame = frame.drop_duplicates("time_local").set_index("time_local")
        frame = frame.drop(columns=["time_utc", "end_seconds", "day_of_year", "year"], errors="ignore")
        frames.append(frame.add_prefix(f"{kind}_"))
    if not frames:
        return pd.DataFrame()
    out = frames[0]
    for frame in frames[1:]:
        out = out.join(frame, how=how)
    return out.sort_index()
