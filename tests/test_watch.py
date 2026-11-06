"""現場監測的測試:用可控制的電源、開機時間和程式清單模擬各種狀況。"""
import csv
import json
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cpd_lwc_qc.live import Feed  # noqa: E402
from cpd_lwc_qc.notify import valid_webhook  # noqa: E402
from cpd_lwc_qc.watch import DRIVE_PROCESS, PADS_PROCESS, Watcher, explain_log  # noqa: E402

HEAD = (
    "[Instrument 1]\r\nSample Area (mm^2)=0.267\r\nSizes=<2>26,28\r\n****\r\n"
    "End Seconds,Day of Year,Year,Status,Number Conc (#/cm^3),LWC (g/m^3),MVD (um),ED (um),Applied PAS (m/s),Dump Spot Monitor (V),Laser Current (mA),CDP Bin 1,CDP Bin 2\r\n"
)
LWC_HEAD = "****\r\nEnd_Seconds,Day_of_Year,Year,Status,LWC-AveragedDAP,PAS(m/s),Static Pressure (mB),Ambient Temperature (C)\r\n"
SID = datetime.now().strftime("%Y%m%d%H%M%S")
AC = {"on_ac": True, "percent": 100, "has_battery": True}


def row(offset=0, number=0.0, dump=4.84):
    now = datetime.now() + timedelta(seconds=offset)
    seconds = now.hour * 3600 + now.minute * 60 + now.second
    return f"{seconds}.5,{now.timetuple().tm_yday},{now.year},1,{number},0,0,0,10,{dump},82,0,0\r\n"


def lwc_row(offset=0):
    now = datetime.now() + timedelta(seconds=offset)
    seconds = now.hour * 3600 + now.minute * 60 + now.second
    return f"{seconds}.5,{now.timetuple().tm_yday},{now.year},1,0.1,7,722,10\r\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folder = self.root / "pads" / SID[:8] / SID
        self.folder.mkdir(parents=True)
        (self.root / "d11").mkdir()
        self.status = self.root / "drive"

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, text):
        with open(self.folder / name, "ab") as handle:
            handle.write(text.encode("cp950"))

    def watcher(self, power=AC, boot=1000.0, processes=(DRIVE_PROCESS, PADS_PROCESS), **extra):
        config = {"pads_roots": [str(self.root / "pads")], "d11_roots": [str(self.root / "d11")], "stale_seconds": 60, "status_dir": str(self.status), "state_file": str(self.root / "state.json"), "fog_seconds": 300, "min_free_gb": 0, **extra}
        feed = Feed(config)
        feed.poll()
        return Watcher(feed, config, power=lambda: power, boot=lambda: boot, processes=lambda: list(processes) if processes is not None else None)

    def healthy(self):
        self.write(f"00CDP PBP{SID}.csv", HEAD + "".join(row(-i) for i in range(120, 0, -1)))
        self.write(f"01LWC 300_{SID}.csv", LWC_HEAD + "".join(lwc_row(-i) for i in range(120, 0, -1)))
        (self.root / "d11" / "a-L.dat").write_text("", encoding="utf-8")


def keys(alerts):
    return {a["key"] for a in alerts}


