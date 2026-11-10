"""把一段時間的資料整理成「有沒有問題、有什麼特徵」。只用 numpy,數值來自每分鐘快取。

狀態是依固定門檻的提醒,不等於資料一定有誤。門檻盡量用過去資料的範圍或手冊說明,寫在 detail 裡。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import numpy as np

OK, WATCH, PROBLEM, NA = "ok", "watch", "problem", "na"
CLOUD_NUMBER = 50.0
CLOUD_LWC = 0.02
QUIET_NUMBER = 1.0
HIGH_NUMBER = 300.0
EVENT_MERGE_MIN = 5
EVENT_MIN_MIN = 5
MAX_SCATTER = 1500
DAY = 86400
# 過去 81 天的統計:基線絕對值 90% 在 0.2 以內、最大 0.39;漂移中位數 0.42、90% 在 0.67 以內。
BASELINE_WATCH, BASELINE_PROBLEM = 0.2, 0.35
DRIFT_WATCH, DRIFT_PROBLEM = 0.7, 1.5
GAP_WATCH_MIN, GAP_PROBLEM_MIN = 5, 120
# 幾乎每天都有、不是當天才發生的項目,不在日曆上標記
CHRONIC = {"pas", "agreement", "coincidence"}


class Aligned:
    """把各儀器放到同一條每分鐘時間軸,沒有資料的地方是 NaN。"""

    def __init__(self, store, start: int, end: int):
        self.start = start - start % 60
        self.end = end
        self.count = int(np.ceil((end - self.start) / 60))
        self.store = store
        self.present: Dict[str, np.ndarray] = {}
        for kind, channel in store.channels.items():
            low, high = np.searchsorted(channel.time, [self.start, end])
            index = ((channel.time[low:high] - self.start) // 60).astype(int)
            flags = np.zeros(self.count, bool)
            flags[index] = True
            self.present[kind] = flags

    def get(self, kind: str, name: str) -> np.ndarray:
        channel = self.store.channels.get(kind)
        out = np.full(self.count, np.nan)
        if channel is None or name not in channel.values:
            return out
        low, high = np.searchsorted(channel.time, [self.start, self.end])
        index = ((channel.time[low:high] - self.start) // 60).astype(int)
        values = channel.values[name][low:high].astype("float64")
        good = np.isfinite(values)
        totals = np.bincount(index[good], weights=values[good], minlength=self.count)
        counts = np.bincount(index[good], minlength=self.count)
        with np.errstate(invalid="ignore", divide="ignore"):
            return totals / counts


def _median(values: np.ndarray) -> Optional[float]:
    values = values[np.isfinite(values)]
    return float(np.median(values)) if len(values) else None


def _runs(flags: np.ndarray) -> List[Tuple[int, int]]:
    """連續為 True 的區段,回傳 [起, 迄)。"""
    edges = np.diff(np.r_[0, flags.astype(int), 0])
    return list(zip(np.flatnonzero(edges == 1).tolist(), np.flatnonzero(edges == -1).tolist()))


def _check(key: str, title: str, status: str, value: str, detail: str) -> dict:
    return {"id": key, "title": title, "status": status, "value": value, "detail": detail}


def _percent_rank(table: Optional[List[float]], value: Optional[float]) -> Optional[float]:
    """value 在過去資料的第幾百分位。table 是 1–99 百分位值。"""
    if not table or value is None:
        return None
    return float(np.interp(value, table, np.arange(1, len(table) + 1), left=0, right=100))


def cloud_events(a: Aligned, number: np.ndarray, lwc: np.ndarray, hot: np.ndarray, mvd: np.ndarray, pm25: np.ndarray) -> List[dict]:
    cloud = (number > CLOUD_NUMBER) & (lwc > CLOUD_LWC)
    merged = cloud.copy()
    for first, last in _runs(~cloud):
        if last - first <= EVENT_MERGE_MIN and first > 0 and last < len(cloud):
            merged[first:last] = True
    events = []
    for first, last in _runs(merged):
        if last - first < EVENT_MIN_MIN:
            continue
        before = pm25[max(0, first - 30):first]
        inside = pm25[first:last]
        pm_before, pm_inside = _median(before), _median(inside)
        events.append({
            "start": a.start + first * 60,
            "end": a.start + last * 60,
            "minutes": last - first,
            "number": _median(number[first:last]),
            "lwc": _median(lwc[first:last]),
            "hotwire": _median(hot[first:last]),
            "mvd": _median(mvd[first:last]),
            "peak_number": float(np.nanmax(number[first:last])),
            "pm_before": pm_before,
            "pm_inside": pm_inside,
        })
    return events


def run_checks(a: Aligned, store, normal: Optional[dict]) -> List[dict]:
    checks: List[dict] = []
    number = a.get("cdp", "Number Conc (#/cm^3)")
    lwc = a.get("cdp", "LWC (g/m^3)")
    mvd = a.get("cdp", "MVD (um)")
    hot = a.get("lwc", "LWC-AveragedDAP")
    cloud = (number > CLOUD_NUMBER) & (lwc > CLOUD_LWC)
    quiet = number < QUIET_NUMBER
    labels = {"cdp": "CDP", "lwc": "LWC-300", "d11": "11-D"}

    # 1. 資料完整度
    minutes = {k: int(a.present.get(k, np.zeros(1)).sum()) for k in labels}
    active = {k: v for k, v in minutes.items() if v}
    if not active:
        checks.append(_check("coverage", "資料完整度", NA, "沒有資料", "這段時間三台都沒有資料。"))
    else:
        any_present = np.any([a.present[k] for k in active], axis=0)
        span = np.flatnonzero(any_present)
        first, last = span[0], span[-1] + 1
        notes, status = [], OK
        longest = max(minutes.values())
        for k, name in labels.items():
            if not minutes[k]:
                notes.append(f"{name} 沒有資料")
                if longest >= 60 and status == OK:
                    status = WATCH
                continue
            flags = a.present[k]
            place = np.flatnonzero(flags)
            holes = [e - s for s, e in _runs(~flags[place[0]:place[-1] + 1])]
            big = max(holes, default=0)
            if big > GAP_WATCH_MIN:
                notes.append(f"{name} 中斷最長 {big} 分鐘")
                status = PROBLEM if big > GAP_PROBLEM_MIN else (WATCH if status == OK else status)
            if longest >= 60 and minutes[k] < 0.8 * longest:
                notes.append(f"{name} 只有 {minutes[k] / 60:.1f} 小時")
                status = WATCH if status == OK else status
        text = " · ".join(f"{labels[k]} {minutes[k] / 60:.1f} h" for k in labels if minutes[k])
        checks.append(_check("coverage", "資料完整度", status, text, ";".join(notes) or "主要儀器連續,沒有超過 5 分鐘的中斷。"))

    # 2. CDP 光路
    dump = a.get("cdp", "Dump Spot Monitor (V)")
    usable = quiet & np.isfinite(dump) & (a.get("cdp", "Laser Current (mA)") > 40)
    if usable.sum() >= 10:
        share = float((dump[usable] < 4).mean())
        status = PROBLEM if share >= 0.1 else (WATCH if share >= 0.01 else OK)
        checks.append(_check("dump", "CDP 光路(Dump Spot)", status, f"無粒子時 {np.median(dump[usable]):.2f} V", f"無粒子時低於 4 V 佔 {share:.0%}(通常約 4.84 V)。手冊:沒有粒子卻偏低,可能是鏡窗起霧、結冰或雷射過熱。"))
    else:
        checks.append(_check("dump", "CDP 光路(Dump Spot)", NA, "—", "無粒子的時間不足,無法判斷。"))

    # 3. 雷射電流
    laser = a.get("cdp", "Laser Current (mA)")
    temperature = a.get("cdp", "Laser Temp (C)")
    model = (normal or {}).get("laser_model")
    good = (laser > 40) & np.isfinite(temperature)
    if model and good.sum() >= 10:
        years = (a.start - model["t0"]) / (365.25 * DAY)
        expected = model["a"] + model["b"] * temperature[good] + model["c"] * years
        residual = float(np.median(laser[good] - expected))
        sd = model["sd"]
        status = PROBLEM if abs(residual) > 6 * sd else (WATCH if abs(residual) > 3 * sd else OK)
        checks.append(_check("laser", "CDP 雷射電流", status, f"{np.median(laser[good]):.1f} mA", f"依溫度和長期趨勢預期 {np.median(expected):.1f} mA,差 {residual:+.1f} mA(歷史差異標準差 {sd:.2f})。"))
    else:
        checks.append(_check("laser", "CDP 雷射電流", NA, "—", "沒有足夠資料。"))

    # 4. 流速設定
    cdp_pas, hot_pas = _median(a.get("cdp", "Applied PAS (m/s)")), _median(a.get("lwc", "PAS(m/s)"))
    if cdp_pas is None:
        checks.append(_check("pas", "流速設定", NA, "—", "沒有 CDP 資料。"))
    else:
        text = f"CDP {cdp_pas:g}" + (f" / LWC {hot_pas:g}" if hot_pas is not None else "") + " m/s"
        if cdp_pas < 2 or cdp_pas > 20:
            status, detail = WATCH, "流速不像實際觀測值,可能是校正或測試;這時濃度和 LWC 沒有物理意義。"
        elif hot_pas is not None and abs(cdp_pas - hot_pas) > 0.01:
            status, detail = WATCH, f"兩台設定不同。若實際相同,CDP 濃度和 LWC 要乘 {cdp_pas / hot_pas:.2f}。"
        else:
            status, detail = OK, "兩台設定一致。設定值不等於實際風速。"
        checks.append(_check("pas", "流速設定", status, text, detail))

    # 5. LWC-300 基線
    clear_hot = hot[quiet & np.isfinite(hot)]
    if len(clear_hot) >= 30:
        base = float(np.median(clear_hot))
        index = np.flatnonzero(quiet & np.isfinite(hot))
        bins = {}
        for position in index:
            bins.setdefault(position // 10, []).append(hot[position])
        medians = [np.median(v) for v in bins.values() if len(v) >= 5]
        drift = float(max(medians) - min(medians)) if len(medians) >= 2 else 0.0
        status = PROBLEM if abs(base) > BASELINE_PROBLEM or drift > DRIFT_PROBLEM else (WATCH if abs(base) > BASELINE_WATCH or drift > DRIFT_WATCH else OK)
        checks.append(_check("baseline", "LWC-300 基線", status, f"{base:+.3f} g/m³", f"無雲時應接近 0;漂移約 {drift:.2f} g/m³。過去中位數是偏移 0.05、漂移 0.42,所以「正常」不代表 LWC-300 的絕對值可靠,只是沒有比平常差。"))
    else:
        checks.append(_check("baseline", "LWC-300 基線", NA, "—", "無雲且有 LWC-300 的時間不足。"))

    # 6. 兩種 LWC
    pair = cloud & np.isfinite(hot)
    if pair.sum() >= 30:
        ratio = (_median(hot[pair]) or 0.0) / max(_median(lwc[pair]) or 1e-6, 1e-6)
        r = float(np.corrcoef(lwc[pair], hot[pair])[0, 1]) if np.std(lwc[pair]) > 0 and np.std(hot[pair]) > 0 else float("nan")
        status = WATCH if not np.isfinite(r) or r < 0.3 else OK
        checks.append(_check("agreement", "CDP 與 LWC-300", status, f"r = {r:.2f}", f"雲中相關 {r:.2f},LWC-300 ÷ CDP 中位數比 {ratio:.2f}。兩者長期就差很多,目前無法判定誰才準。"))
    else:
        checks.append(_check("agreement", "CDP 與 LWC-300", NA, "—", "雲中同時有兩台的時間不足。"))

    # 7. 高濃度
    in_cloud = int(cloud.sum())
    if in_cloud >= 10:
        share = float((number[cloud] > HIGH_NUMBER).mean())
        status = WATCH if share > 0.5 else OK
        checks.append(_check("coincidence", "高濃度(重合風險)", status, f"{share:.0%} 超過 300", f"雲中有 {share:.0%} 的時間濃度超過 300 /cm³,粒子重合可能低估個數、高估粒徑;尚未量化。"))
    else:
        checks.append(_check("coincidence", "高濃度(重合風險)", NA, "—", "雲中時間不足。"))

    # 8. 粒徑分布形狀
    band = (normal or {}).get("cdp_cloud_spectrum")
    if band and store.cdp is not None and in_cloud >= 10:
        low, high = np.searchsorted(store.cdp.time, [a.start, a.end])
        minute_number = store.cdp.values["number_cm3"][low:high]
        minute_lwc = store.cdp.values["lwc_gm3"][low:high]
        chosen = (minute_number > CLOUD_NUMBER) & (minute_lwc > CLOUD_LWC)
        if chosen.sum() >= 10:
            spectrum = np.nanmean(store.cdp.spectrum[low:high][chosen], axis=0)
            edges = np.array(normal["cdp_edges"])
            mid = np.sqrt(edges[:-1] * edges[1:])
            focus = (mid >= 8) & (mid <= 30)
            outside = float(np.mean((spectrum[focus] < np.array(band["p10"])[focus]) | (spectrum[focus] > np.array(band["p90"])[focus])))
            status = PROBLEM if outside >= 0.8 else (WATCH if outside >= 0.4 else OK)
            checks.append(_check("shape", "雲滴粒徑分布", status, f"{outside:.0%} 超出典型", f"8–30 µm 的區間有 {outside:.0%} 超出過去 10–90% 範圍。超出不一定有問題,可能只是這片雲比較特別。"))
    if not any(c["id"] == "shape" for c in checks):
        checks.append(_check("shape", "雲滴粒徑分布", NA, "—", "雲中時間不足。"))

    # 9. 11-D 狀態
    err = a.get("d11", "Err")
    if np.isfinite(err).sum() >= 10:
        share = float((err[np.isfinite(err)] != 0).mean())
        flow = _median(a.get("d11", "Flow[%]"))
        status = PROBLEM if share > 0.2 else (WATCH if share > 0.01 else OK)
        checks.append(_check("d11", "11-D 狀態", status, f"Err≠0 佔 {share:.0%}", f"Err 欄的代碼意義請對照手冊。Flow 中位數 {flow:g}%。" if flow is not None else "Err 欄的代碼意義請對照手冊。"))
    else:
        checks.append(_check("d11", "11-D 狀態", NA, "—", "沒有 11-D 紀錄資料。"))

    # 10. 壓力交叉比對
    pads_pressure, measured = _median(a.get("lwc", "Static Pressure (mB)")), _median(a.get("d11", "Pres"))
    if pads_pressure is not None and measured is not None:
        diff = pads_pressure - measured
        status = PROBLEM if abs(diff) > 50 else (WATCH if abs(diff) > 15 else OK)
        checks.append(_check("pressure", "LWC 壓力設定", status, f"{pads_pressure:g} / 實測 {measured:.0f} hPa", f"LWC-300 手動設定與 11-D 實測差 {diff:+.0f} hPa。差太多會影響 LWC-CalculatedDAP;圖上的 LWC-AveragedDAP 幾乎不受影響。"))
    else:
        checks.append(_check("pressure", "LWC 壓力設定", NA, "—", "需要同時有 LWC-300 和 11-D。"))
    return checks


# (儀器, 欄位, 名稱, 單位):這些都是 PADS 設定裡手動輸入的 LM 值,不是感測器量到的
MANUAL_CHANNELS = [
    ("cdp", "Applied PAS (m/s)", "CDP 流速", "m/s"),
    ("lwc", "PAS(m/s)", "LWC-300 流速", "m/s"),
    ("lwc", "Static Pressure (mB)", "LWC-300 壓力", "hPa"),
    ("lwc", "Ambient Temperature (C)", "LWC-300 環境溫度", "°C"),
    ("lwc", "EffectiveWireTemp(C)", "LWC-300 有效線溫", "°C"),
]


# PADS 的 DAP-Calculated:壓力 722→730 hPa 時 2.3068→2.3221 W,乾空氣功率約正比於 (密度×流速)^0.6
DRY_EXPONENT = 0.6


def _distinct(values: np.ndarray) -> List[float]:
    values = values[np.isfinite(values)]
    return sorted({round(float(v), 2) for v in values})


def _value_text(values: List[float], unit: str) -> str:
    return "、".join(f"{v:g}" for v in values) + f" {unit}"


def manual_settings(a: Aligned, store) -> dict:
    """列出手動輸入的值、對結果的影響,以及能否和實測比對。影響量是依手冊公式的估計。"""
    rows: List[dict] = []
    measured_pressure = _median(a.get("d11", "Pres"))
    hot_pas = _median(a.get("lwc", "PAS(m/s)"))
    power, dap, calculated = a.get("lwc", "TotalPower(W)"), a.get("lwc", "DAP-Calculated"), a.get("lwc", "LWC-CalculatedDAP")
    with np.errstate(invalid="ignore", divide="ignore"):
        watts = _median(((power - dap) / calculated)[np.abs(calculated) > 0.05])  # PADS 實際使用的 W per g/m³
    dry = _median(dap)
    previous = getattr(store, "previous_settings", lambda start: {})(a.start)

    def effect_of(name: str) -> str:
        if name == "Applied PAS (m/s)":
            speed = _median(a.get("cdp", name))
            return f"濃度和 LWC 與它成反比:差 1 m/s 約差 {100 / speed:.0f}%" if speed else "濃度和 LWC 與它成反比"
        if watts is None or dry is None or not watts:
            return "會影響 LWC-300 的計算"
        wire = _median(a.get("lwc", "EffectiveWireTemp(C)")) or 150.0
        ambient = _median(a.get("lwc", "Ambient Temperature (C)")) or 10.0
        # PADS 的 LWC-AveragedDAP = (總功率 − 量測的乾空氣功率) ÷ W,W 約 0.095 × 流速(過去資料驗證)。
        # 壓力和溫度進的是「計算」乾空氣功率,所以只明顯影響 LWC-CalculatedDAP。
        if name == "PAS(m/s)" and hot_pas:
            return f"LWC-300 與它成反比:差 1 m/s 約差 {100 / hot_pas:.0f}%"
        pressure = _median(a.get("lwc", "Static Pressure (mB)"))
        if name == "Static Pressure (mB)" and pressure:
            return f"只明顯影響 LWC-CalculatedDAP:差 50 hPa 約差 {dry * DRY_EXPONENT * 50 / pressure / watts:.2f} g/m³;圖上的 LWC-AveragedDAP 幾乎不受影響"
        if name in ("Ambient Temperature (C)", "EffectiveWireTemp(C)") and wire > ambient:
            amount = 5 if name.startswith("Ambient") else 10
            return f"只明顯影響 LWC-CalculatedDAP:差 {amount} °C 約差 {dry * amount / (wire - ambient) / watts:.2f} g/m³;圖上的 LWC-AveragedDAP 幾乎不受影響"
        return "會影響 LWC-300 的計算"

    for kind, name, label, unit in MANUAL_CHANNELS:
        values = _distinct(a.get(kind, name))
        if not values:
            continue
        notes: List[str] = []
        status = OK
        if len(values) > 1:
            notes.append("這段時間改過設定")
            status = WATCH
        old = previous.get(name)
        if old is not None and len(values) == 1 and abs(old[1] - values[0]) > 0.01:
            notes.append(f"和前一次觀測({old[0]}的 {old[1]:g})不同")
            status = WATCH
        if name == "Applied PAS (m/s)" and hot_pas is not None and abs(values[0] - hot_pas) > 0.01:
            notes.append(f"和 LWC-300 的 {hot_pas:g} m/s 不同")
            status = WATCH
        if name in ("Applied PAS (m/s)", "PAS(m/s)"):
            notes.append("沒有實測風速可以比對")
        if name == "Static Pressure (mB)":
            if measured_pressure is not None:
                diff = values[0] - measured_pressure
                notes.append(f"11-D 實測 {measured_pressure:.0f} hPa,差 {diff:+.0f}")
                status = WATCH if abs(diff) > 15 else status
            else:
                notes.append("沒有同時的實測壓力")
        if name == "Ambient Temperature (C)":
            notes.append("沒有戶外實測溫度可以比對")
        if name == "EffectiveWireTemp(C)":
            notes.append("是計算用的設定,不是線的實際溫度")
        rows.append({"label": label, "value": _value_text(values, unit), "effect": effect_of(name), "note":";".join(notes), "status": status})

    if store.__dict__.get("cdp") is not None and a.present.get("cdp", np.zeros(1)).any():
        areas = sorted({m["sample_area"] for m in store.cdp.meta if m.get("sample_area") and _overlaps(m, a)})
        if areas:
            rows.append({"label": "CDP 取樣面積", "value": _value_text(areas, "mm²"), "effect": "濃度和 LWC 與它成反比", "note": "來自校正,請對照校正資料" + (";這段時間混用" if len(areas) > 1 else ""), "status": WATCH if len(areas) > 1 else OK})
    d11_series = store.__dict__.get("d11")
    if d11_series is not None and a.present.get("d11", np.zeros(1)).any():
        factors = sorted({m["c_factor"] for m in d11_series.meta if m.get("c_factor") is not None and _overlaps(m, a)})
        if factors:
            rows.append({"label": "11-D C-factor", "value": _value_text(factors, ""), "effect": "PM 質量濃度依此換算,粒子數不受影響", "note": "儀器設定,不是實測" + (";這段時間混用" if len(factors) > 1 else ""), "status": WATCH if len(factors) > 1 else OK})
    depends = [r["label"] for r in rows if r["status"] == WATCH]
    return {"rows": rows, "flagged": depends, "has_wind": False}


def _overlaps(meta: dict, a: Aligned) -> bool:
    start = int(np.datetime64(meta["start"].replace(" ", "T"), "s").astype("int64"))
    end = int(np.datetime64(meta["end"].replace(" ", "T"), "s").astype("int64")) + 60
    return start < a.end and end > a.start


def summary_text(checks: List[dict]) -> Dict[str, int]:
    return {s: sum(c["status"] == s for c in checks) for s in (OK, WATCH, PROBLEM, NA)}


def _hist(values: np.ndarray, edges: List[float]) -> List[float]:
    values = values[np.isfinite(values)]
    counts, _ = np.histogram(values, bins=edges)
    return (counts / max(len(values), 1)).round(4).tolist()


def _points(x: np.ndarray, y: np.ndarray, t: np.ndarray) -> dict:
    good = np.isfinite(x) & np.isfinite(y)
    x, y, t = x[good], y[good], t[good]
    step = max(1, len(x) // MAX_SCATTER)
    r = float(np.corrcoef(x, y)[0, 1]) if len(x) >= 10 and np.std(x) > 0 and np.std(y) > 0 else None
    round_list = lambda v: [float(f"{i:.4g}") for i in v[::step].tolist()]
    return {"x": round_list(x), "y": round_list(y), "t": t[::step].astype(int).tolist(), "n": int(len(x)), "r": r}


def diagnose(store, start: int, end: int, normal: Optional[dict]) -> dict:
    a = Aligned(store, start, end)
    number = a.get("cdp", "Number Conc (#/cm^3)")
    lwc = a.get("cdp", "LWC (g/m^3)")
    mvd = a.get("cdp", "MVD (um)")
    hot = a.get("lwc", "LWC-AveragedDAP")
    pm25 = a.get("d11", "PM2.5")
    dump = a.get("cdp", "Dump Spot Monitor (V)")
    laser = a.get("cdp", "Laser Current (mA)")
    cloud = (number > CLOUD_NUMBER) & (lwc > CLOUD_LWC)
    quiet = number < QUIET_NUMBER
    times = a.start + np.arange(a.count) * 60
    checks = run_checks(a, store, normal)
    events = cloud_events(a, number, lwc, hot, mvd, pm25)
    for order, event in enumerate(events, 1):
        event["number_id"] = order

    lanes = [
        {"label": "CDP 有資料", "kind": "data", "runs": _runs(a.present.get("cdp", np.zeros(a.count, bool)))},
        {"label": "LWC-300 有資料", "kind": "data", "runs": _runs(a.present.get("lwc", np.zeros(a.count, bool)))},
        {"label": "11-D 有資料", "kind": "data", "runs": _runs(a.present.get("d11", np.zeros(a.count, bool)))},
        {"label": "雲中", "kind": "cloud", "runs": _runs(cloud)},
        {"label": "濃度 > 300", "kind": "watch", "runs": _runs(number > HIGH_NUMBER)},
        {"label": "無粒子但 Dump 偏低", "kind": "problem", "runs": _runs(quiet & (dump < 4) & (laser > 40))},
    ]

    reference = (normal or {}).get("reference") or {}
    distributions = []
    for key, label, unit, values in (
        ("number", "CDP 濃度(雲中)", "#/cm³", number[cloud]),
        ("lwc", "CDP LWC(雲中)", "g/m³", lwc[cloud]),
        ("mvd", "CDP MVD(雲中)", "µm", mvd[cloud]),
        ("pm25", "11-D PM2.5", "µg/m³", pm25),
    ):
        ref = reference.get(key)
        if not ref:
            continue
        median = _median(values)
        distributions.append({
            "key": key, "label": label, "unit": unit, "edges": ref["edges"], "log": ref["log"],
            "reference": ref["hist"], "today": _hist(values, ref["edges"]), "minutes": int(np.isfinite(values).sum()),
            "median": median, "reference_median": ref["percentiles"][49], "rank": _percent_rank(ref["percentiles"], median),
        })

    def in_state(values: np.ndarray, mask: np.ndarray) -> Optional[float]:
        return _median(values[mask])

    has_pm = np.isfinite(pm25)
    aerosol = {
        "clear": in_state(pm25, quiet & has_pm), "cloud": in_state(pm25, cloud & has_pm),
        "clear_minutes": int((quiet & has_pm).sum()), "cloud_minutes": int((cloud & has_pm).sum()),
    }
    present_any = int(np.any([v for v in a.present.values()], axis=0).sum()) if a.present else 0
    return {
        "start": a.start,
        "end": a.end,
        "minutes": a.count,
        "checks": checks,
        "summary": summary_text(checks),
        "observed_minutes": present_any,
        "cloud_minutes": int(cloud.sum()),
        "lanes": lanes,
        "events": events,
        "distributions": distributions,
        "aerosol": aerosol,
        "manual": manual_settings(a, store),
        "scatter": {
            "lwc": _points(lwc[cloud], hot[cloud], times[cloud]),
            "micro": _points(number[cloud], mvd[cloud], times[cloud]),
            "aerosol": _points(pm25[cloud], number[cloud], times[cloud]),
        },
    }
