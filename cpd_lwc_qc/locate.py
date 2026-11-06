"""尋找 PADS 與 11-D 的存檔資料夾。只看檔名與修改時間,不開啟檔案內容。"""
from __future__ import annotations

import json
import os
import re
import string
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

from .formats import instrument_kind
from .notify import Notifier, valid_webhook

# 不會存放觀測資料、又可能很大的系統資料夾
SKIP_NAMES = {
    "windows", "program files", "program files (x86)", "programdata", "$recycle.bin", "system volume information",
    "recovery", "appdata", "node_modules", ".git", "__pycache__", "$windows.~bt", "$windows.~ws", "msocache", "perflogs",
}
# 合併候選資料夾時,不要往上併到這些太大的資料夾
TOO_BROAD = {"downloads", "documents", "desktop", "users", "下載", "文件", "桌面"}
_SESSION = re.compile(r"^\d{14}")
_DATE = re.compile(r"^\d{8}$")
_D11_NAME = re.compile(r"-(C|M|L)\.(dat|csv)$", re.I)
_D11_FOLDER = re.compile(r"(?i)(^|[^a-z0-9])(11-?d|d11|grimm)([^a-z0-9]|$)")
MERGE_LEVELS = 3  # 最多往上併幾層
PAUSE_EVERY = 200  # 每看這麼多資料夾休息一下,減少對硬碟的負擔


def find_cloud_folder() -> Optional[str]:
    """找 Google 雲端硬碟電腦版的「我的雲端硬碟」。它是虛擬磁碟,不在一般搜尋範圍。"""
    for letter in string.ascii_uppercase:
        for name in ("我的雲端硬碟", "My Drive"):
            path = Path(f"{letter}:/{name}")
            try:
                if path.is_dir():
                    return str(path)
            except OSError:
                continue
    return None


def fixed_drives() -> List[str]:
    """本機固定磁碟。略過網路、隨身碟和雲端虛擬磁碟,避免觸發大量下載。"""
    try:
        import ctypes

        kernel = ctypes.windll.kernel32
    except (ImportError, AttributeError):
        return [str(Path.home().anchor or "/")]
    out = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if kernel.GetDriveTypeW(root) != 3:  # DRIVE_FIXED
            continue
        label = ctypes.create_unicode_buffer(261)
        system = ctypes.create_unicode_buffer(261)
        ok = kernel.GetVolumeInformationW(root, label, 261, None, None, None, system, 261)
        if not ok:
            continue
        if system.value.upper() not in ("NTFS", "REFS", "EXFAT") or re.search(r"(?i)google|pcloud|onedrive|dropbox", label.value):
            continue
        out.append(root)
    return out


def _pads_base(folder: Path) -> Path:
    """由 ...\\20261018\\20261018163933 往上找到存放日期資料夾的那一層。"""
    current = folder
    if _SESSION.match(current.name):
        current = current.parent
    if _DATE.match(current.name):
        current = current.parent
    return current


def _can_merge_into(ancestor: Path, stops: Set[Path]) -> bool:
    if ancestor == ancestor.parent or ancestor.parent == ancestor.parent.parent:
        return False  # 磁碟根目錄和第一層資料夾太大
    return ancestor.name.lower() not in TOO_BROAD and ancestor not in stops


def _common_ancestor(a: Path, b: Path) -> Optional[Path]:
    try:
        return Path(os.path.commonpath([a, b]))
    except ValueError:  # 不同磁碟
        return None


def _merge(found: Dict[str, dict], stops: Iterable[str] = ()) -> List[dict]:
    """同一份資料常分散在幾個批次資料夾,往上合併成共同的上層,清單才不會太長。"""
    items = {Path(k): dict(v) for k, v in found.items()}
    stop_paths = {Path(s) for s in stops}

    def absorb(target: Path, paths: Iterable[Path]) -> None:
        merged = {"files": 0, "latest": 0.0, "kinds": set()}
        for path in paths:
            item = items.pop(path)
            merged["files"] += item["files"]
            merged["latest"] = max(merged["latest"], item["latest"])
            merged["kinds"] |= item["kinds"]
        items[target] = merged

    while True:
        best = None
        paths = sorted(items, key=str)
        for i, first in enumerate(paths):
            for second in paths[i + 1:]:
                ancestor = _common_ancestor(first, second)
                if ancestor is None or not _can_merge_into(ancestor, stop_paths):
                    continue
                reach = max(len(first.parts), len(second.parts)) - len(ancestor.parts)
                if reach <= MERGE_LEVELS and (best is None or len(ancestor.parts) > len(best.parts)):
                    best = ancestor
        if best is None:
            break
        absorb(best, [p for p in list(items) if p == best or best in p.parents])
    out = [{"path": str(p), "files": v["files"], "latest": v["latest"], "kinds": sorted(v["kinds"])} for p, v in items.items()]
    return sorted(out, key=lambda x: -x["latest"])


