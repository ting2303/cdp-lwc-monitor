"""模擬模式:寫假的儀器檔案,讓面板和通知走完整流程,展示每一種狀況的燈號。

資料都是假的,放在暫存資料夾;不會碰真實資料,不會上傳,也不會傳 Discord。
電源、開機時間、程式清單這些讀不到假值的項目,由這裡提供給 Watcher。
"""
from __future__ import annotations

import math
import random
import string
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .watch import DRIVE_PROCESS, PADS_PROCESS

SIZES = [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 16, 18, 20, 22, 24, 26, 28, 30, 32, 34, 36, 38, 40, 42, 44, 46, 48, 50]
AREA = 0.267
D11_SIZES = [round(0.253 * (35.15 / 0.253) ** (i / 30), 3) for i in range(31)]
NORMAL = {
    "cdp_on": True, "lwc_on": True, "d11_on": True, "number": None, "dump": 4.84, "laser_delta": 0.0, "laser_temp": 15.0,
    "pm": None, "pressure_rate": 0.0, "rh": 25.0, "d11_temp": 20.0, "cdp_pas": 10.0, "lwc_pas": 10.0, "pas_toggle": False,
    "lwc_pressure": 722.0, "clock_shift": 0, "internal_lag": 120, "on_ac": True, "percent": 100, "pads": True, "drive": True,
    "log": None, "backup": None, "min_free_gb": 0, "drive_gone": False, "roots": "all", "read_error": False, "reboot": False, "freeze": False,
}
COMM = "Error 5902 occurred at VISA_Read>CDP Serial Read"

