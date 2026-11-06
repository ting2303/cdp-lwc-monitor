"""即時讀取 PADS 與 11-D 持續寫入的檔案。

只開啟、讀取新增的部分、立刻關閉,不寫入也不鎖定來源檔案。
寫到一半的最後一行會留到下次再處理。
"""
from __future__ import annotations

import calendar
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from .formats import BIN_COLUMN, CDP_COLUMNS, ENCODING, LWC_COLUMNS, MARKER, as_float, instrument_kind, parse_key_values, parse_sizes, session_id
from .store import d11_edges

CDP_FIELDS = ["number_cm3", "lwc_gm3", "mvd_um", "ed_um", "pas_ms", "dump_spot_v", "laser_ma", "laser_temp_c"]
LWC_FIELDS = ["lwc_gm3", "pas_ms", "pressure_mb", "temperature_c"]
D11_MASS = ["PM1", "PM2.5", "PM4", "PM10", "TSP"]
D11_LOG = ["Pres", "rH_i", "Temp_i"]
D11_CLOCK = ["Year", "Mon", "Day", "Hr", "Min", "Sec"]
_UM = re.compile(r"^([\d.]+)\s*um$", re.I)
_STAMP_12H = re.compile(r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})\s+(上午|下午|AM|PM)\s+(\d{1,2}):(\d{2}):(\d{2})$", re.I)
_STAMP_12H_END = re.compile(r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})\s+(\d{1,2}):(\d{2}):(\d{2})\s+(AM|PM)$", re.I)
_STAMP_24H = re.compile(r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})[ T](\d{1,2}):(\d{2}):(\d{2})")
READ_LIMIT = 16 * 1024 * 1024  # 每次最多讀這麼多,避免一次卡住太久


def local_now() -> float:
    """當地時間當作 UTC 數字,和資料的時間基準一致。"""
    now = time.time()
    return calendar.timegm(time.localtime(now)) + (now % 1)


def parse_stamp(text: str) -> Optional[float]:
    text = text.strip().strip('"')
    match = _STAMP_12H.match(text)
    try:
        if match:
            y, mo, d, period, h, mi, s = match.groups()
        else:
            match = _STAMP_12H_END.match(text)
            if match:
                y, mo, d, h, mi, s, period = match.groups()
            else:
                match = _STAMP_24H.match(text)
                if not match:
                    return None
                y, mo, d, h, mi, s = match.groups()
                return float(calendar.timegm((int(y), int(mo), int(d), int(h), int(mi), int(s))))
        hour = int(h) % 12 + (12 if period.upper() in ("下午", "PM") else 0)
        return float(calendar.timegm((int(y), int(mo), int(d), hour, int(mi), int(s))))
    except (ValueError, OverflowError):
        return None


class Buffer:
    """依時間附加的數值表,超過保留時間會自動丟棄。"""

    def __init__(self, fields: List[str], extra: int = 0):
        self.fields = fields
        self.columns = len(fields) + extra
        self.time = np.empty(4096, "float64")
        self.values = np.empty((4096, self.columns), "float32")
        self.size = 0

    def append(self, times: List[float], rows: List[List[float]]) -> None:
        count = len(times)
        if not count:
            return
        needed = self.size + count
        if needed > len(self.time):
            capacity = max(needed, len(self.time) * 2)
            self.time = np.resize(self.time, capacity)
            self.values = np.resize(self.values, (capacity, self.columns))
        self.time[self.size:needed] = times
        self.values[self.size:needed] = rows
        self.size = needed

    def trim(self, cutoff: float) -> None:
        keep = int(np.searchsorted(self.time[: self.size], cutoff))
        if keep > 0:
            self.size -= keep
            self.time[: self.size] = self.time[keep: keep + self.size]
            self.values[: self.size] = self.values[keep: keep + self.size]

    def sort_if_needed(self) -> None:
        """多個檔案輪流附加時,時間可能交錯。"""
        if self.size > 1 and np.any(np.diff(self.time[: self.size]) < 0):
            order = np.argsort(self.time[: self.size], kind="stable")
            self.time[: self.size] = self.time[: self.size][order]
            self.values[: self.size] = self.values[: self.size][order]

    def view(self) -> Tuple[np.ndarray, np.ndarray]:
        return self.time[: self.size], self.values[: self.size]

    def last_time(self) -> Optional[float]:
        return float(self.time[self.size - 1]) if self.size else None

    def clear(self) -> None:
        self.size = 0