class Reasons(Base):
    def test_normal_has_no_cdp_alert(self):
        self.healthy()
        found = keys(self.watcher().step())
        self.assertFalse({k for k in found if "CDP" in k or "LWC" in k})

    def test_stopped_data_with_log_reason(self):
        self.write(f"00CDP PBP{SID}.csv", HEAD + row(-600))
        self.write(f"Log{SID}.txt", "12:00:00  20250101:   Recording stopped.\r\n")
        alert = next(a for a in self.watcher().step() if a["key"] == "stale:CDP")
        self.assertEqual(alert["level"], "error")
        self.assertIn("Recording stopped", alert["detail"])

    def test_stopped_data_from_communication_error(self):
        self.write(f"00CDP PBP{SID}.csv", HEAD + row(-600))
        stamp = datetime.now().strftime("%H:%M:%S  %Y%m%d")
        self.write(f"Log{SID}.txt", f"*** {stamp}:   Error 5902 occurred at CDP Initialize.vi > CDP 2.vi        Possible reason(s):\r\n" * 3)
        alerts = self.watcher().step()
        self.assertIn("通訊失敗", next(a for a in alerts if a["key"] == "stale:CDP")["detail"])
        self.assertIn("Error 5902(CDP) 3 次", next(a for a in alerts if a["key"] == "log_errors")["detail"])
        self.assertTrue(any("電源指示燈" in s for s in next(a for a in alerts if a["key"] == "stale:CDP")["steps"]))

    def test_pads_not_running(self):
        self.write(f"00CDP PBP{SID}.csv", HEAD + row(-600))
        alert = next(a for a in self.watcher(processes=(DRIVE_PROCESS,)).step() if a["key"] == "stale:CDP")
        self.assertIn("PADS 目前沒有在執行", alert["detail"])

    def test_fog_when_no_particles_and_dump_low(self):
        self.write(f"00CDP PBP{SID}.csv", HEAD + "".join(row(-i, dump=1.0) for i in range(290, 0, -1)))
        self.assertIn("fog", keys(self.watcher().step()))

    def test_no_fog_in_cloud_or_when_normal(self):
        self.write(f"00CDP PBP{SID}.csv", HEAD + "".join(row(-i, number=200, dump=1.0) for i in range(290, 0, -1)))
        self.assertNotIn("fog", keys(self.watcher().step()))

    def test_power_loss_on_battery(self):
        self.healthy()
        battery = {"on_ac": False, "percent": 80, "has_battery": True}
        self.assertEqual(next(a for a in self.watcher(power=battery).step() if a["key"] == "on_battery")["level"], "warn")
        low = {"on_ac": False, "percent": 20, "has_battery": True}
        self.assertEqual(next(a for a in self.watcher(power=low).step() if a["key"] == "on_battery")["level"], "error")

    def test_desktop_without_battery_is_not_an_alert(self):
        self.healthy()
        self.assertNotIn("on_battery", keys(self.watcher(power={"on_ac": None, "percent": None, "has_battery": False}).step()))

    def test_reboot_is_detected_across_runs(self):
        self.healthy()
        self.assertNotIn("reboot", keys(self.watcher(boot=time.time() - 9000).step()))
        second = self.watcher(boot=time.time() - 60).step()
        alert = next(a for a in second if a["key"] == "reboot")
        self.assertIn("重新開機", alert["title"])
        self.assertIn("上次監測到", alert["detail"])

    def test_drive_not_running(self):
        self.healthy()
        self.assertIn("drive_stopped", keys(self.watcher(processes=(PADS_PROCESS,)).step()))

    def test_backup_failure_is_reported(self):
        self.healthy()
        watcher = self.watcher()
        watcher.backup_result = {"failed": 2, "conflicts": 0, "sample": "a.csv", "time": "t", "copied": 0, "updated": 0, "in_use": 0}
        self.assertIn("backup_failed", keys(watcher.step()))