# id, 分類, 名稱, 預期燈號, 應出現的通知, 說明, 和正常時不同的地方
SCENARIOS = [
    ("normal", "正常", "全部正常", "green", [], "三台都在收資料,沒有問題。", {}),
    ("cloud", "正常", "雲中觀測", "green", [], "雲經過,濃度高但都在合理範圍,不會通知。", {"number": 450.0}),
    ("comm", "資料中斷", "CDP/LWC 通訊失敗", "red", ["stale:CDP", "stale:LWC-300", "log_errors"], "線路鬆脫或儀器沒電,PADS 紀錄有 Error 5902。", {"cdp_on": False, "lwc_on": False, "log": "comm"}),
    ("stopped", "資料中斷", "有人按停止記錄", "red", ["stale:CDP", "stale:LWC-300"], "PADS 紀錄寫著 Recording stopped。", {"cdp_on": False, "lwc_on": False, "log": "stopped"}),
    ("closed", "資料中斷", "PADS 被關閉", "red", ["stale:CDP", "stale:LWC-300"], "PADS 程式不在執行。", {"cdp_on": False, "lwc_on": False, "pads": False, "log": "closed"}),
    ("hot_stopped", "資料中斷", "只有 LWC-300 停止", "red", ["stale:LWC-300"], "CDP 還有資料,LWC-300 沒有。", {"lwc_on": False}),
    ("d11_stopped", "資料中斷", "11-D 停止", "red", ["stale:11-D"], "11-D 軟體停了或儀器沒電。", {"d11_on": False}),
    ("pads_missing", "資料中斷", "找不到 PADS 檔案", "yellow", ["missing:CDP", "missing:LWC-300"], "資料夾裡沒有最近的檔案,例如還沒按記錄。", {"roots": "pads_empty"}),
    ("d11_missing", "資料中斷", "找不到 11-D 檔案", "yellow", ["missing:11-D"], "資料夾裡沒有最近的 11-D 檔案。", {"roots": "d11_empty"}),
    ("setup", "資料中斷", "還沒設定資料夾", "yellow", ["setup"], "第一次使用時的狀態。", {"roots": "unset"}),
    ("read_error", "資料中斷", "檔案讀取失敗", "yellow", ["read:CDP"], "檔案被其他程式鎖住或沒有權限。", {"read_error": True}),
    ("log_errors", "資料中斷", "資料正常但紀錄有錯誤", "yellow", ["log_errors"], "偶發通訊錯誤,資料還在進來。", {"log": "errors"}),
    ("battery", "電力和電腦", "筆電改用電池", "yellow", ["on_battery"], "電源線鬆脫或現場可能停電。", {"on_ac": False, "percent": 75}),
    ("battery_low", "電力和電腦", "電池快沒電", "red", ["on_battery"], "電量低於 30%,紅燈。", {"on_ac": False, "percent": 18}),
    ("outage", "電力和電腦", "現場停電", "red", ["on_battery", "stale:CDP", "stale:LWC-300", "stale:11-D"], "儀器全停,筆電靠電池撐著還能通知。", {"on_ac": False, "percent": 60, "cdp_on": False, "lwc_on": False, "d11_on": False}),
    ("reboot", "電力和電腦", "筆電重開機過", "yellow", ["reboot"], "可能斷電或 Windows 更新。", {"reboot": True}),
    ("dead", "電力和電腦", "筆電整台斷電", "red", [], "筆電自己發不出 Discord。畫面會顯示連不上,30 秒後回到正常。", {"freeze": True}),
    ("disk", "電力和電腦", "硬碟空間不足", "red", ["disk:"], "空間不足可能讓 PADS 停止存檔。", {"min_free_gb": 1e12}),
    ("drive_stopped", "上傳", "雲端硬碟程式沒開", "red", ["drive_stopped"], "資料留在筆電,不會上傳。", {"drive": False}),
    ("drive_gone", "上傳", "雲端硬碟不見了", "red", ["drive_missing"], "G 槽消失,常見於帳號登出。", {"drive_gone": True}),
    ("backup_failed", "上傳", "備份失敗", "red", ["backup_failed"], "雲端空間滿了或網路問題。", {"backup": {"failed": 3, "sample": "00CDP PBP.csv(模擬)", "time": "模擬"}}),
    ("backup_conflict", "上傳", "備份有衝突", "yellow", ["backup_conflict"], "雲端有內容不同的同名檔案,不會覆蓋。", {"backup": {"conflicts": 2, "time": "模擬"}}),
    ("fog", "儀器狀態", "鏡片起霧或結冰", "red", ["fog"], "沒有粒子時 Dump Spot 掉到 0.7 V。", {"number": 0.2, "dump": 0.7}),
    ("laser", "儀器狀態", "雷射電流異常", "yellow", ["env_laser"], "雷射電流偏離溫度和長期趨勢的預期。", {"laser_delta": 6.0}),
    ("laser_hot", "儀器狀態", "雷射過熱", "yellow", ["env_temp:laser_temp"], "雷射溫度超過過去的範圍。", {"laser_temp": 40.0}),
    ("d11_hot", "儀器狀態", "11-D 過熱", "yellow", ["env_temp:d11_temp"], "機箱太熱,可能是日照或通風不良。", {"d11_temp": 48.0}),
    ("pm", "背景環境", "PM2.5 異常高", "yellow", ["env_pm"], "附近燃燒、施工,或大範圍汙染。", {"pm": 95.0}),
    ("pressure", "背景環境", "氣壓變化很快", "yellow", ["env_pressure"], "天氣快速變化,或感測器異常。", {"pressure_rate": 0.4}),
    ("rh", "背景環境", "11-D 濕度高", "yellow", ["env_rh"], "進氣或機箱可能進水、結露。", {"rh": 75.0}),
    ("number", "背景環境", "CDP 濃度異常高", "yellow", ["env_number"], "噴霧測試、汙染源或水氣。", {"number": 4000.0}),
    ("pas_changed", "手動值", "流速被改動", "yellow", ["manual_pas"], "手動流速在短時間內改過,濃度和 LWC 會跳動。", {"pas_toggle": True}),
    ("pas_differ", "手動值", "兩台流速不同", "blue", ["manual_mismatch"], "只是提示,不會亮黃燈或紅燈。", {"lwc_pas": 7.0}),
    ("manual_pressure", "手動值", "手動壓力和實測不符", "yellow", ["manual_pressure"], "LWC-300 輸入 1000 hPa,實測 725 hPa。", {"lwc_pressure": 1000.0}),
    ("clock", "時間", "11-D 時間差一小時", "yellow", ["clock:11-D"], "時區或時鐘設定不同,三台對不起來。", {"clock_shift": 3600}),
    ("d11_clock", "時間", "11-D 內部時鐘偏差大", "blue", ["d11_internal"], "不影響對齊,維護時校正即可。", {"internal_lag": 900}),
]
BY_ID = {s[0]: s for s in SCENARIOS}
FREEZE_SECONDS = 30
AUTO_SECONDS = 35


