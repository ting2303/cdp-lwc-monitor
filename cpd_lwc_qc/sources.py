"""把壓縮檔與已解壓的資料夾視為同一份檔案清單。原始資料一律只讀。"""
from __future__ import annotations

import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Dict, Iterable, List, Optional

_DRIVE_PREFIX = re.compile(r"^(?:Instruments|DMT)-\d{8}T\d+Z-\d+-\d+/")


def normalize_rel(path: str) -> str:
    """去掉 Google Drive 分割下載時多出的頂層資料夾,讓同一檔案只有一個路徑。"""
    rel = path.replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return _DRIVE_PREFIX.sub("", rel)


@dataclass(frozen=True)
class DataFile:
    rel: str
    size: int
    path: Optional[str] = None
    zip_path: Optional[str] = None
    member: Optional[str] = None


class DataSet:
    """一個資料根目錄底下所有可讀檔案的索引。"""

    def __init__(self, root: str, exclude: Iterable[str] = ()):
        self.root = str(root)
        self.files: Dict[str, DataFile] = {}
        self.size_conflicts: List[str] = []
        self.bad_zips: List[str] = []
        self._zips: Dict[str, zipfile.ZipFile] = {}
        self._scan([os.path.abspath(p) for p in exclude])

    def _add(self, item: DataFile) -> None:
        old = self.files.get(item.rel)
        if old is None:
            self.files[item.rel] = item
            return
        if old.size != item.size:
            self.size_conflicts.append(item.rel)
        # 解壓後的檔案讀取較快,優先使用。
        if old.path is None and item.path is not None:
            self.files[item.rel] = item

    def _scan(self, exclude: List[str]) -> None:
        root = Path(self.root)
        if root.is_file():
            self._scan_zip(root)
            return
        zips: List[Path] = []
        for folder, dirs, names in os.walk(root):
            abs_folder = os.path.abspath(folder)
            dirs[:] = [d for d in dirs if os.path.join(abs_folder, d) not in exclude]
            for name in names:
                full = Path(folder) / name
                if name.lower().endswith(".zip"):
                    zips.append(full)
                else:
                    rel = normalize_rel(os.path.relpath(full, root))
                    self._add(DataFile(rel, full.stat().st_size, path=str(full)))
        for item in sorted(zips):
            self._scan_zip(item)

    def _scan_zip(self, path: Path) -> None:
        try:
            archive = zipfile.ZipFile(path)
        except zipfile.BadZipFile:
            self.bad_zips.append(str(path))
            return
        self._zips[str(path)] = archive
        for info in archive.infolist():
            if not info.is_dir():
                rel = normalize_rel(info.filename)
                self._add(DataFile(rel, info.file_size, zip_path=str(path), member=info.filename))

    def open(self, item: DataFile) -> BinaryIO:
        if item.path is not None:
            return open(item.path, "rb")
        return self._zips[item.zip_path].open(item.member)

    def close(self) -> None:
        for archive in self._zips.values():
            archive.close()
        self._zips.clear()

    def __enter__(self) -> "DataSet":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
