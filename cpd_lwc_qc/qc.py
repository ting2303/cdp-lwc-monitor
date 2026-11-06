"""每次量測的指標計算與問題標記。只回報,不修改任何資料。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, fields
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from .pads import InstrumentTable, Session, align_seconds

ERROR, WARN, INFO = "error", "warn", "info"


@dataclass
class Limits:
    gap_s: float = 5.0
    start_offset_s: float = 120.0
    status_bad_frac: float = 0.01
    clear_number_cm3: float = 1.0  # 低於此濃度視為晴空,用來估 LWC-300 基線
    cloud_number_cm3: float = 50.0  # 雲段的工作定義,同時要求 CDP LWC 超過下一項
    cloud_lwc_gm3: float = 0.02
    min_clear_s: int = 60
    min_cloud_s: int = 300
    baseline_abs_gm3: float = 0.05
    baseline_drift_gm3: float = 0.05
    corr_min: float = 0.3
    corr_window_s: int = 60
    lag_search_s: int = 60
    lag_max_s: int = 5
    lag_needs_corr: float = 0.5
    coincidence_number_cm3: float = 300.0
    coincidence_min_s: int = 60
    dump_spot_low_v: float = 4.0
    dump_spot_low_frac: float = 0.01
    v24_range: Tuple[float, float] = (20.0, 30.0)
    v8_range: Tuple[float, float] = (6.0, 10.0)
    pas_plausible: Tuple[float, float] = (2.0, 20.0)
    config_pressure_diff_mb: float = 50.0
    config_temperature_diff_c: float = 8.0
    d11_clock_offset_s: float = 60.0
    d11_err_frac: float = 0.01
    site_pressure: List[dict] = field(default_factory=list)
    expected_pas: List[dict] = field(default_factory=list)


def load_limits(path: Optional[str]) -> Limits:
    limits = Limits()
    if path:
        with open(path, encoding="utf-8") as handle:
            values = json.load(handle)
        known = {f.name for f in fields(Limits)}
        for key, value in values.items():
            if key.startswith("_"):
                continue
            if key not in known:
                raise ValueError(f"qc 設定有未知的項目:{key}")
            setattr(limits, key, tuple(value) if key.endswith("_range") or key == "pas_plausible" else value)
    return limits


def _median(series: pd.Series) -> Optional[float]:
    value = series.median() if len(series) else np.nan
    return None if pd.isna(value) else float(value)


def time_metrics(table: InstrumentTable, session: Session, limits: Limits) -> Dict[str, object]:
    data = table.data
    steps = data["time_local"].diff().dt.total_seconds()
    out: Dict[str, object] = {
        "rows": len(data),
        "start": data["time_local"].iloc[0],
        "end": data["time_local"].iloc[-1],
        "hours": (data["time_local"].iloc[-1] - data["time_local"].iloc[0]).total_seconds() / 3600,
        "gaps": int((steps > limits.gap_s).sum()),
        "max_gap_s": float(steps.max()) if steps.notna().any() else 0.0,
        "backward": int((steps < 0).sum()),
        "status_bad_frac": float((data["status"] != 1).mean()) if "status" in data else None,
    }
    expected = session.start_from_id
    if expected is not None:
        out["start_offset_s"] = (data["time_local"].iloc[0] - pd.Timestamp(expected)).total_seconds()
    return out


def number_metrics(table: InstrumentTable, limits: Limits) -> Dict[str, object]:
    data = table.data
    number = data["number_cm3"]
    return {
        "pas": _median(data["pas_ms"]),
        "sample_area": table.sample_area,
        "thresholds": table.thresholds_hash,
        "number_median": _median(number),
        "number_max": float(number.max()) if number.notna().any() else None,
        "cloud_s": int(((number > limits.cloud_number_cm3) & (data["lwc_gm3"] > limits.cloud_lwc_gm3)).sum()),
        "high_number_s": int((number > limits.coincidence_number_cm3).sum()),
        "dump_low_frac": (
            float((data["dump_spot_v"] < limits.dump_spot_low_v).mean()) if "dump_spot_v" in data else None
        ),
    }


def lwc_metrics(table: InstrumentTable) -> Dict[str, object]:
    data = table.data
    return {
        "lwc_pas": _median(data["pas_ms"]),
        "pressure_mb": _median(data["pressure_mb"]),
        "temperature_c": _median(data["temperature_c"]),
        "wire_temp_c": _median(data["wire_temp_c"]),
        "v24": _median(data["v24"]),
        "v8": _median(data["v8"]),
        "hotwire_median": _median(data["lwc_gm3"]),
    }


def best_lag(a: np.ndarray, b: np.ndarray, search: int) -> int:
    """b 相對 a 的最佳平移秒數(正值 = b 落後 a)。"""
    a = a - a.mean()
    b = b - b.mean()

    def score(lag: int) -> float:
        left = a[max(0, -lag): len(a) - max(0, lag)]
        right = b[max(0, lag): len(b) - max(0, -lag)]
        return float(np.dot(left, right))

    return max(range(-search, search + 1), key=score)


def agreement_metrics(cdp: InstrumentTable, lwc: InstrumentTable, limits: Limits) -> Dict[str, object]:
    joined = align_seconds({"cdp": cdp, "lwc": lwc})
    out: Dict[str, object] = {"overlap_s": len(joined)}
    if joined.empty:
        return out
    number = joined["cdp_number_cm3"]
    cdp_lwc = joined["cdp_lwc_gm3"]
    hotwire = joined["lwc_lwc_gm3"]

    clear = hotwire[number < limits.clear_number_cm3].dropna()
    out["clear_s"] = len(clear)
    if len(clear) >= limits.min_clear_s:
        out["baseline"] = float(clear.median())
        out["baseline_noise"] = float(clear.std())
        bins = clear.resample("10min").median().dropna()
        if len(bins) >= 2:
            out["baseline_drift"] = float(bins.max() - bins.min())

    cloud = (number > limits.cloud_number_cm3) & (cdp_lwc > limits.cloud_lwc_gm3)
    pair = pd.DataFrame({"cdp": cdp_lwc[cloud], "hot": hotwire[cloud]}).dropna()
    if len(pair) < limits.min_cloud_s:
        return out
    out["cloud_cdp_lwc"] = float(pair["cdp"].median())
    out["cloud_hot_lwc"] = float(pair["hot"].median())
    if pair["cdp"].std() > 0 and pair["hot"].std() > 0:
        out["corr_1s"] = float(pair["cdp"].corr(pair["hot"]))
    means = pair.resample(f"{limits.corr_window_s}s").agg(["mean", "count"])
    full = means[means[("cdp", "count")] >= limits.corr_window_s * 0.5]
    if len(full) >= 10 and full[("cdp", "mean")].std() > 0 and full[("hot", "mean")].std() > 0:
        out["corr_avg"] = float(full[("cdp", "mean")].corr(full[("hot", "mean")]))
    out["lag_s"] = best_lag(cdp_lwc.fillna(0).to_numpy(), hotwire.fillna(0).to_numpy(), limits.lag_search_s)
    return out


def session_flags(row: Dict[str, object], session: Session, limits: Limits) -> List[Dict[str, str]]:
    """把指標轉成問題清單。所有文字都是事實陳述,原因判斷留給使用者。"""
    flags: List[Dict[str, str]] = []

    def add(severity: str, code: str, message: str) -> None:
        flags.append({"severity": severity, "code": code, "message": message})

    def has(key: str) -> bool:
        value = row.get(key)
        return value is not None and not (isinstance(value, float) and np.isnan(value))

    if not session.instruments:
        add(INFO, "NO_DATA", "資料夾內沒有儀器 1 Hz 檔")
    names = " ".join(session.files)
    if session.instruments and "Original" not in names:
        add(INFO, "NO_ORIGINAL_INI", "缺少 Original*.ini")
    if session.instruments and ".raw" not in names:
        add(INFO, "NO_RAW", "缺少 .raw,PADS 可能無法回放")

    if row.get("backward"):
        add(ERROR, "TIME_BACKWARD", f"時間倒退 {row['backward']} 次")
    if row.get("gaps"):
        add(WARN, "GAPS", f"超過 {limits.gap_s:g} 秒的中斷 {row['gaps']} 次,最長 {row['max_gap_s']:.0f} 秒")
    if has("start_offset_s") and abs(row["start_offset_s"]) > limits.start_offset_s:
        add(WARN, "START_MISMATCH", f"第一筆時間與資料夾名稱相差 {row['start_offset_s']:.0f} 秒")
    if has("status_bad_frac") and row["status_bad_frac"] > limits.status_bad_frac:
        add(WARN, "STATUS", f"Status 不是 1 的列佔 {row['status_bad_frac']:.1%}")
    if session.instruments and row.get("log_ending") is None:
        add(INFO, "NO_LOG", "缺少這次記錄的紀錄檔,無法判斷怎麼結束")
    elif session.instruments and row.get("log_ending") in ("no_stop", "empty"):
        add(WARN, "ABNORMAL_END", "紀錄檔沒有「Recording stopped」,可能是斷電或當機")
    if row.get("comm_errors"):
        detail = f"PADS 與儀器通訊錯誤 {row['comm_errors']} 次"
        add(WARN if row.get("gaps") else INFO, "COMM_ERRORS", detail + ("(同一檔案內有中斷)" if row.get("gaps") else ""))
    if row.get("log_resets"):
        add(INFO, "LOG_RESET", f"紀錄中 PADS 重置 {row['log_resets']} 次")

    low, high = limits.pas_plausible
    if has("pas") and not low <= row["pas"] <= high:
        add(WARN, "PAS_IMPLAUSIBLE", f"CDP/FM 流速設定 {row['pas']:g} m/s 超出 {low:g}–{high:g}")
    if has("pas") and has("lwc_pas") and abs(row["pas"] - row["lwc_pas"]) > 0.01:
        add(
            WARN,
            "PAS_MISMATCH",
            f"CDP {row['pas']:g} 與 LWC-300 {row['lwc_pas']:g} m/s 不同"
            f"(若兩者實際相同,CDP 濃度與 LWC 需乘 {row['pas'] / row['lwc_pas']:.2f})"
            if row["lwc_pas"]
            else "LWC-300 流速設定為 0",
        )
    for rule in limits.expected_pas:
        if re.search(rule["match"], session.rel_dir):
            for key, column in (("cdp", "pas"), ("lwc", "lwc_pas")):
                if key in rule and has(column) and abs(row[column] - rule[key]) > 0.01:
                    add(WARN, "PAS_UNEXPECTED", f"{key.upper()} 流速 {row[column]:g},預期 {rule[key]:g} m/s")

    if has("dump_low_frac") and row["dump_low_frac"] > limits.dump_spot_low_frac:
        add(WARN, "DUMP_SPOT_LOW", f"Dump Spot 低於 {limits.dump_spot_low_v:g} V 佔 {row['dump_low_frac']:.1%}")
    if row.get("high_number_s", 0) >= limits.coincidence_min_s:
        hours = row["high_number_s"] / 3600
        add(INFO, "HIGH_NUMBER", f"濃度超過 {limits.coincidence_number_cm3:g} /cm³ 共 {hours:.2f} 小時,重合誤差可能明顯")

    if has("v24") and not limits.v24_range[0] <= row["v24"] <= limits.v24_range[1]:
        add(INFO, "V24_RANGE", f"24V_Supply 讀值 {row['v24']:.3g},不像 24 V")
    if has("v8") and not limits.v8_range[0] <= row["v8"] <= limits.v8_range[1]:
        add(INFO, "V8_RANGE", f"8V_Supply 讀值 {row['v8']:.3g},不像 8 V")
    if has("baseline") and abs(row["baseline"]) > limits.baseline_abs_gm3:
        add(WARN, "BASELINE_OFFSET", f"LWC-300 晴空基線 {row['baseline']:+.3f} g/m³")
    if has("baseline_drift") and row["baseline_drift"] > limits.baseline_drift_gm3:
        add(WARN, "BASELINE_DRIFT", f"LWC-300 晴空基線漂移 {row['baseline_drift']:.3f} g/m³(10 分鐘中位數的全距)")
    if has("corr_1s") and row["corr_1s"] < limits.corr_min:
        averaged = f",{limits.corr_window_s} 秒平均後 {row['corr_avg']:.2f}" if has("corr_avg") else ""
        add(WARN, "LOW_AGREEMENT", f"雲中 CDP 與 LWC-300 逐秒相關 {row['corr_1s']:.2f}{averaged}")
    if (
        has("lag_s")
        and has("corr_avg")
        and abs(row["lag_s"]) > limits.lag_max_s
        and row["corr_avg"] >= limits.lag_needs_corr
    ):
        add(WARN, "TIME_LAG", f"CDP 與 LWC-300 最佳對齊差 {row['lag_s']} 秒")

    if has("pressure_mb") and has("config_pressure_mb"):
        diff = row["pressure_mb"] - row["config_pressure_mb"]
        if abs(diff) > limits.config_pressure_diff_mb:
            add(WARN, "PRESSURE_CONFIG_DIFF", f"LWC 壓力 {row['pressure_mb']:g},全域設定 {row['config_pressure_mb']:g} mb")
    if has("temperature_c") and has("config_temperature_c"):
        diff = row["temperature_c"] - row["config_temperature_c"]
        if abs(diff) > limits.config_temperature_diff_c:
            add(WARN, "TEMPERATURE_CONFIG_DIFF", f"LWC 溫度 {row['temperature_c']:g},全域設定 {row['config_temperature_c']:g} °C")
    if has("pressure_mb"):
        for rule in limits.site_pressure:
            if re.search(rule["match"], session.rel_dir):
                if abs(row["pressure_mb"] - rule["pressure_mb"]) > rule.get("tolerance_mb", 15):
                    add(WARN, "SITE_PRESSURE", f"LWC 壓力 {row['pressure_mb']:g} mb,此站預期約 {rule['pressure_mb']:g} mb")
    return flags


def analyse_session(
    session: Session, tables: Dict[str, InstrumentTable], extras: Dict[str, object], limits: Limits
) -> Tuple[Dict[str, object], List[Dict[str, str]]]:
    row: Dict[str, object] = {
        "sid": session.sid,
        "campaign": session.campaign,
        "subcampaign": session.subcampaign,
        "instruments": "+".join(sorted(session.instruments)),
        "path": session.rel_dir,
    }
    row.update(extras)
    primary = tables.get("cdp") or tables.get("fm") or tables.get("lwc")
    usable = {k: t for k, t in tables.items() if not t.data.empty}
    if primary is not None and not primary.data.empty:
        row.update(time_metrics(primary, session, limits))
    number_table = usable.get("cdp") or usable.get("fm")
    if number_table is not None:
        row.update(number_metrics(number_table, limits))
        row["number_source"] = number_table.kind
    if "lwc" in usable:
        row.update(lwc_metrics(usable["lwc"]))
    if "cdp" in usable and "lwc" in usable:
        row.update(agreement_metrics(usable["cdp"], usable["lwc"], limits))
    return row, session_flags(row, session, limits)


SPLIT_LABELS = {
    "restart_now": "立刻開新檔",
    "restart_soon": "停止後 10 分鐘內重新記錄",
    "stopped": "停止記錄",
    "last": "停止記錄(之後沒有資料)",
    "abnormal": "未正常結束",
    "unknown": "無紀錄檔",
}


def add_split_info(sessions: pd.DataFrame) -> pd.DataFrame:
    """加上「這次為什麼結束」與和同型儀器下一筆的間隔。"""
    frame = sessions.copy()
    frame["gap_to_next_s"] = np.nan
    frame["next_sid"] = None
    frame["split_reason"] = None
    if frame.empty or "start" not in frame:
        return frame
    source = frame["number_source"].fillna("lwc") if "number_source" in frame else pd.Series("lwc", index=frame.index)
    starts = pd.to_datetime(frame["start"], errors="coerce")
    ends = pd.to_datetime(frame["end"], errors="coerce")
    for _, group in frame[starts.notna()].groupby(source[starts.notna()]):
        order = starts.loc[group.index].sort_values().index
        for current, following in zip(order, order[1:]):
            gap = (starts[following] - ends[current]).total_seconds()
            if gap >= 0:  # 重疊表示是副本或另一台電腦,不算接續
                frame.loc[current, "gap_to_next_s"] = gap
                frame.loc[current, "next_sid"] = frame.loc[following, "sid"]

    def reason(row) -> Optional[str]:
        if pd.isna(row.get("start")):
            return None
        ending = row.get("log_ending")
        gap = row.get("gap_to_next_s")
        if ending is None or (isinstance(ending, float) and np.isnan(ending)):
            return SPLIT_LABELS["unknown"]
        if ending in ("no_stop", "empty"):
            return SPLIT_LABELS["abnormal"]
        if ending == "restarted" or (pd.notna(gap) and gap <= 5):
            return SPLIT_LABELS["restart_now"]
        if pd.notna(gap) and gap <= 600:
            return SPLIT_LABELS["restart_soon"]
        return SPLIT_LABELS["stopped"] if pd.notna(gap) else SPLIT_LABELS["last"]

    frame["split_reason"] = frame.apply(reason, axis=1)
    return frame


def campaign_flags(sessions: pd.DataFrame) -> List[Dict[str, str]]:
    """同一批次內設定不一致的項目。"""
    flags: List[Dict[str, str]] = []
    # FM-100 的 Applied PAS 常隨實測變動,且 CDP 與 FM 是不同探頭,所以探頭設定只在同型儀器內比較。
    checks = (
        ("pas", "PAS_MIXED", WARN, "CDP 流速設定", "cdp"),
        ("lwc_pas", "LWC_PAS_MIXED", WARN, "LWC-300 流速設定", None),
        ("wire_temp_c", "WIRE_MIXED", INFO, "LWC-300 線溫", None),
        ("sample_area", "AREA_MIXED", WARN, "CDP 取樣面積", "cdp"),
        ("sample_area", "AREA_MIXED", WARN, "FM-100 取樣面積", "fm"),
        ("thresholds", "THRESHOLDS_MIXED", WARN, "CDP 粒徑門檻", "cdp"),
        ("thresholds", "THRESHOLDS_MIXED", WARN, "FM-100 粒徑門檻", "fm"),
    )
    for campaign, group in sessions.groupby("campaign", sort=True):
        for column, code, severity, label, source in checks:
            if column not in group:
                continue
            subset = group if source is None else group[group["number_source"] == source]
            counts = subset[column].dropna().value_counts()
            if len(counts) > 1:
                detail = "、".join(f"{value:g}" if isinstance(value, float) else str(value) for value in counts.index)
                flags.append(
                    {"campaign": campaign, "severity": severity, "code": code, "message": f"{label}混用:{detail}"}
                )
    return flags
