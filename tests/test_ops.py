"""無人值守維運:心跳、看門狗、診斷報告、維護模式、通知防洗版、雲端檢查、每日摘要。"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cpd_lwc_qc import cloud_check, diagnostics  # noqa: E402
from cpd_lwc_qc.heartbeat import FAILS_TO_ALERT, Heartbeat, valid_url  # noqa: E402
from cpd_lwc_qc.live import Feed  # noqa: E402
from cpd_lwc_qc.notify import Notifier  # noqa: E402
from cpd_lwc_qc.watch import Watcher, daily_lines  # noqa: E402
from cpd_lwc_qc.watchdog import Watchdog  # noqa: E402

SECRET = "https://discord.com/api/webhooks/1/SECRET"
PING = "https://hc-ping.com/SECRET-UUID"


class Server:
    def __enter__(self):
        hits = self.hits = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/ping"
        return self

    def __exit__(self, *a):
        self.server.shutdown()
        self.server.server_close()


class HeartbeatTests(unittest.TestCase):
    def test_only_https(self):
        self.assertTrue(valid_url(PING))
        self.assertFalse(valid_url("http://hc-ping.com/x"))
        beat = Heartbeat("http://hc-ping.com/x")
        self.assertIsNone(beat.url)
        self.assertTrue(beat.configured)

    def test_ping_interval_and_failure_count(self):
        now = [0.0]
        with Server() as server:
            beat = Heartbeat(server.url, interval=300, allow_local=True, clock=lambda: now[0])
            beat.step()
            beat.step()
            self.assertEqual(server.hits, ["/ping"])
            now[0] = 301
            beat.step()
            self.assertEqual(len(server.hits), 2)
            self.assertFalse(beat.failing)
        beat = Heartbeat("http://127.0.0.1:9/x", allow_local=True)
        for _ in range(FAILS_TO_ALERT):
            beat.ping()
        self.assertTrue(beat.failing)
        self.assertNotIn("127.0.0.1", beat.last_error)

    def test_recovers(self):
        with Server() as server:
            beat = Heartbeat(server.url, allow_local=True)
            beat.fails = 5
            self.assertTrue(beat.ping())
            self.assertFalse(beat.failing)


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "pads").mkdir()
        (self.root / "d11").mkdir()
        self.now = [time.time()]
        self.sent = []

    def tearDown(self):
        self.temp.cleanup()

    def watcher(self, **extra):
        config = {"pads_roots": [str(self.root / "pads")], "d11_roots": [str(self.root / "d11")], "state_file": str(self.root / "state.json"), "min_free_gb": 0, **extra}
        feed = Feed(config)
        feed.poll()
        watcher = Watcher(feed, config, power=lambda: None, boot=lambda: 1000.0, processes=lambda: [], clock=lambda: self.now[0])
        # 只測邏輯:假的 Notifier 收訊息
        notifier = Notifier(None, [])
        notifier.add = self.sent.append
        notifier.flush = lambda: 0
        watcher.notifier = notifier
        return watcher


def fake(watcher, *keys):
    watcher.evaluate = lambda: [{"key": k, "level": "error", "title": k, "detail": "d", "action": "", "steps": ["s"]} for k in keys]


class Notifications(Base):
    def test_flapping_does_not_repeat(self):
        watcher = self.watcher(flap_seconds=600)
        fake(watcher, "a")
        watcher.step()
        self.assertEqual(len(self.sent), 1)
        fake(watcher)
        watcher.step()
        self.assertEqual(len(self.sent), 2)  # 已恢復
        fake(watcher, "a")
        self.now[0] += 60
        watcher.step()
        fake(watcher)
        self.now[0] += 60
        watcher.step()
        self.assertEqual(len(self.sent), 2)  # 短時間又發生:不再傳,也不傳恢復

    def test_long_lasting_alert_is_sent_after_cooldown(self):
        watcher = self.watcher(flap_seconds=600)
        fake(watcher, "a")
        watcher.step()
        fake(watcher)
        watcher.step()
        self.sent.clear()
        fake(watcher, "a")
        watcher.step()
        self.assertEqual(self.sent, [])
        self.now[0] += 601
        watcher.step()
        self.assertEqual(len(self.sent), 1)


class Maintenance(Base):
    def test_mutes_then_reports_what_is_left(self):
        watcher = self.watcher()
        watcher.set_maintenance(60, "換濾網")
        self.assertIn("開始維護", self.sent[0])
        self.assertIn("換濾網", self.sent[0])
        self.sent.clear()
        fake(watcher, "a", "b")
        alerts = watcher.step()
        self.assertEqual(self.sent, [])
        self.assertEqual(len(alerts), 2)  # 問題仍照常判斷
        self.assertIsNotNone(watcher.feed.board["maintenance"])
        fake(watcher, "a")
        watcher.step()
        self.assertEqual(self.sent, [])  # 維護中解除也不通知
        self.now[0] += 3601
        watcher.step()
        self.assertIn("維護時間到了", self.sent[0])
        self.assertEqual(len(self.sent), 2)  # 只補上還沒解決的 a
        self.assertIn("a", self.sent[1])
        self.assertIsNone(watcher.feed.board["maintenance"])

    def test_end_early(self):
        watcher = self.watcher()
        watcher.set_maintenance(60)
        watcher.end_maintenance()
        self.assertIsNone(watcher.maintenance())
        self.assertIn("維護結束", self.sent[-1])

    def test_survives_restart(self):
        watcher = self.watcher()
        watcher.set_maintenance(60)
        again = self.watcher()
        self.assertIsNotNone(again.maintenance())


class SystemChecks(Base):
    def test_com_port_loss_only_when_instruments_are_not_ok(self):
        watcher = self.watcher()
        watcher.state["com_ports"] = ["COM3", "COM4"]
        watcher.system = lambda: {"com_ports": ["COM4"]}
        found = {a["key"]: a for a in watcher.evaluate()}
        self.assertIn("COM3", found["com_missing"]["detail"])
        self.assertTrue(found["com_missing"]["steps"])

    def test_power_plan_and_updates(self):
        watcher = self.watcher()
        watcher.system = lambda: {"sleep_ac_seconds": 1800, "hibernate_ac_seconds": 0, "lid_ac_action": 1, "reboot_pending": True}
        found = {a["key"]: a for a in watcher.evaluate()}
        self.assertIn("接電時會睡眠", found["sleep_enabled"]["detail"])
        self.assertIn("闔上螢幕", found["sleep_enabled"]["detail"])
        self.assertNotIn("休眠", found["sleep_enabled"]["detail"])
        self.assertIn("reboot_pending", found)
        watcher.system = lambda: {"sleep_ac_seconds": 0, "hibernate_ac_seconds": 0, "lid_ac_action": 0, "reboot_pending": False}
        found = {a["key"] for a in watcher.evaluate()}
        self.assertFalse(found & {"sleep_enabled", "reboot_pending"})

    def test_backup_stale(self):
        watcher = self.watcher(backup_jobs=[{}], backup_minutes=30)
        watcher.backup_at = self.now[0]
        self.assertNotIn("backup_stale", {a["key"] for a in watcher.evaluate()})
        watcher.backup_at = self.now[0] - 3 * 3600
        self.assertIn("backup_stale", {a["key"] for a in watcher.evaluate()})

    def test_parse_power_setting(self):
        output = """電源設定 GUID: ...
      目前的 AC 電源設定索引: 0x00000708
      目前的 DC 電源設定索引: 0x00000384