@dataclass
class Tail:
    """記住讀到哪裡。檔案變短就從頭讀。"""

    path: Path
    offset: int = 0
    pending: bytes = b""
    error: Optional[str] = None

    def read_lines(self, start_at: Optional[int] = None) -> List[str]:
        try:
            with open(self.path, "rb") as handle:
                size = os.fstat(handle.fileno()).st_size
                if size < self.offset:
                    self.offset = 0
                    self.pending = b""
                if start_at is not None and start_at > self.offset:
                    # 跳過太舊的資料,從下一行開始
                    self.offset = start_at
                    self.pending = b""
                    handle.seek(self.offset)
                    handle.readline()
                    self.offset = handle.tell()
                handle.seek(self.offset)
                chunk = handle.read(READ_LIMIT)
            self.error = None
        except OSError as error:
            self.error = str(error)
            return []
        self.offset += len(chunk)
        data = self.pending + chunk
        complete, separator, rest = data.rpartition(b"\n")
        self.pending = rest
        if not separator:
            return []
        return complete.decode(ENCODING, errors="replace").split("\n")

    def remaining(self) -> bool:
        try:
            return self.path.stat().st_size > self.offset
        except OSError:
            return False


@dataclass
class PadsFile:
    kind: str
    tail: Tail
    header: List[str] = field(default_factory=list)
    columns: Optional[Dict[str, int]] = None
    bin_indexes: List[int] = field(default_factory=list)
    settings: Dict[str, str] = field(default_factory=dict)
    positioned: bool = False
    bad_lines: int = 0


@dataclass
class D11File:
    tail: Tail
    header_found: bool = False
    delimiter: str = "\t"
    channels: List[Tuple[float, int]] = field(default_factory=list)
    mass: Dict[str, int] = field(default_factory=dict)
    log: Dict[str, int] = field(default_factory=dict)
    unit: str = ""
    positioned: bool = False
    bad_lines: int = 0
    clock: Dict[str, int] = field(default_factory=dict)
    internal_offset: Optional[float] = None


def _clean_name(raw: str) -> str:
    return raw.strip().strip('"').split(" [")[0].strip()


