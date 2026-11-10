"""資料校正層:原始檔和快取都不改,校正結果另外算,每一項都記下原因。

分三種處理:
- 重算:原因明確、公式已用資料驗證。例如 LWC-300 用無雲時的功率重算基線。
- 只標記:知道有問題但無法還原,或還不確定原因。例如鏡片起霧、11-D 最小通道偏高。
- 等你確認:需要資料裡沒有的資訊。例如實際風速;提供 corrections_config.json 後才會套用。

python -m cpd_lwc_qc.correct --cache cache --out output/corrected
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

CLEAR_NUMBER = 1.0
HEATER_POWER = 1.5  # W;正常約 2.4 W,過去 −3 g/m³ 的假訊號都是功率接近 0
DAP_MIN = 1.0  # W;2024-03-01 DAP-Averaged 變成 0,LWC 整段 +3.8
DUMP_LOW = 4.0
LASER_ON = 40.0
START_MINUTES = 2
SEGMENT_GAP = 1800
BASELINE_WINDOW = 60  # 分鐘,前後各這麼長
# 離無雲時段 30 分鐘內,每天的 LWC-300÷CDP 比值分散度從 0.74 降到 0.26;超過 60 分鐘反而讓相關變差,所以不重算。
# 比較結果見 investigations/history_oddities/README.md
BASELINE_MAX_GAP = 30
BASELINE_STEP = 5
BASELINE_MIN_POINTS = 5
FINE_RATIO = 5.0
FINE_PM = 3.43
FINE_MIN_PER_LITER = 10.0
PM_SPIKE_FACTOR = 10.0
PM_SPIKE_MIN = 50.0
W_PER_SPEED = 0.0949  # W per (g/m³)/(m/s);過去資料 0.0933–0.0949

HEATER, DAP_BAD, START, NO_BASELINE = 1, 2, 4, 8
FOG, TEST_PAS, PAS_OVERRIDE = 1, 2, 4
D11_ERR, FINE, PM_SPIKE = 1, 2, 4

LWC_FLAGS = {HEATER: "加熱線沒功率", DAP_BAD: "乾空氣功率異常", START: "剛開始記錄", NO_BASELINE: "沒有基線可重算"}
CDP_FLAGS = {FOG: "鏡片可能起霧", TEST_PAS: "流速不像實際觀測", PAS_OVERRIDE: "已套用指定流速"}
D11_FLAGS = {D11_ERR: "11-D 錯誤碼", FINE: "0.253 µm 偏高", PM_SPIKE: "PM 瞬間尖峰"}


@dataclass
class Corrections:
    lwc_time: np.ndarray = field(default_factory=lambda: np.empty(0, "int64"))
    lwc_raw: np.ndarray = field(default_factory=lambda: np.empty(0))
    lwc_corrected: np.ndarray = field(default_factory=lambda: np.empty(0))
    lwc_flags: np.ndarray = field(default_factory=lambda: np.empty(0, "int32"))
    dry_power: np.ndarray = field(default_factory=lambda: np.empty(0))
    cdp_time: np.ndarray = field(default_factory=lambda: np.empty(0, "int64"))
    number: np.ndarray = field(default_factory=lambda: np.empty(0))
    number_corrected: np.ndarray = field(default_factory=lambda: np.empty(0))
    lwc_cdp: np.ndarray = field(default_factory=lambda: np.empty(0))
    lwc_cdp_corrected: np.ndarray = field(default_factory=lambda: np.empty(0))
    cdp_flags: np.ndarray = field(default_factory=lambda: np.empty(0, "int32"))
    d11_time: np.ndarray = field(default_factory=lambda: np.empty(0, "int64"))
    d11_flags: np.ndarray = field(default_factory=lambda: np.empty(0, "int32"))
    items: List[dict] = field(default_factory=list)


def _epoch(text: str) -> int:
    return int(np.datetime64(text.strip().replace(" ", "T"), "s").astype("int64"))


def _stamp(t: float) -> str:
    return datetime.fromtimestamp(int(t), timezone.utc).strftime("%Y-%m-%d %H:%M")


def _unique(time: np.ndarray, values: Dict[str, np.ndarray]) -> Tuple[np.ndarray, Dict[str, np.ndarray]]:
    """同一分鐘有重複(錄製交界)時留第一筆。"""
    unique, first = np.unique(time.astype("int64"), return_index=True)
    return unique, {k: np.asarray(v, "float64")[first] for k, v in values.items()}


def _segments(time: np.ndarray, gap: float = SEGMENT_GAP) -> List[Tuple[int, int]]:
    if not len(time):
        return []
    cuts = np.flatnonzero(np.diff(time) > gap) + 1
    edges = np.r_[0, cuts, len(time)]
    return list(zip(edges[:-1].tolist(), edges[1:].tolist()))


def _at(source_time: np.ndarray, source: np.ndarray, time: np.ndarray) -> np.ndarray:
    """對到同一分鐘的值;沒有就是 NaN。"""
    out = np.full(len(time), np.nan)
    if len(source_time):
        place = np.clip(np.searchsorted(source_time, time), 0, len(source_time) - 1)
        same = source_time[place] == time
        out[same] = source[place[same]]
    return out


def baseline_power(time: np.ndarray, power: np.ndarray, clear: np.ndarray, window: float = BASELINE_WINDOW, max_gap: float = BASELINE_MAX_GAP) -> np.ndarray:
    """由無雲時的總功率估乾空氣功率:在時間上取移動中位數,雲中用內插。"""
    out = np.full(len(time), np.nan)
    chosen = clear & np.isfinite(power)
    clear_time, clear_power = time[chosen], power[chosen]
    if len(clear_time) < BASELINE_MIN_POINTS:
        return out
    grid = np.arange(time[0], time[-1] + BASELINE_STEP * 60, BASELINE_STEP * 60)
    low = np.searchsorted(clear_time, grid - window * 60)
    high = np.searchsorted(clear_time, grid + window * 60)
    medians = np.full(len(grid), np.nan)
    for i, (a, b) in enumerate(zip(low.tolist(), high.tolist())):
        if b - a >= BASELINE_MIN_POINTS:
            medians[i] = np.median(clear_power[a:b])
    good = np.isfinite(medians)
    if not good.any():
        return out
    estimate = np.interp(time, grid[good], medians[good])
    place = np.clip(np.searchsorted(clear_time, time), 1, max(1, len(clear_time) - 1))
    nearest = np.minimum(np.abs(time - clear_time[place - 1]), np.abs(time - clear_time[np.minimum(place, len(clear_time) - 1)]))
    usable = nearest <= max_gap * 60
    out[usable] = estimate[usable]
    return out


def _runs(flags: np.ndarray, time: np.ndarray, merge: float = 600) -> List[Tuple[int, int, int]]:
    """把旗標相連的分鐘合併成區段。回傳 (開始時間, 結束時間, 分鐘數)。"""
    index = np.flatnonzero(flags)
    if not len(index):
        return []
    cuts = np.flatnonzero(np.diff(time[index]) > merge) + 1
    return [(int(time[g[0]]), int(time[g[-1]]) + 60, len(g)) for g in np.split(index, cuts)]


def _item(start: int, end: int, instrument: str, kind: str, action: str, status: str, reason: str, minutes: Optional[int] = None) -> dict:
    return {"start": start, "end": end, "start_text": _stamp(start), "end_text": _stamp(end), "instrument": instrument, "kind": kind, "action": action, "status": status, "reason": reason, "minutes": minutes if minutes is not None else int((end - start) // 60)}


def load_overrides(path: Optional[Path]) -> List[dict]:
    """讀你指定的實際流速。格式見 corrections_config.example.json。"""
    if path is None or not path.exists():
        return []
    config = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for row in config.get("pas_true", []):
        speed = float(row["speed"])
        if not 0 < speed < 100 or row.get("instrument", "cdp") not in ("cdp", "lwc"):
            raise ValueError(f"實際流速設定不合理:{row}")
        out.append({"instrument": row.get("instrument", "cdp"), "start": _epoch(row["start"]), "end": _epoch(row["end"]), "speed": speed, "note": str(row.get("note", ""))})
    return out


def _speed_factor(time: np.ndarray, applied: np.ndarray, overrides: List[dict], instrument: str) -> Tuple[np.ndarray, np.ndarray]:
    """濃度和 LWC 與流速成反比。回傳 (要乘的倍率, 是否有套用)。"""
    factor, used = np.ones(len(time)), np.zeros(len(time), bool)
    for row in overrides:
        if row["instrument"] != instrument:
            continue
        chosen = (time >= row["start"]) & (time < row["end"]) & np.isfinite(applied) & (applied > 0)
        factor[chosen] = applied[chosen] / row["speed"]
        used |= chosen
    return factor, used


def compute(store, overrides: Optional[List[dict]] = None, window: float = BASELINE_WINDOW) -> Corrections:
    overrides = overrides or []
    result = Corrections()
    channels = store.channels
    cdp = channels.get("cdp")
    number_time = np.empty(0, "int64")
    number = np.empty(0)
    if cdp is not None and "Number Conc (#/cm^3)" in cdp.values:
        names = {"number": "Number Conc (#/cm^3)", "lwc": "LWC (g/m^3)", "dump": "Dump Spot Monitor (V)", "laser": "Laser Current (mA)", "pas": "Applied PAS (m/s)"}
        number_time, cdp_values = _unique(cdp.time, {k: cdp.values.get(n, np.full(len(cdp.time), np.nan)) for k, n in names.items()})
        number = cdp_values["number"]
        flags = np.zeros(len(number_time), "int32")
        fog = (number < CLEAR_NUMBER) & (cdp_values["dump"] < DUMP_LOW) & (cdp_values["laser"] > LASER_ON)
        flags[fog] |= FOG
        pas = cdp_values["pas"]
        test = np.isfinite(pas) & ((pas < 2) | (pas > 20))
        flags[test] |= TEST_PAS
        factor, used = _speed_factor(number_time, pas, overrides, "cdp")
        flags[used] |= PAS_OVERRIDE
        result.cdp_time, result.number, result.lwc_cdp = number_time, number, cdp_values["lwc"]
        result.number_corrected = number * factor
        result.lwc_cdp_corrected = cdp_values["lwc"] * factor
        result.cdp_flags = flags
        for start, end, minutes in _runs(fog, number_time):
            result.items.append(_item(start, end, "CDP", "鏡片可能起霧", "只標記", "自動", f"無粒子時 Dump Spot 低於 {DUMP_LOW:g} V(通常約 4.84 V)。這段 CDP 可能不可靠,數值無法還原。", minutes))
        for row in overrides:
            if row["instrument"] == "cdp":
                result.items.append(_item(row["start"], row["end"], "CDP", "套用指定流速", "重算", "你指定", f"實際流速 {row['speed']:g} m/s。濃度和 LWC 依流速成反比重算。{row['note']}"))
        # 手動流速不一致或被改動:需要實際風速才能修
        _pas_items(result, number_time, pas, channels, overrides)

    lwc = channels.get("lwc")
    if lwc is not None and "TotalPower(W)" in lwc.values:
        names = {"hot": "LWC-AveragedDAP", "power": "TotalPower(W)", "dap": "DAP-Averaged", "pas": "PAS(m/s)", "pressure": "Static Pressure (mB)"}
        time, values = _unique(lwc.time, {k: lwc.values.get(n, np.full(len(lwc.time), np.nan)) for k, n in names.items()})
        hot, power, dap, pas = values["hot"], values["power"], values["dap"], values["pas"]
        flags = np.zeros(len(time), "int32")
        heater = power < HEATER_POWER
        # 加熱線恢復時功率還沒穩定,前後多標 2 分鐘
        wide = heater.copy()
        for shift in (1, 2):
            wide[shift:] |= heater[:-shift]
            wide[:-shift] |= heater[shift:]
        flags[wide] |= HEATER
        flags[(dap < DAP_MIN) & ~wide] |= DAP_BAD
        for a, b in _segments(time, gap=120):
            flags[a:min(b, a + START_MINUTES)] |= START
        factor, used = _speed_factor(time, pas, overrides, "lwc")
        number_here = _at(number_time, number, time)
        dry = np.full(len(time), np.nan)
        for a, b in _segments(time):
            clear = (number_here[a:b] < CLEAR_NUMBER) & ~wide[a:b] & (flags[a:b] & START == 0)
            dry[a:b] = baseline_power(time[a:b], power[a:b], clear, window=window)
        with np.errstate(invalid="ignore", divide="ignore"):
            watts = (power - dap) / hot
        # 每一段用 PADS 實際用的 W;算不出來時用流速推
        w = np.where(np.isfinite(pas) & (pas > 0), W_PER_SPEED * pas, np.nan)
        for a, b in _segments(time):
            chosen = (np.abs(hot[a:b]) > 0.05) & np.isfinite(watts[a:b]) & (watts[a:b] > 0) & (dap[a:b] >= DAP_MIN)
            if chosen.sum() >= 5:
                w[a:b] = np.median(watts[a:b][chosen])
        with np.errstate(invalid="ignore", divide="ignore"):
            corrected = (power - dry) / w * factor
        missing = ~np.isfinite(dry)
        flags[missing & ~wide] |= NO_BASELINE
        # 沒有基線時保留原值(乾空氣功率也壞掉的除外),標記讓人知道沒校正
        corrected[missing] = (hot * factor)[missing]
        corrected[wide | (flags & START != 0)] = np.nan
        corrected[missing & (flags & DAP_BAD != 0)] = np.nan
        result.lwc_time, result.lwc_raw, result.lwc_corrected, result.lwc_flags, result.dry_power = time, hot, corrected, flags, dry
        for start, end, minutes in _runs(wide, time):
            result.items.append(_item(start, end, "LWC-300", "加熱線沒功率", "移除", "自動", f"總功率低於 {HEATER_POWER:g} W(正常約 2.4 W),LWC 算出約 −3 g/m³ 是假訊號。", minutes))
        for start, end, minutes in _runs(flags & DAP_BAD != 0, time):
            result.items.append(_item(start, end, "LWC-300", "乾空氣功率異常", "重算", "自動", f"DAP-Averaged 低於 {DAP_MIN:g} W,原值整段偏高。改用無雲時的總功率重算。", minutes))
        _baseline_items(result, time, hot, corrected, flags, number_here, missing)
        _pressure_items(result, time, values["pressure"], channels)
        for row in overrides:
            if row["instrument"] == "lwc":
                result.items.append(_item(row["start"], row["end"], "LWC-300", "套用指定流速", "重算", "你指定", f"實際流速 {row['speed']:g} m/s。LWC 依流速成反比重算。{row['note']}"))

    series = getattr(store, "d11", None)
    if series is not None and len(series.time):
        unique, first = np.unique(series.time.astype("int64"), return_index=True)
        flags = np.zeros(len(unique), "int32")
        err = series.values.get("Err")
        if err is not None:
            flags[np.isfinite(err[first]) & (err[first] != 0)] |= D11_ERR
        pm = series.values["PM2.5"][first] if "PM2.5" in series.values else np.full(len(unique), np.nan)
        widths = np.log10(series.edges[1:] / series.edges[:-1])
        per_liter = series.spectrum[first] * widths * 1000
        with np.errstate(invalid="ignore", divide="ignore"):
            ratio = per_liter[:, 0] / np.where(per_liter[:, 1] > 0, per_liter[:, 1], np.nan)
        fine = (ratio >= FINE_RATIO) & (pm < FINE_PM) & (per_liter[:, 0] >= FINE_MIN_PER_LITER)
        flags[fine] |= FINE
        # PM 尖峰:遠高於前後 30 分鐘的中位數,可能是附近短暫汙染
        spike = np.zeros(len(unique), bool)
        for a, b in _segments(unique, gap=600):
            part = pm[a:b]
            if b - a >= 10:
                low = np.searchsorted(unique[a:b], unique[a:b] - 1800)
                high = np.searchsorted(unique[a:b], unique[a:b] + 1800)
                candidates = np.flatnonzero(part > PM_SPIKE_MIN)
                for i in candidates.tolist():
                    around = part[low[i]:high[i]]
                    around = around[np.isfinite(around)]
                    if len(around) >= 5 and part[i] > PM_SPIKE_FACTOR * max(np.median(around), 1.0):
                        spike[a + i] = True
        flags[spike] |= PM_SPIKE
        result.d11_time, result.d11_flags = unique, flags
        for start, end, minutes in _runs(fine, unique):
            if minutes >= 5:
                result.items.append(_item(start, end, "11-D", "0.253 µm 偏高", "只標記", "自動", f"最小通道是下一通道的 {FINE_RATIO:g} 倍以上,而且空氣很乾淨。可能是雲洗掉大粒子,也可能是雜訊;零氣測試前不扣除。", minutes))
        for start, end, minutes in _runs(spike, unique):
            result.items.append(_item(start, end, "11-D", "PM 瞬間尖峰", "只標記", "自動", "比前後 30 分鐘的中位數高 10 倍以上,可能是附近短暫汙染。數值保留。", minutes))
    result.items.sort(key=lambda item: (item["start"], item["instrument"]))
    return result


def _pas_items(result: Corrections, time: np.ndarray, pas: np.ndarray, channels: dict, overrides: List[dict]) -> None:
    lwc = channels.get("lwc")
    other = _at(*_unique_pair(lwc, "PAS(m/s)"), time) if lwc is not None else np.full(len(time), np.nan)
    days = time // 86400
    groups: List[dict] = []
    for day in np.unique(days).tolist():
        chosen = days == day
        a, b = np.nanmedian(pas[chosen]) if np.isfinite(pas[chosen]).any() else np.nan, np.nanmedian(other[chosen]) if np.isfinite(other[chosen]).any() else np.nan
        if not (np.isfinite(a) and np.isfinite(b)) or abs(a - b) <= 0.01 or a < 2 or a > 20:
            continue
        if groups and groups[-1]["a"] == a and groups[-1]["b"] == b:
            groups[-1]["end"] = int(time[chosen][-1]) + 60
        else:
            groups.append({"a": float(a), "b": float(b), "start": int(time[chosen][0]), "end": int(time[chosen][-1]) + 60})
    for group in groups:
        covered = any(row["start"] <= group["start"] and row["end"] >= group["end"] for row in overrides)
        if not covered:
            result.items.append(_item(group["start"], group["end"], "CDP / LWC-300", "兩台流速不同", "沒有改值", "等你確認", f"CDP {group['a']:g}、LWC-300 {group['b']:g} m/s,都是手動值。若實際相同,CDP 濃度和 LWC 要乘 {group['a'] / group['b']:.2f}。請提供實際風速後才能校正。"))


def _unique_pair(channel, name: str) -> Tuple[np.ndarray, np.ndarray]:
    time, values = _unique(channel.time, {"v": channel.values.get(name, np.full(len(channel.time), np.nan))})
    return time, values["v"]


def _baseline_items(result: Corrections, time: np.ndarray, hot: np.ndarray, corrected: np.ndarray, flags: np.ndarray, number: np.ndarray, missing: np.ndarray) -> None:
    days = time // 86400
    for day in np.unique(days).tolist():
        chosen = days == day
        clear = chosen & (number < CLEAR_NUMBER) & np.isfinite(hot) & (flags & (HEATER | START) == 0)
        if clear.sum() < 30:
            continue
        before = float(np.median(hot[clear]))
        after = corrected[clear]
        after = after[np.isfinite(after)]
        if not len(after) or abs(before) < 0.1:
            continue
        result.items.append(_item(int(time[chosen][0]), int(time[chosen][-1]) + 60, "LWC-300", "基線偏移", "重算", "自動", f"無雲時中位數 {before:+.2f} g/m³,用無雲時的總功率重算後 {float(np.median(after)):+.2f}。只重算離無雲時段 {BASELINE_MAX_GAP} 分鐘內的資料,是估計值;LWC 很低時雜訊比訊號大,單分鐘的值(包含負值)不要單獨解讀。"))
    for start, end, minutes in _runs(missing & (flags & HEATER == 0), time):
        if minutes >= 60:
            result.items.append(_item(start, end, "LWC-300", "沒有基線可重算", "只標記", "自動", f"前後 {BASELINE_MAX_GAP} 分鐘內沒有足夠的無雲資料,保留 PADS 原值(基線未校正)。", minutes))


def _pressure_items(result: Corrections, time: np.ndarray, pressure: np.ndarray, channels: dict) -> None:
    d11 = channels.get("d11")
    if d11 is None or "Pres" not in d11.values:
        return
    measured = _at(*_unique_pair(d11, "Pres"), time)
    days = time // 86400
    for day in np.unique(days).tolist():
        chosen = days == day
        if not np.isfinite(measured[chosen]).any() or not np.isfinite(pressure[chosen]).any():
            continue
        set_value, real = float(np.nanmedian(pressure[chosen])), float(np.nanmedian(measured[chosen]))
        if abs(set_value - real) > 15:
            result.items.append(_item(int(time[chosen][0]), int(time[chosen][-1]) + 60, "LWC-300", "手動壓力和實測不符", "沒有改值", "不影響主要結果", f"輸入 {set_value:g}、實測 {real:.0f} hPa。只明顯影響 LWC-CalculatedDAP;這裡用的 LWC-AveragedDAP 幾乎不受影響。"))


def flag_text(value: int, table: Dict[int, str]) -> str:
    return ";".join(text for bit, text in table.items() if value & bit)


def _text(values: np.ndarray) -> List[str]:
    return ["" if not np.isfinite(v) else f"{v:.5g}" for v in values.tolist()]


def export(result: Corrections, out: Path) -> Dict[str, int]:
    """每天一個 CSV,加上校正紀錄。寫到 out,不碰原始資料。"""
    out.mkdir(parents=True, exist_ok=True)
    times = sorted(set(result.cdp_time.tolist()) | set(result.lwc_time.tolist()) | set(result.d11_time.tolist()))
    grid = np.array(times, "int64")
    columns = {
        "CDP濃度_原始": _at(result.cdp_time, result.number, grid), "CDP濃度_校正": _at(result.cdp_time, result.number_corrected, grid),
        "CDP_LWC_原始": _at(result.cdp_time, result.lwc_cdp, grid), "CDP_LWC_校正": _at(result.cdp_time, result.lwc_cdp_corrected, grid),
        "LWC300_原始": _at(result.lwc_time, result.lwc_raw, grid), "LWC300_校正": _at(result.lwc_time, result.lwc_corrected, grid),
        "估計乾空氣功率W": _at(result.lwc_time, result.dry_power, grid),
    }
    flag_columns = (("CDP標記", result.cdp_time, result.cdp_flags, CDP_FLAGS), ("LWC300標記", result.lwc_time, result.lwc_flags, LWC_FLAGS), ("11D標記", result.d11_time, result.d11_flags, D11_FLAGS))
    flags = {name: _at(t, f.astype("float64"), grid) for name, t, f, _ in flag_columns}
    days = grid // 86400
    count = 0
    for day in np.unique(days).tolist():
        chosen = np.flatnonzero(days == day)
        text = _stamp(day * 86400)[:10]
        with open(out / f"校正_{text}.csv", "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(["時間"] + list(columns) + [c[0] for c in flag_columns])
            texts = {name: _text(values[chosen]) for name, values in columns.items()}
            for row, index in enumerate(chosen.tolist()):
                labels = []
                for name, _, _, table in flag_columns:
                    value = flags[name][index]
                    labels.append(flag_text(int(value), table) if np.isfinite(value) else "")
                writer.writerow([_stamp(grid[index])] + [texts[name][row] for name in columns] + labels)
        count += 1
    with open(out / "校正紀錄.csv", "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["開始", "結束", "分鐘", "儀器", "項目", "處理", "狀態", "原因"])
        for item in result.items:
            writer.writerow([item["start_text"], item["end_text"], item["minutes"], item["instrument"], item["kind"], item["action"], item["status"], item["reason"]])
    return {"days": count, "items": len(result.items)}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="產生校正後的資料和紀錄,不更動原始資料")
    parser.add_argument("--cache", default="cache")
    parser.add_argument("--out", default="output/corrected")
    parser.add_argument("--config", default="corrections_config.json", help="你指定的實際流速;沒有這個檔就不套用")
    args = parser.parse_args(argv)
    from .viewer import Store

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    store = Store(Path(args.cache))
    result = compute(store, load_overrides(Path(args.config)))
    summary = export(result, Path(args.out))
    print(f"完成:{summary['days']} 天、{summary['items']} 筆校正紀錄,放在 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