def config_for(root: Path) -> dict:
    """判斷時間縮短,按下情境後幾十秒內就看得到結果。"""
    return {
        "pads_roots": [str(root / "pads")], "d11_roots": [str(root / "d11")], "refresh_seconds": 1, "rescan_seconds": 1, "history_hours": 3,
        "stale_seconds": 15, "watch_seconds": 2, "status_dir": str(root / "status"), "state_file": str(root / "watch_state.json"),
        "env_seconds": 20, "pressure_span": 60, "fog_seconds": 20, "site_name": "模擬測站", "min_free_gb": 0, "backup_jobs": [], "discord_webhook": "",
    }


def _unused_drive() -> str:
    for letter in reversed(string.ascii_uppercase):
        if not Path(f"{letter}:/").exists():
            return f"{letter}:/CDPLWC_模擬"
    return "Q:/CDPLWC_模擬"


class World:
    BY_ID_NAMES = frozenset(BY_ID)

    def __init__(self, root: Path, config: dict, baseline: float = 3700, end_offset: float = 0, normal: Optional[dict] = None):
        self.root = root
        self.normal = normal
        self.config = config
        self.lock = threading.RLock()
        self.state = dict(NORMAL)
        self.scenario = "normal"
        self.started = time.time()
        self.boot_time = time.time() - 3600
        self.auto = False
        self.next_auto = 0.0
        self.frozen_until = 0.0
        self.feed = None
        self.watcher = None
        self.stop_event = threading.Event()
        self.last_sid = 0.0
        self.d11_generation = 0
        self.d11_path: Optional[Path] = None
        self.last_row = time.time() - baseline - end_offset
        for name in ("pads", "d11", "empty"):
            (root / name).mkdir(parents=True, exist_ok=True)
        self._new_session()
        self._new_d11()
        self.write_until(time.time() - end_offset, force=True)

    def attach(self, feed, watcher) -> None:
        self.feed, self.watcher = feed, watcher
        original = feed.status

        def status() -> dict:
            result = original()
            if self.state["read_error"]:
                for item in result["instruments"]:
                    if item["label"] == "CDP":
                        item["code"], item["text"], item["errors"] = "error", "讀取失敗", ["00CDP PBP.csv:檔案被其他程式鎖住(模擬)"]
            return result

        feed.status = status

    # ---- 給 Watcher 的假值 --------------------------------------------
    def power(self) -> dict:
        return {"on_ac": self.state["on_ac"], "percent": self.state["percent"], "has_battery": True}

    def boot(self) -> float:
        return self.boot_time

    def processes(self) -> List[str]:
        return [name for name, on in ((PADS_PROCESS, self.state["pads"]), (DRIVE_PROCESS, self.state["drive"])) if on]

    # ---- 寫檔 ---------------------------------------------------------
    def _new_session(self) -> None:
        moment = max(time.time(), self.last_sid + 1)
        self.last_sid = moment
        now = datetime.fromtimestamp(moment)
        self.sid = now.strftime("%Y%m%d%H%M%S")
        folder = self.root / "pads" / now.strftime("%Y%m%d") / self.sid
        folder.mkdir(parents=True, exist_ok=True)
        self.cdp_path = folder / f"00CDP PBP{self.sid}.csv"
        self.lwc_path = folder / f"01LWC 300_{self.sid}.csv"
        self.log_path = folder / f"Log{self.sid}.txt"
        bins = ",".join(f"CDP Bin {i + 1}" for i in range(len(SIZES)))
        sizes = ",".join(str(s) for s in SIZES)
        self.cdp_path.write_bytes((
            f"[Instrument 1]\r\nSample Area (mm^2)={AREA}\r\nSizes=<{len(SIZES)}>{sizes}\r\n Offset from UTC = 08:00:00\r\n****\r\n"
            "End Seconds,Day of Year,Year,Status,Number Conc (#/cm^3),LWC (g/m^3),MVD (um),ED (um),Applied PAS (m/s),Dump Spot Monitor (V),Laser Current (mA),Laser Temp (C),"
            f"{bins}\r\n").encode("cp950"))
        self.lwc_path.write_bytes((
            " Offset from UTC = 08:00:00\r\n****\r\n"
            "End_Seconds,Day_of_Year,Year,Status,LWC-AveragedDAP,PAS(m/s),Static Pressure (mB),Ambient Temperature (C)\r\n").encode("cp950"))
        self._log("Recording started.")

    def _log(self, message: str) -> None:
        now = datetime.now()
        with open(self.log_path, "a", encoding="cp950", newline="") as handle:
            handle.write(f"{now:%H:%M:%S} {now:%Y%m%d}: {message}\r\n")

    def _new_d11(self) -> None:
        if self.d11_path is not None:
            self.d11_path.unlink(missing_ok=True)
        self.d11_generation += 1
        self.d11_path = self.root / "d11" / f"demo{self.d11_generation}.csv"
        channels = "\t".join(f"{s}um" for s in D11_SIZES)
        self.d11_path.write_text("Date\tPM1\tPM2.5\tPM4\tPM10\tTSP\tPres\trH_i\tTemp_i\tYear\tMon\tDay\tHr\tMin\tSec\t" + channels + "\n", encoding="cp950")

    def _number(self, t: float, state: dict) -> float:
        if state["number"] is not None:
            base = state["number"]
        else:
            base = max(0.0, 320 * math.sin(2 * math.pi * t / 600))
        return base * random.uniform(0.92, 1.08)

    def _laser(self, t: float, state: dict) -> float:
        model = (self.normal or {}).get("laser_model")
        temperature = state["laser_temp"]
        if not model:
            return 90 + state["laser_delta"]
        years = (t - model["t0"]) / (365.25 * 86400)
        return model["a"] + model["b"] * temperature + model["c"] * years + state["laser_delta"] + random.uniform(-0.1, 0.1)

    def _cdp_row(self, t: float, state: dict) -> str:
        moment = datetime.fromtimestamp(t)
        seconds = moment.hour * 3600 + moment.minute * 60 + moment.second
        number = self._number(t, state)
        pas = state["cdp_pas"]
        if state["pas_toggle"] and int(t / 5) % 2:
            pas = 8.0
        mvd = 9 + min(number, 800) / 60
        weights = [math.exp(-0.5 * (math.log(s / mvd) / 0.35) ** 2) for s in SIZES]
        total = sum(weights) or 1
        counts = [round(number * AREA * pas * w / total) for w in weights]
        lwc = number * 0.0012 * (mvd / 12) ** 3
        dump = state["dump"] + random.uniform(-0.02, 0.02)
        return f"{seconds}.5,{moment.timetuple().tm_yday},{moment.year},1,{number:.2f},{lwc:.4f},{mvd:.2f},{mvd * 0.9:.2f},{pas:g},{dump:.3f},{self._laser(t, state):.2f},{state['laser_temp'] + random.uniform(-0.1, 0.1):.2f}," + ",".join(str(c) for c in counts) + "\r\n"

    def _lwc_row(self, t: float, state: dict) -> str:
        moment = datetime.fromtimestamp(t)
        seconds = moment.hour * 3600 + moment.minute * 60 + moment.second
        value = self._number(t, state) * 0.0003 + random.uniform(-0.01, 0.01)
        return f"{seconds}.5,{moment.timetuple().tm_yday},{moment.year},1,{value:.4f},{state['lwc_pas']:g},{state['lwc_pressure']:g},10\r\n"

    def _d11_row(self, t: float, state: dict, since: float) -> str:
        moment = datetime.fromtimestamp(t + state["clock_shift"])
        inner = datetime.fromtimestamp(t + state["clock_shift"] - state["internal_lag"])
        pm = state["pm"] if state["pm"] is not None else 9 + 4 * math.sin(2 * math.pi * t / 1500)
        pm *= random.uniform(0.95, 1.05)
        pressure = 725 - state["pressure_rate"] * max(0.0, t - since) + random.uniform(-0.05, 0.05)
        counts = [round(pm * 400 * (0.253 / s) ** 2.6 * random.uniform(0.85, 1.15), 1) for s in D11_SIZES]
        values = [pm * 0.7, pm, pm * 1.3, pm * 1.7, pm * 2.0, pressure, state["rh"], state["d11_temp"], inner.year % 100, inner.month, inner.day, inner.hour, inner.minute, inner.second]
        return f"{moment:%Y-%m-%d %H:%M:%S}\t" + "\t".join(f"{v:.2f}" if isinstance(v, float) else str(v) for v in values) + "\t" + "\t".join(str(c) for c in counts) + "\n"

    def write_until(self, end: float, force: bool = False) -> None:
        """補上到 end 為止的資料,每秒一筆;11-D 每 2 秒一筆。"""
        with self.lock:
            state, since = self.state, self.started
            cdp, lwc, d11 = [], [], []
            t = self.last_row + 1
            while t <= end:
                if state["cdp_on"] or force:
                    cdp.append(self._cdp_row(t, state))
                if state["lwc_on"] or force:
                    lwc.append(self._lwc_row(t, state))
                if (state["d11_on"] or force) and int(t) % 2 == 0:
                    d11.append(self._d11_row(t, state, since))
                self.last_row = t
                t += 1
            for path, rows, encoding in ((self.cdp_path, cdp, "ascii"), (self.lwc_path, lwc, "ascii"), (self.d11_path, d11, "cp950")):
                if rows:
                    with open(path, "ab") as handle:
                        handle.write("".join(rows).encode(encoding))

    # ---- 情境 ---------------------------------------------------------
    def apply(self, scenario: str, catch_up: bool = True) -> None:
        item = BY_ID[scenario]
        with self.lock:
            old = self.state
            if catch_up:
                self.write_until(time.time())
            self.state = {**NORMAL, **item[6]}
            self.scenario = scenario
            self.started = time.time()
            self.frozen_until = time.time() + FREEZE_SECONDS if self.state["freeze"] else 0.0
            state = self.state
            self._new_session()
            if state["log"] == "comm":
                for _ in range(3):
                    self._log(COMM)
            elif state["log"] == "errors":
                self._log(COMM)
                self._log("Recording resumed.")
            elif state["log"] == "stopped":
                self._log("Recording stopped.")
            elif state["log"] == "closed":
                self._log("Recording stopped.")
                self._log("PADS program stopped.")
            shifted = state["clock_shift"] != old["clock_shift"]
            if shifted:
                self._new_d11()
            pads, d11 = [str(self.root / "pads")], [str(self.root / "d11")]
            empty = [str(self.root / "empty")]
            roots = {"all": (pads, d11), "pads_empty": (empty, d11), "d11_empty": (pads, empty), "unset": ([], [])}[state["roots"]]
            if self.feed is not None and (shifted or state["roots"] != old["roots"]):
                self.config["pads_roots"], self.config["d11_roots"] = list(roots[0]), list(roots[1])
                self.feed.set_roots(roots[0], roots[1])
            self.config["min_free_gb"] = state["min_free_gb"]
            if state["reboot"]:
                self.boot_time = time.time() - 60
            if self.watcher is not None:
                self.watcher.state.pop("reboot_at", None)
                if state["reboot"]:
                    self.watcher.state["boot"] = self.boot_time - 3600
                self.watcher.backup_result = state["backup"]
                self.watcher.status_dir = Path(_unused_drive()) if state["drive_gone"] else Path(self.config["status_dir"])

    def set_auto(self, on: bool) -> None:
        with self.lock:
            self.auto = on
            self.next_auto = time.time() + AUTO_SECONDS
            if on:
                self.apply(SCENARIOS[0][0])

    def frozen(self) -> bool:
        return time.time() < self.frozen_until

    def describe(self) -> dict:
        groups: Dict[str, list] = {}
        for sid, group, label, color, expect, note, _ in SCENARIOS:
            groups.setdefault(group, []).append({"id": sid, "label": label, "color": color, "note": note})
        with self.lock:
            return {"groups": [{"name": k, "items": v} for k, v in groups.items()], "current": self.scenario, "auto": self.auto, "stale_seconds": self.config["stale_seconds"], "env_seconds": self.config["env_seconds"]}

    def step(self) -> None:
        now = time.time()
        with self.lock:
            self.write_until(now)
            if self.state["freeze"] and not self.frozen():
                self.apply("normal")
            elif self.auto and now >= self.next_auto:
                ids = [s[0] for s in SCENARIOS]
                self.apply(ids[(ids.index(self.scenario) + 1) % len(ids)])
                self.next_auto = now + AUTO_SECONDS

    def run(self) -> None:
        while not self.stop_event.wait(1.0):
            try:
                self.step()
            except OSError as error:
                print("模擬寫檔失敗:", error, flush=True)

    def start(self) -> None:
        threading.Thread(target=self.run, daemon=True, name="demo").start()

    def stop(self) -> None:
        self.stop_event.set()
