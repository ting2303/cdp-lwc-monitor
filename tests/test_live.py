"""即時讀檔的測試。"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from cpd_lwc_qc.live import CDP_FIELDS, Feed, Tail, local_now, parse_stamp  # noqa: E402
from cpd_lwc_qc.viewer import Store, make_handler  # noqa: E402

CDP_HEAD = (
    "[Instrument 1]\r\nSample Area (mm^2)=0.267\r\nSizes=<2>26,28\r\n Offset from UTC = 08:00:00\r\n****\r\n"
    "End Seconds,Day of Year,Year,Status,Number Conc (#/cm^3),LWC (g/m^3),MVD (um),ED (um),Applied PAS (m/s),Dump Spot Monitor (V),Laser Current (mA),CDP Bin 1,CDP Bin 2\r\n"
)
LWC_HEAD = (
    "[Instrument 0]\r\n Offset from UTC = 08:00:00\r\n****\r\n"
    "End_Seconds,Day_of_Year,Year,Status,LWC-AveragedDAP,PAS(m/s),Static Pressure (mB),Ambient Temperature (C)\r\n"
)


def row_time(offset=0):
    now = datetime.now() + timedelta(seconds=offset)
    seconds = now.hour * 3600 + now.minute * 60 + now.second
    return seconds, now.timetuple().tm_yday, now.year


def cdp_row(offset=0, number=66.0, dump=4.8, pas=12):
    s, d, y = row_time(offset)
    return f"{s}.5,{d},{y},1,{number},0.49,27,26,{pas},{dump},92,213,0\r\n"


def lwc_row(offset=0, value=0.3, pas=7):
    s, d, y = row_time(offset)
    return f"{s}.5,{d},{y},1,{value},{pas},722,10\r\n"


def feed_for(root, **extra):
    config = {"pads_roots": [str(root / "pads")], "d11_roots": [str(root / "d11")], "stale_seconds": 60, **extra}
    return Feed(config)


class Stamps(unittest.TestCase):
    def test_formats(self):
        base = parse_stamp("2025-03-29 17:36:05")
        self.assertEqual(parse_stamp("2025/3/29 下午 05:36:05"), base)
        self.assertEqual(parse_stamp("2025/3/29 PM 05:36:05"), base)
        self.assertEqual(parse_stamp("2025/3/29 05:36:05 PM"), base)
        self.assertEqual(parse_stamp("2025/3/29 下午 12:00:00") - parse_stamp("2025/3/29 上午 12:00:00"), 12 * 3600)
        self.assertIsNone(parse_stamp("P"))


class Tailing(unittest.TestCase):
    def test_partial_line_waits_and_truncate_restarts(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "a.csv"
            path.write_bytes(b"one\ntw")
            tail = Tail(path)
            self.assertEqual(tail.read_lines(), ["one"])
            with open(path, "ab") as handle:
                handle.write(b"o\nthr")
            self.assertEqual(tail.read_lines(), ["two"])
            path.write_bytes(b"new\n")
            self.assertEqual(tail.read_lines(), ["new"])

    def test_missing_file_reports_error(self):
        tail = Tail(Path("C:/no/such/file.csv"))
        self.assertEqual(tail.read_lines(), [])
        self.assertTrue(tail.error)


class Live(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sid = datetime.now().strftime("%Y%m%d%H%M%S")
        self.folder = self.root / "pads/20261105" / self.sid
        self.folder.mkdir(parents=True)
        (self.root / "d11").mkdir()
        self.cdp = self.folder / f"00CDP PBP{self.sid}.csv"
        self.lwc = self.folder / f"01LWC 300_{self.sid}.csv"

    def tearDown(self):
        self.temp.cleanup()

    def append(self, path, text):
        with open(path, "ab") as handle:
            handle.write(text.encode("cp950"))

    def test_values_match_pads_and_grow_with_file(self):
        self.append(self.cdp, CDP_HEAD + cdp_row(-5))
        self.append(self.lwc, LWC_HEAD + lwc_row(-5))
        feed = feed_for(self.root)
        feed.poll()
        self.assertEqual(feed.cdp.size, 1)
        # 213 顆 ÷(0.267 × 12)= 66.479 /cm³,再除以該區間的 dlogD
        self.assertAlmostEqual(feed.cdp.values[0, len(CDP_FIELDS)] * np.log10(26 / 2), 66.479, places=2)
        self.append(self.cdp, cdp_row(-4)[:15])
        feed.poll()
        self.assertEqual(feed.cdp.size, 1)
        self.append(self.cdp, cdp_row(-4)[15:])
        feed.poll()
        self.assertEqual(feed.cdp.size, 2)
        window = feed.window(600, 800)
        json.dumps(window, allow_nan=False)
        self.assertEqual(max(v for v in window["cdp"]["series"]["number_cm3"] if v is not None), 66.0)
        self.assertEqual(max(v for v in window["cdp"]["series"]["hotwire_gm3"] if v is not None), 0.3)
        self.assertIn("流速設定不同", " ".join(window["status"]["notices"]))

    def test_new_session_is_found(self):
        self.append(self.cdp, CDP_HEAD + cdp_row(-2))
        feed = feed_for(self.root, rescan_seconds=0)
        feed.poll()
        other = self.root / "pads/20261105/20991231235959"
        other.mkdir()
        self.append(other / "00CDP PBP20991231235959.csv", CDP_HEAD + cdp_row(-1))
        feed.poll()
        self.assertEqual(feed.cdp.size, 2)

    def test_status_states(self):
        feed = feed_for(self.root)
        feed.poll()
        codes = {i["label"]: i["code"] for i in feed.status()["instruments"]}
        self.assertEqual(codes, {"CDP": "missing", "LWC-300": "missing", "11-D": "missing"})
        self.append(self.cdp, CDP_HEAD + cdp_row(-300))
        feed2 = feed_for(self.root)
        feed2.poll()
        self.assertEqual({i["label"]: i["code"] for i in feed2.status()["instruments"]}["CDP"], "stale")
        self.append(self.cdp, cdp_row(0))
        feed2.poll()
        self.assertEqual({i["label"]: i["code"] for i in feed2.status()["instruments"]}["CDP"], "ok")

    def test_dump_spot_notice_only_without_particles(self):
        text = CDP_HEAD + "".join(cdp_row(-i, number=0, dump=3.0) for i in range(40, 0, -1))
        self.append(self.cdp, text)
        feed = feed_for(self.root)
        feed.poll()
        self.assertIn("Dump Spot", " ".join(feed.status()["notices"]))

    def test_no_dump_spot_notice_in_cloud(self):
        text = CDP_HEAD + "".join(cdp_row(-i, number=200, dump=3.0) for i in range(40, 0, -1))
        self.append(self.cdp, text)
        feed = feed_for(self.root)
        feed.poll()
        self.assertNotIn("Dump Spot", " ".join(feed.status()["notices"]))

    def test_old_files_are_ignored(self):
        self.append(self.cdp, CDP_HEAD + cdp_row(-5))
        old = time.time() - 3 * 86400
        os.utime(self.cdp, (old, old))
        feed = feed_for(self.root)
        feed.poll()
        self.assertEqual(len(feed.pads), 0)

    def d11_text(self, separator="\t", tail="\t"):
        now = datetime.now()
        stamp = f"{now.year}/{now.month}/{now.day} {'上午' if now.hour < 12 else '下午'} {(now.hour % 12) or 12:02d}:{now.minute:02d}:{now.second:02d}"
        return stamp, separator, tail

    def test_d11_dat_and_csv(self):
        stamp, _, _ = self.d11_text()
        header = f"<Header>\nUnit: counts/liter (differential)\n<Data>\n[d&t2035/12/31 上午 11:50:55]\t0.3 um\t0.5 um\n"
        self.append(self.root / "d11/a-C.dat", header + f"{stamp}\t1000.0\t0.0\t\n")
        self.append(self.root / "d11/a-M.dat", f"<Header>\nUnit: ug/m3\n<Data>\n[d&t]\tTSP [ug/m3]\tPM10 [ug/m3]\tPM2.5 [ug/m3]\n{stamp}\t9.0\t8.0\t7.0\t\n")
        self.append(self.root / "d11/a-dM.dat", f"<Header>\nUnit: dM [ug/m3]\n<Data>\n[d&t]\t0.3 um\t0.5 um\n{stamp}\t1.0\t1.0\t\n")
        self.append(self.root / "d11/b.csv", f"Time,PM2.5,Pres\n{stamp},5.0,723.1\n")
        feed = feed_for(self.root)
        feed.poll()
        self.assertEqual(feed.d11_spec.size, 1)  # dM 不會被當成粒子數
        self.assertAlmostEqual(float(feed.d11_spec.values[0, 0]), 1 / np.log10(0.5 / 0.3), places=4)
        self.assertEqual(feed.d11_mass.size, 2)
        self.assertEqual(feed.d11_log.size, 1)
        window = feed.window(600, 800)
        self.assertEqual({i["label"]: i["code"] for i in window["status"]["instruments"]}["11-D"], "ok")
        self.assertIsNotNone(window["d11"]["spectrum"])
        # 同一時間兩個來源(7.0、5.0)取平均
        self.assertEqual([v for v in window["d11"]["series"]["PM2.5"] if v is not None], [6.0])

    def test_http_endpoints_without_cache(self):
        self.append(self.cdp, CDP_HEAD + cdp_row(-5))
        feed = feed_for(self.root)
        feed.poll()
        store = Store(self.root / "no_cache", allow_empty=True)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store, feed))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            index = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/index"))
            self.assertTrue(index["live"])
            self.assertFalse(index["history"])
            live = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/live?span=600&width=500"))
            self.assertTrue(live["live"])
            page = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode("utf-8")
            self.assertIn("即時", page)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
