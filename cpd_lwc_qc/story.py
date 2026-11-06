"""把 CDP、LWC-300、11-D 同時間的資料寫成一段看得懂的敘述。

每句話都來自這段時間的數字或過去資料的統計;原因沒有驗證過的,文字會直接說明。
"""
from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np

from .diagnose import CLOUD_LWC, CLOUD_NUMBER, QUIET_NUMBER, Aligned, _median, _percent_rank

MIN_MINUTES_FOR_STATE = 10
MAX_PHASES = 6
MIN_DAYS_FOR_SITE = 10
SIZE_RANGES = [(0.25, 1.0, "1 µm 以下"), (1.0, 2.5, "1–2.5 µm"), (2.5, 10.0, "2.5–10 µm")]


def fmt(value: Optional[float], digits: int = 3) -> str:
    if value is None or not np.isfinite(value):
        return "—"
    return f"{value:.{digits}g}"


def clock(a: Aligned, minute: int, multi_day: bool) -> str:
    stamp = np.datetime64(a.start + int(minute) * 60, "s").astype(str)
    return f"{stamp[5:10]} {stamp[11:16]}" if multi_day else stamp[11:16]


def build_phases(a: Aligned, number: np.ndarray, lwc: np.ndarray, mvd: np.ndarray, pm25: np.ndarray) -> List[dict]:
    """把時間切成粗的階段,不追蹤每一小段雲。"""
    block = max(30, int(np.ceil(a.count / 48)))
    cloud = (number > CLOUD_NUMBER) & (lwc > CLOUD_LWC)
    has_data = np.isfinite(number) | np.isfinite(pm25)
    states: List[str] = []
    for first in range(0, a.count, block):
        last = min(first + block, a.count)
        if has_data[first:last].mean() < 0.3:
            states.append("none")
            continue
        share = cloud[first:last][has_data[first:last]].mean()
        states.append("cloud" if share >= 0.6 else ("patchy" if share >= 0.1 else "clear"))
    phases = []
    for index, state in enumerate(states):
        if phases and phases[-1]["state"] == state:
            phases[-1]["last"] = index + 1
        else:
            phases.append({"state": state, "first": index, "last": index + 1})

    def merge(position: int) -> None:
        phase = phases.pop(position)
        if position > 0 and (position >= len(phases) or phases[position - 1]["last"] - phases[position - 1]["first"] >= phases[position]["last"] - phases[position]["first"]):
            phases[position - 1]["last"] = phase["last"]
        elif phases:
            phases[position]["first"] = phase["first"]

    # 太短的階段併到旁邊,再把相同狀態接起來,故事才不會太碎
    while len(phases) > 1:
        lengths = [p["last"] - p["first"] for p in phases]
        shortest = int(np.argmin(lengths))
        if len(phases) <= MAX_PHASES and lengths[shortest] > 1:
            break
        merge(shortest)
        joined = [phases[0]]
        for phase in phases[1:]:
            if phase["state"] == joined[-1]["state"]:
                joined[-1]["last"] = phase["last"]
            else:
                joined.append(phase)
        phases = joined
    out = []
    for phase in phases:
        first, last = phase["first"] * block, min(phase["last"] * block, a.count)
        chosen = cloud[first:last]
        out.append({
            "state": phase["state"],
            "first": first,
            "last": last,
            "number": _median(number[first:last][chosen]),
            "lwc": _median(lwc[first:last][chosen]),
            "mvd": _median(mvd[first:last][chosen]),
            "pm25": _median(pm25[first:last]),
        })
    return out


def phase_text(phase: dict) -> str:
    pm = f"PM2.5 約 {fmt(phase['pm25'])} µg/m³" if phase["pm25"] is not None else "沒有 11-D 資料"
    if phase["state"] == "none":
        return "沒有資料"
    if phase["state"] == "clear":
        return f"大致無雲,{pm}"
    label = "持續有雲" if phase["state"] == "cloud" else "斷續有雲"
    if phase["number"] is None:
        return f"{label},{pm}"
    return f"{label},雲滴約 {fmt(phase['number'])} /cm³、MVD {fmt(phase['mvd'])} µm、LWC {fmt(phase['lwc'], 2)} g/m³,{pm}"