class Search:
    def __init__(self, roots: Iterable[str], skip: Iterable[str] = ()):
        self.roots = list(roots)
        self.skip = {os.path.normcase(os.path.abspath(p)) for p in skip}
        self.cancelled = threading.Event()
        self.scanned = 0
        self.current = ""
        self.running = False
        self.done = False
        self.started: Optional[float] = None
        self.finished: Optional[float] = None
        self.pads: Dict[str, dict] = {}
        self.d11: Dict[str, dict] = {}
        self.lock = threading.Lock()

    def _note(self, table: Dict[str, dict], folder: Path, kind: str, mtime: float) -> None:
        item = table.setdefault(str(folder), {"files": 0, "latest": 0.0, "kinds": set()})
        item["files"] += 1
        item["latest"] = max(item["latest"], mtime)
        item["kinds"].add(kind)

    def _visit(self, folder: str, names: List[str]) -> None:
        path = Path(folder)
        in_d11_folder = any(_D11_FOLDER.search(part) for part in path.parts[1:])
        for name in names:
            kind = instrument_kind(name)
            lower = name.lower()
            if kind in ("cdp", "lwc"):
                try:
                    mtime = (path / name).stat().st_mtime
                except OSError:
                    continue
                with self.lock:
                    self._note(self.pads, _pads_base(path), "CDP" if kind == "cdp" else "LWC-300", mtime)
            elif kind is None and (_D11_NAME.search(name) or (in_d11_folder and lower.endswith((".dat", ".csv")))):
                if lower.endswith(("shared.csv", "_pbp.csv")):
                    continue
                try:
                    mtime = (path / name).stat().st_mtime
                except OSError:
                    continue
                with self.lock:
                    self._note(self.d11, path, "11-D", mtime)

    def run(self) -> None:
        self.running = True
        self.started = time.time()
        try:
            for root in self.roots:
                for folder, dirs, names in os.walk(root):
                    if self.cancelled.is_set():
                        return
                    dirs[:] = [
                        d for d in dirs
                        if d.lower() not in SKIP_NAMES and not d.startswith("$")
                        and os.path.normcase(os.path.abspath(os.path.join(folder, d))) not in self.skip
                    ]
                    self.current = folder
                    self.scanned += 1
                    self._visit(folder, names)
                    if self.scanned % PAUSE_EVERY == 0:
                        time.sleep(0.01)
        finally:
            self.running = False
            self.done = True
            self.finished = time.time()

    def result(self) -> dict:
        with self.lock:
            pads = _merge(self.pads, self.roots)
            d11 = _merge(self.d11, self.roots)
        return {
            "running": self.running,
            "done": self.done,
            "cancelled": self.cancelled.is_set(),
            "scanned": self.scanned,
            "current": self.current,
            "elapsed": (self.finished or time.time()) - (self.started or time.time()),
            "drives": self.roots,
            "pads": pads,
            "d11": d11,
        }