class Feed:
    def __init__(self, config: dict):
        self.pads_roots = [Path(p) for p in config.get("pads_roots", [])]
        self.d11_roots = [Path(p) for p in config.get("d11_roots", [])]
        self.refresh = float(config.get("refresh_seconds", 10))
        self.rescan = float(config.get("rescan_seconds", 60))
        self.history = float(config.get("history_hours", 24)) * 3600
        self.stale = float(config.get("stale_seconds", 60))
        self.lock = threading.Lock()
        self.cdp: Optional[Buffer] = None
        self.cdp_edges: Optional[np.ndarray] = None
        self.hot = Buffer(LWC_FIELDS)
        self.d11_spec: Optional[Buffer] = None
        self.d11_edges: Optional[np.ndarray] = None
        self.d11_mass = Buffer(D11_MASS)
        self.d11_log = Buffer(D11_LOG)
        self.pads: Dict[str, PadsFile] = {}
        self.d11_files: Dict[str, D11File] = {}
        self.last_scan = 0.0
        self.last_poll: Optional[float] = None
        self.ready = False
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.external_notices: List[str] = []
        self.pending_roots: Optional[Tuple[List[Path], List[Path]]] = None
        self.thread: Optional[threading.Thread] = None

    # ---- 掃描 -------------------------------------------------------
    def _recent(self, path: Path, now_epoch: float) -> bool:
        try:
            return now_epoch - path.stat().st_mtime <= self.history
        except OSError:
            return False

    def scan(self) -> None:
        now_epoch = time.time()
        for root in self.pads_roots:
            if not root.is_dir():
                continue
            for folder, dirs, names in os.walk(root):
                # 螢幕截圖等資料夾不需要看
                dirs[:] = [d for d in dirs if "螢幕擷取" not in d]
                for name in names:
                    kind = instrument_kind(name)
                    if kind not in ("cdp", "lwc"):
                        continue
                    path = Path(folder) / name
                    if str(path) not in self.pads and self._recent(path, now_epoch):
                        self.pads[str(path)] = PadsFile(kind, Tail(path))
        for root in self.d11_roots:
            if not root.is_dir():
                continue
            for folder, dirs, names in os.walk(root):
                dirs[:] = [d for d in dirs if "螢幕擷取" not in d]
                for name in names:
                    lower = name.lower()
                    if not lower.endswith((".dat", ".csv")) or lower.endswith(("-dm.dat", "-dm.csv")):
                        continue
                    if instrument_kind(name) or lower.endswith(("shared.csv", "_pbp.csv")):
                        continue
                    path = Path(folder) / name
                    if str(path) not in self.d11_files and self._recent(path, now_epoch):
                        self.d11_files[str(path)] = D11File(Tail(path))
        self.last_scan = time.monotonic()

    # ---- PADS -------------------------------------------------------
    def _read_pads(self, item: PadsFile, now: float) -> None:
        start_at = None
        if item.columns is not None and not item.positioned:
            # 檔頭讀完後,只保留時間內的資料,不必解析整個檔案
            item.positioned = True
            try:
                size = item.tail.path.stat().st_size
                estimate = int(self.history * 900 * 1.2)
                if size - item.tail.offset > estimate:
                    start_at = size - estimate
            except OSError:
                pass
        lines = item.tail.read_lines(start_at)
        if not lines:
            return
        times: List[float] = []
        rows: List[List[float]] = []
        mapping = LWC_COLUMNS if item.kind == "lwc" else CDP_COLUMNS
        for raw in lines:
            line = raw.rstrip("\r")
            if item.columns is None:
                if not item.settings and line.startswith(MARKER):
                    item.settings = parse_key_values(item.header) or {"_": ""}
                    continue
                if item.settings:
                    names = [c.strip() for c in line.split(",")]
                    item.columns = {mapping[n]: i for i, n in enumerate(names) if n in mapping}
                    bins = sorted(((int(BIN_COLUMN.match(n).group(1)), i) for i, n in enumerate(names) if BIN_COLUMN.match(n)), key=lambda x: x[0])
                    item.bin_indexes = [i for _, i in bins]
                    self._prepare_cdp(item)
                    continue
                item.header.append(line)
                continue
            parsed = self._parse_pads(item, line)
            if parsed is None:
                if line.strip():
                    item.bad_lines += 1
                continue
            times.append(parsed[0])
            rows.append(parsed[1])
        if item.kind == "cdp" and self.cdp is not None:
            self.cdp.append(times, rows)
        elif item.kind == "lwc":
            self.hot.append(times, rows)

    def _prepare_cdp(self, item: PadsFile) -> None:
        if item.kind != "cdp":
            return
        sizes = parse_sizes(item.settings)
        if not sizes or len(sizes) != len(item.bin_indexes):
            return
        edges = np.r_[2.0, np.array(sizes, float)]
        if self.cdp_edges is None or len(edges) != len(self.cdp_edges) or not np.allclose(edges, self.cdp_edges):
            self.cdp_edges = edges
            self.cdp = Buffer(CDP_FIELDS, extra=len(sizes))

    def _parse_pads(self, item: PadsFile, line: str) -> Optional[Tuple[float, List[float]]]:
        fields = line.split(",")
        columns = item.columns
        try:
            year = int(float(fields[columns["year"]]))
            day = int(float(fields[columns["day_of_year"]]))
            seconds = float(fields[columns["end_seconds"]])
            stamp = calendar.timegm((year, 1, 1, 0, 0, 0)) + (day - 1) * 86400 + seconds
        except (KeyError, IndexError, ValueError, OverflowError):
            return None

        def number(name: str) -> float:
            try:
                return float(fields[columns[name]])
            except (KeyError, IndexError, ValueError):
                return float("nan")

        if item.kind == "lwc":
            return stamp, [number(n) for n in LWC_FIELDS]
        if self.cdp is None:
            return None
        values = [number(n) for n in CDP_FIELDS]
        try:
            counts = np.array([float(fields[i]) for i in item.bin_indexes], "float64")
        except (IndexError, ValueError):
            return None
        area = as_float(item.settings.get("Sample Area (mm^2)")) or float("nan")
        pas = values[CDP_FIELDS.index("pas_ms")]
        volume = area * pas if pas > 0 else float("nan")
        spectrum = counts / volume / np.log10(self.cdp_edges[1:] / self.cdp_edges[:-1])
        return stamp, values + spectrum.tolist()

    # ---- 11-D -------------------------------------------------------
    def _d11_header(self, item: D11File, line: str) -> None:
        if line.lower().startswith("unit:"):
            item.unit = line.partition(":")[2].strip().lower()
            return
        delimiter = "\t" if "\t" in line else ","
        names = line.split(delimiter)
        channels = []
        mass, log, clock = {}, {}, {}
        for index, raw in enumerate(names):
            name = _clean_name(raw)
            match = _UM.match(name)
            if match:
                channels.append((float(match.group(1)), index))
            elif name in D11_MASS:
                mass[name] = index
            elif name in D11_LOG:
                log[name] = index
            if name in D11_CLOCK:
                clock[name] = index
        if channels or mass or log:
            item.header_found = True
            item.delimiter = delimiter
            is_counts = "dm" not in item.unit and ("ug" not in item.unit or "count" in item.unit)
            item.channels = channels if is_counts else []
            item.mass = mass
            item.log = log
            item.clock = clock if len(clock) == len(D11_CLOCK) else {}
            if item.channels:
                edges = d11_edges([c for c, _ in item.channels])
                if self.d11_edges is None or len(edges) != len(self.d11_edges):
                    self.d11_edges = edges
                    self.d11_spec = Buffer([], extra=len(item.channels))

    def _read_d11(self, item: D11File) -> None:
        start_at = None
        if item.header_found and not item.positioned:
            item.positioned = True
            try:
                size = item.tail.path.stat().st_size
                estimate = int(self.history / 6 * 900 * 1.2)
                if size - item.tail.offset > estimate:
                    start_at = size - estimate
            except OSError:
                pass
        lines = item.tail.read_lines(start_at)
        spec_t, spec_r, mass_t, mass_r, log_t, log_r = [], [], [], [], [], []

        def number(fields: List[str], index: int) -> float:
            try:
                return float(fields[index].strip().rstrip("A"))
            except (IndexError, ValueError):
                return float("nan")

        for raw in lines:
            line = raw.rstrip("\r").lstrip("﻿")
            if not item.header_found:
                self._d11_header(item, line)
                continue
            fields = line.split(item.delimiter)
            stamp = parse_stamp(fields[0]) if fields else None
            if stamp is None:
                if line.strip() and not line.startswith("["):
                    item.bad_lines += 1
                continue
            if item.channels and self.d11_spec is not None:
                # 檔案是每通道 counts/L
                values = np.array([number(fields, i) for _, i in item.channels], "float64")
                spec_t.append(stamp)
                spec_r.append((values / 1000.0 / np.log10(self.d11_edges[1:] / self.d11_edges[:-1])).tolist())
            if item.mass:
                mass_t.append(stamp)
                mass_r.append([number(fields, item.mass[n]) if n in item.mass else float("nan") for n in D11_MASS])
            if item.log:
                log_t.append(stamp)
                log_r.append([number(fields, item.log[n]) if n in item.log else float("nan") for n in D11_LOG])
            if item.clock:
                try:
                    y, mo, d, h, mi, s = (int(float(fields[item.clock[n]])) for n in D11_CLOCK)
                    internal = parse_stamp(f"{y + 2000 if y < 100 else y}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d}")
                    if internal is not None:
                        item.internal_offset = stamp - internal
                except (IndexError, ValueError):
                    pass
        if self.d11_spec is not None:
            self.d11_spec.append(spec_t, spec_r)
        self.d11_mass.append(mass_t, mass_r)
        self.d11_log.append(log_t, log_r)

    # ---- 主迴圈 -----------------------------------------------------
    def set_roots(self, pads: List[str], d11: List[str]) -> None:
        """換資料夾,不必重新啟動;在讀檔執行緒的下一輪生效。"""
        self.pending_roots = ([Path(p) for p in pads], [Path(p) for p in d11])
        self.wake.set()

    def _apply_roots(self) -> None:
        pending, self.pending_roots = self.pending_roots, None
        if pending is None:
            return
        with self.lock:
            self.pads_roots, self.d11_roots = pending
            self.pads.clear()
            self.d11_files.clear()
            self.cdp = None
            self.cdp_edges = None
            self.d11_spec = None
            self.d11_edges = None
            for buffer in (self.hot, self.d11_mass, self.d11_log):
                buffer.clear()
            self.last_scan = 0.0

    def poll(self) -> None:
        self._apply_roots()
        if time.monotonic() - self.last_scan >= self.rescan or not self.last_scan:
            self.scan()
        now = local_now()
        with self.lock:
            for item in self.pads.values():
                self._read_pads(item, now)
            for item in self.d11_files.values():
                self._read_d11(item)
            for buffer in (self.cdp, self.hot, self.d11_spec, self.d11_mass, self.d11_log):
                if buffer is not None:
                    buffer.sort_if_needed()
                    buffer.trim(now - self.history)
            self.last_poll = now
            self.ready = True

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.poll()
                busy = any(i.tail.remaining() for i in self.pads.values()) or any(i.tail.remaining() for i in self.d11_files.values())
            except Exception as error:  # 面板不能因為單一檔案異常就停止
                print("讀取時發生錯誤:", error, flush=True)
                busy = False
            self.wake.wait(0.2 if busy else self.refresh)
            self.wake.clear()

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True, name="live-feed")
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=5)

    # ---- 查詢 -------------------------------------------------------
    def status(self) -> dict:
        now = local_now()

        def state(label: str, last: Optional[float], files: int, errors: List[str], bad: int) -> dict:
            age = None if last is None else max(0.0, now - last)
            if errors and last is None:
                code, text = "error", "檔案讀取失敗"
            elif not files:
                code, text = "missing", "找不到檔案"
            elif last is None:
                code, text = "waiting", "等待資料"
            elif age > self.stale:
                code, text = "stale", "資料已停止更新"
            else:
                code, text = "ok", "更新中"
            return {"label": label, "code": code, "text": text, "age": age, "files": files, "errors": errors[:3], "bad_lines": bad}

        with self.lock:
            cdp_files = [i for i in self.pads.values() if i.kind == "cdp"]
            hot_files = [i for i in self.pads.values() if i.kind == "lwc"]
            d11_last = max((b.last_time() or 0 for b in (self.d11_spec, self.d11_mass, self.d11_log) if b is not None), default=0) or None
            instruments = [
                state("CDP", self.cdp.last_time() if self.cdp else None, len(cdp_files), [str(i.tail.error) for i in cdp_files if i.tail.error], sum(i.bad_lines for i in cdp_files)),
                state("LWC-300", self.hot.last_time(), len(hot_files), [str(i.tail.error) for i in hot_files if i.tail.error], sum(i.bad_lines for i in hot_files)),
                state("11-D", d11_last, len(self.d11_files), [str(i.tail.error) for i in self.d11_files.values() if i.tail.error], sum(i.bad_lines for i in self.d11_files.values())),
            ]
            notices = self._notices(now)
            for item in instruments:
                roots = self.d11_roots if item["label"] == "11-D" else self.pads_roots
                if item["code"] == "missing" and not roots:
                    item["code"], item["text"] = "unset", "未設定資料夾"
        return {"instruments": instruments, "notices": notices, "ready": self.ready, "now": now, "stale_seconds": self.stale}

    def clock_check(self, active_seconds: float = 180) -> dict:
        """PADS 和 11-D 是不同軟體:檢查檔案還在更新時,最新資料的時間和電腦時鐘差多少。

        兩邊都以筆電時鐘為準才對得起來。回傳 {"PADS": 秒, "11-D": 秒, "internal": 11-D 內部時鐘差}。
        """
        now, wall = local_now(), time.time()
        out: dict = {}
        with self.lock:
            groups = (
                ("PADS", [i for i in self.pads.values() if i.kind in ("cdp", "lwc")], [self.cdp, self.hot]),
                ("11-D", list(self.d11_files.values()), [self.d11_spec, self.d11_mass, self.d11_log]),
            )
            for label, items, buffers in groups:
                latest = 0.0
                for item in items:
                    try:
                        latest = max(latest, item.tail.path.stat().st_mtime)
                    except OSError:
                        continue
                stamps = [b.view()[0][-1] for b in buffers if b is not None and b.size]
                if stamps and wall - latest <= active_seconds:
                    out[label] = float((now - (wall - latest)) - max(stamps))
            offsets = [i.internal_offset for i in self.d11_files.values() if i.internal_offset is not None]
            if offsets:
                out["internal"] = float(offsets[-1])
        return out

    def recent_environment(self, seconds: float = 900, pressure_span: float = 1800) -> dict:
        """最近一段時間的環境和儀器狀態中位數,給異常判斷用。"""
        now = local_now()
        out: dict = {}

        def median(buffer: Optional[Buffer], fields: List[str], name: str, span: float) -> Optional[np.ndarray]:
            if buffer is None or not buffer.size:
                return None
            times, values = buffer.view()
            low = int(np.searchsorted(times, now - span))
            column = values[low:, fields.index(name)]
            column = column[np.isfinite(column)]
            return column if len(column) else None

        with self.lock:
            for key, buffer, fields, name in (
                ("pm25", self.d11_mass, D11_MASS, "PM2.5"),
                ("rh", self.d11_log, D11_LOG, "rH_i"),
                ("d11_temp", self.d11_log, D11_LOG, "Temp_i"),
                ("number", self.cdp, CDP_FIELDS, "number_cm3"),
                ("laser", self.cdp, CDP_FIELDS, "laser_ma"),
                ("laser_temp", self.cdp, CDP_FIELDS, "laser_temp_c"),
                ("cdp_pas", self.cdp, CDP_FIELDS, "pas_ms"),
                ("hot_pas", self.hot, LWC_FIELDS, "pas_ms"),
                ("hot_pressure", self.hot, LWC_FIELDS, "pressure_mb"),
            ):
                column = median(buffer, fields, name, seconds)
                if column is not None:
                    out[key] = float(np.median(column))
                    if key == "cdp_pas":
                        out["cdp_pas_values"] = sorted({round(float(v), 2) for v in column})
            pressure = median(self.d11_log, D11_LOG, "Pres", pressure_span)
            if pressure is not None and len(pressure) >= 10:
                out["pressure"] = float(np.median(pressure[-10:]))
                out["pressure_change"] = float(np.median(pressure[-10:]) - np.median(pressure[:10]))
        return out

    def dump_low(self, seconds: float) -> Optional[dict]:
        """最近一段時間,無粒子時 Dump Spot 是否持續偏低(手冊:可能起霧、結冰或雷射過熱)。"""
        now = local_now()
        with self.lock:
            if self.cdp is None or not self.cdp.size:
                return None
            times, values = self.cdp.view()
            low = int(np.searchsorted(times, now - seconds))
            recent = values[low:]
            if len(recent) < min(seconds * 0.5, 60):
                return None
            quiet = recent[:, CDP_FIELDS.index("number_cm3")] < 1
            dump = recent[:, CDP_FIELDS.index("dump_spot_v")]
            good = quiet & np.isfinite(dump)
            if good.sum() < 0.5 * len(recent):
                return None
            median = float(np.median(dump[good]))
        return {"median": median} if median < 4 else None

    def _notices(self, now: float) -> List[str]:
        out: List[str] = list(self.external_notices)
        if self.cdp is not None and self.cdp.size:
            times, values = self.cdp.view()
            recent = values[times >= now - 120]
            if len(recent):
                number = recent[:, CDP_FIELDS.index("number_cm3")]
                dump = recent[:, CDP_FIELDS.index("dump_spot_v")]
                quiet = number < 1
                if quiet.sum() >= 30 and np.nanmedian(dump[quiet]) < 4:
                    out.append(f"CDP 無粒子時 Dump Spot 為 {np.nanmedian(dump[quiet]):.2f} V(通常約 4.84 V),手冊提到可能是鏡窗起霧、結冰或雷射過熱")
                pas = np.nanmedian(recent[:, CDP_FIELDS.index("pas_ms")])
                if self.hot.size:
                    hot_times, hot_values = self.hot.view()
                    hot_recent = hot_values[hot_times >= now - 120]
                    if len(hot_recent):
                        hot_pas = np.nanmedian(hot_recent[:, LWC_FIELDS.index("pas_ms")])
                        if np.isfinite(pas) and np.isfinite(hot_pas) and abs(pas - hot_pas) > 0.01:
                            out.append(f"流速設定不同:CDP {pas:g}、LWC-300 {hot_pas:g} m/s")
        return out

    def window(self, span: float, width: int) -> dict:
        width = min(max(width, 50), 1600)
        end = int(np.ceil(local_now()))
        start = end - int(span)
        step = max(6, int(np.ceil(span / width)))
        count = int(np.ceil((end - start) / step))

        def mean(times: np.ndarray, values: np.ndarray) -> np.ndarray:
            index = ((times - start) // step).astype(int)
            good = np.isfinite(values) & (index >= 0) & (index < count)
            totals = np.bincount(index[good], weights=values[good], minlength=count)
            counts = np.bincount(index[good], minlength=count)
            with np.errstate(invalid="ignore", divide="ignore"):
                return totals / counts

        def clean(values: np.ndarray) -> List[Optional[float]]:
            return [None if not np.isfinite(v) else float(f"{v:.4g}") for v in values.tolist()]

        with self.lock:
            cdp = None
            if self.cdp is not None:
                times, values = self.cdp.view()
                low = int(np.searchsorted(times, start))
                if low < len(times):
                    t, v = times[low:], values[low:].astype("float64")
                    series = {name: clean(mean(t, v[:, i])) for i, name in enumerate(CDP_FIELDS)}
                    series["hotwire_gm3"] = [None] * count
                    cdp = {"series": series, "spectrum": [clean(mean(t, v[:, len(CDP_FIELDS) + c])) for c in range(v.shape[1] - len(CDP_FIELDS))]}
            hot_times, hot_values = self.hot.view()
            low = int(np.searchsorted(hot_times, start))
            if low < len(hot_times):
                hot = clean(mean(hot_times[low:], hot_values[low:, 0].astype("float64")))
                if cdp is None:
                    cdp = {"series": {n: [None] * count for n in CDP_FIELDS}, "spectrum": None}
                cdp["series"]["hotwire_gm3"] = hot
            d11 = None
            series_d: Dict[str, List[Optional[float]]] = {}
            for buffer, names in ((self.d11_mass, D11_MASS), (self.d11_log, D11_LOG)):
                times, values = buffer.view()
                low = int(np.searchsorted(times, start))
                for i, name in enumerate(names):
                    series_d[name] = clean(mean(times[low:], values[low:, i].astype("float64"))) if low < len(times) else [None] * count
            spectrum = None
            if self.d11_spec is not None:
                times, values = self.d11_spec.view()
                low = int(np.searchsorted(times, start))
                if low < len(times):
                    spectrum = [clean(mean(times[low:], values[low:, c].astype("float64"))) for c in range(values.shape[1])]
            if spectrum is not None or any(any(v is not None for v in s) for s in series_d.values()):
                d11 = {"series": series_d, "spectrum": spectrum}
            cdp_edges = self.cdp_edges.tolist() if self.cdp_edges is not None else None
            d11_edge_list = self.d11_edges.tolist() if self.d11_edges is not None else None
        return {"start": start, "end": end, "step": step, "count": count, "cdp": cdp, "d11": d11, "sessions": [], "cdp_edges": cdp_edges, "d11_edges": d11_edge_list, "live": True, "status": self.status()}
