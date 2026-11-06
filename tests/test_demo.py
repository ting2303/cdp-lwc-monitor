"""每個模擬情境都要亮出預期的燈號和通知;watch.py 的每一種通知都要有情境。"""
import json
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cpd_lwc_qc import demo  # noqa: E402
from cpd_lwc_qc.live import Feed  # noqa: E402
from cpd_lwc_qc.watch import Watcher, board_for  # noqa: E402

NORMAL_REFERENCE = json.loads((ROOT / "cpd_lwc_qc" / "normal_reference.json").read_text(encoding="utf-8"))


def run_scenario(root, scenario):
    config = demo.config_for(root)
    config["rescan_seconds"] = 0
    # 基準資料停在 30 秒前:停掉的儀器已超過 stale_seconds,其他儀器由下面補上這 30 秒
    world = demo.World(root, config, baseline=300, end_offset=30, normal=NORMAL_REFERENCE)
    feed = Feed(config)
    watcher = Watcher(feed, config, power=world.power, boot=world.boot, processes=world.processes, normal=NORMAL_REFERENCE)
    world.attach(feed, watcher)
    feed.poll()
    world.apply(scenario, catch_up=False)
    world.started = time.time() - 30  # 情境從基準資料結束時就開始
    world.write_until(time.time())
    feed.poll()
    feed.poll()
    return world, feed, watcher, watcher.step()


class Scenarios(unittest.TestCase):
    def test_every_scenario_shows_expected_light_and_alerts(self):
        for sid, _, label, color, expect, _, changes in demo.SCENARIOS:
            if changes.get("freeze"):
                continue
            with self.subTest(sid), tempfile.TemporaryDirectory() as temp:
                _, _, _, alerts = run_scenario(Path(temp), sid)
                found = {a["key"] for a in alerts}
                for key in expect:
                    self.assertTrue(any(k.startswith(key) for k in found), f"{label}: 少了 {key},實際 {sorted(found)}")
                self.assertEqual(board_for(alerts, {}, [])["overall"], {"blue": "green"}.get(color, color), f"{label}: {sorted(found)}")
                for alert in alerts:
                    self.assertTrue(alert["steps"], f"{alert['key']} 沒有建議")

    def test_every_alert_in_watch_has_a_scenario(self):
        source = (ROOT / "cpd_lwc_qc" / "watch.py").read_text(encoding="utf-8")
        keys = set(re.findall(r'add\(f?"([a-z_0-9]+)', source))
        covered = {key.split(":")[0] for s in demo.SCENARIOS for key in s[4]}
        self.assertEqual(keys - covered, set())

    def test_recovers_after_scenario(self):
        with tempfile.TemporaryDirectory() as temp:
            world, feed, watcher, alerts = run_scenario(Path(temp), "disk")
            self.assertTrue(alerts)
            world.apply("normal")
            feed.poll()
            self.assertEqual(watcher.step(), [])
            events = feed.board["events"]
            self.assertEqual(events[0]["kind"], "解除")

    def test_dead_laptop_freezes_then_returns(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            world = demo.World(root, demo.config_for(root), baseline=30)
            world.apply("dead")
            self.assertTrue(world.frozen())
            world.frozen_until = 0
            world.step()
            self.assertEqual(world.scenario, "normal")

    def test_demo_never_has_webhook_or_backup(self):
        config = demo.config_for(Path("x"))
        self.assertEqual(config["discord_webhook"], "")
        self.assertEqual(config["backup_jobs"], [])


class Board(unittest.TestCase):
    def alert(self, key, level="warn"):
        return {"key": key, "level": level, "title": key, "detail": "", "steps": []}

    def test_overall_follows_worst(self):
        self.assertEqual(board_for([], {}, [])["overall"], "green")
        self.assertEqual(board_for([self.alert("manual_mismatch", "info")], {}, [])["overall"], "green")
        self.assertEqual(board_for([self.alert("env_pm")], {}, [])["overall"], "yellow")
        self.assertEqual(board_for([self.alert("env_pm"), self.alert("fog", "error")], {}, [])["overall"], "red")

    def test_lights_by_category(self):
        lights = {l["id"]: l["color"] for l in board_for([self.alert("stale:11-D", "error"), self.alert("env_pm"), self.alert("env_temp:d11_temp")], {}, [])["lights"]}
        self.assertEqual(lights["d11"], "red")
        self.assertEqual(lights["cdp"], "green")
        self.assertEqual(lights["env"], "yellow")
        self.assertEqual(lights["optics"], "yellow")

    def test_setup_lights_only_what_is_unset(self):
        alert = self.alert("setup")
        alert["title"] = "還沒設定 11-D 資料夾"
        lights = {l["id"]: l["color"] for l in board_for([alert], {}, [])["lights"]}
        self.assertEqual((lights["d11"], lights["cdp"]), ("yellow", "green"))


if __name__ == "__main__":
    unittest.main()