class FakeDiscord:
    """本機假的 Discord,記下收到的訊息。"""

    def __init__(self, fail=False):
        self.messages, self.fail = [], fail
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))
                if owner.fail:
                    self.send_response(500)
                    self.end_headers()
                    return
                owner.messages.append(body)
                self.send_response(204)
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/webhooks/1/x"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Discord(Base):
    def setUp(self):
        super().setUp()
        self.discord = FakeDiscord()

    def tearDown(self):
        self.discord.close()
        super().tearDown()

    def run_watcher(self, **extra):
        return self.watcher(discord_webhook=self.discord.url, _allow_local_webhook=True, daily_report_hour=None, site_name="鹿林", **extra)

    def texts(self):
        return [m["content"] for m in self.discord.messages]

    def test_alert_has_reason_and_advice_and_is_sent_once(self):
        self.write(f"00CDP PBP{SID}.csv", HEAD + row(-600))
        watcher = self.run_watcher(processes=(DRIVE_PROCESS,))
        watcher.step()
        watcher.step()
        hits = [t for t in self.texts() if "CDP 資料停止更新" in t]
        self.assertEqual(len(hits), 1)
        self.assertIn("🔴", hits[0])
        self.assertIn("鹿林", hits[0])
        self.assertIn("PADS 目前沒有在執行", hits[0])
        self.assertIn("建議", hits[0])
        self.assertIn("1. ", hits[0])
        self.assertEqual(self.discord.messages[0]["allowed_mentions"], {"parse": []})

    def test_recovery_is_announced(self):
        self.write(f"00CDP PBP{SID}.csv", HEAD + row(-600))
        watcher = self.run_watcher()
        watcher.step()
        self.write(f"00CDP PBP{SID}.csv", "".join(row(-i) for i in range(5, 0, -1)))
        watcher.feed.poll()
        watcher.step()
        self.assertTrue(any("已恢復:CDP 資料停止更新" in t for t in self.texts()))

    def test_offline_messages_are_kept_and_sent_later(self):
        self.write(f"00CDP PBP{SID}.csv", HEAD + row(-600))
        self.discord.fail = True
        watcher = self.run_watcher()
        watcher.step()
        self.assertEqual(self.texts(), [])
        self.assertTrue(watcher.notifier.queue)
        self.assertIn("Discord", watcher.notifier.last_error)
        again = self.run_watcher()  # 程式重開後佇列還在
        self.assertTrue(again.notifier.queue)
        self.discord.fail = False
        again.step()
        self.assertFalse(again.notifier.queue)
        self.assertTrue(any("CDP 資料停止更新" in t for t in self.texts()))

    def test_bad_webhook_is_not_used(self):
        for url in ("https://evil.example/api/webhooks/1/x", "http://discord.com/api/webhooks/1/x", "https://discord.com/other"):
            self.assertFalse(valid_webhook(url))
        self.assertTrue(valid_webhook("https://discord.com/api/webhooks/123/abc"))
        watcher = self.watcher(discord_webhook="https://evil.example/api/webhooks/1/x", daily_report_hour=None)
        self.assertIsNone(watcher.notifier.url)
        watcher.step()
        self.assertIn("Webhook 網址格式不對", (self.status / "現場狀態.txt").read_text(encoding="utf-8-sig"))

    def test_webhook_never_goes_to_cloud_files(self):
        self.write(f"00CDP PBP{SID}.csv", HEAD + row(-600))
        self.run_watcher().step()
        for path in self.status.iterdir():
            self.assertNotIn(self.discord.url, path.read_text(encoding="utf-8-sig"))

    def test_daily_report_once_a_day(self):
        self.healthy()
        watcher = self.watcher(discord_webhook=self.discord.url, _allow_local_webhook=True, daily_report_hour=datetime.now().hour)
        watcher.step()
        watcher.step()
        self.assertEqual(sum("每日回報" in t for t in self.texts()), 1)


