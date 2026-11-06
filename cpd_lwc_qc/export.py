"""輸出對齊到整秒、欄名統一的資料。"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Dict

import pandas as pd

from .formats import parse_utc_offset
from .pads import InstrumentTable, Session, align_seconds


def export_session(session: Session, tables: Dict[str, InstrumentTable], out_dir: Path) -> Path:
    usable = {k: t for k, t in tables.items() if not t.data.empty}
    frame = align_seconds(usable, how="outer")
    first = next(iter(usable.values()))
    offset: timedelta = parse_utc_offset(first.header.get("Offset from UTC"))
    frame.insert(0, "time_utc", frame.index - offset)
    frame.insert(1, "session", session.sid)
    frame.insert(2, "campaign", session.campaign)
    frame = frame.drop(columns=[c for c in frame.columns if c.endswith("_status")])
    target = out_dir / f"{session.sid}.csv.gz"
    target.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(target, date_format="%Y-%m-%d %H:%M:%S", float_format="%.6g", encoding="utf-8")
    return target
