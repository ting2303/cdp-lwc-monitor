"""從另一台電腦確認資料真的上傳了。

現場筆電只知道檔案複製進 Google 雲端硬碟資料夾,不知道是否已傳上雲端。
在家裡或辦公室的電腦(同一個 Google 帳號)執行這個檢查:如果雲端的 現場狀態.txt 太久沒更新,
代表筆電、網路或上傳有問題,就發 Discord。可以和外部心跳互相補位。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

from .notify import Notifier

STATUS_NAME = "現場狀態.txt"


def find_status_files(root: Path) -> Dict[str, Path]:
    """root 是 雲端硬碟 或 CDPLWC_現場。回傳 {測站: 狀態檔}。"""
    base = root / "CDPLWC_現場" if (root / "CDPLWC_現場").is_dir() else root
    return {path.parent.parent.name: path for path in sorted(base.glob(f"*/狀態/{STATUS_NAME}"))}


def check(root: Path, stale_minutes: float, active: Dict[str, bool], notifier: Notifier, now: Optional[float] = None) -> List[str]:
    """回傳每個測站的狀態文字,狀態改變時發通知。"""
    now = time.time() if now is None else now
    out = []
    files = find_status_files(root)
    if not files:
        out.append(f"{root}:找不到任何 {STATUS_NAME}")
    for site, path in files.items():
        try:
            age = (now - path.stat().st_mtime) / 60
        except OSError:
            continue
        stale = age > stale_minutes
        out.append(f"{site}:{age:.0f} 分鐘前更新" + ("(太舊)" if stale else ""))
        if stale and not active.get(site):
            active[site] = True
            notifier.add(f"🔴 **{site} 的雲端狀態 {age:.0f} 分鐘沒更新**\n筆電、網路或 Google 雲端硬碟上傳可能停了。\n**建議:**\n1. 用 AnyDesk 看筆電還在不在線上\n2. 看 Google 雲端硬碟電腦版有沒有卡住\n3. 連不上就請現場確認電源和網路")
        elif not stale and active.get(site):
            active[site] = False
            notifier.add(f"🟢 **已恢復:{site} 的雲端狀態又開始更新**")
    return out


def run(root: Path, config: dict, state_path: Path, stale_minutes: float = 30, interval: float = 300) -> int:
    try:
        active = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        active = {}
    queue: List[str] = []
    notifier = Notifier(config.get("discord_webhook") or None, queue, allow_local=bool(config.get("_allow_local_webhook")))
    print(f"雲端檢查:{root},超過 {stale_minutes:g} 分鐘沒更新就通知。" + ("" if notifier.url else "還沒設定 Discord,只顯示在這裡。"), flush=True)
    last: List[str] = []
    try:
        while True:
            lines = check(root, stale_minutes, active, notifier)
            notifier.flush()
            if [re.sub(r"\d+ 分鐘前", "", line) for line in lines] != [re.sub(r"\d+ 分鐘前", "", line) for line in last]:
                for line in lines:
                    print(time.strftime("%H:%M:%S"), line, flush=True)
            last = lines
            try:
                state_path.write_text(json.dumps(active, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0
