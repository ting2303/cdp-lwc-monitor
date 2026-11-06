"""現場監測:判斷資料是否中斷、可能的原因,並把狀態寫到雲端資料夾。

只讀取儀器檔案和系統狀態,不控制儀器。原因是依紀錄和系統狀態推斷,文字會說明依據。
"""
from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .notify import Notifier, format_alert, format_resolved

# 每種問題的處理建議,依序試
ADVICE = {
    "reboot": ["確認 PADS 和 11-D 有沒有重新按開始記錄", "看 Windows 更新設定;如果不是更新,檢查電源", "到問題紀錄看中斷了多久,寫進觀測紀錄"],
    "on_battery": ["檢查筆電電源線和延長線", "確認現場是否停電,儀器可能也沒電", "電量低於 20% 前存好資料;復電後確認 PADS 重新記錄"],
    "stale_pads": ["用 AnyDesk 連上,看 PADS 是否顯示 Recording", "沒有在記錄就按 Sample 和 Record,會產生新資料夾", "PADS 顯示 No Fault 以外的狀態時,檢查儀器電源和傳輸線"],
    "stale_comm": ["檢查 CDP、LWC-300 的電源指示燈", "檢查 USB 或序列線有沒有鬆脫,確認 COM 埠還在", "在 PADS 暫停再重新 Sample;仍失敗就重開儀器電源"],
    "stale_stopped": ["確認是否有人刻意停止(例如換日整理資料)", "如果不是,用 AnyDesk 重新按 Record"],
    "stale_not_running": ["用 AnyDesk 連上,開啟 PADS 4", "載入設定檔後按 Sample 和 Record", "PADS 若一直關閉,查看 Windows 事件或電腦是否重開過"],
    "stale_d11": ["用 AnyDesk 看 11-D 軟體是否還在記錄", "檢查 11-D 電源和傳輸線", "確認存檔資料夾沒有變更"],
    "setup": ["打開面板,按「設定資料夾」", "勾選自動找到的資料夾後按「套用」;找不到時直接輸入路徑"],
    "missing": ["確認儀器軟體已開始記錄", "按面板的「設定資料夾」確認路徑"],
    "log_errors": ["持續出現時,檢查儀器電源和傳輸線", "把紀錄檔裡的錯誤訊息記下來,問 DMT 時會用到"],
    "fog": ["有人在現場時,檢查並依手冊清潔 CDP 鏡窗", "看現場濕度和氣溫,判斷是起霧或結冰", "檢查雷射溫度;確認前的 CDP 資料先標記為可疑"],
    "drive_missing": ["用 AnyDesk 打開 Google 雲端硬碟電腦版", "確認帳號登入和網路", "資料還在筆電上,恢復後會自動補傳"],
    "drive_stopped": ["用 AnyDesk 打開 Google 雲端硬碟電腦版", "確認它有開機自動啟動", "資料還在筆電上,恢復後會自動補傳"],
    "backup_failed": ["檢查 Google 雲端硬碟剩餘空間", "確認雲端硬碟電腦版正常執行", "下次備份會自動重試"],
    "backup_conflict": ["比對筆電和雲端的同名檔案", "確認後手動處理,程式不會自動覆蓋"],
    "disk": ["確認資料已上傳到雲端", "刪除前先核對雲端檔案;空間不足可能讓 PADS 停止存檔"],
    "read": ["確認檔案沒有被其他程式鎖住", "檢查資料夾權限"],
    "env_pm": ["確認附近有沒有燃燒、施工或車輛", "看 PM 是否和其他測站同時升高,判斷是本地或大範圍", "如果是本地汙染,把這段時間在觀測紀錄標註"],
    "env_pressure": ["確認天氣是否快速變化(例如鋒面或颱風)", "同時檢查 11-D 是否正常;如果只有它異常,可能是儀器問題"],
    "env_rh": ["檢查 11-D 進氣和機箱有沒有進水或結露", "必要時加乾燥管或改善遮蔽,並標記這段資料"],
    "env_temp": ["檢查機箱通風和日照", "必要時遮陽或改善散熱,過熱會影響儀器"],
    "env_number": ["確認不是校正或噴霧測試", "檢查附近有沒有汙染源或水氣", "持續很高時,注意粒子重合會讓數值偏差"],
    "env_laser": ["對照 Dump Spot 和雷射溫度", "持續偏離時記錄下來,年度校正時請 DMT 檢查"],
    "clock": ["用 AnyDesk 檢查筆電的日期、時間和時區", "確認 PADS 和 11-D 在同一台筆電上記錄", "時鐘修正前後的資料要分開對齊,並寫進觀測紀錄"],
    "d11_internal": ["不影響面板對齊:程式用電腦記錄的時間", "下次維護時校正 11-D 內部時鐘"],
    "manual_pas": ["確認是不是有人改了設定", "在觀測紀錄寫下修改時間;濃度和 LWC 會在這個時間點跳動"],
    "manual_mismatch": ["確認兩台實際吹到的風是否相同", "決定要用哪個值,之後統一設定"],
    "manual_pressure": ["把 LWC-300 的手動壓力改成接近實測值", "在觀測紀錄寫下修改時間"],
}