class Environment(Base):
    def env(self, **values):
        watcher = self.watcher(processes=(DRIVE_PROCESS, PADS_PROCESS), ignore_pas_mismatch=False)
        watcher.feed.recent_environment = lambda: values
        return {a["key"]: a for a in watcher.evaluate()}

    def test_normal_values_are_quiet(self):
        found = self.env(pm25=10.0, pressure=725.0, pressure_change=0.5, rh=25.0, d11_temp=20.0, number=300.0, laser_temp=15.0, cdp_pas=10.0, hot_pas=10.0, cdp_pas_values=[10.0], hot_pressure=722.0)
        self.assertFalse({k for k in found if k.startswith(("env", "manual"))})

    def test_each_anomaly(self):
        self.assertIn("env_pm", self.env(pm25=80.0))
        self.assertIn("env_pressure", self.env(pressure=720.0, pressure_change=-6.0))
        self.assertIn("env_rh", self.env(rh=75.0))
        self.assertIn("env_temp:d11_temp", self.env(d11_temp=50.0))
        self.assertIn("env_temp:laser_temp", self.env(laser_temp=40.0))
        self.assertIn("env_number", self.env(number=4000.0))

    def test_manual_value_alerts(self):
        self.assertIn("manual_pas", self.env(cdp_pas_values=[8.0, 10.0]))
        self.assertEqual(self.env(cdp_pas=10.0, hot_pas=7.0)["manual_mismatch"]["level"], "info")
        found = self.env(pressure=725.0, hot_pressure=1000.0)
        self.assertIn("差 +275", found["manual_pressure"]["detail"])
        self.assertTrue(found["manual_pressure"]["steps"])

    def test_laser_model(self):
        watcher = self.watcher()
        watcher.normal = {"laser_model": {"a": 73.0, "b": 0.5, "c": 0.0, "t0": 0.0, "sd": 0.4}}
        watcher.feed.recent_environment = lambda: {"laser": 90.0, "laser_temp": 15.0}
        self.assertIn("env_laser", keys(watcher.evaluate()))
        watcher.feed.recent_environment = lambda: {"laser": 80.5, "laser_temp": 15.0}
        self.assertNotIn("env_laser", keys(watcher.evaluate()))

    def test_live_environment_reads_files(self):
        self.write(f"00CDP PBP{SID}.csv", HEAD + "".join(row(-i) for i in range(30, 0, -1)))
        feed = Feed({"pads_roots": [str(self.root / "pads")], "d11_roots": []})
        feed.poll()
        env = feed.recent_environment()
        self.assertEqual(env["cdp_pas"], 10.0)
        self.assertEqual(env["cdp_pas_values"], [10.0])


class Clocks(Base):
    def test_matching_clocks_are_quiet_and_skew_is_found(self):
        self.write(f"00CDP PBP{SID}.csv", HEAD + "".join(row(-i) for i in range(5, 0, -1)))
        feed = Feed({"pads_roots": [str(self.root / "pads")], "d11_roots": []})
        feed.poll()
        self.assertLess(abs(feed.clock_check()["PADS"]), 30)
        watcher = self.watcher()
        watcher.feed.clock_check = lambda: {"PADS": 3.0, "11-D": 8 * 3600.0, "internal": 100.0}
        found = keys(watcher.evaluate())
        self.assertIn("clock:11-D", found)
        self.assertNotIn("clock:PADS", found)
        self.assertNotIn("d11_internal", found)

    def test_d11_internal_clock(self):
        folder = self.root / "d11x"
        folder.mkdir()
        from datetime import datetime, timedelta

        stamp = datetime.now().replace(microsecond=0)
        inner = stamp - timedelta(seconds=187)
        (folder / "a.csv").write_text(
            "Date,PM2.5,Pres,Year,Mon,Day,Hr,Min,Sec\n"
            f"{stamp:%Y-%m-%d %H:%M:%S},5,725,{inner:%y},{inner.month},{inner.day},{inner.hour},{inner.minute},{inner.second}\n",
            encoding="utf-8",
        )
        feed = Feed({"pads_roots": [], "d11_roots": [str(folder)]})
        feed.poll()
        self.assertEqual(feed.clock_check()["internal"], 187)


