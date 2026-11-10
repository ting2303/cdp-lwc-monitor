"""遠端診斷:把判斷問題需要的資訊寫成一份報告,放到雲端資料夾,人不在現場也看得到。

COM 埠有沒有消失,能幫忙分辨是線路或儀器電源的問題,還是軟體的問題。
報告不含 Discord Webhook 和心跳網址。只讀取系統狀態,不會更改任何設定。
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SYSTEM_CACHE_SECONDS = 300
_HEX = re.compile(r"0x([0-9a-fA-F]+)\s*$")
SHOT = (
    "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
    "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen;"
    "$i=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
    "$g=[System.Drawing.Graphics]::FromImage($i);"
    "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size);"
    "$i.Save($env:CDP_SHOT_PATH,[System.Drawing.Imaging.ImageFormat]::Png)"
)


def _run(command: List[str], timeout: float = 20, env: Optional[dict] = None) -> str:
    try:
        return subprocess.run(command, capture_output=True, timeout=timeout, creationflags=NO_WINDOW, env=env, encoding="cp950", errors="replace").stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


def read_com_ports() -> Optional[List[str]]:
    """目前存在的 COM 埠。讀不到時回傳 None。"""
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM") as key:
            ports, index = [], 0
            while True:
                try:
                    ports.append(str(winreg.EnumValue(key, index)[1]))
                    index += 1
                except OSError:
                    return sorted(ports)
    except FileNotFoundError:
        return []
    except OSError:
        return None


def read_reboot_pending() -> Optional[bool]:
    """Windows 更新是否等著重開機。"""
    try:
        import winreg
    except ImportError:
        return None
    for path in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending"):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path):
                return True
        except FileNotFoundError:
            continue
        except OSError:
            return None
    return False


def parse_ac_setting(output: str) -> Optional[int]:
    """powercfg /query 的接電設定值。行名會依語言不同,接電固定是倒數第二個索引值。"""
    values = [int(m.group(1), 16) for line in output.splitlines() if (m := _HEX.search(line.strip())) and ("AC" in line or "DC" in line)]
    return values[-2] if len(values) >= 2 else None


def read_power_plan() -> dict:
    """接電時的睡眠和闔上螢幕設定。0 代表不睡眠、闔上不動作。"""
    out: Dict[str, Optional[int]] = {}
    for key, sub, setting in (("sleep_ac_seconds", "SUB_SLEEP", "STANDBYIDLE"), ("hibernate_ac_seconds", "SUB_SLEEP", "HIBERNATEIDLE"), ("lid_ac_action", "4f971e89-eebd-4455-a8de-9e59040e7347", "5ca83367-6e45-459f-a27b-476b1d01c936")):
        out[key] = parse_ac_setting(_run(["powercfg", "/query", "SCHEME_CURRENT", sub, setting]))
    return out


class SystemStatus:
    """系統資訊幾分鐘讀一次就好。"""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.cached: Optional[dict] = None
        self.at = 0.0

    def __call__(self) -> dict:
        now = self.clock()
        if self.cached is None or now - self.at >= SYSTEM_CACHE_SECONDS:
            self.cached = {"reboot_pending": read_reboot_pending(), **read_power_plan()}
            self.at = now
        # COM 埠只查登錄檔,很快,而且要即時
        return {**self.cached, "com_ports": read_com_ports()}


def problem_devices() -> List[str]:
    """裝置管理員裡有問題的裝置,以及序列埠。"""
    script = "Get-CimInstance Win32_PnPEntity | ? { $_.Status -ne 'OK' -or $_.Name -match 'COM\\d+' } | % { $_.Status + ' | ' + $_.Name }"
    env = {**os.environ}
    return [line.strip() for line in _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout=40, env=env).splitlines() if line.strip()][:40]


def check_site(url: str) -> str:
    started = time.monotonic()
    try:
        request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "CDP-LWC-Monitor/1.0"})
        with urllib.request.urlopen(request, timeout=10):
            pass
    except urllib.error.HTTPError:
        pass  # 有回應就代表連得到
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        return f"連不上({type(error).__name__})"
    return f"正常 {(time.monotonic() - started) * 1000:.0f} ms"


def take_screenshot(path: Path) -> bool:
    env = {**os.environ, "CDP_SHOT_PATH": str(path)}
    _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", SHOT], timeout=30, env=env)
    return path.exists()


def build_report(feed, watcher, config: dict, system: Optional[dict] = None) -> str:
    system = system if system is not None else watcher.system()
    status = feed.status()
    lines = [f"遠端診斷報告 {datetime.now():%Y-%m-%d %H:%M:%S}", f"電腦:{os.environ.get('COMPUTERNAME', '')} / {platform.platform()}", ""]
    lines.append("【總結】")
    board = status.get("board") or {}
    alerts = board.get("alerts") or []
    lines.append({"red": "需要處理", "yellow": "需要注意", "green": "全部正常"}.get(board.get("overall"), "檢查中") + f",{len(alerts)} 個項目")
    for alert in alerts:
        lines.append(f"- [{alert.get('color')}] {alert['title']}:{alert['detail']}")
    lines += ["", "【儀器】"]
    for item in status["instruments"]:
        lines.append(f"- {item['label']}:{item['text']},{item['files']} 個檔案,最後資料 {'—' if item['age'] is None else str(round(item['age'])) + ' 秒前'}" + (f",無法解析 {item['bad_lines']} 行" if item.get("bad_lines") else ""))
    lines += ["", "【程式】"]
    running = watcher.processes()
    from .watch import DRIVE_PROCESS, PADS_PROCESS

    if running is None:
        lines.append("- 讀不到程式清單")
    else:
        lines.append(f"- PADS:{'執行中' if PADS_PROCESS in running else '沒有執行'}")
        lines.append(f"- Google 雲端硬碟:{'執行中' if DRIVE_PROCESS in running else '沒有執行'}")
        others = sorted({n for n in running if re.search(r"grimm|11-?d|anydesk", n, re.I) and not n.startswith("CDP_LWC")})
        lines.append("- 其他相關:" + ("、".join(others) if others else "沒有看到"))
    power = watcher.power() or {}
    boot = watcher.boot()
    lines += ["", "【電力和系統】", f"- 電源:{'接電' if power.get('on_ac') else '電池'},電量 {power.get('percent')}%"]
    if boot:
        lines.append(f"- 開機時間:{datetime.fromtimestamp(boot):%Y-%m-%d %H:%M}")
    lines.append(f"- 接電時睡眠:{system.get('sleep_ac_seconds')} 秒;休眠:{system.get('hibernate_ac_seconds')} 秒;闔上螢幕動作:{system.get('lid_ac_action')}(0 是不動作)")
    lines.append(f"- Windows 更新等重開機:{system.get('reboot_pending')}")
    lines += ["", "【COM 埠】", "- 目前:" + ("、".join(system.get("com_ports") or []) or "沒有"), "- 上次全部正常時:" + ("、".join(watcher.state.get("com_ports") or []) or "尚未記錄")]
    lines += ["", "【硬碟】"]
    for root in sorted({Path(p).anchor for p in list(config.get("pads_roots") or []) + list(config.get("d11_roots") or []) if Path(p).anchor}):
        try:
            usage = shutil.disk_usage(root)
            lines.append(f"- {root} 剩 {usage.free / 1e9:.1f} GB / {usage.total / 1e9:.0f} GB")
        except OSError as error:
            lines.append(f"- {root} 讀不到:{type(error).__name__}")
    lines += ["", "【網路】", f"- Google:{check_site('https://drive.google.com')}", f"- Discord:{check_site('https://discord.com')}"]
    notifier = watcher.notifier
    lines.append(f"- Discord 通知:{'已設定' if notifier.url else '未設定'},待送 {len(notifier.queue)} 則" + (f",{notifier.last_error}" if notifier.last_error else ""))
    beat = getattr(watcher, "heartbeat", None)
    if beat is not None:
        lines.append(f"- 外部心跳:{'已設定' if beat.url else '未設定'}" + (f",連續失敗 {beat.fails} 次" if beat.fails else ""))
    result = watcher.backup_result
    lines += ["", "【備份】", f"- 最近一次:{result}" if result else "- 還沒有備份紀錄"]
    log_path = watcher.newest_log()
    if log_path is not None and log_path.exists():
        from .watch import read_log_tail

        lines += ["", f"【PADS 紀錄檔最後幾行】{log_path.name}"] + [f"  {line[:200]}" for line in read_log_tail(log_path)[-15:]]
    lines += ["", "【最近通知】"] + [f"- {e['time']} {e['kind']} {e['title']}" for e in (board.get("events") or [])[:20]]
    devices = problem_devices()
    lines += ["", "【裝置(序列埠和有問題的裝置)】"] + [f"- {d}" for d in devices]
    return "\n".join(lines) + "\n"


def write_report(feed, watcher, config: dict) -> Optional[Path]:
    """寫到雲端狀態資料夾。沒有設定雲端時回傳 None,但呼叫者仍可用 build_report。"""
    status_dir = watcher.status_dir
    if status_dir is None:
        return None
    try:
        status_dir.mkdir(parents=True, exist_ok=True)
        temporary = status_dir / "遠端診斷.tmp"
        temporary.write_text(build_report(feed, watcher, config), encoding="utf-8-sig")
        target = status_dir / "遠端診斷.txt"
        os.replace(temporary, target)
        if config.get("diag_screenshot", True):
            shot = status_dir / "螢幕截圖.tmp.png"
            if take_screenshot(shot):
                os.replace(shot, status_dir / "螢幕截圖.png")
        return target
    except OSError as error:
        print("無法寫入診斷報告:", type(error).__name__, flush=True)
        return None
