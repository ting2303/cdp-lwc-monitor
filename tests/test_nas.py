"""NAS 備份:兩個目的地、記錄中的檔案先傳目前內容、NAS 沒連上時記成失敗。"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import backup  # noqa: E402
from cpd_lwc_qc.live import Feed  # noqa: E402
from cpd_lwc_qc.locate import Locator  # noqa: E402


class ActiveFiles(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "src"
        self.dest = self.root / "dst"
        self.source.mkdir()
        self.file = self.source / "00CDP PBP.csv"
        self.file.write_bytes(b"header\r\n" + b"1,2,3\r\n" * 100)

    def tearDown(self):
        self.temp.cleanup()

    def job(self, **extra):
        return {"name": "PADS", "source": str(self.source), "dest": str(self.dest), **extra}

    def test_without_option_active_files_wait(self):
        result = backup.run_job(self.job(), 15, False)
        self.assertEqual((result.in_use, result.copied), (1, 0))
        self.assertFalse((self.dest / self.file.name).exists())

    def test_active_file_copied_then_updated_as_it_grows(self):
        result = backup.run_job(self.job(include_active=True), 15, False)
        self.assertEqual(result.copied, 1)
        target = self.dest / self.file.name
        self.assertEqual(target.read_bytes(), self.file.read_bytes())
        self.assertAlmostEqual(target.stat().st_mtime, self.file.stat().st_mtime, delta=2)
        again = backup.run_job(self.job(include_active=True), 15, False)
        self.assertEqual((again.copied, again.updated, again.unchanged), (0, 0, 1))  # 沒變就不重傳
        with open(self.file, "ab") as handle:
            handle.write(b"4,5,6\r\n" * 10)
        grown = backup.run_job(self.job(include_active=True), 15, False)
        self.assertEqual(grown.updated, 1)
        self.assertEqual(target.read_bytes(), self.file.read_bytes())
        self.assertTrue(any("記錄中" in row[2] for row in grown.rows))

    def test_big_active_file_waits(self):
        result = backup.run_job(self.job(include_active=True, active_max_mb=0.0001), 15, False)
        self.assertEqual(result.in_use, 1)

    def test_snapshot_takes_only_the_size_seen(self):
        target = self.dest / "x.csv"
        backup.copy_snapshot(self.file, target, 10, time.time())
        self.assertEqual(target.read_bytes(), self.file.read_bytes()[:10])

    def test_never_shrinks_a_bigger_copy(self):
        self.dest.mkdir()
        target = self.dest / self.file.name
        target.write_bytes(self.file.read_bytes() * 2)
        old = time.time() - 3600
        os.utime(target, (old, old))
        result = backup.run_job(self.job(include_active=True), 15, False)
        self.assertEqual(result.conflicts, 1)
        self.assertEqual(len(target.read_bytes()), 2 * len(self.file.read_bytes()))


class Setup(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in ("pads", "d11", "cloud", "nas"):
            (self.root / name).mkdir()
        self.path = self.root / "panel_config.json"
        self.config = {"pads_roots": [str(self.root / "pads")], "d11_roots": [str(self.root / "d11")]}
        self.path.write_text(json.dumps(self.config), encoding="utf-8")
        self.locator = Locator(Feed(self.config), self.path, self.config)

    def tearDown(self):
        self.temp.cleanup()

    def saved(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_cloud_and_nas_both_get_cdp_and_11d(self):
        self.assertEqual(self.locator.apply_remote(str(self.root / "cloud"), "東眼山", None, None, str(self.root / "nas")), [])
        jobs = self.saved()["backup_jobs"]
        self.assertEqual([j["name"] for j in jobs], ["PADS", "11-D", "PADS→NAS", "11-D→NAS"])
        self.assertTrue(all(j["include_active"] for j in jobs))
        self.assertTrue(jobs[2]["dest"].endswith("nas/CDPLWC_現場/東眼山/PADS"))
        self.assertTrue(self.saved()["status_dir"].startswith(str(self.root / "cloud").replace("\\", "/")))
        self.assertEqual(self.locator.state()["nas_folder"], str(self.root / "nas").replace("\\", "/"))

    def test_nas_only(self):
        self.assertEqual(self.locator.apply_remote("", "東眼山", None, None, str(self.root / "nas")), [])
        saved = self.saved()
        self.assertEqual([j["name"] for j in saved["backup_jobs"]], ["PADS→NAS", "11-D→NAS"])
        self.assertIn("nas/CDPLWC_現場/東眼山/狀態", saved["status_dir"])

    def test_missing_nas_is_rejected_and_blank_keeps_off(self):
        self.assertTrue(self.locator.apply_remote("", "a", None, None, str(self.root / "no-such")))
        self.assertEqual(self.locator.apply_remote(str(self.root / "cloud"), "a", None, None, ""), [])
        self.assertEqual([j["name"] for j in self.saved()["backup_jobs"]], ["PADS", "11-D"])


if __name__ == "__main__":
    unittest.main()
