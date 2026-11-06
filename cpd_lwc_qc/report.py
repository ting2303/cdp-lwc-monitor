"""產生 CSV 明細與單一 HTML 品質報告(不依賴網路)。"""
from __future__ import annotations

import html
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

SEVERITY_LABEL = {"error": "錯誤", "warn": "警告", "info": "提示"}
FLAG_HELP = {
    "PAS_MISMATCH": "同次量測兩台儀器的流速設定不同;CDP 濃度與 LWC 與流速成反比。",
    "PAS_MIXED": "同批次 CDP 流速設定不只一種,批次內直接比較會有落差。",
    "LWC_PAS_MIXED": "同批次 LWC-300 流速設定不只一種。",
    "PAS_IMPLAUSIBLE": "流速設定值不像實際觀測值,可能是測試用設定。",
    "PAS_UNEXPECTED": "與 qc_config.json 指定的流速不同。",
    "BASELINE_OFFSET": "晴空(CDP 濃度 < 1 /cm³)時 LWC-300 應接近 0。",
    "BASELINE_DRIFT": "同次量測中晴空基線有變化,單一常數無法完全校正。",
    "LOW_AGREEMENT": "雲中兩種 LWC 相關偏低;比較逐秒與時間平均可判斷是否主要為雜訊。",
    "TIME_LAG": "只有在平均後相關夠高時才標記,以免雜訊造成誤判。",
    "HIGH_NUMBER": "高濃度時粒子重合可能造成低估個數、高估粒徑。此為提醒,不是已確認的誤差。",
    "V24_RANGE": "現有設定的 24V_Supply 通道使用 P-I-D-T 多項式係數,此讀值不能代表電源電壓。",
    "V8_RANGE": "8V_Supply 換算後遠低於 8 V,請確認監測通道與係數。",
    "DUMP_SPOT_LOW": "原因未判定,可能是開關機過程或雷射、光路狀態,請對照該時段。",
    "GAPS": "主要儀器的資料中斷。",
    "TIME_BACKWARD": "時間倒退,需人工檢查。",
    "START_MISMATCH": "第一筆時間和資料夾名稱不一致。",
    "STATUS": "Status 非 1 的意義請對照儀器手冊。",
    "PRESSURE_CONFIG_DIFF": "只代表兩處不一致,不代表哪一個是錯的。",
    "TEMPERATURE_CONFIG_DIFF": "只代表兩處不一致,不代表哪一個是錯的。",
    "SITE_PRESSURE": "與 qc_config.json 的測站預期壓力不符;若為室內或異地測試可忽略。",
    "WIRE_MIXED": "線溫不同會改變 LWC-300 基線與反應。",
    "AREA_MIXED": "取樣面積不同會直接影響濃度。",
    "THRESHOLDS_MIXED": "粒徑門檻不同會影響粒徑分布。",
    "ABNORMAL_END": "正常停止時紀錄會寫 Recording stopped;沒有這行可能是斷電或當機,紀錄無法分辨。",
    "COMM_ERRORS": "Error 5902、5903 表示 PADS 與儀器通訊失敗,常見於線路或儀器斷線;此時不會分新資料夾。",
    "NO_LOG": "沒有紀錄檔可判斷結束方式。",
    "NO_ORIGINAL_INI": "缺少原始設定紀錄。",
    "NO_RAW": "缺少原始資料檔。",
    "NO_DATA": "沒有可分析的儀器檔。",
    "READ_ERROR": "檔案讀取失敗。",
    "LOG_RESET": "量測中曾重置程式。",
    "D11_CLOCK_OFFSET": "11-D 儀器時鐘與電腦時間不同,與其他儀器對齊前要注意。",
    "D11_GAPS": "11-D 紀錄中斷。",
    "D11_ERR": "11-D Err 欄非 0,代碼意義請對照手冊。",
    "D11_MISSING_KIND": "11-D 匯出檔不齊全。",
    "D11_TIME_BACKWARD": "11-D 時間倒退,需人工檢查。",
    "D11_NO_ROWS": "11-D -L 檔沒有讀到資料,請檢查檔案格式。",
}

