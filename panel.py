"""CDP / LWC-300 / 11-D 即時監測面板的入口,也是 EXE 的進入點。"""
from __future__ import annotations

import argparse
import json
import socket
import os
import shutil
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Optional


def app_dir() -> Path:
    """EXE 旁邊的資料夾;設定檔和快取放這裡,方便使用者修改。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


sys.path.insert(0, str(Path(__file__).resolve().parent))

from cpd_lwc_qc import demo as demo_mode  # noqa: E402
from cpd_lwc_qc.live import Feed  # noqa: E402
from cpd_lwc_qc.locate import Locator  # noqa: E402
from cpd_lwc_qc.viewer import Store, make_handler  # noqa: E402
from cpd_lwc_qc.watch import Watcher  # noqa: E402

import backup  # noqa: E402

DEFAULT_CONFIG = {
    "_說明": "建議在面板按「設定資料夾」設定。discord_webhook 等同密碼,不要分享或上傳這個檔案。",
    "pads_roots": [],
    "d11_roots": [],
    "refresh_seconds": 10,
    "rescan_seconds": 60,
    "history_hours": 24,
    "stale_seconds": 60,
    "port": 8765,
    "status_dir": "",
    "watch_seconds": 60,
    "backup_minutes": 30,
    "stable_minutes": 15,
    "backup_jobs": [],
    "site_name": "",
    "discord_webhook": "",
    "daily_report_hour": 8,
}


def load_config(path: Path) -> dict:
    if not path.exists():
        path.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已建立設定檔:{path}", flush=True)
    return {**DEFAULT_CONFIG, **json.loads(path.read_text(encoding="utf-8"))}


def already_running(config_path: Path) -> bool:
    """同一份設定只跑一個,否則 Discord 通知和備份會重複。"""
    if sys.platform != "win32":
        return False
    import ctypes
    import hashlib

    name = "Local\\CDP_LWC_Panel_" + hashlib.sha256(str(config_path).lower().encode("utf-8")).hexdigest()[:24]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    handle = kernel32.CreateMutexW(None, False, name)
    globals()["_instance_handle"] = handle  # 程式結束時才釋放
    return bool(handle) and ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


def bind(port: int, handler) -> ThreadingHTTPServer:
    last: Optional[OSError] = None
    for candidate in range(port, port + 20):
        try:
            return ThreadingHTTPServer(("127.0.0.1", candidate), handler)
        except OSError as error:
            last = error
    raise SystemExit(f"找不到可用的連接埠:{last}")


def run_backups(config: dict, base: Path) -> dict:
    """把儀器資料複製到雲端硬碟資料夾,回傳摘要給監測。"""
    total = backup.Result()
    for job in config.get("backup_jobs", []):
        if not Path(job["source"]).is_dir():
            continue
        result = backup.run_job(job, float(config.get("stable_minutes", 15)), False)
        for name in ("copied", "updated", "unchanged", "in_use", "conflicts", "failed"):
            setattr(total, name, getattr(total, name) + getattr(result, name))
        total.rows += result.rows
    failed = [row for row in total.rows if row[2].startswith("失敗")]
    return {
        "time": time.strftime("%Y-%m-%d %H:%M"), "copied": total.copied, "updated": total.updated, "in_use": total.in_use,
        "failed": total.failed, "conflicts": total.conflicts, "sample": (failed[0][3] + " " + failed[0][4])[:160] if failed else "",
    }


def colored(line: str, alerts: list) -> str:
    """黑色視窗也分紅黃綠。文字本身就說明狀態,複製出來不會少資訊。"""
    text = time.strftime("%H:%M:%S ") + line
    if not sys.stdout.isatty():
        return text
    if line.startswith("需要注意"):
        code = 91 if any(a["level"] == "error" for a in alerts) else 93
    else:
        code = 92
    return f"\033[{code}m{text}\033[0m"


def load_normal(base: Path, config: dict):
    path = base / str(config.get("cache", "cache")) / "normal_reference.json"
    bundled = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "cpd_lwc_qc" / "normal_reference.json"
    source = path if path.exists() else bundled
    return json.loads(source.read_text(encoding="utf-8")) if source.exists() else None


def run_demo(base: Path, args) -> int:
    """假資料放暫存資料夾,結束時刪除。不讀 panel_config.json,不上傳,不傳 Discord。"""
    root = Path(tempfile.mkdtemp(prefix="cdp_lwc_demo_"))
    config = demo_mode.config_for(root)
    print("模擬模式:資料都是假的,不會上傳,也不會傳 Discord。", flush=True)
    print("正在產生模擬資料……", flush=True)
    normal = load_normal(base, config)
    world = demo_mode.World(root, config, normal=normal)
    feed = Feed(config)
    watcher = Watcher(feed, config, power=world.power, boot=world.boot, processes=world.processes, normal=normal)
    world.attach(feed, watcher)
    feed.start()
    world.start()
    stop_watch = threading.Event()
    threading.Thread(target=run_watch, args=(feed, watcher, config, base, stop_watch), daemon=True, name="watch").start()
    store = Store(base / "cache", allow_empty=True)
    server = bind(args.port or 8780, make_handler(store, feed, None, demo=world))
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"模擬面板:{url}", flush=True)
    print("關閉這個視窗就會結束模擬。", flush=True)
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_watch.set()
        world.stop()
        feed.stop()
        server.server_close()
        shutil.rmtree(root, ignore_errors=True)
    return 0


def run_watch(feed: Feed, watcher: Watcher, config: dict, base: Path, stop: threading.Event) -> None:
    """定時檢查和備份。出錯時只記下來,不影響面板。"""
    next_backup = 0.0
    while not stop.is_set():
        if not feed.ready:  # 第一次讀完檔案前不判斷,否則會誤報找不到檔案
            stop.wait(1)
            continue
        try:
            if config.get("backup_jobs") and time.monotonic() >= next_backup:
                next_backup = time.monotonic() + float(config.get("backup_minutes", 30)) * 60
                watcher.backup_result = run_backups(config, base)
            alerts = watcher.step()
            for line in watcher.console_changes(alerts):
                print(colored(line, alerts), flush=True)
        except Exception as error:  # 監測不能因單次錯誤停止
            print("監測發生錯誤:", error, flush=True)
        stop.wait(float(config.get("watch_seconds", 60)))


def main() -> int:
    parser = argparse.ArgumentParser(description="即時監測面板")
    parser.add_argument("--config", default=None)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--demo", action="store_true", help="模擬各種狀況,不碰真實資料")
    args = parser.parse_args()
    base = app_dir()
    if sys.platform == "win32":
        os.system("")  # 讓黑色視窗顯示顏色
    if args.demo:
        return run_demo(base, args)
    config_path = Path(args.config).resolve() if args.config else base / "panel_config.json"
    try:
        config = load_config(config_path)
    except (OSError, json.JSONDecodeError) as error:
        print(f"設定檔有誤:{config_path}\n{error}", flush=True)
        input("按 Enter 結束")
        return 2

    if already_running(config_path):
        print("面板已經在執行,直接開啟現有的畫面。", flush=True)
        if not args.no_browser:
            webbrowser.open(f"http://127.0.0.1:{args.port or int(config['port'])}/")
        return 0

    missing = [p for p in config["pads_roots"] + config["d11_roots"] if not Path(p).is_dir()]
    for path in missing:
        print(f"注意:找不到資料夾 {path},請檢查 {config_path}", flush=True)
    feed = Feed(config)
    feed.start()
    config.setdefault("state_file", str(config_path.with_name("watch_state.json")))
    watcher = Watcher(feed, config, normal=load_normal(base, config))
    stop_watch = threading.Event()
    threading.Thread(target=run_watch, args=(feed, watcher, config, base, stop_watch), daemon=True, name="watch").start()
    store = Store(base / str(config.get("cache", "cache")), allow_empty=True)
    locator = Locator(feed, config_path, config, skip=[str(base)])
    locator.on_change = watcher.reconfigure
    server = bind(args.port or int(config["port"]), make_handler(store, feed, locator))
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"面板已啟動:{url}", flush=True)
    print("此程式只讀取資料,不會控制儀器。關閉這個視窗就會結束面板。", flush=True)
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_watch.set()
        feed.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
