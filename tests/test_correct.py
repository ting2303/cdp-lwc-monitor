"""校正層:原始值不變、每項都有紀錄、公式正確、等待確認的不亂改。"""
import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cpd_lwc_qc import correct  # noqa: E402
from cpd_lwc_qc.store import Channels, Series  # noqa: E402

N = 600
T0 = 1_700_000_040
TIME = T0 + np.arange(N) * 60
W = 0.664


def make(number=None, power=None, dap=None, pas=7.0, cdp_pas=10.0, dump=None):
    number = np.zeros(N) if number is None else number
    dap = np.full(N, 2.33) if dap is None else dap
    power = np.full(N, 2.33) if power is None else power
    hot = (power - dap) / W
    return SimpleNamespace(channels={
        "cdp": Channels(TIME.copy(), {"Number Conc (#/cm^3)": number, "LWC (g/m^3)": number * 0.001, "Dump Spot Monitor (V)": np.full(N, 4.84) if dump is None else dump, "Laser Current (mA)": np.full(N, 90.0), "Applied PAS (m/s)": np.full(N, cdp_pas)}),
        "lwc": Channels(TIME.copy(), {"LWC-AveragedDAP": hot, "TotalPower(W)": power, "DAP-Averaged": dap, "PAS(m/s)": np.full(N, pas), "Static Pressure (mB)": np.full(N, 722.0)}),
    }, d11=None)


def kinds(result):
    return {i["kind"] for i in result.items}


class Lwc(unittest.TestCase):
    def test_heater_off_is_removed_and_logged(self):
        power = np.full(N, 2.33)
        power[100:120] = 0.03
        store = make(power=power)
        raw = store.channels["lwc"].values["LWC-AveragedDAP"].copy()
        result = correct.compute(store)
        index = np.searchsorted(result.lwc_time, TIME[110])
        self.assertTrue(np.isnan(result.lwc_corrected[index]))
        self.assertTrue(np.isnan(result.lwc_corrected[index - 12]))  # 恢復前後多標 2 分鐘
        self.assertTrue(np.isfinite(result.lwc_corrected[index + 15]))
        self.assertAlmostEqual(result.lwc_raw[index], (0.03 - 2.33) / W)
        self.assertIn("加熱線沒功率", kinds(result))
        np.testing.assert_array_equal(store.channels["lwc"].values["LWC-AveragedDAP"], raw)  # 原始值不變

    def test_bad_dap_is_recomputed_from_clear_power(self):
        dap = np.full(N, 2.33)
        dap[300:460] = 0.0
        power = np.full(N, 2.40)
        result = correct.compute(make(power=power, dap=dap))
        index = 350
        self.assertAlmostEqual(result.lwc_raw[index], 2.40 / W, places=3)
        self.assertLess(abs(result.lwc_corrected[index]), 0.01)
        self.assertIn("乾空氣功率異常", kinds(result))

    def test_baseline_offset_removed_near_clear_air(self):
        power = np.full(N, 2.33)
        dap = np.full(N, 2.23)  # 基線偏 +0.15
        number = np.zeros(N)
        number[300:310] = 200  # 短暫的雲
        power[300:310] = 2.33 + 0.2 * W
        result = correct.compute(make(number=number, power=power, dap=dap))
        self.assertAlmostEqual(result.lwc_corrected[200], 0.0, places=3)
        self.assertAlmostEqual(result.lwc_corrected[305], 0.2, places=2)
        self.assertAlmostEqual(result.lwc_raw[305], 0.2 + 0.1 / W, places=2)
        self.assertIn("基線偏移", kinds(result))

    def test_far_from_clear_air_keeps_original_and_flags(self):
        number = np.full(N, 200.0)
        number[:20] = 0
        dap = np.full(N, 2.23)
        result = correct.compute(make(number=number, dap=dap))
        far = 400
        self.assertEqual(result.lwc_corrected[far], result.lwc_raw[far])
        self.assertTrue(result.lwc_flags[far] & correct.NO_BASELINE)
        self.assertIn("沒有基線可重算", kinds(result))

    def test_start_of_recording_removed(self):
        result = correct.compute(make())
        self.assertTrue(np.isnan(result.lwc_corrected[0]))
        self.assertTrue(np.isfinite(result.lwc_corrected[5]))


class Cdp(unittest.TestCase):
    def test_fog_is_flagged_not_changed(self):
        dump = np.full(N, 4.84)
        dump[50:80] = 0.7
        result = correct.compute(make(dump=dump))
        self.assertTrue(result.cdp_flags[60] & correct.FOG)
        self.assertEqual(result.number_corrected[60], result.number[60])
        item = next(i for i in result.items if i["kind"] == "鏡片可能起霧")
        self.assertEqual(item["action"], "只標記")

    def test_different_speeds_wait_for_you(self):
        result = correct.compute(make())
        item = next(i for i in result.items if i["kind"] == "兩台流速不同")
        self.assertEqual((item["action"], item["status"]), ("沒有改值", "等你確認"))
        self.assertIn("1.43", item["reason"])
        np.testing.assert_array_equal(result.number_corrected, result.number)

    def test_speed_override(self):
        number = np.full(N, 100.0)
        start, end = correct._stamp(TIME[100]), correct._stamp(TIME[200])
        overrides = [{"instrument": "cdp", "start": correct._epoch(start), "end": correct._epoch(end), "speed": 7.0, "note": "測試"}]
        result = correct.compute(make(number=number), overrides)
        self.assertAlmostEqual(result.number_corrected[150], 100.0 * 10 / 7)
        self.assertEqual(result.number_corrected[50], 100.0)
        self.assertTrue(result.cdp_flags[150] & correct.PAS_OVERRIDE)
        self.assertIn("套用指定流速", kinds(result))