# 面板的燈號分類
LIGHTS = [("cdp", "CDP"), ("lwc", "LWC-300"), ("d11", "11-D"), ("power", "電力和電腦"), ("upload", "上傳"), ("optics", "儀器狀態"), ("env", "背景環境"), ("manual", "手動值"), ("time", "時間")]
_LEVEL_COLOR = {"error": "red", "warn": "yellow", "info": "blue"}
_COLOR_RANK = {"green": 0, "blue": 1, "yellow": 2, "red": 3}
_INSTRUMENT = {"CDP": "cdp", "LWC-300": "lwc", "11-D": "d11"}


def categories_of(alert: dict) -> List[str]:
    key = alert["key"]
    head, _, tail = key.partition(":")
    if head in ("stale", "missing", "read"):
        return [_INSTRUMENT.get(tail, "cdp")]
    if head == "setup":
        names = []
        if "PADS" in alert["title"]:
            names += ["cdp", "lwc"]
        if "11-D" in alert["title"]:
            names.append("d11")
        return names or ["cdp"]
    if head == "log_errors":
        return ["cdp", "lwc"]
    if head in ("on_battery", "reboot", "disk"):
        return ["power"]
    if head.startswith(("drive", "backup")):
        return ["upload"]
    if head in ("fog", "env_laser", "env_temp"):
        return ["optics"]
    if head.startswith("env_"):
        return ["env"]
    if head.startswith("manual_"):
        return ["manual"]
    if head in ("clock", "d11_internal"):
        return ["time"]
    return ["power"]


def board_for(alerts: List[dict], active: Dict[str, str], events: List[dict]) -> dict:
    """面板上的紅黃綠燈。info 只是提示,不會讓總燈變黃。"""
    lights = {key: {"id": key, "label": label, "color": "green", "titles": []} for key, label in LIGHTS}
    cards = []
    for alert in alerts:
        color = _LEVEL_COLOR.get(alert["level"], "yellow")
        for name in categories_of(alert):
            light = lights[name]
            light["titles"].append(alert["title"])
            if _COLOR_RANK[color] > _COLOR_RANK[light["color"]]:
                light["color"] = color
        cards.append({**alert, "color": color, "since": active.get(alert["key"], "")})
    cards.sort(key=lambda c: -_COLOR_RANK[c["color"]])
    worst = max((_COLOR_RANK[c["color"]] for c in cards), default=0)
    return {"overall": {3: "red", 2: "yellow"}.get(worst, "green"), "lights": list(lights.values()), "alerts": cards, "events": events}


