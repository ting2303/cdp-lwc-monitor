"""PADS 檔案格式的共用定義。這裡只用標準函式庫,讓即時面板不必依賴 pandas。"""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Dict, List, Optional

ENCODING = "cp950"  # 繁中 Windows 的 PADS 會以 Big5 寫入時區名稱與路徑
MARKER = "****"

# PADS 欄名 -> 統一欄名。
CDP_COLUMNS = {
    "End Seconds": "end_seconds",
    "Day of Year": "day_of_year",
    "Year": "year",
    "Status": "status",
    "Number Conc (#/cm^3)": "number_cm3",
    "LWC (g/m^3)": "lwc_gm3",
    "MVD (um)": "mvd_um",
    "ED (um)": "ed_um",
    "Applied PAS (m/s)": "pas_ms",
    "Dump Spot Monitor (V)": "dump_spot_v",
    "Laser Current (mA)": "laser_ma",
    "Laser Temp (C)": "laser_temp_c",
    "Oversize Particles": "oversize",
}
LWC_COLUMNS = {
    "End_Seconds": "end_seconds",
    "Day_of_Year": "day_of_year",
    "Year": "year",
    "Status": "status",
    "LWC-AveragedDAP": "lwc_gm3",
    "LWC-CalculatedDAP": "lwc_calculated_gm3",
    "DAP-Averaged": "dap_averaged",
    "EffectiveWireTemp(C)": "wire_temp_c",
    "24V_Supply": "v24",
    "8V_Supply": "v8",
    "PAS(m/s)": "pas_ms",
    "Static Pressure (mB)": "pressure_mb",
    "Ambient Temperature (C)": "temperature_c",
}

_INSTRUMENT_FILE = re.compile(r"^\d\d(LWC 300_|CDP PBP|CDP|FM 100)(\d{14})\.csv$")
BIN_COLUMN = re.compile(r"^(?:CDP|Fog Monitor) Bin (\d+)$")


def instrument_kind(name: str) -> Optional[str]:
    match = _INSTRUMENT_FILE.match(name)
    if not match:
        return None
    label = match.group(1)
    if label.startswith("LWC"):
        return "lwc"
    if label.startswith("FM"):
        return "fm"
    return "cdp"


def session_id(name: str) -> Optional[str]:
    match = _INSTRUMENT_FILE.match(name)
    return match.group(2) if match else None


def parse_key_values(lines: List[str]) -> Dict[str, str]:
    """把 key=value 的設定行轉成 dict(同名鍵保留第一個)。"""
    out: Dict[str, str] = {}
    for line in lines:
        key, sep, value = line.partition("=")
        if sep:
            out.setdefault(key.strip(), value.strip())
    return out


def as_float(value: Optional[str]) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def parse_utc_offset(text: Optional[str]) -> timedelta:
    """解析 'Offset from UTC = 08:00:00' 的值;讀不到時視為台灣時間 +8。"""
    match = re.match(r"^\s*(-?)(\d+):(\d+)(?::(\d+))?", text or "")
    if not match:
        return timedelta(hours=8)
    sign = -1 if match.group(1) else 1
    return sign * timedelta(
        hours=int(match.group(2)), minutes=int(match.group(3)), seconds=int(match.group(4) or 0)
    )


def parse_sizes(header: Dict[str, str]) -> List[float]:
    """檔頭 Sizes=<30>3,4,... 是各粒徑區間的上限(µm)。"""
    value = header.get("Sizes", "")
    return [float(x) for x in value.partition(">")[2].split(",") if x.strip()]