CSS = """
:root{color-scheme:light;--bg:#fcfcfb;--card:#ffffff;--text:#0b0b0b;--muted:#52514e;--line:#e4e3de;
--s1:#2a78d6;--s2:#eb6834;--error:#b3261e;--warn:#8a5a00;--info:#2f5f9e;--band:#eeeeea}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#1a1a19;--card:#232322;
--text:#f0efec;--muted:#c3c2b7;--line:#3a3a37;--s1:#3987e5;--s2:#d95926;--error:#f28b82;--warn:#e0b050;--info:#8ab4f8;--band:#2c2c2a}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#1a1a19;--card:#232322;--text:#f0efec;--muted:#c3c2b7;--line:#3a3a37;
--s1:#3987e5;--s2:#d95926;--error:#f28b82;--warn:#e0b050;--info:#8ab4f8;--band:#2c2c2a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.55 "Microsoft JhengHei","PingFang TC","Noto Sans TC",sans-serif}
main{max-width:1180px;margin:auto;padding:24px 16px 64px}h1{font-size:26px;margin:0 0 4px}h2{font-size:19px;margin:36px 0 10px}
.sub{color:var(--muted)}.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:18px 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 14px}
.tile b{display:block;font-size:24px}.tile span{color:var(--muted);font-size:13px}
.wrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:13.5px}th,td{padding:6px 9px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{position:sticky;top:0;background:var(--card);cursor:pointer;white-space:nowrap}td.n{text-align:right;font-variant-numeric:tabular-nums}
.sev{white-space:nowrap;font-weight:600}.sev.error{color:var(--error)}.sev.warn{color:var(--warn)}.sev.info{color:var(--info)}
.chart{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;margin-bottom:12px}
.chart h3{margin:0 0 2px;font-size:15px}.chart p{margin:0 0 6px;color:var(--muted);font-size:13px}
.legend{display:flex;gap:16px;font-size:13px;color:var(--muted);margin-bottom:4px}.key{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px}
svg{width:100%;height:auto;display:block}svg text{fill:var(--muted);font-size:12px}
.controls{display:flex;gap:10px;flex-wrap:wrap;margin:8px 0}input,select{font:inherit;padding:5px 8px;background:var(--card);color:var(--text);border:1px solid var(--line);border-radius:6px}
details{margin:6px 0}summary{cursor:pointer}ul{margin:6px 0;padding-left:20px}
"""

SCRIPT = """
const q=document.getElementById('q'),c=document.getElementById('camp'),s=document.getElementById('sev');
function apply(){const t=q.value.toLowerCase();document.querySelectorAll('#sessions tbody tr').forEach(r=>{
const ok=(!c.value||r.dataset.camp===c.value)&&(!s.value||r.dataset.sev.includes(s.value))&&(!t||r.textContent.toLowerCase().includes(t));
r.style.display=ok?'':'none'})}
[q,c,s].forEach(e=>e.addEventListener('input',apply));
document.querySelectorAll('th').forEach(h=>h.addEventListener('click',()=>{const tb=h.closest('table').tBodies[0],i=h.cellIndex;
const asc=h.dataset.asc!=='1';h.dataset.asc=asc?'1':'0';
const rows=[...tb.rows].sort((a,b)=>{const x=a.cells[i].dataset.v??a.cells[i].textContent,y=b.cells[i].dataset.v??b.cells[i].textContent;
const nx=parseFloat(x),ny=parseFloat(y);const r=(!isNaN(nx)&&!isNaN(ny))?nx-ny:String(x).localeCompare(String(y));return asc?r:-r});
rows.forEach(r=>tb.appendChild(r))}));
"""


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value))


def _fmt(value: object, digits: int = 3) -> str:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return ""
    if isinstance(value, (float, np.floating)):
        return f"{value:.{digits}g}"
    return str(value)


