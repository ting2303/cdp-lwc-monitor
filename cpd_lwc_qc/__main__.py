"""命令列入口:python -m cpd_lwc_qc --data <資料夾> --out <輸出資料夾>"""
from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import pandas as pd

from . import __version__, d11
from .export import export_session
from .pads import (
    InstrumentTable,
    discover_sessions,
    read_config,
    read_instrument,
    read_log_counts,
)
from .qc import ERROR, Limits, add_split_info, analyse_session, campaign_flags, load_limits
from .report import write_outputs
from .sources import DataSet

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "qc_config.json"
FLAG_COLUMNS = ["sid", "campaign", "path", "severity", "code", "message"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cpd_lwc_qc", description="檢查 PADS / 11-D 觀測資料並產生品質報告")
    parser.add_argument("--data", required=True, help="資料資料夾(可含 .zip 與已解壓檔案)或單一 .zip")
    parser.add_argument("--out", required=True, help="輸出資料夾")
    parser.add_argument("--config", default=None, help="門檻設定 JSON,預設為工具內的 qc_config.json")
    parser.add_argument("--match", default=None, help="只處理路徑符合此正規表示式的量測與 11-D 檔")
    parser.add_argument("--export", action="store_true", help="另外輸出每次量測對齊到整秒的 .csv.gz")
    parser.add_argument("--skip-11d", action="store_true", help="不檢查 11-D 檔")
    return parser


def run(args: argparse.Namespace) -> int:
    out = Path(args.out).resolve()
    data = Path(args.data).resolve()
    if not data.exists():
        print(f"找不到資料路徑:{data}", file=sys.stderr)
        return 2
    limits: Limits = load_limits(args.config or (str(DEFAULT_CONFIG) if DEFAULT_CONFIG.exists() else None))
    pattern = re.compile(args.match) if args.match else None
    started = time.time()
    with DataSet(str(data), exclude=[str(out)]) as dataset:
        sessions = discover_sessions(dataset)
        if pattern:
            sessions = [s for s in sessions if pattern.search(s.rel_dir)]
        print(f"找到 {len(dataset.files)} 個檔案、{len(sessions)} 次量測", flush=True)

        rows: List[dict] = []
        flag_rows: List[dict] = []
        for number, session in enumerate(sessions, 1):
            tables: Dict[str, InstrumentTable] = {}
            read_errors: List[str] = []
            for kind, item in session.instruments.items():
                try:
                    tables[kind] = read_instrument(dataset, item, kind)
                except Exception as error:  # 單一壞檔不應中斷全部掃描
                    read_errors.append(f"{item.rel}: {error}")
            extras: Dict[str, object] = {}
            try:
                extras.update(read_config(dataset, session))
                extras.update(read_log_counts(dataset, session))
            except Exception as error:
                read_errors.append(f"設定或紀錄檔:{error}")
            row, flags = analyse_session(session, tables, extras, limits)
            flags += [{"severity": ERROR, "code": "READ_ERROR", "message": m} for m in read_errors]
            rows.append(row)
            base = {"sid": session.sid, "campaign": session.campaign, "path": session.rel_dir}
            flag_rows += [{**base, **flag} for flag in flags]
            if args.export and any(not t.data.empty for t in tables.values()):
                export_session(session, tables, out / "aligned")
            if number % 20 == 0 or number == len(sessions):
                print(f"  量測 {number}/{len(sessions)},{time.time() - started:.0f} 秒", flush=True)

        d11_rows: List[dict] = []
        d11_flag_rows: List[dict] = []
        if not args.skip_11d:
            groups = d11.discover(dataset)
            for stem, members in sorted(groups.items()):
                if pattern and not pattern.search(stem):
                    continue
                try:
                    row = d11.analyse(dataset, stem, members)
                    flags = d11.flags_for(row, limits)
                except Exception as error:
                    row = {"file": stem, "kinds": ",".join(sorted(members))}
                    flags = [{"severity": ERROR, "code": "READ_ERROR", "message": str(error)}]
                d11_rows.append(row)
                d11_flag_rows += [{"file": stem, **flag} for flag in flags]

        notes = []
        if dataset.bad_zips:
            notes.append("無法開啟的壓縮檔:" + "、".join(dataset.bad_zips))
        if dataset.size_conflicts:
            notes.append(f"{len(dataset.size_conflicts)} 個檔案在不同來源大小不同,請確認解壓是否完整。")

    frame = pd.DataFrame(rows)
    if "number_source" not in frame:  # 全部量測都讀不到 CDP/FM 時仍要能出報告
        frame["number_source"] = None
    frame = add_split_info(frame)
    flag_frame = pd.DataFrame(flag_rows, columns=FLAG_COLUMNS)
    camp_frame = pd.DataFrame(
        campaign_flags(frame) if len(frame) else [], columns=["campaign", "severity", "code", "message"]
    )
    d11_frame = pd.DataFrame(d11_rows)
    d11_flag_frame = pd.DataFrame(d11_flag_rows, columns=["file", "severity", "code", "message"])
    report = write_outputs(
        out,
        frame,
        flag_frame,
        camp_frame,
        d11_frame,
        d11_flag_frame,
        {
            "root": str(data),
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "version": __version__,
            "notes": notes,
        },
    )
    print(f"完成,{time.time() - started:.0f} 秒。報告:{report}")
    return 0


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    sys.exit(main())