class Locator:
    """面板的設定視窗後端。"""

    def __init__(self, feed, config_path: Path, config: dict, skip: Iterable[str] = (), roots: Optional[List[str]] = None):
        self.feed = feed
        self.config_path = config_path
        self.config = config
        self.skip = list(skip)
        self.roots = roots
        self.search: Optional[Search] = None
        self.on_change = None

    def state(self) -> dict:
        return {
            "pads_roots": self.config.get("pads_roots", []),
            "d11_roots": self.config.get("d11_roots", []),
            "config_path": str(self.config_path),
            "cloud_folder": self.config.get("cloud_folder") or find_cloud_folder() or "",
            "site_name": self.config.get("site_name", ""),
            "has_webhook": bool(self.config.get("discord_webhook")),
            "drive_active": bool(self.config.get("status_dir")),
            "backup_minutes": self.config.get("backup_minutes", 30),
            "search": self.search.result() if self.search else None,
        }

    def start(self) -> None:
        if self.search and self.search.running:
            return
        search = Search(self.roots if self.roots is not None else fixed_drives(), self.skip)
        search.running = True
        self.search = search
        threading.Thread(target=search.run, daemon=True, name="locate").start()

    def cancel(self) -> None:
        if self.search:
            self.search.cancelled.set()

    def apply_remote(self, cloud: object, site: object, webhook: object) -> List[str]:
        """雲端上傳和 Discord。webhook 是 None 表示維持原本的設定,不會回傳到網頁。"""
        errors: List[str] = []
        if not isinstance(cloud, str) or not isinstance(site, str):
            return ["設定格式不正確"]
        if cloud and not Path(cloud).is_dir():
            errors.append(f"找不到雲端資料夾:{cloud}")
        if webhook not in (None, "") and not self.config.get("_allow_local_webhook") and not (isinstance(webhook, str) and valid_webhook(webhook.strip())):
            errors.append("Discord Webhook 網址格式不對,應該以 https://discord.com/api/webhooks/ 開頭")
        if errors:
            return errors
        clean = re.sub(r'[\\/:*?"<>|]', "_", site.strip()) or "現場"
        self.config["site_name"] = site.strip()
        self.config["cloud_folder"] = cloud.replace("\\", "/")
        if webhook is not None:
            self.config["discord_webhook"] = webhook.strip()
        self._apply_cloud_paths(clean)
        self._save()
        if self.on_change:
            self.on_change()
        return []

    def _apply_cloud_paths(self, folder: Optional[str] = None) -> None:
        cloud = self.config.get("cloud_folder")
        if not cloud:
            self.config["status_dir"] = ""
            self.config["backup_jobs"] = []
            return
        clean = folder or re.sub(r'[\\/:*?"<>|]', "_", str(self.config.get("site_name", "")).strip()) or "現場"
        base = f"{cloud}/CDPLWC_現場/{clean}"
        self.config["status_dir"] = f"{base}/狀態"
        jobs = []
        for label, roots, sub in (("PADS", self.config.get("pads_roots", []), "PADS"), ("11-D", self.config.get("d11_roots", []), "11D")):
            for number, root in enumerate(roots):
                suffix = f"_{number + 1}" if len(roots) > 1 else ""
                jobs.append({"name": label + suffix, "source": root, "dest": f"{base}/{sub}{suffix}"})
        self.config["backup_jobs"] = jobs

    def test_message(self) -> Optional[str]:
        """傳一則測試訊息到 Discord。回傳錯誤文字;成功回傳 None。"""
        url = self.config.get("discord_webhook")
        if not url:
            return "還沒設定 Discord Webhook"
        notifier = Notifier(url, [], allow_local=bool(self.config.get("_allow_local_webhook")))
        notifier.add("🟢 **測試訊息**:現場監測可以傳到這個頻道。")
        if notifier.url is None:
            return "Webhook 網址格式不對"
        return None if notifier.flush() else (notifier.last_error or "傳送失敗")

    def _save(self) -> None:
        temporary = self.config_path.with_name(self.config_path.name + ".tmp")
        temporary.write_text(json.dumps(self.config, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.config_path)

    def apply(self, pads: object, d11: object) -> List[str]:
        """檢查路徑後寫入設定檔,並立刻改讀新資料夾。回傳錯誤訊息;沒有錯誤時為空。"""
        errors: List[str] = []
        for label, value in (("PADS", pads), ("11-D", d11)):
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                errors.append(f"{label} 資料夾格式不正確")
                continue
            for item in value:
                if not Path(item).is_dir():
                    errors.append(f"找不到資料夾:{item}")
        if errors:
            return errors
        pads_list = [p.replace("\\", "/") for p in pads]
        d11_list = [p.replace("\\", "/") for p in d11]
        self.config["pads_roots"] = pads_list
        self.config["d11_roots"] = d11_list
        self._apply_cloud_paths()
        self._save()
        self.feed.set_roots(pads_list, d11_list)
        if self.on_change:
            self.on_change()
        return []