def timeline_summary(timeline: List[dict]) -> str:
    """挑最長的無雲和最後一段有雲,寫成一句話。"""
    def minutes(item: dict) -> int:
        return item["last"] - item["first"]

    clear = [t for t in timeline if t["state"] == "clear"]
    cloudy = [t for t in timeline if t["state"] in ("cloud", "patchy")]
    parts = []
    if clear:
        longest = max(clear, key=minutes)
        parts.append((longest["first"], f"{longest['from']}–{longest['to']} 大致無雲"))
    if cloudy:
        main = max(cloudy, key=minutes)
        word = "持續有雲" if main["state"] == "cloud" else "斷續有雲"
        parts.append((main["first"], f"{main['from']}–{main['to']} {word}"))
    return ",".join(text for _, text in sorted(parts))


def rank_word(rank: Optional[float], low: str, high: str) -> Optional[str]:
    if rank is None:
        return None
    if rank >= 80:
        return f"{high}(第 {rank:.0f} 百分位)"
    if rank <= 20:
        return f"{low}(第 {rank:.0f} 百分位)"
    return None


def describe_rank(rank: Optional[float], low: str, high: str) -> str:
    if rank is None:
        return ""
    if rank >= 75:
        return high
    if rank <= 25:
        return low
    return "接近平常"


