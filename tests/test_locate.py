"""自動尋找資料夾的測試。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cpd_lwc_qc.live import Feed  # noqa: E402
from cpd_lwc_qc.locate import Locator, Search  # noqa: E402
from cpd_lwc_qc.viewer import Store, make_handler  # noqa: E402

SID = "20261018163933"


def touch(path: Path, mtime=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    if mtime:
        os.utime(path, (mtime, mtime))


def run_search(root, skip=()):
    search = Search([str(root)], skip)
    search.run()
    return search.result()


class PageScript(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "需要 Node.js")
    def test_script_has_no_syntax_error(self):
        html = (ROOT / "cpd_lwc_qc" / "viewer.html").read_text(encoding="utf-8")
        script = html[html.index("<script>") + 8: html.rindex("</script>")]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "page.js"
            path.write_text(script, encoding="utf-8")
            result = subprocess.run([shutil.which("node"), "--check", str(path)], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)


class Finding(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_pads_root_is_above_date_and_session(self):
        touch(self.root / f"DMT/PADS Data/20261018/{SID}/00CDP PBP{SID}.csv")
        touch(self.root / f"DMT/PADS Data/20261018/{SID}/01LWC 300_{SID}.csv")
        touch(self.root / f"DMT/PADS Data/20261018/{SID}/00CDP PBP{SID}_PBP.csv")
        result = run_search(self.root)
        self.assertEqual([Path(p["path"]).name for p in result["pads"]], ["PADS Data"])
        self.assertEqual(result["pads"][0]["files"], 2)
        self.assertEqual(result["pads"][0]["kinds"], ["CDP", "LWC-300"])
        self.assertEqual(result["d11"], [])

    def test_campaign_folders_merge_to_common_parent(self):
        for campaign in ("A", "B"):
            touch(self.root / f"Lab/Instruments/CDP/OBS_data/{campaign}/20250506/{SID}/00CDP PBP{SID}.csv")
        touch(self.root / f"Lab/Instruments/LWC-300/Obs_data/C/20250506/{SID}/00LWC 300_{SID}.csv")
        result = run_search(self.root)
        self.assertEqual([Path(p["path"]).name for p in result["pads"]], ["Instruments"])
        self.assertEqual(result["pads"][0]["files"], 3)

    def test_d11_by_name_and_by_folder(self):
        touch(self.root / "Data/x/20251104-C.dat")
        touch(self.root / "Data/x/20251104-dM.dat")
        touch(self.root / "Grimm/raw.csv")
        touch(self.root / "other/notes.csv")
        touch(self.root / "other/result.dat")
        names = {Path(p["path"]).name for p in run_search(self.root)["d11"]}
        self.assertEqual(names, {"x", "Grimm"})

    def test_system_folders_and_own_folder_are_skipped(self):
        touch(self.root / f"Windows/a/20261018/{SID}/00CDP PBP{SID}.csv")
        touch(self.root / f"Users/me/AppData/Local/b/{SID}/00CDP PBP{SID}.csv")
        touch(self.root / f"Panel/c/{SID}/00CDP PBP{SID}.csv")
        self.assertEqual(run_search(self.root, skip=[str(self.root / "Panel")])["pads"], [])

    def test_newest_first(self):
        now = time.time()
        touch(self.root / f"Old/PADS/20200101/{SID}/00CDP PBP{SID}.csv", now - 9000)
        touch(self.root / f"Users/new/PADS/20200101/{SID}/00CDP PBP{SID}.csv", now)
        result = run_search(self.root)
        self.assertGreater(result["pads"][0]["latest"], result["pads"][-1]["latest"])

    def test_cancel(self):
        for i in range(20):
            touch(self.root / f"d{i}/a.txt")
        search = Search([str(self.root)])
        search.cancelled.set()
        search.run()
        self.assertTrue(search.result()["cancelled"])
        self.assertEqual(search.scanned, 0)


class Applying(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "pads").mkdir()
        (self.root / "d11").mkdir()
        self.config = self.root / "panel_config.json"
        self.config.write_text(json.dumps({"pads_roots": [], "d11_roots": [], "port": 1}), encoding="utf-8")
        self.feed = Feed({"pads_roots": [], "d11_roots": []})
        self.locator = Locator(self.feed, self.config, json.loads(self.config.read_text(encoding="utf-8")), roots=[str(self.root)])
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Store(self.root / "none", allow_empty=True), self.feed, self.locator))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def post(self, path, body=None, headers=None):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body or {}).encode(), method="POST", headers=headers if headers is not None else {"X-Panel": "1"})
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def test_apply_saves_and_switches_without_restart(self):
        status, result = self.post("/api/locate/apply", {"pads_roots": [str(self.root / "pads")], "d11_roots": [str(self.root / "d11")]})
        self.assertEqual((status, result["ok"]), (200, True))
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(saved["pads_roots"], [str(self.root / "pads").replace("\\", "/")])
        self.assertEqual(saved["port"], 1)  # 其他設定保留
        self.feed.poll()
        self.assertEqual(self.feed.pads_roots, [Path(saved["pads_roots"][0])])

    def test_missing_folder_is_rejected_and_nothing_saved(self):
        before = self.config.read_text(encoding="utf-8")
        status, result = self.post("/api/locate/apply", {"pads_roots": ["C:/no/such/folder"], "d11_roots": []})
        self.assertEqual(status, 400)
        self.assertIn("找不到資料夾", result["errors"][0])
        self.assertEqual(self.config.read_text(encoding="utf-8"), before)

    def test_bad_types_are_rejected(self):
        status, _ = self.post("/api/locate/apply", {"pads_roots": "C:/", "d11_roots": [1]})
        self.assertEqual(status, 400)

    def test_request_without_panel_header_is_refused(self):
        status, _ = self.post("/api/locate/start", headers={})
        self.assertEqual(status, 403)
        self.assertIsNone(self.locator.search)

    def test_foreign_host_is_refused(self):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/locate", headers={"Host": "evil.example"})
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(request)
        status, _ = self.post("/api/locate/start", headers={"X-Panel": "1", "Host": "evil.example"})
        self.assertEqual(status, 403)

    def test_search_over_http(self):
        touch(self.root / f"PADS Data/20261018/{SID}/00CDP PBP{SID}.csv")
        self.assertEqual(self.post("/api/locate/start")[0], 200)
        for _ in range(50):
            state = json.load(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/locate"))
            if state["search"]["done"]:
                break
            time.sleep(0.05)
        self.assertIn("PADS Data", [Path(p["path"]).name for p in state["search"]["pads"]])
        self.feed.stop()


if __name__ == "__main__":
    unittest.main()