def _gap_text(seconds: object) -> str:
    if seconds is None or (isinstance(seconds, float) and np.isnan(seconds)):
        return ""
    value = float(seconds)
    if value < 90:
        return f"{value:.0f} 秒"
    if value < 5400:
        return f"{value / 60:.0f} 分鐘"
    if value < 172800:
        return f"{value / 3600:.1f} 小時"
    return f"{value / 86400:.0f} 天"


def dot_chart(
    title: str,
    note: str,
    series: Sequence[Tuple[str, str, pd.DataFrame]],
    band: Optional[Tuple[float, float]] = None,
) -> str:
    """時間軸上的點圖。series 為 (名稱, CSS 色彩變數, 含 x/y/label 的表)。"""
    frames = [f.dropna(subset=["x", "y"]) for _, _, f in series]
    if not any(len(f) for f in frames):
        return ""
    xs = pd.concat([f["x"] for f in frames])
    ys = pd.concat([f["y"] for f in frames])
    width, height, left, right, top, bottom = 1100, 250, 58, 14, 10, 28
    x0, x1 = xs.min(), xs.max()
    span = (x1 - x0).total_seconds() or 1.0
    low, high = float(ys.min()), float(ys.max())
    if band:
        low, high = min(low, band[0]), max(high, band[1])
    pad = (high - low) * 0.06 or 0.5
    low, high = low - pad, high + pad

    def px(value: pd.Timestamp) -> float:
        return left + (value - x0).total_seconds() / span * (width - left - right)

    def py(value: float) -> float:
        return top + (high - value) / (high - low) * (height - top - bottom)

    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{_esc(title)}">']
    if band:
        parts.append(
            f'<rect x="{left}" y="{py(band[1]):.1f}" width="{width - left - right}" '
            f'height="{py(band[0]) - py(band[1]):.1f}" fill="var(--band)"/>'
        )
    for tick in np.linspace(low + pad, high - pad, 5):
        y = py(float(tick))
        parts.append(f'<line x1="{left}" x2="{width - right}" y1="{y:.1f}" y2="{y:.1f}" stroke="var(--line)"/>')
        parts.append(f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end">{tick:.3g}</text>')
    years = range(x0.year, x1.year + 2)
    for year in years:
        stamp = pd.Timestamp(year, 1, 1)
        if x0 <= stamp <= x1:
            x = px(stamp)
            parts.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{top}" y2="{height - bottom}" stroke="var(--line)"/>')
            parts.append(f'<text x="{x:.1f}" y="{height - 8}" text-anchor="middle">{year}</text>')
    for (name, color, _), frame in zip(series, frames):
        for row in frame.itertuples():
            parts.append(
                f'<circle cx="{px(row.x):.1f}" cy="{py(row.y):.1f}" r="4" fill="var({color})" '
                f'stroke="var(--card)" stroke-width="1.5"><title>{_esc(name)} {row.y:.3g}\n{_esc(row.label)}</title></circle>'
            )
    parts.append("</svg>")
    legend = ""
    if len(series) > 1:
        keys = "".join(
            f'<span><i class="key" style="background:var({color})"></i>{_esc(name)}</span>' for name, color, _ in series
        )
        legend = f'<div class="legend">{keys}</div>'
    return f'<div class="chart"><h3>{_esc(title)}</h3><p>{_esc(note)}</p>{legend}{"".join(parts)}</div>'


def _series(frame: pd.DataFrame, column: str) -> pd.DataFrame:
    if column not in frame:
        return pd.DataFrame({"x": [], "y": [], "label": []})
    return pd.DataFrame(
        {
            "x": pd.to_datetime(frame["sid"].str[:14], format="%Y%m%d%H%M%S", errors="coerce"),
            "y": pd.to_numeric(frame[column], errors="coerce"),
            "label": frame["sid"] + " " + frame["campaign"],
        }
    )


def _table(frame: pd.DataFrame, columns: Dict[str, str], digits: int = 3) -> str:
    head = "".join(f"<th>{_esc(label)}</th>" for label in columns.values())
    body = []
    for row in frame.to_dict("records"):
        cells = []
        for key in columns:
            value = row.get(key)
            numeric = isinstance(value, (int, float, np.integer, np.floating)) and not isinstance(value, bool)
            cells.append(f'<td class="n">{_esc(_fmt(value, digits))}</td>' if numeric else f"<td>{_esc(_fmt(value))}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f'<div class="wrap"><table><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def write_outputs(
    out: Path,
    sessions: pd.DataFrame,
    flags: pd.DataFrame,
    campaign_flags: pd.DataFrame,
    d11: pd.DataFrame,
    d11_flags: pd.DataFrame,
    meta: Dict[str, object],
) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    sessions.to_csv(out / "sessions.csv", index=False, encoding="utf-8-sig")
    flags.to_csv(out / "flags.csv", index=False, encoding="utf-8-sig")
    campaign_flags.to_csv(out / "campaign_flags.csv", index=False, encoding="utf-8-sig")
    d11.to_csv(out / "d11_files.csv", index=False, encoding="utf-8-sig")
    d11_flags.to_csv(out / "d11_flags.csv", index=False, encoding="utf-8-sig")

    all_flags = pd.concat(
        [flags.assign(scope="量測"), campaign_flags.assign(scope="批次"), d11_flags.assign(scope="11-D")],
        ignore_index=True,
    )
    summary = pd.DataFrame(columns=["severity", "code", "count"])
    if not all_flags.empty:
        summary = all_flags.groupby(["severity", "code"]).size().reset_index(name="count")
        order = {"error": 0, "warn": 1, "info": 2}
        summary = summary.sort_values(by=["severity", "count"], key=lambda s: s.map(order) if s.name == "severity" else -s)

    tiles = [
        (len(sessions), "次量測"),
        (f"{pd.to_numeric(sessions.get('hours'), errors='coerce').sum():,.0f}", "紀錄小時"),
        (sessions["campaign"].nunique() if len(sessions) else 0, "個批次"),
        (int((all_flags["severity"] == "error").sum()) if len(all_flags) else 0, "錯誤"),
        (int((all_flags["severity"] == "warn").sum()) if len(all_flags) else 0, "警告"),
        (len(d11), "組 11-D 檔"),
    ]
    tile_html = "".join(f'<div class="tile"><b>{_esc(v)}</b><span>{_esc(k)}</span></div>' for v, k in tiles)

    rows = []
    for row in summary.itertuples():
        rows.append(
            f'<tr><td class="sev {row.severity}">{SEVERITY_LABEL.get(row.severity, row.severity)}</td>'
            f'<td>{_esc(row.code)}</td><td class="n">{row.count}</td><td>{_esc(FLAG_HELP.get(row.code, ""))}</td></tr>'
        )
    summary_html = (
        '<div class="wrap"><table><thead><tr><th>等級</th><th>代碼</th><th>次數</th><th>說明</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>'
    )

    charts = "".join(
        [
            dot_chart(
                "流速設定",
                "每個點是一次量測。兩台儀器同時量測卻落在不同高度,就是設定不一致。",
                [
                    ("CDP", "--s1", _series(sessions[sessions["number_source"] == "cdp"], "pas")),
                    ("LWC-300", "--s2", _series(sessions, "lwc_pas")),
                ],
            ),
            dot_chart(
                "LWC-300 晴空基線(g/m³)",
                "CDP 濃度低於 1 /cm³ 時的 LWC-300 中位數。灰色帶是 ±0.05 的檢查範圍。",
                [("基線", "--s1", _series(sessions, "baseline"))],
                band=(-0.05, 0.05),
            ),
            dot_chart(
                "雲中 CDP 與 LWC-300 相關係數",
                "比較逐秒與 60 秒平均。若平均後明顯提高,逐秒雜訊是主因;若沒有提高,要往基線漂移或其他原因找。",
                [("逐秒", "--s1", _series(sessions, "corr_1s")), ("60 秒平均", "--s2", _series(sessions, "corr_avg"))],
            ),
        ]
    )

    split_html = "<p>沒有可判斷的記錄。</p>"
    if "split_reason" in sessions and sessions["split_reason"].notna().any():
        counts = sessions["split_reason"].value_counts()
        share = counts / counts.sum()
        meaning = {
            "立刻開新檔": "紀錄顯示停止後同一秒或 5 秒內開始,例如按下新檔或重新記錄",
            "停止後 10 分鐘內重新記錄": "紀錄有停止,之後不久又開始新的記錄",
            "停止記錄": "紀錄有停止,之後隔了一段時間才有下一筆",
            "停止記錄(之後沒有資料)": "紀錄有停止,且這是同型儀器的最後一筆",
            "未正常結束": "紀錄沒有 Recording stopped,可能是斷電或當機",
            "無紀錄檔": "資料夾裡沒有這次的 Log 檔",
        }
        rows_html = "".join(
            f'<tr><td>{_esc(name)}</td><td class="n">{count}</td><td class="n">{share[name]:.0%}</td><td>{_esc(meaning.get(name, ""))}</td></tr>'
            for name, count in counts.items()
        )
        split_html = (
            '<div class="wrap"><table><thead><tr><th>結束方式</th><th>次數</th><th>比例</th><th>依據</th></tr></thead>'
            f"<tbody>{rows_html}</tbody></table></div>"
            '<p class="sub">依 PADS 紀錄檔和下一筆的開始時間判斷。PADS 不會跨天或依時間自動分檔(New File=Never);儀器斷線時同一檔案會留下空白,不會分新資料夾。</p>'
        )

    if len(sessions):
        grouped = sessions.groupby("campaign", sort=True)
        first_cols = {
            "n": grouped.size(),
            "hours": pd.to_numeric(sessions["hours"], errors="coerce").groupby(sessions["campaign"]).sum()
            if "hours" in sessions
            else None,
        }
        campaign_table = pd.DataFrame({k: v for k, v in first_cols.items() if v is not None}).reset_index()
        for column in ("pas", "lwc_pas", "pressure_mb", "temperature_c", "wire_temp_c"):
            if column in sessions:
                campaign_table[column] = campaign_table["campaign"].map(
                    grouped[column].agg(lambda s: "、".join(f"{v:g}" for v in sorted(s.dropna().unique())))
                )
        if "baseline" in sessions:
            campaign_table["baseline"] = campaign_table["campaign"].map(grouped["baseline"].median())
        for column in ("corr_1s", "corr_avg"):
            if column in sessions:
                campaign_table[column] = campaign_table["campaign"].map(grouped[column].median())
        campaign_html = _table(
            campaign_table,
            {
                "campaign": "批次",
                "n": "次數",
                "hours": "小時",
                "pas": "CDP/FM 流速",
                "lwc_pas": "LWC 流速",
                "pressure_mb": "LWC 壓力",
                "temperature_c": "LWC 溫度",
                "wire_temp_c": "線溫",
                "baseline": "基線中位數",
                "corr_1s": "逐秒相關",
                "corr_avg": "60 秒相關",
            },
        )
    else:
        campaign_html = "<p>沒有量測資料。</p>"

    campaign_flag_html = (
        _table(
            campaign_flags.assign(severity=campaign_flags["severity"].map(SEVERITY_LABEL)),
            {"campaign": "批次", "severity": "等級", "code": "代碼", "message": "內容"},
        )
        if len(campaign_flags)
        else "<p>沒有批次層級的不一致。</p>"
    )

    per_session = {sid: group for sid, group in flags.groupby("path")} if len(flags) else {}
    session_rows = []
    for row in sessions.to_dict("records"):
        items = per_session.get(row["path"])
        severities = " ".join(sorted(set(items["severity"]))) if items is not None else ""
        listing = ""
        if items is not None:
            listing = "<ul>" + "".join(
                f'<li><span class="sev {i.severity}">{SEVERITY_LABEL[i.severity]}</span> {_esc(i.message)}</li>'
                for i in items.itertuples()
            ) + "</ul>"
        session_rows.append(
            f'<tr data-camp="{_esc(row["campaign"])}" data-sev="{severities}">'
            f'<td>{_esc(row["sid"])}</td><td>{_esc(row["campaign"])}</td><td>{_esc(row["instruments"])}</td>'
            f'<td class="n">{_esc(_fmt(row.get("hours")))}</td>'
            f'<td class="n">{_esc(_fmt(row.get("pas")))}</td><td class="n">{_esc(_fmt(row.get("lwc_pas")))}</td>'
            f'<td class="n">{_esc(_fmt(row.get("baseline")))}</td>'
            f'<td>{_esc(_fmt(row.get("split_reason")))}</td>'
            f'<td class="n" data-v="{_fmt(row.get("gap_to_next_s")) or -1}">{_esc(_gap_text(row.get("gap_to_next_s")))}</td>'
            f'<td class="n" data-v="{0 if items is None else len(items)}">{0 if items is None else len(items)}</td>'
            f"<td>{listing}</td></tr>"
        )
    options = "".join(f'<option>{_esc(c)}</option>' for c in sorted(sessions["campaign"].unique())) if len(sessions) else ""

    d11_html = (
        _table(
            d11,
            {
                "file": "檔案",
                "kinds": "匯出類型",
                "hours": "小時",
                "median_step_s": "間隔(秒)",
                "gaps": "中斷",
                "pressure_mb": "壓力",
                "clock_offset_s": "時鐘差(秒)",
                "err_nonzero_frac": "Err≠0 比例",
            },
        )
        if len(d11)
        else "<p>沒有 11-D 檔。</p>"
    )

    notes = "".join(f"<li>{_esc(n)}</li>" for n in meta.get("notes", []))
    page = f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>CDP LWC 品質報告</title><style>{CSS}</style></head><body><main>
<h1>CDP / LWC 資料品質報告</h1>
<div class="sub">資料來源:{_esc(meta.get("root"))} · 產生時間:{_esc(meta.get("generated"))} · 工具版本 {_esc(meta.get("version"))}</div>
<div class="tiles">{tile_html}</div>
<p class="sub">本報告只標記需要查看的地方,不會更動原始資料。標記是依固定門檻產生的提醒,不等於資料一定有誤;門檻可在 qc_config.json 調整。</p>
{f"<ul>{notes}</ul>" if notes else ""}
<h2>問題總覽</h2>{summary_html}
<h2>跨批次趨勢</h2>{charts}
<h2>記錄怎麼分檔</h2>{split_html}
<h2>批次摘要</h2>{campaign_html}
<h2>批次內設定混用</h2>{campaign_flag_html}
<h2>每次量測</h2>
<div class="controls"><input id="q" placeholder="搜尋編號或訊息" aria-label="搜尋">
<select id="camp" aria-label="批次"><option value="">全部批次</option>{options}</select>
<select id="sev" aria-label="等級"><option value="">全部等級</option><option value="error">有錯誤</option><option value="warn">有警告</option><option value="info">有提示</option></select></div>
<div class="wrap"><table id="sessions"><thead><tr><th>編號</th><th>批次</th><th>儀器</th><th>小時</th><th>CDP/FM 流速</th><th>LWC 流速</th><th>基線</th><th>結束方式</th><th>到下一筆</th><th>標記數</th><th>內容</th></tr></thead>
<tbody>{"".join(session_rows)}</tbody></table></div>
<h2>11-D 檔案</h2>{d11_html}
</main><script>{SCRIPT}</script></body></html>"""
    target = out / "report.html"
    target.write_text(page, encoding="utf-8")
    return target
