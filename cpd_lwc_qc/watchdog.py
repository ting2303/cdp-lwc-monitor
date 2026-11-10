"""看門狗:面板當掉或沒回應時自動重開,Google 雲端硬碟程式沒開時也幫忙打開。

PADS 和 11-D 不會自動重開:它們要在視窗上按 Record,自動點按可能影響記錄,所以只發通知。
關掉黑色視窗時,看門狗和面板會一起結束。
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, List, Optional

GRACE_SECONDS = 180
CHECK_SECONDS = 60
HANG_FAILS = 3
MAX_QUICK_RESTARTS = 5
QUICK_WINDOW = 600
LONG_BACKOFF = 300
DRIVE_RETRY_SECONDS = 1800
LOG_LIMIT = 200_000
DRIVE_GLOB = r"C:\Program Files\Google\Drive File Stream\*\GoogleDriveFS.exe"
DRIVE_PROCESS = "GoogleDriveFS.exe"


def healthy(port_file: Path, timeout: float = 10) -> bool:
    try:
        port = int(port_file.read_text(encoding="utf-8").strip())
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as response:
            return bool(json.loads(response.read().decode("utf-8")).get("ok"))
    except (OSError, ValueError, urllib.error.URLError):
        return False


def find_drive() -> Optional[str]:
    found = sorted(glob.glob(DRIVE_GLOB))
    return found[-1] if found else None


class Watchdog:
    def __init__(self, command: List[str], cwd: Path, port_file: Path, log_path: Path, watch_drive: Callable[[], bool] = lambda: False, processes: Optional[Callable[[], Optional[List[str]]]] = None, check: Callable[[Path], bool] = healthy, grace: float = GRACE_SECONDS, interval: float = CHECK_SECONDS, start_drive: Optional[Callable[[], bool]] = None):
        self.command, self.cwd, self.port_file, self.log_path = command, cwd, port_file, log_path
        self.watch_drive, self.processes, self.check = watch_drive, processes, check
        self.grace, self.interval = grace, interval
        self.start_drive = start_drive or self._start_drive
        self.child: Optional[subprocess.Popen] = None
        self.restarts = 0
        self.restart_times: List[float] = []
        self.drive_tried = 0.0
        self.stop_requested = False

    def log(self, message: str) -> None:
        line = time.strftime("%Y-%m-%d %H:%M:%S ") + message
        print("[看門狗] " + line, flush=True)
        try:
            if self.log_path.exists() and self.log_path.stat().st_size > LOG_LIMIT:
                self.log_path.replace(self.log_path.with_suffix(".old.log"))
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass

    def launch(self) -> None:
        env = {**os.environ, "PANEL_RESTART_COUNT": str(self.restarts)}
        self.port_file.unlink(missing_ok=True)
        self.child = subprocess.Popen(self.command, cwd=str(self.cwd), env=env)
        self.started = time.monotonic()
        self.fails = 0

    def _start_drive(self) -> bool:
        path = find_drive()
        if not path:
            return False
        subprocess.Popen([path], close_fds=True)
        return True

    def check_drive(self) -> None:
        if self.processes is None or not self.watch_drive():
            return
        running = self.processes()
        now = time.monotonic()
        if running is None or DRIVE_PROCESS in running or (self.drive_tried and now - self.drive_tried < DRIVE_RETRY_SECONDS):
            return
        self.drive_tried = now
        self.log("Google 雲端硬碟程式沒有執行,嘗試開啟:" + ("成功" if self.start_drive() else "找不到程式"))

    def wait_before_restart(self) -> float:
        now = time.monotonic()
        self.restart_times = [t for t in self.restart_times if now - t < QUICK_WINDOW] + [now]
        return LONG_BACKOFF if len(self.restart_times) > MAX_QUICK_RESTARTS else 5.0

    def step(self) -> Optional[float]:
        """檢查一次。需要重開時回傳要等幾秒。"""
        code = self.child.poll()
        if code is not None:
            self.log(f"面板結束了(代碼 {code}),準備重開")
            return self.wait_before_restart()
        if time.monotonic() - self.started >= self.grace:
            if self.check(self.port_file):
                self.fails = 0
            else:
                self.fails += 1
                self.log(f"面板沒有回應({self.fails}/{HANG_FAILS})")
                if self.fails >= HANG_FAILS:
                    self.child.kill()
                    self.child.wait(timeout=30)
                    self.log("面板沒有回應,已強制結束,準備重開")
                    return self.wait_before_restart()
        return None

    def run(self, sleep: Callable[[float], None] = time.sleep) -> int:
        self.log("開始監看面板")
        self.launch()
        try:
            while not self.stop_requested:
                sleep(self.interval)
                wait = self.step()
                if wait is not None:
                    self.restarts += 1
                    sleep(wait)
                    self.log(f"重開面板(第 {self.restarts} 次)")
                    self.launch()
                self.check_drive()
        except KeyboardInterrupt:
            pass
        finally:
            if self.child is not None and self.child.poll() is None:
                self.child.terminate()
        return 0