"""
        self.assertEqual(diagnostics.parse_ac_setting(output), 0x708)
        self.assertIsNone(diagnostics.parse_ac_setting(""))


class Reports(Base):
    def test_report_has_no_secrets_and_explains_ports(self):
        watcher = self.watcher(discord_webhook=SECRET, heartbeat_url=PING, status_dir=str(self.root / "status"))
        watcher.system = lambda: {"com_ports": ["COM4"], "sleep_ac_seconds": 0}
        watcher.state["com_ports"] = ["COM3", "COM4"]
        watcher.step()
        original = diagnostics.problem_devices
        site = diagnostics.check_site
        diagnostics.problem_devices = lambda: ["OK | USB Serial (COM4)"]
        diagnostics.check_site = lambda url: "正常"
        try:
            path = diagnostics.write_report(watcher.feed, watcher, {**watcher.config, "diag_screenshot": False})
        finally:
            diagnostics.problem_devices, diagnostics.check_site = original, site
        text = path.read_text(encoding="utf-8-sig")
        self.assertNotIn("SECRET", text)
        self.assertIn("COM3、COM4", text)
        self.assertIn("USB Serial", text)

    def test_daily_lines(self):
        lines = daily_lines({"coverage": {"CDP": 0.5, "11-D": 1.0}, "cloud_minutes": 90, "number": 300.0, "mvd": 12.0, "cloud_by_hour": [0] * 20 + [10, 60, 20, 0], "pm25": 8.0})
        text = "\n".join(lines)
        self.assertIn("⚠️CDP 50%", text)
        self.assertNotIn("⚠️11-D", text)
        self.assertIn("█", text)
        self.assertIn("雲中 90 分鐘", text)

    def test_daily_summary_from_feed(self):
        summary = self.watcher().feed.daily_summary()
        self.assertEqual(summary["coverage"]["CDP"], 0.0)


class CloudCheck(Base):
    def test_stale_then_recovered(self):
        folder = self.root / "cloud" / "CDPLWC_現場" / "鹿林" / "狀態"
        folder.mkdir(parents=True)
        status = folder / "現場狀態.txt"
        status.write_text("最後更新", encoding="utf-8")
        notes = []
        notifier = Notifier(None, [])
        notifier.add = notes.append
        active = {}
        future = time.time() + 3600
        lines = cloud_check.check(self.root / "cloud", 30, active, notifier, now=future)
        self.assertIn("太舊", lines[0])
        self.assertIn("鹿林", notes[0])
        cloud_check.check(self.root / "cloud", 30, active, notifier, now=future)
        self.assertEqual(len(notes), 1)  # 不重複
        cloud_check.check(self.root / "cloud", 30, active, notifier, now=time.time())
        self.assertIn("已恢復", notes[-1])

    def test_missing_folder_is_reported(self):
        self.assertIn("找不到", cloud_check.check(self.root / "none", 30, {}, Notifier(None, []))[0])


class WatchdogTests(Base):
    def dog(self, script, check=lambda path: True, **extra):
        path = self.root / "child.py"
        path.write_text(script, encoding="utf-8")
        return Watchdog([sys.executable, str(path)], self.root, self.root / "port.txt", self.root / "dog.log", check=check, grace=0, interval=0, **extra)

    def test_restarts_after_crash_and_passes_count(self):
        marker = self.root / "runs.txt"
        script = f"import os\nopen({str(marker)!r},'a').write(os.environ['PANEL_RESTART_COUNT']+'\\n')\n"
        dog = self.dog(script)
        dog.launch()
        dog.child.wait(timeout=20)
        wait = dog.step()
        self.assertIsNotNone(wait)
        dog.restarts += 1
        dog.launch()
        dog.child.wait(timeout=20)
        self.assertEqual(marker.read_text().split(), ["0", "1"])
        self.assertIn("面板結束了", (self.root / "dog.log").read_text(encoding="utf-8"))

    def test_kills_hung_child(self):
        dog = self.dog("import time\ntime.sleep(120)\n", check=lambda path: False)
        dog.launch()
        try:
            self.assertIsNone(dog.step())
            self.assertIsNone(dog.step())
            self.assertIsNotNone(dog.step())
            self.assertIsNotNone(dog.child.poll())
        finally:
            if dog.child.poll() is None:
                dog.child.kill()

    def test_many_crashes_back_off(self):
        dog = self.dog("pass")
        delays = [dog.wait_before_restart() for _ in range(7)]
        self.assertEqual(delays[0], 5.0)
        self.assertEqual(delays[-1], 300)

    def test_opens_drive_once_when_missing(self):
        opened = []
        dog = self.dog("pass", watch_drive=lambda: True, processes=lambda: [], start_drive=lambda: opened.append(1) or True)
        dog.check_drive()
        dog.check_drive()
        self.assertEqual(len(opened), 1)
        off = self.dog("pass", watch_drive=lambda: False, processes=lambda: [], start_drive=lambda: opened.append(1) or True)
        off.check_drive()
        self.assertEqual(len(opened), 1)


class SetupFields(Base):
    def test_heartbeat_saved_not_returned(self):
        from cpd_lwc_qc.locate import Locator

        path = self.root / "panel_config.json"
        config = {"pads_roots": [], "d11_roots": []}
        path.write_text(json.dumps(config), encoding="utf-8")
        locator = Locator(Feed(config), path, config)
        cloud = self.root / "cloud"
        cloud.mkdir()
        self.assertTrue(locator.apply_remote(str(cloud), "a", None, "http://insecure/x"))
        self.assertEqual(locator.apply_remote(str(cloud), "a", None, PING), [])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["heartbeat_url"], PING)
        state = locator.state()
        self.assertTrue(state["has_heartbeat"])
        self.assertNotIn("SECRET", json.dumps(state))


class StartupFile(unittest.TestCase):
    def test_startup_uses_watchdog(self):
        text = (ROOT / "tools" / "設定開機自動啟動.bat").read_bytes()
        self.assertIn(b"--watchdog --no-browser", text)
        self.assertIn(b"\r\n", text)


if __name__ == "__main__":
    unittest.main()
