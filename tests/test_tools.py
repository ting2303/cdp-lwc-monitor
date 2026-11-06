"""備份、每分鐘快取與視覺化後端的測試。"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import backup  # noqa: E402
from cpd_lwc_qc.diagnose import Aligned, cloud_events, diagnose, manual_settings, run_checks  # noqa: E402
from cpd_lwc_qc.minute import build  # noqa: E402
from cpd_lwc_qc.story import build_story, similar_days  # noqa: E402
from cpd_lwc_qc.normal import site_of  # noqa: E402
from cpd_lwc_qc.store import cdp_spectrum, d11_edges, d11_spectrum, load  # noqa: E402
from cpd_lwc_qc.viewer import Store, bucket  # noqa: E402

OLD = time.time() - 3600
DAY_S = 86400


def write(path: Path, text: str, mtime: float = OLD) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))


class Backup(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.src = Path(self.temp.name) / "src"
        self.dst = Path(self.temp.name) / "dst"
        self.src.mkdir()
        self.job = {"name": "t", "source": str(self.src), "dest": str(self.dst)}

    def tearDown(self):
        self.temp.cleanup()

    def run_job(self, dry=False):
        return backup.run_job(self.job, 15, dry)

    def test_copy_then_unchanged(self):
        write(self.src / "a/b.csv", "x")
        first = self.run_job()
        self.assertEqual((first.copied, first.failed), (1, 0))
        self.assertEqual((self.dst / "a/b.csv").read_text(encoding="utf-8"), "x")
        self.assertEqual(self.run_job().unchanged, 1)

    def test_dry_run_writes_nothing(self):
        write(self.src / "a.csv", "x")
        self.assertEqual(self.run_job(dry=True).copied, 1)
        self.assertFalse(self.dst.exists())

    def test_recent_file_is_skipped(self):
        write(self.src / "live.csv", "x", mtime=time.time())
        result = self.run_job()
        self.assertEqual((result.in_use, result.copied), (1, 0))

    def test_grown_file_is_updated(self):
        write(self.src / "a.csv", "x", mtime=OLD - 100)
        self.run_job()
        write(self.src / "a.csv", "xy", mtime=OLD)
        self.assertEqual(self.run_job().updated, 1)
        self.assertEqual((self.dst / "a.csv").read_text(encoding="utf-8"), "xy")

    def test_newer_destination_is_not_overwritten(self):
        write(self.src / "a.csv", "old", mtime=OLD - 1000)
        write(self.dst / "a.csv", "new!", mtime=OLD)
        self.assertEqual(self.run_job().conflicts, 1)
        self.assertEqual((self.dst / "a.csv").read_text(encoding="utf-8"), "new!")

    def test_same_content_with_different_time_is_not_copied_again(self):
        write(self.src / "a.csv", "same", mtime=OLD)
        write(self.dst / "a.csv", "same", mtime=OLD - 5000)
        result = self.run_job()
        self.assertEqual((result.unchanged, result.updated), (1, 0))

    def test_same_size_different_content_is_updated(self):
        write(self.src / "a.csv", "abcd", mtime=OLD)
        write(self.dst / "a.csv", "wxyz", mtime=OLD - 5000)
        self.assertEqual(self.run_job().updated, 1)
        self.assertEqual((self.dst / "a.csv").read_text(encoding="utf-8"), "abcd")

    def test_never_deletes_destination(self):
        write(self.src / "a.csv", "x")
        write(self.dst / "only_here.csv", "keep")
        self.run_job()
        self.assertTrue((self.dst / "only_here.csv").exists())

    def test_no_partial_file_left(self):
        write(self.src / "a.csv", "x")
        self.run_job()
        self.assertEqual(list(self.dst.glob("*.part")), [])

    def test_lock_prevents_overlap_and_is_released(self):
        write(self.src / "a.csv", "x")
        config = Path(self.temp.name) / "c.json"
        config.write_text(json.dumps({"stable_minutes": 15, "log_dir": str(Path(self.temp.name) / "logs"), "jobs": [self.job]}), encoding="utf-8")
        self.assertEqual(backup.main(["--config", str(config)]), 0)
        self.assertFalse((Path(self.temp.name) / "logs/backup.lock").exists())
        self.assertTrue((self.dst / "a.csv").exists())


class Conversions(unittest.TestCase):
    def test_cdp_concentration_matches_pads(self):
        # 實際紀錄:213 顆、0.267 mm²、12 m/s、1 秒,PADS 顯示 66.48 /cm³
        edges = np.array([24.0, 26.0])
        out = cdp_spectrum(np.array([[213.0]]), np.array([1.0]), 0.267, np.array([12.0]), edges)
        self.assertAlmostEqual(out[0, 0] * np.log10(26 / 24), 66.479, places=2)

    def test_zero_air_speed_gives_nan(self):
        out = cdp_spectrum(np.array([[5.0]]), np.array([1.0]), 0.267, np.array([0.0]), np.array([2.0, 3.0]))
        self.assertTrue(np.isnan(out[0, 0]))

    def test_d11_edges_and_units(self):
        edges = d11_edges([1.0, 2.0])
        self.assertEqual(edges.tolist(), [1.0, 2.0, 4.0])
        out = d11_spectrum(np.array([[1000.0, 1000.0]]), edges)
        self.assertAlmostEqual(out[0, 0], 1 / np.log10(2), places=6)

    def test_site_classification(self):
        self.assertEqual(site_of("2025 Cloud IOP_Lulin"), "鹿林")
        self.assertEqual(site_of("KAPAS"), "鹿林")
        self.assertEqual(site_of("20240912 Donyan_seeding"), "東眼山")
        self.assertEqual(site_of("20250312 lab test"), "實驗室")


class FakeStore:
    """只有每分鐘參數的最小資料,用來測判斷規則。"""

    def __init__(self, cdp=None, lwc=None, d11=None, minutes=240):
        from cpd_lwc_qc.store import Channels

        self.cdp = None
        time_axis = np.arange(minutes, dtype="int64") * 60
        self.channels = {}
        for kind, values in (("cdp", cdp), ("lwc", lwc), ("d11", d11)):
            if values is not None:
                self.channels[kind] = Channels(time_axis, {k: np.full(minutes, float(v)) if np.isscalar(v) else np.asarray(v, "float64") for k, v in values.items()})


def statuses(store, normal=None, minutes=240):
    return {c["id"]: c for c in run_checks(Aligned(store, 0, minutes * 60), store, normal)}


class Rules(unittest.TestCase):
    QUIET = {"Number Conc (#/cm^3)": 0.0, "LWC (g/m^3)": 0.0, "MVD (um)": np.nan, "Dump Spot Monitor (V)": 4.84, "Laser Current (mA)": 82.0, "Laser Temp (C)": 15.0, "Applied PAS (m/s)": 10.0}

    def test_dump_spot_only_counts_without_particles(self):
        low = {**self.QUIET, "Dump Spot Monitor (V)": 3.0}
        self.assertEqual(statuses(FakeStore(low))["dump"]["status"], "problem")
        self.assertEqual(statuses(FakeStore(self.QUIET))["dump"]["status"], "ok")
        cloudy = {**low, "Number Conc (#/cm^3)": 200.0, "LWC (g/m^3)": 0.2}
        self.assertEqual(statuses(FakeStore(cloudy))["dump"]["status"], "na")

    def test_laser_uses_temperature_model(self):
        model = {"laser_model": {"a": 73.0, "b": 0.5, "c": 0.0, "t0": 0.0, "sd": 0.5}}
        fine = {**self.QUIET, "Laser Current (mA)": 80.5}
        high = {**self.QUIET, "Laser Current (mA)": 85.0}
        self.assertEqual(statuses(FakeStore(fine), model)["laser"]["status"], "ok")
        self.assertEqual(statuses(FakeStore(high), model)["laser"]["status"], "problem")

    def test_pas_rules(self):
        lwc = {"LWC-AveragedDAP": 0.0, "PAS(m/s)": 7.0}
        self.assertIn("1.43", statuses(FakeStore(self.QUIET, lwc))["pas"]["detail"])
        same = {"LWC-AveragedDAP": 0.0, "PAS(m/s)": 10.0}
        self.assertEqual(statuses(FakeStore(self.QUIET, same))["pas"]["status"], "ok")
        test_speed = {**self.QUIET, "Applied PAS (m/s)": 1.0}
        self.assertIn("校正", statuses(FakeStore(test_speed))["pas"]["detail"])

    def test_baseline_rules(self):
        self.assertEqual(statuses(FakeStore(self.QUIET, {"LWC-AveragedDAP": 0.01}))["baseline"]["status"], "ok")
        self.assertEqual(statuses(FakeStore(self.QUIET, {"LWC-AveragedDAP": 0.25}))["baseline"]["status"], "watch")
        self.assertEqual(statuses(FakeStore(self.QUIET, {"LWC-AveragedDAP": -0.4}))["baseline"]["status"], "problem")

    def test_coverage_gap_and_missing_instrument(self):
        store = FakeStore(self.QUIET)
        result = statuses(store)["coverage"]
        self.assertEqual(result["status"], "watch")  # 只有 CDP
        self.assertIn("11-D 沒有資料", result["detail"])
        from cpd_lwc_qc.store import Channels

        keep = np.r_[0:80, 230:240]
        store.channels["cdp"] = Channels(store.channels["cdp"].time[keep], {k: v[keep] for k, v in store.channels["cdp"].values.items()})
        result = statuses(store)["coverage"]
        self.assertEqual(result["status"], "problem")
        self.assertIn("中斷最長 150 分鐘", result["detail"])

    def test_pressure_cross_check(self):
        lwc = {"LWC-AveragedDAP": 0.0, "Static Pressure (mB)": 1000.0}
        self.assertEqual(statuses(FakeStore(self.QUIET, lwc, {"Pres": 725.0}))["pressure"]["status"], "problem")
        lwc = {"LWC-AveragedDAP": 0.0, "Static Pressure (mB)": 722.0}
        self.assertEqual(statuses(FakeStore(self.QUIET, lwc, {"Pres": 725.0}))["pressure"]["status"], "ok")

    def test_d11_err_share(self):
        err = np.zeros(240)
        err[:60] = 2
        self.assertEqual(statuses(FakeStore(self.QUIET, None, {"Err": err}))["d11"]["status"], "problem")
        self.assertEqual(statuses(FakeStore(self.QUIET, None, {"Err": 0.0}))["d11"]["status"], "ok")

    def test_manual_settings_flag_conflicts(self):
        lwc = {
            "PAS(m/s)": 7.0, "Static Pressure (mB)": 1000.0, "Ambient Temperature (C)": 26.0, "EffectiveWireTemp(C)": 132.0,
            "TotalPower(W)": 2.4, "DAP-Calculated": 2.3, "LWC-CalculatedDAP": 0.15,
        }
        result = manual_settings(Aligned(FakeStore(self.QUIET, lwc, {"Pres": 910.0}), 0, 14400), FakeStore(self.QUIET, lwc, {"Pres": 910.0}))
        rows = {r["label"]: r for r in result["rows"]}
        self.assertFalse(result["has_wind"])
        self.assertEqual(rows["CDP 流速"]["status"], "watch")
        self.assertIn("LWC-300 的 7", rows["CDP 流速"]["note"])
        self.assertIn("沒有實測風速", rows["CDP 流速"]["note"])
        self.assertIn("差 10%", rows["CDP 流速"]["effect"])
        self.assertEqual(rows["LWC-300 壓力"]["status"], "watch")
        self.assertIn("差 +90", rows["LWC-300 壓力"]["note"])
        # (2.4-2.3)/0.15=0.667 W per g/m³;差 1 m/s:2.3×0.6/7/0.667=0.30
        self.assertIn("0.30", rows["LWC-300 流速"]["effect"])

    def test_manual_setting_changed_during_window(self):
        pas = np.full(240, 8.0)
        pas[120:] = 10.0
        store = FakeStore({**self.QUIET, "Applied PAS (m/s)": pas})
        rows = {r["label"]: r for r in manual_settings(Aligned(store, 0, 14400), store)["rows"]}
        self.assertIn("改過設定", rows["CDP 流速"]["note"])
        self.assertIn("8、10", rows["CDP 流速"]["value"])

    def test_manual_setting_changed_since_previous_day(self):
        store = FakeStore(self.QUIET)
        store.previous_settings = lambda start: {"Applied PAS (m/s)": ("2025-04-01", 8.0)}
        rows = {r["label"]: r for r in manual_settings(Aligned(store, 0, 14400), store)["rows"]}
        self.assertIn("2025-04-01", rows["CDP 流速"]["note"])
        self.assertEqual(rows["CDP 流速"]["status"], "watch")

    def test_cloud_events_merge_short_gaps(self):
        number = np.zeros(120)
        number[10:30] = 200
        number[33:50] = 300  # 3 分鐘空檔要合併
        number[80:83] = 200  # 太短,不算
        lwc = np.where(number > 0, 0.2, 0.0)
        nan = np.full(120, np.nan)
        pm = np.full(120, 10.0)
        pm[10:50] = 4.0

        class Axis:
            start = 0

        events = cloud_events(Axis, number, lwc, nan, nan, pm)
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["start"], events[0]["end"], events[0]["minutes"]), (600, 3000, 40))
        self.assertEqual((events[0]["pm_before"], events[0]["pm_inside"]), (10.0, 4.0))


class StoryText(unittest.TestCase):
    """用一天的合成資料:前半無雲、後半有雲,PM 在雲中下降。"""

    def make(self, cloud_number=400.0, pm_cloud=1.0, pas=10.0):
        minutes = 1440
        number = np.where(np.arange(minutes) < 720, 0.05, cloud_number)
        lwc = np.where(np.arange(minutes) < 720, 0.0, 0.2)
        mvd = np.where(np.arange(minutes) < 720, np.nan, 12.0)
        pm = np.where(np.arange(minutes) < 720, 10.0, pm_cloud)
        cdp = {"Number Conc (#/cm^3)": number, "LWC (g/m^3)": lwc, "MVD (um)": mvd, "Applied PAS (m/s)": pas, "Dump Spot Monitor (V)": 4.84, "Laser Current (mA)": 82.0, "Laser Temp (C)": 15.0}
        return FakeStore(cdp, {"LWC-AveragedDAP": np.where(np.arange(minutes) < 720, 0.02, 0.1), "PAS(m/s)": 7.0}, {"PM2.5": pm}, minutes=minutes)

    def story(self, store, site="鹿林", table=None):
        checks = run_checks(Aligned(store, 0, 86400), store, None)
        return build_story(store, 0, 86400, None, checks, site, table or [])

    def test_timeline_is_coarse_and_true(self):
        story = self.story(self.make())
        self.assertEqual([p["state"] for p in story["timeline"]], ["clear", "cloud"])
        self.assertEqual((story["timeline"][0]["from"], story["timeline"][1]["from"], story["timeline"][1]["to"]), ("00:00", "12:00", "24:00"))
        self.assertIn("400 /cm³", story["timeline"][1]["text"])
        self.assertEqual(story["headline"], "00:00–12:00 大致無雲,12:00–24:00 持續有雲。")

    def test_aerosol_drop_and_not_stated_as_fact(self):
        story = self.story(self.make())
        lines = " ".join(l for s in story["sections"] if s["title"] == "氣膠和雲的關係" for l in s["lines"])
        self.assertIn("只剩無雲時的 10%", lines)
        self.assertIn("沒有資料能分辨", lines)

    def test_rank_against_other_days(self):
        table = [dict(day=f"d{i}", site="鹿林", number=100.0 + i, mvd=20.0, lwc=0.1, pm_clear=1.0, pm_cloud=1.0) for i in range(12)]
        story = self.story(self.make(), table=table)
        notes = next(s["lines"] for s in story["sections"] if s["title"] == "這段時間最值得注意")
        self.assertTrue(any("雲滴偏多" in n for n in notes))
        self.assertTrue(any("水滴偏小" in n for n in notes))

    def test_baseline_larger_than_signal_is_said_plainly(self):
        store = self.make()
        store.channels["lwc"].values["LWC-AveragedDAP"] = np.where(np.arange(1440) < 720, 0.3, 0.1)
        lines = " ".join(l for s in self.story(store)["sections"] for l in s["lines"])
        self.assertIn("基線漂移比雲的訊號", lines)

    def test_lab_day_is_not_described_as_a_cloud(self):
        story = self.story(self.make(), site="實驗室")
        self.assertIn("實驗室或校正", story["headline"])
        self.assertTrue(all(s["title"] not in ("這天的雲", "兩種 LWC") for s in story["sections"]))
        story = self.story(self.make(pas=1.0))
        self.assertTrue(story["test_like"])
        self.assertEqual(story["trust"][0]["level"], "bad")

    def test_clear_day_and_empty_range(self):
        story = self.story(self.make(cloud_number=0.05))
        self.assertIn("幾乎沒有雲", story["headline"])
        empty = FakeStore(None)
        self.assertIn("沒有資料", build_story(empty, 0, 3600, None, [], None, [])["headline"])

    def test_phases_stay_few_on_a_noisy_day(self):
        store = self.make()
        number = np.where(np.arange(1440) % 120 < 45, 300.0, 0.05)
        store.channels["cdp"].values["Number Conc (#/cm^3)"] = number
        store.channels["cdp"].values["LWC (g/m^3)"] = np.where(number > 1, 0.2, 0.0)
        self.assertLessEqual(len(self.story(store)["timeline"]), 6)

    def test_similar_days_and_rank_helpers(self):
        today = dict(day="a", number=300.0, mvd=12.0, lwc=0.2, pm_clear=5.0)
        table = [today, dict(day="b", number=310.0, mvd=12.1, lwc=0.21, pm_clear=5.2), dict(day="c", number=900.0, mvd=6.0, lwc=0.9, pm_clear=30.0), dict(day="d", number=100.0, mvd=20.0, lwc=0.1, pm_clear=None)]
        self.assertEqual(similar_days(today, table)[0], "b")


def cdp_csv() -> str:
    head = ",".join(["End Seconds", "Day of Year", "Year", "Status", "Number Conc (#/cm^3)", "LWC (g/m^3)", "MVD (um)", "ED (um)", "Applied PAS (m/s)", "Dump Spot Monitor (V)", "Laser Current (mA)", "CDP Bin 1", "CDP Bin 2"])
    rows = ["[Instrument 0]", "Sample Area (mm^2)=0.267", "Sizes=<2>3,4", " Offset from UTC = 08:00:00", "****", head]
    for second in range(120):
        particles = 10 if second < 60 else 0
        rows.append(f"{36000 + second}.5,126,2025,1,{particles},0.1,{12 if particles else 0},{11 if particles else 0},10,4.8,82,{particles},0")
    return "\n".join(rows) + "\n"


class CacheAndViewer(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name) / "data"
        sid = "20250506100000"
        write(root / f"Instruments/CDP/OBS_data/test/20250506/{sid}/00CDP PBP{sid}.csv", cdp_csv())
        header = "<Header>\n" + "x" * 90 + "\n<Data>\n[d&t]\t0.3 um\t0.5 um\n"
        rows = "".join(f"2025/5/6 上午 10:00:{second:02d}\t1000.0\t0.0\t\n" for second in range(0, 60, 6))
        (root / "Instruments/11D/site").mkdir(parents=True)
        (root / "Instruments/11D/site/a-C.dat").write_bytes((header + rows).encode("cp950"))
        self.cache = Path(self.temp.name) / "cache"
        self.index = build(str(root), self.cache)
        self.root = root

    def tearDown(self):
        self.temp.cleanup()

    def test_minute_values(self):
        cdp, d11 = load(self.cache)
        self.assertEqual(cdp.time.tolist()[1] - cdp.time.tolist()[0], 60)
        self.assertEqual(len(cdp.time), 2)
        self.assertAlmostEqual(cdp.values["mvd_um"][0], 12.0)  # 無粒子的秒數不計入 MVD
        self.assertTrue(np.isnan(cdp.values["mvd_um"][1]))
        self.assertAlmostEqual(cdp.values["number_cm3"][0], 10.0)
        self.assertAlmostEqual(cdp.spectrum[0, 0] * np.log10(3 / 2), 600 / (0.267 * 10 * 60), places=5)
        self.assertEqual(d11.edges.tolist()[:2], [0.3, 0.5])
        self.assertEqual(len(d11.time), 1)

    def test_second_build_reuses_cache(self):
        first = (self.cache / f"cdp_{next(iter(self.index['cdp']))}.npz").stat().st_mtime_ns
        build(str(self.root), self.cache)
        self.assertEqual((self.cache / f"cdp_{next(iter(self.index['cdp']))}.npz").stat().st_mtime_ns, first)

    def test_window_api(self):
        store = Store(self.cache)
        start = int(store.cdp.time[0])
        window = store.window(start, start + 600, 800)
        self.assertEqual(window["step"], 60)
        self.assertEqual(window["count"], 10)
        self.assertEqual(window["cdp"]["series"]["number_cm3"][0], 10.0)
        self.assertIsNone(window["cdp"]["series"]["number_cm3"][5])
        json.dumps(window, allow_nan=False)
        empty = store.window(start - 86400, start - 86000, 800)
        self.assertIsNone(empty["cdp"])
        self.assertIsNone(empty["d11"])
        # 測試資料沒有雲段,預設範圍退回到最後一筆資料
        self.assertEqual(store.default_range()["end"], int(store.cdp.time[-1]) + 60)

    def test_calendar_counts_unique_minutes_per_day(self):
        store = Store(self.cache)
        days = store.calendar()
        self.assertEqual(sorted(days), ["2025-05-06"])
        self.assertEqual(days["2025-05-06"]["minutes"], {"cdp": 2, "d11": 1})
        self.assertIn("test", days["2025-05-06"]["campaigns"])

    def test_all_channels_are_listed_and_served(self):
        store = Store(self.cache)
        names = store.channel_list()
        self.assertIn("Number Conc (#/cm^3)", names["cdp"]["names"])
        self.assertNotIn("CDP Bin 1", names["cdp"]["names"])
        self.assertNotIn("End Seconds", names["cdp"]["names"])
        self.assertEqual(names["cdp"]["label"], "CDP")
        start = int(store.cdp.time[0])
        out = store.channel_window(["cdp|Number Conc (#/cm^3)", "cdp|nope", "bad|x"], start, start + 180, 60)
        self.assertEqual(out, {"cdp|Number Conc (#/cm^3)": [10.0, 0.0, None]})

    def test_diagnose_shape_and_events(self):
        store = Store(self.cache)
        start = int(store.cdp.time[0])
        result = diagnose(store, start, start + 3600, None)
        json.dumps(result, allow_nan=False)
        self.assertEqual({c["id"] for c in result["checks"]}, {"coverage", "dump", "laser", "pas", "baseline", "agreement", "coincidence", "shape", "d11", "pressure"})
        self.assertEqual(result["observed_minutes"], 2)
        self.assertEqual(result["lanes"][0]["runs"], [(0, 2)])
        self.assertEqual(result["events"], [])
        empty = diagnose(store, start + 10 * DAY_S, start + 10 * DAY_S + 3600, None)
        self.assertEqual(empty["observed_minutes"], 0)
        self.assertEqual(empty["checks"][0]["status"], "na")
        json.dumps(empty, allow_nan=False)

    def test_calendar_has_day_status(self):
        days = Store(self.cache).calendar()
        self.assertIn("problem", days["2025-05-06"])
        self.assertIn("watch", days["2025-05-06"])

    def test_bucket_step_averages(self):
        store = Store(self.cache)
        start = int(store.cdp.time[0])
        out = bucket(store.cdp, ["number_cm3"], start, start + 120, 120)
        self.assertEqual(out["series"]["number_cm3"], [5.0])


if __name__ == "__main__":
    unittest.main()