def size_change(store, a: Aligned, cloud: np.ndarray, quiet: np.ndarray) -> List[dict]:
    """無雲和雲中,各粒徑範圍的 11-D 粒子數變化。"""
    series = getattr(store, "d11", None)
    if series is None:
        return []
    low, high = np.searchsorted(series.time, [a.start, a.end])
    if high <= low:
        return []
    index = ((series.time[low:high] - a.start) // 60).astype(int)
    inside, outside = cloud[index], quiet[index]
    if inside.sum() < MIN_MINUTES_FOR_STATE or outside.sum() < MIN_MINUTES_FOR_STATE:
        return []
    edges = series.edges
    widths = np.log10(edges[1:] / edges[:-1])
    mid = np.sqrt(edges[:-1] * edges[1:])
    spectrum = series.spectrum[low:high]
    out = []
    for first, last, label in SIZE_RANGES:
        chosen = (mid >= first) & (mid < last)
        if not chosen.any():
            continue
        total = np.nansum(spectrum[:, chosen] * widths[chosen], axis=1)
        clear, cloudy = _median(total[outside]), _median(total[inside])
        if clear and clear > 0 and cloudy is not None:
            out.append({"label": label, "clear": clear, "cloud": cloudy, "ratio": cloudy / clear})
    return out


def day_metrics(store, text: str) -> Optional[dict]:
    """某一天的特徵,用來和其他日子比較。"""
    day = int(np.datetime64(text + "T00:00:00", "s").astype("int64"))
    a = Aligned(store, day, day + 86400)
    number, lwc = a.get("cdp", "Number Conc (#/cm^3)"), a.get("cdp", "LWC (g/m^3)")
    mvd, pm25 = a.get("cdp", "MVD (um)"), a.get("d11", "PM2.5")
    cloud = (number > CLOUD_NUMBER) & (lwc > CLOUD_LWC)
    quiet = number < QUIET_NUMBER
    return {
        "day": text,
        "cloud_hours": float(cloud.sum()) / 60,
        "clear_hours": float(quiet.sum()) / 60,
        "number": _median(number[cloud]),
        "lwc": _median(lwc[cloud]),
        "mvd": _median(mvd[cloud]),
        "pm_clear": _median(pm25[quiet]),
        "pm_cloud": _median(pm25[cloud]),
    }


def _table_rank(values: List[float], value: Optional[float]) -> Optional[float]:
    if value is None or not values:
        return None
    return float(np.mean(np.array(values) <= value) * 100)


def similar_days(today: dict, table: List[dict], limit: int = 2) -> List[str]:
    keys = ["number", "mvd", "lwc", "pm_clear"]
    pool = [d for d in table if d["day"] != today["day"] and all(d.get(k) is not None for k in keys[:3])]
    if not pool or today.get("number") is None:
        return []
    spreads = {k: np.nanstd([d[k] if d[k] is not None else np.nan for d in pool]) or 1.0 for k in keys}

    def distance(item: dict) -> float:
        return sum(((item[k] - today[k]) / spreads[k]) ** 2 for k in keys if item.get(k) is not None and today.get(k) is not None)

    return [d["day"] for d in sorted(pool, key=distance)[:limit]]


def trust_items(checks: List[dict], a: Aligned, number: np.ndarray, hot: np.ndarray, test_like: bool = False) -> List[dict]:
    status = {c["id"]: c["status"] for c in checks}
    items = []
    if status.get("pas") != "na":
        wrong_speed = test_like or "校正" in next((c["detail"] for c in checks if c["id"] == "pas"), "")
        if wrong_speed:
            items.append({"level": "bad", "what": "濃度和 CDP LWC", "why": "這是實驗室、校正或測試用的設定,絕對值不代表真實的雲。"})
        else:
            items.append({"level": "care", "what": "濃度和 CDP LWC 的絕對值", "why": "和手動流速成反比,目前沒有實測風速可以確認;同一天內的相對變化比較可靠。"})
    if status.get("dump") == "problem":
        items.append({"level": "bad", "what": "CDP 粒徑和濃度", "why": "無粒子時 Dump Spot 偏低,可能是鏡窗起霧或結冰,先確認再使用。"})
    elif status.get("coincidence") == "watch":
        items.append({"level": "care", "what": "高濃度時段的 CDP 粒徑", "why": "濃度經常超過 300 /cm³,粒子重合可能讓粒徑偏大、個數偏低。"})
    elif status.get("pas") != "na":
        items.append({"level": "good", "what": "CDP 粒徑和 MVD", "why": "不直接受流速設定影響,這段時間也沒有發現光路異常;2026-10 的 32 µm 玻璃珠比對相符。"})
    if np.isfinite(hot).any():
        if status.get("baseline") in ("problem", "watch") or status.get("pressure") in ("problem", "watch"):
            items.append({"level": "bad", "what": "LWC-300 的絕對值", "why": "基線或手動壓力有異常,不建議當定量結果。"})
        else:
            items.append({"level": "care", "what": "LWC-300 的絕對值", "why": "訊號常小於背景漂移,且受手動流速和溫度影響,不建議單獨當定量結果。"})
    if status.get("d11") == "problem":
        items.append({"level": "care", "what": "11-D 資料", "why": "Err 欄非 0 的比例偏高,代碼意義要對照手冊。"})
    elif status.get("d11") != "na":
        items.append({"level": "good", "what": "11-D 的粒子數變化", "why": "沒有發現 Err 異常;PM 質量另外受 C-factor 影響。雲中的數值是否受進氣影響尚未查明。"})
    return items


def build_story(store, start: int, end: int, normal: Optional[dict], checks: List[dict], site: Optional[str], table: Optional[List[dict]] = None) -> dict:
    a = Aligned(store, start, end)
    number, lwc = a.get("cdp", "Number Conc (#/cm^3)"), a.get("cdp", "LWC (g/m^3)")
    mvd, hot = a.get("cdp", "MVD (um)"), a.get("lwc", "LWC-AveragedDAP")
    pm25 = a.get("d11", "PM2.5")
    cloud = (number > CLOUD_NUMBER) & (lwc > CLOUD_LWC)
    quiet = number < QUIET_NUMBER
    instruments = [name for key, name in (("cdp", "CDP"), ("lwc", "LWC-300"), ("d11", "11-D")) if a.present.get(key, np.zeros(1)).any()]
    if not instruments:
        return {"headline": "這段時間沒有資料。", "timeline": [], "sections": [], "trust": []}

    multi_day = a.count > 1440
    cloud_hours = cloud.sum() / 60
    phases = build_phases(a, number, lwc, mvd, pm25)
    timeline = [{"from": clock(a, p["first"], multi_day), "to": clock(a, p["last"], multi_day) if p["last"] < a.count or multi_day else "24:00", "state": p["state"], "text": phase_text(p), "first": p["first"], "last": p["last"]} for p in phases]

    pas = _median(a.get("cdp", "Applied PAS (m/s)"))
    test_like = site == "實驗室" or (pas is not None and not 2 <= pas <= 20)
    sections: List[dict] = []
    highlights: List[str] = []
    table = table or []
    same_site = [d for d in table if d.get("site") == site] if site else []
    pool = same_site if len(same_site) >= MIN_DAYS_FOR_SITE else table
    pool_label = f"{site}其他有雲的 {len(pool)} 天" if pool is same_site and site else f"其他有雲的 {len(pool)} 天"
    median_number, median_mvd, median_lwc = _median(number[cloud]), _median(mvd[cloud]), _median(lwc[cloud])

    if test_like:
        headline = f"這看起來是實驗室或校正資料:{'、'.join(instruments)} 有資料,濃度和 LWC 不代表真實的雲。"
    elif cloud_hours < 0.25:
        headline = f"這段時間幾乎沒有雲,{'、'.join(instruments)} 有資料,主要能看的是背景氣膠。"
    else:
        rank_n = _table_rank([d["number"] for d in pool if d.get("number") is not None], median_number)
        rank_d = _table_rank([d["mvd"] for d in pool if d.get("mvd") is not None], median_mvd)
        rank_l = _table_rank([d["lwc"] for d in pool if d.get("lwc") is not None], median_lwc)
        highlights += [w for w in (rank_word(rank_n, "雲滴偏少", "雲滴偏多"), rank_word(rank_d, "水滴偏小", "水滴偏大"), rank_word(rank_l, "含水量偏低", "含水量偏高")) if w]
        headline = f"{timeline_summary(timeline)}。" if timeline_summary(timeline) else f"有雲約 {cloud_hours:.1f} 小時。"
        cloud_phases = [p for p in phases if p["state"] in ("cloud", "patchy") and p["mvd"] is not None]
        if len(cloud_phases) >= 2 and abs(cloud_phases[-1]["mvd"] - cloud_phases[0]["mvd"]) >= 2:
            trend = "變大" if cloud_phases[-1]["mvd"] > cloud_phases[0]["mvd"] else "變小"
            highlights.append(f"越後面的雲水滴{trend}:MVD 從 {fmt(cloud_phases[0]['mvd'])} 到 {fmt(cloud_phases[-1]['mvd'])} µm")
        lines = [f"雲滴濃度中位數 {fmt(median_number)} /cm³、MVD {fmt(median_mvd)} µm、CDP LWC {fmt(median_lwc, 2)} g/m³。"]
        if pool:
            lines.append(f"和{pool_label}相比,濃度排第 {fmt(rank_n, 2)} 百分位、MVD 第 {fmt(rank_d, 2)} 百分位、LWC 第 {fmt(rank_l, 2)} 百分位。")
        pair = cloud & np.isfinite(mvd)
        if pair.sum() >= 30 and np.std(lwc[pair]) > 0:
            by_number = float(np.corrcoef(lwc[pair], number[pair])[0, 1])
            by_size = float(np.corrcoef(lwc[pair], mvd[pair])[0, 1])
            if max(by_number, by_size) >= 0.3:
                driver = "水滴大小" if by_size > by_number else "雲滴數量"
                lines.append(f"這天 LWC 的起伏主要跟著{driver}變(和 MVD 相關 {by_size:.2f}、和濃度相關 {by_number:.2f})。")
        sections.append({"title": "這天的雲", "lines": lines})

    if a.present.get("d11", np.zeros(1)).any():
        lines = []
        clear_pm, cloud_pm = _median(pm25[quiet]), _median(pm25[cloud])
        if clear_pm is not None:
            ref = (normal or {}).get("reference", {}).get("pm25")
            rank = _percent_rank(ref["percentiles"], clear_pm) if ref else None
            lines.append(f"無雲時 PM2.5 約 {fmt(clear_pm)} µg/m³" + (f",{describe_rank(rank, '比平常乾淨', '比平常髒') if rank is not None else ''}(過去第 {fmt(rank, 2)} 百分位)。" if rank is not None else "。"))
        minutes_cloud, minutes_clear = int((cloud & np.isfinite(pm25)).sum()), int((quiet & np.isfinite(pm25)).sum())
        if not test_like and clear_pm and cloud_pm is not None and minutes_cloud >= MIN_MINUTES_FOR_STATE and minutes_clear >= MIN_MINUTES_FOR_STATE:
            ratio = cloud_pm / clear_pm
            past = [d["pm_cloud"] / d["pm_clear"] for d in table if d.get("pm_cloud") is not None and d.get("pm_clear") and d["day"]]
            rank = _table_rank(past, ratio)
            change = f"雲中降到 {fmt(cloud_pm)} µg/m³,只剩無雲時的 {ratio:.0%}" if ratio < 0.8 else (f"雲中約 {fmt(cloud_pm)} µg/m³,和無雲時差不多" if ratio <= 1.25 else f"雲中反而升到 {fmt(cloud_pm)} µg/m³")
            lines.append(change + (f",過去 {len(past)} 天的中位數是 {np.median(past):.0%}。" if past else "。"))
            if past and rank is not None and (rank <= 25 or rank >= 75):
                highlights.append(f"雲中 PM2.5 是無雲時的 {ratio:.0%},{'比多數日子下降得更多' if rank <= 25 else '不像多數日子有明顯下降'}")
            changes = size_change(store, a, cloud, quiet)
            if changes and ratio < 0.8:
                lines.append("各粒徑的粒子數:" + "、".join(f"{c['label']} {'幾乎沒有' if c['ratio'] < 0.005 else '剩 ' + format(c['ratio'], '.0%')}" for c in changes) + "。")
            if ratio < 0.5:
                lines.append("下降可能是粒子變成雲滴被帶走,也可能和進氣口或濕度有關,目前沒有資料能分辨。")
        if not test_like and median_mvd is not None and clear_pm is not None and table:
            ref_rank = _percent_rank((normal or {}).get("reference", {}).get("pm25", {}).get("percentiles"), clear_pm)
            if ref_rank is not None and (ref_rank >= 75 or ref_rank <= 25):
                lines.append("提醒:過去資料裡,PM2.5 高低和雲滴濃度幾乎無關(相關約 0),和 MVD 只有弱負相關(約 −0.3),不能直接說這天的雲是氣膠造成的。")
        if lines:
            sections.append({"title": "氣膠和雲的關係", "lines": lines})

    if not test_like and np.isfinite(hot[cloud]).any() and cloud.sum() >= 30:
        hot_cloud, hot_clear = _median(hot[cloud]), _median(hot[quiet])
        lines = [f"CDP 算出 {fmt(median_lwc, 2)} g/m³,LWC-300 讀到 {fmt(hot_cloud, 2)} g/m³(無雲時本身就有 {fmt(hot_clear, 2)})。"]
        if hot_cloud is not None and hot_clear is not None and median_lwc:
            share = (hot_cloud - hot_clear) / median_lwc
            if share <= 0:
                lines.append("雲中讀值比無雲時還低,代表 LWC-300 的基線漂移比雲的訊號還大,這天兩者無法比較。")
                highlights.append("LWC-300 的基線漂移比雲的訊號大,這天不能用它驗證 CDP")
            else:
                lines.append(f"扣掉無雲的基線後,LWC-300 約是 CDP 的 {share:.0%};過去多在 10–30%,誰才準還不確定。")
        sections.append({"title": "兩種 LWC", "lines": lines})

    if not test_like and cloud_hours >= 0.25 and table and a.count <= 1440:
        day_text = str(np.datetime64(a.start, "s"))[:10]
        today = next((d for d in table if d["day"] == day_text), None)
        if today:
            similar = similar_days(today, pool)
            if similar:
                sections.append({"title": "可以比較的日子", "lines": ["雲的特徵最接近:" + "、".join(similar) + "。"], "days": similar})

    if highlights:
        sections.insert(0, {"title": "這段時間最值得注意", "lines": [f"• {item}" for item in highlights[:5]]})
    for item in timeline:
        item.pop("first"), item.pop("last")
    return {"headline": headline, "timeline": timeline, "sections": sections, "trust": trust_items(checks, a, number, hot, bool(test_like)), "test_like": bool(test_like)}