class D11(unittest.TestCase):
    def store(self, c0, pm):
        edges = np.array([0.253, 0.298, 0.352, 0.414])
        widths = np.log10(edges[1:] / edges[:-1])
        per_liter = np.column_stack([np.full(N, c0), np.full(N, 100.0), np.full(N, 50.0)])
        series = Series(time=TIME.copy(), values={"PM2.5": pm, "Err": np.zeros(N)}, spectrum=per_liter / widths / 1000, edges=edges, labels=np.zeros(N, int), meta=[])
        return SimpleNamespace(channels={}, d11=series)

    def test_fine_channel_flagged_only_when_clean(self):
        clean = correct.compute(self.store(1000.0, np.full(N, 1.0)))
        self.assertTrue(clean.d11_flags[10] & correct.FINE)
        item = next(i for i in clean.items if i["kind"] == "0.253 µm 偏高")
        self.assertEqual(item["action"], "只標記")
        dirty = correct.compute(self.store(1000.0, np.full(N, 20.0)))
        self.assertFalse(dirty.d11_flags[10] & correct.FINE)

    def test_pm_spike(self):
        pm = np.full(N, 5.0)
        pm[300] = 400.0
        result = correct.compute(self.store(200.0, pm))
        self.assertTrue(result.d11_flags[300] & correct.PM_SPIKE)
        self.assertFalse(result.d11_flags[100] & correct.PM_SPIKE)


class Files(unittest.TestCase):
    def test_export_and_overrides(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            power = np.full(N, 2.33)
            power[100:110] = 0.03
            summary = correct.export(correct.compute(make(power=power)), root)
            self.assertGreaterEqual(summary["days"], 1)
            with open(root / "校正紀錄.csv", encoding="utf-8-sig") as handle:
                rows = list(csv.DictReader(handle))
            self.assertTrue(any(r["項目"] == "加熱線沒功率" for r in rows))
            day = next(root.glob("校正_*.csv"))
            with open(day, encoding="utf-8-sig") as handle:
                rows = list(csv.DictReader(handle))
            self.assertIn("LWC300_校正", rows[0])
            self.assertTrue(any("加熱線沒功率" in r["LWC300標記"] for r in rows))
            good = root / "good.json"
            good.write_text(json.dumps({"pas_true": [{"start": "2025-01-01", "end": "2025-01-02", "speed": 7}]}), encoding="utf-8")
            self.assertEqual(correct.load_overrides(good)[0]["instrument"], "cdp")
            bad = root / "bad.json"
            bad.write_text(json.dumps({"pas_true": [{"start": "2025-01-01", "end": "2025-01-02", "speed": -1}]}), encoding="utf-8")
            with self.assertRaises(ValueError):
                correct.load_overrides(bad)
            self.assertEqual(correct.load_overrides(root / "none.json"), [])


class RealCache(unittest.TestCase):
    """有快取時,用真實資料確認幾個已知的異常都被抓到。"""

    @classmethod
    def setUpClass(cls):
        cache = ROOT / "cache"
        if not (cache / "index.json").exists():
            raise unittest.SkipTest("沒有快取")
        from cpd_lwc_qc.viewer import Store

        cls.store = Store(cache)
        cls.result = cls.store.corrections

    def test_known_events_found(self):
        items = self.result.items
        self.assertTrue(any(i["kind"] == "乾空氣功率異常" and i["start_text"].startswith("2024-03-01") for i in items))
        self.assertTrue(any(i["kind"] == "加熱線沒功率" and i["start_text"].startswith("2024-02-22") for i in items))
        self.assertTrue(any(i["kind"] == "鏡片可能起霧" and i["start_text"].startswith("2024-03-06") for i in items))

    def test_extremes_go_away(self):
        hot, cor = self.result.lwc_raw, self.result.lwc_corrected
        self.assertGreater(np.count_nonzero(np.abs(hot) > 1), 300)
        self.assertLess(np.nansum(np.abs(cor) > 1), 150)

    def test_window_and_list(self):
        start = correct._epoch("2024-03-01 00:00")
        window = self.store.window(start, start + 86400, 400)
        self.assertIn("hotwire_corrected", window["cdp"]["series"])
        listed = self.store.correction_items(start, start + 86400)
        self.assertTrue(listed["items"])


if __name__ == "__main__":
    unittest.main()