class RemoteSetup(Base):
    def locator(self):
        from cpd_lwc_qc.locate import Locator

        config_path = self.root / "panel_config.json"
        config = {"pads_roots": [str(self.root / "pads")], "d11_roots": [str(self.root / "d11")]}
        config_path.write_text(json.dumps(config), encoding="utf-8")
        feed = Feed(config)
        changes = []
        locator = Locator(feed, config_path, config)
        locator.on_change = lambda: changes.append(1)
        return locator, config_path, changes

    def test_cloud_paths_and_webhook_are_saved(self):
        locator, path, changes = self.locator()
        cloud = self.root / "cloud"
        cloud.mkdir()
        self.assertEqual(locator.apply_remote(str(cloud), "鹿林/站", "https://discord.com/api/webhooks/1/a"), [])
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(saved["status_dir"].endswith("CDPLWC_現場/鹿林_站/狀態"))
        self.assertEqual([j["name"] for j in saved["backup_jobs"]], ["PADS", "11-D"])
        self.assertEqual(saved["discord_webhook"], "https://discord.com/api/webhooks/1/a")
        self.assertEqual(changes, [1])
        state = locator.state()
        self.assertTrue(state["has_webhook"])
        self.assertNotIn("discord.com", json.dumps(state))  # 網址不會回傳給網頁

    def test_blank_webhook_keeps_existing(self):
        locator, path, _ = self.locator()
        cloud = self.root / "cloud"
        cloud.mkdir()
        locator.apply_remote(str(cloud), "a", "https://discord.com/api/webhooks/1/a")
        locator.apply_remote(str(cloud), "b", None)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["discord_webhook"], "https://discord.com/api/webhooks/1/a")

    def test_bad_values_are_rejected(self):
        locator, path, _ = self.locator()
        before = path.read_text(encoding="utf-8")
        self.assertTrue(locator.apply_remote("C:/no/such", "a", None))
        self.assertTrue(locator.apply_remote(str(self.root), "a", "https://evil.example/x"))
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_changing_folders_updates_backup_jobs(self):
        locator, path, _ = self.locator()
        cloud = self.root / "cloud"
        cloud.mkdir()
        locator.apply_remote(str(cloud), "a", None)
        new = self.root / "other"
        new.mkdir()
        self.assertEqual(locator.apply([str(new)], []), [])
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual([j["source"] for j in saved["backup_jobs"]], [str(new).replace("\\", "/")])


class Output(Base):
    def test_status_file_and_history(self):
        self.write(f"00CDP PBP{SID}.csv", HEAD + row(-600))
        watcher = self.watcher()
        watcher.step()
        text = (self.status / "現場狀態.txt").read_text(encoding="utf-8-sig")
        self.assertIn("最後更新", text)
        self.assertIn("CDP 資料停止更新", text)
        self.assertIn("建議:", text)
        with open(self.status / "問題紀錄.csv", encoding="utf-8-sig") as handle:
            first = list(csv.reader(handle))
        self.assertTrue(any(r[3] == "CDP 資料停止更新" and r[1] == "發生" for r in first))
        # 同一個問題不重複記錄
        watcher.step()
        with open(self.status / "問題紀錄.csv", encoding="utf-8-sig") as handle:
            self.assertEqual(len(list(csv.reader(handle))), len(first))
        # 恢復後記錄解除
        self.write(f"00CDP PBP{SID}.csv", "".join(row(-i) for i in range(5, 0, -1)))
        watcher.feed.poll()
        watcher.step()
        with open(self.status / "問題紀錄.csv", encoding="utf-8-sig") as handle:
            rows = list(csv.reader(handle))
        self.assertTrue(any(r[1] == "解除" and r[3] == "CDP 資料停止更新" for r in rows))
        self.assertNotIn("CDP 資料停止更新", (self.status / "現場狀態.txt").read_text(encoding="utf-8-sig"))
        json.loads((self.root / "state.json").read_text(encoding="utf-8"))


class LogText(unittest.TestCase):
    def test_empty_and_unknown(self):
        self.assertIsNone(explain_log([], time.time())["reason"])
        self.assertIsNone(explain_log(["08:00:00  20250101:   Saving Configuration file"], time.time())["reason"])

    def test_old_errors_are_not_recent(self):
        result = explain_log(["*** 08:00:00  20200101:   Error 5902 occurred at CDP Initialize.vi"], time.time())
        self.assertEqual(result["recent_errors"], 0)


if __name__ == "__main__":
    unittest.main()
