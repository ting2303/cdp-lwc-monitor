"""CDP + 11-D 離線視覺化。只在本機提供網頁,不需要網路。

python -m cpd_lwc_qc.viewer --cache cache
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import numpy as np

from .diagnose import CHRONIC, MANUAL_CHANNELS, Aligned, diagnose, run_checks
from .normal import CLOUD_NUMBER, site_of
from .story import build_story, day_metrics
from .store import Channels, Series, load, load_channels

HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)) / "cpd_lwc_qc"
PAGE = HERE / "viewer.html"
BUNDLED_NORMAL = HERE / "normal_reference.json"
MIN_STEP_S = 60


def _epoch(text: str) -> int:
    return int(np.datetime64(text.replace(" ", "T"), "s").astype("int64"))


def _clean(values: np.ndarray) -> List[Optional[float]]:
    """四捨五入到 4 位有效數字,NaN 變成 null,回應會小很多。"""
    return [None if not np.isfinite(v) else float(f"{v:.4g}") for v in values.tolist()]


INSTRUMENTS = {"cdp": "CDP", "lwc": "LWC-300", "d11": "11-D", "fm": "FM-100"}
DAY = 86400


def _day_text(day: int) -> str:
    return str(np.datetime64(int(day) * DAY, "s").astype("datetime64[D]"))


def bucket(series: Optional[Series], names: List[str], start: int, end: int, step: int) -> Optional[dict]:
    if series is None:
        return None
    low, high = np.searchsorted(series.time, [start, end])
    if high <= low:
        return None
    count = int(np.ceil((end - start) / step))
    index = ((series.time[low:high] - start) // step).astype(int)

    def mean(values: np.ndarray) -> np.ndarray:
        good = np.isfinite(values)
        totals = np.bincount(index[good], weights=values[good], minlength=count)
        counts = np.bincount(index[good], minlength=count)
        with np.errstate(invalid="ignore", divide="ignore"):
            return totals / counts

    return {
        "series": {n: _clean(mean(series.values[n][low:high])) for n in names},
        "spectrum": [_clean(mean(series.spectrum[low:high, c])) for c in range(series.spectrum.shape[1])],
    }


class Store:
    def __init__(self, cache: Path, allow_empty: bool = False):
        self.cdp, self.d11 = load(cache) if (cache / "index.json").exists() else (None, None)
        if self.cdp is None and self.d11 is None and not allow_empty:
            raise SystemExit("快取是空的,請先執行 python -m cpd_lwc_qc.minute")
        self.channels: Dict[str, Channels] = load_channels(cache) if (cache / "index.json").exists() else {}
        if self.d11 is not None:
            self.channels["d11"] = Channels(self.d11.time, {k: v for k, v in self.d11.values.items() if k != "seconds"})
        for channel in self.channels.values():
            channel.values.pop("seconds", None)
        path = cache / "normal_reference.json"
        if not path.exists():
            path = BUNDLED_NORMAL
        self.normal = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def default_range(self) -> Dict[str, int]:
        """預設顯示最近一段野外 CDP 有雲、且 11-D 也有資料的 3 天。"""
        if self.cdp is not None and self.d11 is not None:
            field = ~np.isin(self.cdp.labels, [i for i, m in enumerate(self.cdp.meta) if site_of(m["campaign"]) == "實驗室"])
            cloudy = field & (self.cdp.values["number_cm3"] > CLOUD_NUMBER)
            common = np.intersect1d(self.cdp.time[cloudy], self.d11.time)
            if len(common):
                return {"start": int(common[-1]) - 3 * 86400, "end": int(common[-1]) + 60}
        one = self.cdp if self.cdp is not None else self.d11
        if one is None:
            return {"start": 0, "end": 0}
        return {"start": int(one.time[-1]) - 3 * 86400, "end": int(one.time[-1]) + 60}

    def calendar(self) -> Dict[str, dict]:
        """每天各儀器有幾分鐘資料。同一分鐘重複的副本只算一次;日期依當地時間。結果會留著。"""
        if hasattr(self, "_calendar"):
            return self._calendar
        days: Dict[str, dict] = {}
        for kind, channel in self.channels.items():
            unique = np.unique((channel.time // 60).astype("int64")) * 60
            day_index, counts = np.unique(unique // DAY, return_counts=True)
            for day, count in zip(day_index.tolist(), counts.tolist()):
                item = days.setdefault(_day_text(day), {"minutes": {}, "campaigns": [], "cloud": 0})
                item["minutes"][kind] = int(count)
        if self.cdp is not None:
            unique, first = np.unique(self.cdp.time, return_index=True)
            number = self.cdp.values["number_cm3"][first]
            lwc = self.cdp.values["lwc_gm3"][first]
            cloudy = unique[(number > CLOUD_NUMBER) & (lwc > 0.02)]
            day_index, counts = np.unique(cloudy // DAY, return_counts=True)
            for day, count in zip(day_index.tolist(), counts.tolist()):
                text = _day_text(day)
                if text in days:
                    days[text]["cloud"] = int(count)
            for series in (self.cdp, self.d11):
                if series is None:
                    continue
                for meta in series.meta:
                    for day in range(_epoch(meta["start"]) // DAY, _epoch(meta["end"]) // DAY + 1):
                        item = days.get(_day_text(day))
                        if item is not None and meta["campaign"] not in item["campaigns"]:
                            item["campaigns"].append(meta["campaign"])
        self.add_day_status(days)
        self._calendar = days
        return days

    def add_day_status(self, days: Dict[str, dict]) -> None:
        """每天需注意幾項,讓日曆可以標出來。結果會留著,不必每次重算。"""
        if not hasattr(self, "_day_status"):
            status = {}
            for text in days:
                start = _epoch(text + " 00:00:00")
                checks = [c for c in run_checks(Aligned(self, start, start + DAY), self, self.normal) if c["id"] not in CHRONIC]
                status[text] = {"problem": sum(c["status"] == "problem" for c in checks), "watch": sum(c["status"] == "watch" for c in checks)}
            self._day_status = status
        for text, item in days.items():
            item.update(self._day_status.get(text, {}))

    def story_days(self) -> List[dict]:
        """每個有雲的野外日子的特徵,給敘述做比較。"""
        if not hasattr(self, "_story_days"):
            table = []
            for text, info in self.calendar().items():
                campaigns = info.get("campaigns") or []
                site = site_of(campaigns[0]) if campaigns else None
                if site == "實驗室" or info.get("cloud", 0) < 60:
                    continue
                row = day_metrics(self, text)
                if row and row["number"] is not None:
                    row["site"] = site
                    table.append(row)
            self._story_days = table
        return self._story_days

    def story(self, start: int, end: int, checks: List[dict]) -> dict:
        info = self.calendar().get(_day_text(start // DAY)) or {}
        campaigns = info.get("campaigns") or []
        site = site_of(campaigns[0]) if campaigns else None
        return build_story(self, start, end, self.normal, checks, site, self.story_days())

    def previous_settings(self, start: int) -> Dict[str, tuple]:
        """前一個有資料的日子用的手動值,用來發現設定被改過。"""
        if not hasattr(self, "_settings_days"):
            self._settings_days = {}
            for kind, name, _, _ in MANUAL_CHANNELS:
                channel = self.channels.get(kind)
                if channel is None or name not in channel.values:
                    continue
                values = channel.values[name]
                good = np.isfinite(values)
                days = (channel.time[good] // DAY).astype("int64")
                table = {}
                for day in np.unique(days).tolist():
                    table[day] = float(np.median(values[good][days == day]))
                self._settings_days[name] = (np.array(sorted(table)), table)
        out = {}
        today = start // DAY
        for name, (days, table) in self._settings_days.items():
            place = int(np.searchsorted(days, today)) - 1
            if place >= 0:
                day = int(days[place])
                out[name] = (_day_text(day), table[day])
        return out

    def channel_list(self) -> Dict[str, dict]:
        return {
            kind: {"label": INSTRUMENTS.get(kind, kind), "names": list(channel.values)}
            for kind, channel in sorted(self.channels.items(), key=lambda kv: list(INSTRUMENTS).index(kv[0]) if kv[0] in INSTRUMENTS else 9)
        }

    def channel_window(self, names: List[str], start: int, end: int, step: int) -> Dict[str, List[Optional[float]]]:
        count = int(np.ceil((end - start) / step))
        out: Dict[str, List[Optional[float]]] = {}
        for full in names[:60]:
            kind, _, name = full.partition("|")
            channel = self.channels.get(kind)
            if channel is None or name not in channel.values:
                continue
            low, high = np.searchsorted(channel.time, [start, end])
            values = channel.values[name][low:high].astype("float64")
            index = ((channel.time[low:high] - start) // step).astype(int)
            good = np.isfinite(values)
            totals = np.bincount(index[good], weights=values[good], minlength=count)
            counts = np.bincount(index[good], minlength=count)
            with np.errstate(invalid="ignore", divide="ignore"):
                out[full] = _clean(totals / counts)
        return out

    def index(self) -> dict:
        campaigns: Dict[str, dict] = {}
        for kind, series in (("cdp", self.cdp), ("d11", self.d11)):
            if series is None:
                continue
            for meta in series.meta:
                name = meta.get("campaign", "?")
                item = campaigns.setdefault(name, {"name": name, "start": _epoch(meta["start"]), "end": _epoch(meta["end"]), "cdp": 0, "d11": 0})
                item["start"] = min(item["start"], _epoch(meta["start"]))
                item["end"] = max(item["end"], _epoch(meta["end"]) + 60)
                item[kind] += 1
        present = [s for s in (self.cdp, self.d11) if s is not None]
        return {
            "history": bool(present),
            "calendar": self.calendar(),
            "channels": self.channel_list(),
            "default": self.default_range(),
            "campaigns": sorted(campaigns.values(), key=lambda c: c["start"]),
            "cdp_edges": self.cdp.edges.tolist() if self.cdp is not None else None,
            "d11_edges": self.d11.edges.tolist() if self.d11 is not None else None,
            "normal": self.normal,
        }

    def window(self, start: int, end: int, width: int) -> dict:
        end = max(end, start + MIN_STEP_S)
        width = min(max(width, 50), 1600)
        step = max(MIN_STEP_S, int(np.ceil((end - start) / width / MIN_STEP_S)) * MIN_STEP_S)
        count = int(np.ceil((end - start) / step))
        sessions = []
        if self.cdp is not None:
            for meta in self.cdp.meta:
                if _epoch(meta["start"]) < end and _epoch(meta["end"]) + 60 > start:
                    sessions.append({"sid": meta["sid"], "campaign": meta["campaign"], "start": _epoch(meta["start"]), "end": _epoch(meta["end"]) + 60})
        return {
            "cdp_edges": self.cdp.edges.tolist() if self.cdp is not None else None,
            "d11_edges": self.d11.edges.tolist() if self.d11 is not None else None,
            "start": start,
            "end": end,
            "step": step,
            "count": count,
            "cdp": bucket(self.cdp, ["number_cm3", "lwc_gm3", "hotwire_gm3", "mvd_um", "ed_um", "pas_ms", "dump_spot_v", "laser_ma"], start, end, step),
            "d11": bucket(self.d11, ["PM1", "PM2.5", "PM10", "TSP", "Pres", "rH_i", "Temp_i"], start, end, step),
            "sessions": sessions,
        }


def make_handler(store: Store, feed=None, locator=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def _send(self, body: bytes, kind: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, value: object, status: int = 200) -> None:
            self._send(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"), "application/json; charset=utf-8", status)

        def _local_request(self) -> bool:
            """只接受本機網址,避免其他網站透過瀏覽器讀取路徑或改設定。"""
            host = self.headers.get("Host", "").rsplit(":", 1)[0]
            return host in ("127.0.0.1", "localhost")

        def do_POST(self) -> None:
            url = urlparse(self.path)
            if locator is None or not url.path.startswith("/api/locate"):
                self._json({"error": "not found"}, 404)
                return
            # 自訂標頭會讓跨網站請求被瀏覽器擋下
            if not self._local_request() or self.headers.get("X-Panel") != "1":
                self._json({"error": "只接受面板本身的操作"}, 403)
                return
            if url.path == "/api/locate/start":
                locator.start()
                self._json({"ok": True})
            elif url.path == "/api/locate/cancel":
                locator.cancel()
                self._json({"ok": True})
            elif url.path == "/api/locate/test_discord":
                error = locator.test_message()
                self._json({"ok": error is None, "errors": [error] if error else []}, 200 if error is None else 400)
            elif url.path == "/api/locate/remote":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length > 65536:
                        raise ValueError("內容太大")
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                    errors = locator.apply_remote(body.get("cloud_folder"), body.get("site_name"), body.get("discord_webhook"))
                except (ValueError, AttributeError) as error:
                    errors = [str(error)]
                except OSError as error:
                    errors = [f"無法寫入設定檔:{error}"]
                self._json({"ok": not errors, "errors": errors}, 200 if not errors else 400)
            elif url.path == "/api/locate/apply":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length > 65536:
                        raise ValueError("內容太大")
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                    errors = locator.apply(body.get("pads_roots"), body.get("d11_roots"))
                except (ValueError, AttributeError) as error:
                    errors = [str(error)]
                except OSError as error:
                    errors = [f"無法寫入設定檔:{error}"]
                self._json({"ok": not errors, "errors": errors}, 200 if not errors else 400)
            else:
                self._json({"error": "not found"}, 404)

        def do_GET(self) -> None:
            url = urlparse(self.path)
            if url.path == "/api/locate":
                if locator is None or not self._local_request():
                    self._json({"error": "not found"}, 404)
                else:
                    self._json(locator.state())
                return
            if url.path == "/":
                self._send(PAGE.read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/index":
                index = store.index()
                index["live"] = feed is not None
                index["locate"] = locator is not None
                self._json(index)
            elif url.path == "/api/live":
                if feed is None:
                    self._json({"error": "沒有啟用即時模式"}, 404)
                    return
                query = parse_qs(url.query)
                try:
                    span = min(max(int(query.get("span", ["3600"])[0]), 60), 7 * 86400)
                    width = int(query.get("width", ["800"])[0])
                except ValueError:
                    self._json({"error": "span、width 需要是整數"}, 400)
                    return
                self._json(feed.window(span, width))
            elif url.path == "/api/window":
                query = parse_qs(url.query)
                try:
                    start = int(query["start"][0])
                    end = int(query["end"][0])
                    width = int(query.get("width", ["800"])[0])
                except (KeyError, ValueError):
                    self._json({"error": "start、end 需要是整數"}, 400)
                    return
                self._json(store.window(start, end, width))
            elif url.path == "/api/diagnose":
                query = parse_qs(url.query)
                try:
                    start = int(query["start"][0])
                    end = int(query["end"][0])
                except (KeyError, ValueError):
                    self._json({"error": "start、end 需要是整數"}, 400)
                    return
                if end <= start or end - start > 400 * DAY:
                    self._json({"error": "範圍不正確"}, 400)
                    return
                result = diagnose(store, start, end, store.normal)
                result["story"] = store.story(start, end, result["checks"])
                self._json(result)
            elif url.path == "/api/channels":
                query = parse_qs(url.query)
                try:
                    start = int(query["start"][0])
                    end = int(query["end"][0])
                    step = max(60, int(query["step"][0]))
                    names = query.get("names", [""])[0].split(";")
                except (KeyError, ValueError):
                    self._json({"error": "start、end、step 需要是整數"}, 400)
                    return
                if end <= start or (end - start) / step > 4000:
                    self._json({"error": "範圍不正確"}, 400)
                    return
                self._json(store.channel_window([n for n in names if n], start, end, step))
            else:
                self._json({"error": "not found"}, 404)

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description="CDP + 11-D 視覺化")
    parser.add_argument("--cache", default="cache")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    store = Store(Path(args.cache).resolve())
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(store))
    url = f"http://127.0.0.1:{args.port}/"
    print(f"請開啟 {url},按 Ctrl+C 結束", flush=True)
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