def advice_for(alert: dict) -> List[str]:
    key = alert["key"]
    if key.startswith("env_temp"):
        return ADVICE["env_temp"]
    if key.startswith("clock:"):
        return ADVICE["clock"]
    if key == "stale:11-D":
        return ADVICE["stale_d11"]
    if key.startswith("stale:"):
        detail = alert["detail"]
        if "沒有在執行" in detail:
            return ADVICE["stale_not_running"]
        if "5902" in detail or "通訊" in detail:
            return ADVICE["stale_comm"]
        if "Recording stopped" in detail:
            return ADVICE["stale_stopped"]
        return ADVICE["stale_pads"]
    return ADVICE.get(key.split(":")[0], [])


# 依 2023–2026 的資料定:過去 99% 的值再留一點餘裕
ENV_LIMITS = {"pm25": 45.0, "pm25_typical": 31.0, "pressure_change": 4.0, "d11_rh": 60.0, "d11_temp": 42.0, "laser_temp": 36.0, "number": 1500.0}
DRIVE_PROCESS = "GoogleDriveFS.exe"
PADS_PROCESS = "PADS 4.exe"
LOG_TAIL_BYTES = 65536
RECENT_LOG_SECONDS = 900
_LOG_TIME = re.compile(r"(\d\d:\d\d:\d\d)\s+(\d{8}):")
_ERROR = re.compile(r"Error (-?\d+) occurred at ([^\s>]+)")


def read_power() -> Optional[dict]:
    """筆電接電狀態。讀不到(例如桌機)回傳 None。"""
    try:
        import ctypes

        class Status(ctypes.Structure):
            _fields_ = [("ac", ctypes.c_ubyte), ("flag", ctypes.c_ubyte), ("percent", ctypes.c_ubyte), ("saver", ctypes.c_ubyte), ("life", ctypes.c_ulong), ("full", ctypes.c_ulong)]

        status = Status()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return None
        return {"on_ac": {0: False, 1: True}.get(status.ac), "percent": None if status.percent == 255 else int(status.percent), "has_battery": status.flag != 128}
    except (ImportError, AttributeError, OSError):
        return None


def read_boot_time() -> Optional[float]:
    try:
        import ctypes

        kernel = ctypes.windll.kernel32
        kernel.GetTickCount64.restype = ctypes.c_ulonglong
        return time.time() - kernel.GetTickCount64() / 1000.0
    except (ImportError, AttributeError, OSError):
        return None


def list_processes() -> Optional[List[str]]:
    try:
        output = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), encoding="cp950", errors="replace").stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return [row[0] for row in csv.reader(output.splitlines()) if row]


