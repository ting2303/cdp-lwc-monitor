"""把觀測資料增量備份到另一個資料夾(例如 Google 雲端硬碟電腦版的 G: 槽)。

python backup.py --config backup_config.json --dry-run   # 只列出會做什麼
python backup.py --config backup_config.json             # 實際備份

原則:
- 只新增或更新,絕不刪除目的地的檔案。
- 最近仍在變動的檔案先跳過,因為 PADS 可能還在寫入。
- 先寫成 .part 並核對 SHA-256,確認相同才改名。
- 目的地比來源新且內容不同時不覆蓋,回報為衝突。
只需要 Python 標準函式庫。
"""
from __future__ import annotations

import argparse
import csv
import fnmatch
import hashlib
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator, List, Optional

MTIME_TOLERANCE_S = 2.0  # FAT 與雲端檔案系統的時間解析度較粗


@dataclass
class Result:
    copied: int = 0
    updated: int = 0
    unchanged: int = 0
    in_use: int = 0
    conflicts: int = 0
    failed: int = 0
    bytes_copied: int = 0
    rows: List[List[str]] = field(default_factory=list)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def walk(source: Path, exclude: List[str]) -> Iterator[Path]:
    for folder, dirs, names in os.walk(source):
        dirs.sort()
        for name in sorted(names):
            path = Path(folder) / name
            rel = path.relative_to(source).as_posix()
            if any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(name, p) for p in exclude):
                continue
            yield path


def copy_verified(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    try:
        shutil.copy2(source, part)
        if sha256(source) != sha256(part):
            raise OSError("複製後 SHA-256 不符")
        os.replace(part, target)
    finally:
        if part.exists():
            part.unlink()


def run_job(job: dict, stable_minutes: float, dry_run: bool, now: Optional[float] = None) -> Result:
    source = Path(job["source"])
    dest = Path(job["dest"])
    exclude = list(job.get("exclude", [])) + ["*.part", "Thumbs.db", "desktop.ini"]
    now = time.time() if now is None else now
    result = Result()

    def note(action: str, path: Path, detail: str = "") -> None:
        result.rows.append([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), job["name"], action, str(path), detail])

    for path in walk(source, exclude):
        rel = path.relative_to(source)
        target = dest / rel
        try:
            stat = path.stat()
            if now - stat.st_mtime < stable_minutes * 60:
                result.in_use += 1
                note("使用中,先跳過", rel)
                continue
            action = "新增"
            if target.exists():
                old = target.stat()
                same_size = old.st_size == stat.st_size
                if same_size and abs(old.st_mtime - stat.st_mtime) <= MTIME_TOLERANCE_S:
                    result.unchanged += 1
                    continue
                # 解壓或雲端同步可能改變修改時間,大小相同時比內容,避免重複上傳。
                if same_size and sha256(path) == sha256(target):
                    result.unchanged += 1
                    continue
                if old.st_mtime > stat.st_mtime + MTIME_TOLERANCE_S:
                    result.conflicts += 1
                    note("衝突:目的地較新,未覆蓋", rel)
                    continue
                action = "更新"
            if not dry_run:
                existing = target.parent
                while not existing.exists() and existing != existing.parent:
                    existing = existing.parent
                if shutil.disk_usage(existing).free < stat.st_size * 1.1:
                    raise OSError("目的地空間不足")
                copy_verified(path, target)
            if action == "新增":
                result.copied += 1
            else:
                result.updated += 1
            result.bytes_copied += stat.st_size
            note(("將" if dry_run else "已") + action, rel, f"{stat.st_size}")
        except OSError as error:
            result.failed += 1
            note("失敗", rel, str(error))
    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="增量備份觀測資料")
    parser.add_argument("--config", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    config_path = Path(args.config).resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    log_dir = Path(config.get("log_dir", config_path.parent / "backup_logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    lock = log_dir / "backup.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if time.time() - lock.stat().st_mtime < 6 * 3600:
            print("上一次備份還在執行,這次略過。", flush=True)
            return 0
        lock.unlink()  # 超過 6 小時視為上次異常中止
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(descriptor)

    status = 0
    try:
        for job in config["jobs"]:
            source, dest = Path(job["source"]), Path(job["dest"])
            if not source.is_dir():
                print(f"[{job['name']}] 找不到來源:{source}", flush=True)
                status = 1
                continue
            anchor = Path(dest.anchor) if dest.anchor else dest
            if not anchor.exists():
                print(f"[{job['name']}] 找不到目的地磁碟:{anchor},可能雲端硬碟電腦版沒有開。", flush=True)
                status = 1
                continue
            result = run_job(job, float(config.get("stable_minutes", 15)), args.dry_run)
            verb = "預計複製" if args.dry_run else "已複製"
            print(
                f"[{job['name']}] 新增 {result.copied}、更新 {result.updated}、未變 {result.unchanged}、"
                f"使用中 {result.in_use}、衝突 {result.conflicts}、失敗 {result.failed},"
                f"{verb} {result.bytes_copied / 1e9:.2f} GB",
                flush=True,
            )
            if not args.dry_run and result.rows:
                log = log_dir / f"backup_{datetime.now():%Y%m}.csv"
                new = not log.exists()
                with open(log, "a", newline="", encoding="utf-8-sig") as handle:
                    writer = csv.writer(handle)
                    if new:
                        writer.writerow(["時間", "工作", "動作", "檔案", "備註"])
                    writer.writerows(result.rows)
            if result.failed or result.conflicts:
                status = 1
                for row in result.rows:
                    if row[2].startswith(("失敗", "衝突")):
                        print("  ", row[2], row[3], row[4], flush=True)
    finally:
        if lock.exists():
            lock.unlink()
    return status


if __name__ == "__main__":
    sys.exit(main())