def read_log_tail(path: Path) -> List[str]:
    try:
        with open(path, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            handle.seek(max(0, size - LOG_TAIL_BYTES))
            text = handle.read().decode("cp950", errors="replace")
    except OSError:
        return []
    return [line.strip() for line in text.splitlines() if line.strip()]


def explain_log(lines: List[str], now: float) -> dict:
    """由 PADS 紀錄檔判斷最近發生什麼事。"""
    out = {"last": lines[-1][:160] if lines else None, "reason": None, "errors": [], "recent_errors": 0}
    if not lines:
        return out
    recent: Dict[str, int] = {}
    newest_error = None
    for line in lines:
        match = _ERROR.search(line)
        if not match:
            continue
        stamp = _LOG_TIME.search(line)
        when = None
        if stamp:
            try:
                when = datetime.strptime(stamp.group(2) + stamp.group(1), "%Y%m%d%H:%M:%S").timestamp()
            except ValueError:
                pass
        if when is not None and now - when <= RECENT_LOG_SECONDS:
            key = f"Error {match.group(1)}({match.group(2)})"
            recent[key] = recent.get(key, 0) + 1
            newest_error = line
    out["errors"] = [f"{name} {count} 次" for name, count in recent.items()]
    out["recent_errors"] = sum(recent.values())
    tail = lines[-1]
    if tail.endswith("Recording stopped."):
        out["reason"] = "PADS 紀錄顯示 Recording stopped:有人停止記錄,或程式正常結束記錄。"
    elif tail.endswith("PADS program stopped."):
        out["reason"] = "PADS 紀錄顯示程式已關閉。"
    elif any(code in (newest_error or "") for code in ("Error 5902", "Error 5903")):
        out["reason"] = "PADS 紀錄有 Error 5902/5903:電腦和儀器通訊失敗,常見於線路鬆脫或儀器沒電。"
    elif newest_error:
        out["reason"] = f"PADS 紀錄有錯誤:{newest_error[:120]}"
    return out


class Watcher:
    def __init__(self, feed, config: dict, power: Callable = read_power, boot: Callable = read_boot_time, processes: Callable = list_processes, clock: Callable = time.time, normal: Optional[dict] = None):
        self.feed = feed
        self.config = config
        self.normal = normal
        self.console_shown: dict = {}
        self.recent_events: List[dict] = []
        self.power, self.boot, self.processes, self.clock = power, boot, processes, clock
        self.state_path = Path(config.get("state_file", "watch_state.json"))
        self.fog_seconds = float(config.get("fog_seconds", 300))
        self.state = self._load_state()
        self.backup_result: Optional[dict] = None
        self.last_alerts: List[dict] = []
        self.reconfigure()
        self.daily_hour = config.get("daily_report_hour", 8)
        if self.notifier.url and self.state.get("started_notice_boot") != self.boot():
            self.notifier.add("🟢 **監測已啟動**" + (f"({self.site})" if self.site else "") + f"\n{datetime.fromtimestamp(self.clock()).strftime('%m-%d %H:%M')} 開始監測。之後有問題會在這裡通知。")
            self.state["started_notice_boot"] = self.boot()

    def reconfigure(self) -> None:
        """設定改了以後不必重開程式。"""
        config = self.config
        self.status_dir = Path(config["status_dir"]) if config.get("status_dir") else None
        self.site = str(config.get("site_name", ""))
        self.notifier = Notifier(config.get("discord_webhook") or None, self.state.setdefault("discord_queue", []), allow_local=bool(config.get("_allow_local_webhook")))

    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        temporary = self.state_path.with_name(self.state_path.name + ".tmp")
        try:
            temporary.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, self.state_path)
        except OSError:
            pass

    # ---- 判斷 --------------------------------------------------------
    def newest_log(self) -> Optional[Path]:
        with self.feed.lock:
            paths = [item.tail.path for item in self.feed.pads.values()]
        if not paths:
            return None
        newest = max(paths, key=lambda p: p.parent.name)
        match = re.match(r"^(\d{14})", newest.parent.name)
        return newest.parent / f"Log{match.group(1)}.txt" if match else None

    def console_changes(self, alerts: List[dict]) -> List[str]:
        """黑色視窗只印有變化的項目;完整狀態看面板和狀態檔。"""
        current = {a["key"]: a["title"] for a in alerts}
        previous = self.console_shown
        lines = []
        added = [title for key, title in current.items() if key not in previous]
        cleared = [title for key, title in previous.items() if key not in current]
        if added:
            lines.append("需要注意:" + "、".join(added))
        if cleared:
            lines.append("已解除:" + "、".join(cleared))
        if cleared and not current:
            lines.append("目前沒有需要注意的項目")
        self.console_shown = current
        return lines

    def evaluate(self) -> List[dict]:
        now = self.clock()
        alerts: List[dict] = []

        def add(key: str, level: str, title: str, detail: str, action: str) -> None:
            item = {"key": key, "level": level, "title": title, "detail": detail, "action": action}
            item["steps"] = advice_for(item)
            alerts.append(item)

        status = self.feed.status()
        log_path = self.newest_log()
        log = explain_log(read_log_tail(log_path), now) if log_path else explain_log([], now)
        running = self.processes()
        boot = self.boot()
        power = self.power()
        previous_boot = self.state.get("boot")
        rebooted = bool(boot and previous_boot and boot - previous_boot > 120)
        if rebooted:
            self.state["reboot_at"] = boot
        if boot:
            self.state["boot"] = boot
        recent_reboot = self.state.get("reboot_at")
        if recent_reboot and now - recent_reboot < 6 * 3600:
            stamp = datetime.fromtimestamp(recent_reboot).strftime("%m-%d %H:%M")
            last_seen = self.state.get("last_seen")
            gap = f"上次監測到 {datetime.fromtimestamp(last_seen).strftime('%m-%d %H:%M')}," if last_seen else ""
            add("reboot", "warn", "電腦重新開機過", f"這次開機時間 {stamp}。{gap}中間可能斷電、更新或有人重開。", "確認 PADS 和 11-D 是否重新開始記錄。")

        if power and power.get("has_battery") and power.get("on_ac") is False:
            percent = power.get("percent")
            level = "error" if percent is not None and percent <= 30 else "warn"
            add("on_battery", level, "電腦改用電池", f"目前沒有接電(電量 {percent}%)。可能斷電或電源線鬆脫。", "檢查現場電源;儀器可能也同時斷電。")

        unset = [name for name, roots in (("PADS", self.config.get("pads_roots")), ("11-D", self.config.get("d11_roots"))) if not roots]
        if unset:
            add("setup", "warn", f"還沒設定 {'、'.join(unset)} 資料夾", "設定之前不會讀取這些資料,也不會上傳。", "在面板按「設定資料夾」。")

        pads_running = None if running is None else PADS_PROCESS in running
        for instrument in status["instruments"]:
            label, code = instrument["label"], instrument["code"]
            if label == "11-D":
                if code == "stale":
                    add("stale:11-D", "error", "11-D 資料停止更新", f"已 {_age(instrument['age'])} 沒有新資料。11-D 沒有紀錄檔可判斷原因。", "檢查 11-D 軟體和電源。")
                elif code == "missing":
                    add("missing:11-D", "warn", "找不到 11-D 檔案", "設定的資料夾裡沒有最近 24 小時的 11-D 檔案。", "檢查 11-D 是否在存檔,以及面板的資料夾設定。")
                continue
            if code == "stale":
                reasons = []
                if pads_running is False:
                    reasons.append("PADS 目前沒有在執行。")
                if log["reason"]:
                    reasons.append(log["reason"])
                if recent_reboot and now - recent_reboot < 6 * 3600:
                    reasons.append("電腦剛重開機過。")
                if power and power.get("on_ac") is False:
                    reasons.append("電腦正在用電池,可能斷電。")
                add(f"stale:{label}", "error", f"{label} 資料停止更新", f"已 {_age(instrument['age'])} 沒有新資料。" + (" ".join(reasons) or "紀錄檔沒有說明原因。"), "檢查 PADS 是否在記錄、儀器電源和傳輸線。")
            elif code == "missing":
                add(f"missing:{label}", "warn", f"找不到 {label} 檔案", "設定的資料夾裡沒有最近 24 小時的檔案。" + ("PADS 目前沒有在執行。" if pads_running is False else ""), "確認 PADS 已按開始記錄,以及資料夾設定。")
            elif code == "error":
                add(f"read:{label}", "warn", f"{label} 檔案讀取失敗", "; ".join(instrument["errors"])[:200], "檢查檔案權限。")

        if log["recent_errors"]:
            add("log_errors", "warn", "PADS 紀錄有錯誤", "最近 15 分鐘:" + "、".join(log["errors"]) + "。", "如果持續出現,檢查儀器電源和傳輸線。")

        fog = self.feed.dump_low(self.fog_seconds)
        if fog:
            add("fog", "error", "CDP 可能起霧或結冰", f"最近 {self.fog_seconds / 60:.0f} 分鐘無粒子時 Dump Spot 約 {fog['median']:.2f} V(通常約 4.84 V)。手冊提到可能是鏡窗起霧、結冰或雷射過熱。CDP 雖然有資料,但可能不可靠。", "檢查鏡窗。")

        self._environment_alerts(add, now)
        self._clock_alerts(add)
        self._storage_alerts(add, running)
        self.state["last_seen"] = now
        return alerts

    def _environment_alerts(self, add: Callable, now: float) -> None:
        """背景環境和儀器狀態。門檻來自過去資料,持續 15 分鐘的中位數才算,避免短暫跳動。"""
        env = self.feed.recent_environment()
        limits = {**ENV_LIMITS, **self.config.get("env_limits", {})}
        pm = env.get("pm25")
        if pm is not None and pm > limits["pm25"]:
            add("env_pm", "warn", "背景 PM2.5 異常高", f"最近 15 分鐘約 {pm:.0f} µg/m³,過去野外資料 99% 在 {limits['pm25_typical']:.0f} 以下,過去最高約 41。", "確認附近有沒有汙染源。")
        change = env.get("pressure_change")
        if change is not None and abs(change) > limits["pressure_change"]:
            add("env_pressure", "warn", "氣壓變化很快", f"30 分鐘內變化 {change:+.1f} hPa(目前 {env['pressure']:.0f} hPa),可能是天氣快速變化或感測異常。", "確認天氣和儀器。")
        rh = env.get("rh")
        if rh is not None and rh > limits["d11_rh"]:
            add("env_rh", "warn", "11-D 內部濕度偏高", f"最近 15 分鐘約 {rh:.0f}%,過去野外資料 99% 在 42% 以下,可能有結露風險。", "檢查進氣和機箱。")
        for key, label, typical in (("d11_temp", "11-D 內部", "35"), ("laser_temp", "CDP 雷射", "27")):
            value = env.get(key)
            if value is not None and value > limits[key]:
                add(f"env_temp:{key}", "warn", f"{label}溫度偏高", f"最近 15 分鐘約 {value:.1f} °C,過去野外資料 99% 在 {typical} °C 以下。", "檢查散熱。")
        number = env.get("number")
        if number is not None and number > limits["number"]:
            add("env_number", "warn", "CDP 濃度異常高", f"最近 15 分鐘中位數 {number:.0f} /cm³,過去野外資料雲中 99% 在 960 以下。", "確認是否測試或有汙染源。")
        model = self.normal.get("laser_model") if self.normal else None
        if model and env.get("laser", 0) > 40 and env.get("laser_temp") is not None:
            years = (now - model["t0"]) / (365.25 * 86400)
            expected = model["a"] + model["b"] * env["laser_temp"] + model["c"] * years
            residual = env["laser"] - expected
            if abs(residual) > 6 * model["sd"]:
                add("env_laser", "warn", "CDP 雷射電流偏離常態", f"目前 {env['laser']:.1f} mA,依溫度和長期趨勢預期 {expected:.1f} mA,差 {residual:+.1f} mA。", "記錄並持續觀察。")
        values = env.get("cdp_pas_values") or []
        if len(values) > 1:
            add("manual_pas", "warn", "CDP 流速設定被改過", f"最近 15 分鐘出現 {'、'.join(f'{v:g}' for v in values)} m/s。這是手動輸入值,濃度和 LWC 會跟著跳動。", "確認並記錄修改時間。")
        cdp_pas, hot_pas = env.get("cdp_pas"), env.get("hot_pas")
        if cdp_pas is not None and hot_pas is not None and abs(cdp_pas - hot_pas) > 0.01 and not self.config.get("ignore_pas_mismatch"):
            add("manual_mismatch", "info", "兩台流速設定不同", f"CDP {cdp_pas:g}、LWC-300 {hot_pas:g} m/s,都是手動輸入。若實際相同,CDP 濃度和 LWC 要乘 {cdp_pas / hot_pas:.2f}。", "確認實際風速。")
        pressure, manual = env.get("pressure"), env.get("hot_pressure")
        if pressure is not None and manual is not None and abs(pressure - manual) > 15:
            add("manual_pressure", "warn", "LWC-300 手動壓力和實測不符", f"手動輸入 {manual:g} hPa,11-D 實測 {pressure:.0f} hPa,差 {manual - pressure:+.0f} hPa。", "修正手動壓力。")

    def _clock_alerts(self, add: Callable) -> None:
        """PADS 和 11-D 是兩套軟體,時間要靠同一台筆電的時鐘才對得起來。"""
        clocks = self.feed.clock_check()
        limit = float(self.config.get("clock_skew_seconds", 300))
        for label in ("PADS", "11-D"):
            skew = clocks.get(label)
            if skew is not None and abs(skew) > limit:
                add(f"clock:{label}", "warn", f"{label} 的資料時間和電腦時鐘不符", f"檔案還在更新,但最新一筆和電腦時鐘差 {abs(skew) / 60:.0f} 分鐘。可能是時區或時鐘設定不同;三台的時間會對不起來。", "檢查時鐘和時區。")
        internal = clocks.get("internal")
        if internal is not None and abs(internal) > float(self.config.get("d11_internal_seconds", 600)):
            add("d11_internal", "info", "11-D 內部時鐘偏差較大", f"內部時鐘和電腦差 {internal:+.0f} 秒(過去約每年多慢 40 秒)。面板用電腦時間對齊,不受影響。", "下次維護時校正。")

    def _storage_alerts(self, add: Callable, running: Optional[List[str]]) -> None:
        if self.status_dir is not None:
            anchor = Path(self.status_dir.anchor or ".")
            if not anchor.exists():
                add("drive_missing", "error", "雲端硬碟不見了", f"找不到 {anchor},資料無法上傳。", "打開 Google 雲端硬碟電腦版並確認登入。")
            elif running is not None and DRIVE_PROCESS not in running:
                add("drive_stopped", "error", "Google 雲端硬碟電腦版沒有執行", "資料會留在這台電腦,不會上傳。", "打開 Google 雲端硬碟電腦版。")
        result = self.backup_result
        if result:
            if result.get("failed"):
                add("backup_failed", "error", "備份失敗", f"{result['failed']} 個檔案複製失敗:{result.get('sample', '')}", "檢查雲端硬碟空間和連線。")
            if result.get("conflicts"):
                add("backup_conflict", "warn", "備份有衝突", f"{result['conflicts']} 個檔案雲端版本較新,沒有覆蓋。", "人工確認這些檔案。")
        minimum = float(self.config.get("min_free_gb", 5))
        for root in {Path(p).anchor for p in self.config.get("pads_roots", []) if Path(p).anchor}:
            try:
                free = shutil.disk_usage(root).free / 1e9
            except OSError:
                continue
            if free < minimum:
                add(f"disk:{root}", "error", "硬碟空間不足", f"{root} 只剩 {free:.1f} GB。", "清出空間,否則可能無法繼續存檔。")

    # ---- 輸出 --------------------------------------------------------
    def step(self) -> List[dict]:
        alerts = self.evaluate()
        now = self.clock()
        stamp = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")
        active = self.state.setdefault("active", {})
        events = []
        current = {a["key"]: a for a in alerts}
        for key, alert in current.items():
            if key not in active:
                active[key] = stamp
                events.append([stamp, "發生", alert["level"], alert["title"], alert["detail"]])
                self.notifier.add(format_alert(alert, self.site))
        titles = self.state.setdefault("titles", {})
        titles.update({key: alert["title"] for key, alert in current.items()})
        for key in [k for k in active if k not in current]:
            since = active.pop(key)
            title = titles.pop(key, key)
            events.append([stamp, "解除", "info", title, f"從 {since} 開始"])
            self.notifier.add(format_resolved(title, since, self.site))
        self._daily_report(alerts, now)
        self.notifier.flush()
        self._save_state()
        self.last_alerts = alerts
        for row in events:
            self.recent_events.insert(0, {"time": row[0], "kind": row[1], "level": row[2], "title": row[3]})
        del self.recent_events[40:]
        self.feed.board = board_for(alerts, active, self.recent_events)
        self.feed.external_notices = [f"{a['title']}:{a['detail']}" for a in alerts if not a["key"].startswith(("stale", "missing"))]
        if self.status_dir is not None:
            self._write_status(alerts, active, stamp, events)
        return alerts

    def _daily_report(self, alerts: List[dict], now: float) -> None:
        """每天回報一次,就算沒事也說一聲:收不到代表筆電或網路可能停了。"""
        if self.daily_hour is None or self.notifier.url is None:
            return
        moment = datetime.fromtimestamp(now)
        today = moment.strftime("%Y-%m-%d")
        if moment.hour != int(self.daily_hour) or self.state.get("daily_sent") == today:
            return
        self.state["daily_sent"] = today
        status = self.feed.status()
        lines = [f"📋 **每日回報 {today}**" + (f"({self.site})" if self.site else "")]
        for item in status["instruments"]:
            age = "" if item["age"] is None else f",最後資料 {_age(item['age'])}前"
            lines.append(f"- {item['label']}:{item['text']}{age}")
        lines.append(f"目前有 {len(alerts)} 個問題。" if alerts else "目前沒有發現問題。")
        if self.backup_result:
            lines.append(f"備份:{self.backup_result.get('time')},失敗 {self.backup_result.get('failed')} 個。")
        lines.append("如果哪天沒收到這則回報,代表筆電、網路或這個程式可能停了。")
        self.notifier.add("\n".join(lines))

    def _write_status(self, alerts: List[dict], active: Dict[str, str], stamp: str, events: List[list]) -> None:
        try:
            self.status_dir.mkdir(parents=True, exist_ok=True)
            status = self.feed.status()
            lines = [f"最後更新:{stamp}(電腦 {os.environ.get('COMPUTERNAME', '')})", "如果這個時間太舊,代表筆電、網路或雲端硬碟停了。", ""]
            lines.append("【有問題】" if alerts else "【目前沒有發現問題】")
            for alert in alerts:
                lines += [f"- {alert['title']}(從 {active.get(alert['key'], stamp)})", f"  {alert['detail']}", "  建議:"]
                lines += [f"    {i}. {step}" for i, step in enumerate(alert["steps"], 1)]
            lines += ["", "【儀器】"]
            for item in status["instruments"]:
                age = "" if item["age"] is None else f",最後資料 {_age(item['age'])}前"
                lines.append(f"- {item['label']}:{item['text']}{age}")
            power = self.power()
            if power and power.get("has_battery"):
                lines += ["", f"【電源】{'接電' if power.get('on_ac') else '使用電池'},電量 {power.get('percent')}%"]
            if self.notifier.configured:
                state = "Webhook 網址格式不對" if self.notifier.invalid else (self.notifier.last_error or "正常")
                lines += ["", f"【Discord】{state},待送 {len(self.notifier.queue)} 則"]
            if self.backup_result:
                lines += ["", f"【備份】{self.backup_result.get('time')}:新增 {self.backup_result.get('copied')}、更新 {self.backup_result.get('updated')}、使用中 {self.backup_result.get('in_use')}、失敗 {self.backup_result.get('failed')}"]
            temporary = self.status_dir / "現場狀態.tmp"
            temporary.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
            os.replace(temporary, self.status_dir / "現場狀態.txt")
            if events:
                path = self.status_dir / "問題紀錄.csv"
                new = not path.exists()
                with open(path, "a", newline="", encoding="utf-8-sig") as handle:
                    writer = csv.writer(handle)
                    if new:
                        writer.writerow(["時間", "事件", "等級", "項目", "內容"])
                    writer.writerows(events)
        except OSError as error:
            print("無法寫入狀態檔:", error, flush=True)


def _age(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} 秒"
    if seconds < 5400:
        return f"{seconds / 60:.0f} 分鐘"
    return f"{seconds / 3600:.1f} 小時"
