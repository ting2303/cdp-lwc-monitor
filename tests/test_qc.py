"""用合成資料測試讀檔、時間處理與標記規則。python -m unittest discover -s tests"""
import argparse
import sys
import tempfile
import unittest
import zipfile
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cpd_lwc_qc import d11  # noqa: E402
from cpd_lwc_qc.__main__ import run  # noqa: E402
from cpd_lwc_qc.pads import add_time, campaign_of, instrument_kind, parse_utc_offset, summarise_log  # noqa: E402
from cpd_lwc_qc.qc import Limits, add_split_info, best_lag, session_flags  # noqa: E402
from cpd_lwc_qc.pads import Session  # noqa: E402
from cpd_lwc_qc.sources import DataSet, normalize_rel  # noqa: E402

SID = "20250506235950"
CDP_COLUMNS = (
    "End Seconds,Day of Year,Year,Status,Number Conc (#/cm^3),LWC (g/m^3),MVD (um),ED (um),"
    "Applied PAS (m/s),Dump Spot Monitor (V),Laser Current (mA),Oversize Particles"
)
LWC_COLUMNS = (
    "End_Seconds,Day_of_Year,Year,Status,LWC-AveragedDAP,LWC-CalculatedDAP,DAP-Averaged,"
    "EffectiveWireTemp(C),24V_Supply,8V_Supply,PAS(m/s),Static Pressure (mB),Ambient Temperature (C)"
)


def cdp_text(seconds: int = 900) -> str:
    start = 23 * 3600 + 59 * 60 + 50
    lines = [
        "[Instrument 1]",
        "Instrument Type=CDP PBP",
        "Sample Area (mm^2)=0.267",
        "Thresholds=<30>91,111",
        "Time Zone = 台北標準時間",
        " Offset from UTC = 08:00:00",
        "",
        "****",
        CDP_COLUMNS,
    ]
    for i in range(seconds):
        in_cloud = i >= 300
        number = 100.0 if in_cloud else 0.0
        lwc = 0.1 + 0.1 * np.sin(i / 20) if in_cloud else 0.0
        lines.append(f"{start + i}.0,126,2025,1,{number},{lwc:.4f},10,10,10,4.8,82,0")
    return "\n".join(lines) + "\n"


def lwc_text(seconds: int = 900, baseline: float = 0.1) -> str:
    start = 23 * 3600 + 59 * 60 + 50
    lines = ["[Instrument 0]", "Instrument Type=LWC 300", " Offset from UTC = 08:00:00", "", "****", LWC_COLUMNS]
    for i in range(seconds):
        lwc = baseline + (0.1 + 0.1 * np.sin(i / 20) if i >= 300 else 0.0)
        lines.append(f"{start + i}.0,126,2025,1,{lwc:.4f},{lwc:.4f},2.3,150,2.7E+11,0.015,7,722,10")
    return "\n".join(lines) + "\n"


def write_dataset(root: Path) -> None:
    folder = "Instruments/CDP/OBS_data/2025 Cloud IOP_Lulin/0427-/20250506/" + SID
    archive = root / "Instruments-20261105T070548Z-1-001.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr(f"{folder}/01CDP PBP{SID}.csv", cdp_text().encode("cp950"))
        handle.writestr(f"{folder}/01CDP PBP{SID}_PBP.csv", "should be ignored")
        handle.writestr(f"{folder}/00LWC 300_{SID}.csv", lwc_text().encode("cp950"))
        handle.writestr(f"{folder}/00LWC 300_{SID}.raw", "x")
        handle.writestr(f"{folder}/Original{SID}.ini", "Pressure(mb)=730\nTemp (C)=10\n")
        handle.writestr(f"{folder}/Log{SID}.txt", "10:00:00 20250506: PADS program RESET\n")
    extracted = root / "Instruments-20261105T070548Z-1-009" / folder
    extracted.mkdir(parents=True)
    (extracted / f"Log{SID}.txt").write_text("10:00:00 20250506: PADS program RESET\n", encoding="cp950")
    d11_text = (
        "<Header>\nModel: 11-D\n<Data>\n"
        "[d&t2035/12/31 上午 11:50:55]\tP\tYear\tMon\tDay\tHr\tMin\tSec\tErr\tPres\n"
        "2025/3/27 下午 05:36:05\tP\t25\t3\t27\t17\t33\t58\t0\t723.1\t\n"
        "2025/3/27 下午 05:36:11\tP\t25\t3\t27\t17\t34\t5\t0\t723.1\t\n"
    )
    with zipfile.ZipFile(archive, "a") as handle:
        handle.writestr("Instruments/11D/2025 Cloud IOP_Lulin/03271735-L.dat", d11_text.encode("cp950"))


class Parsing(unittest.TestCase):
    def test_instrument_names(self):
        self.assertEqual(instrument_kind("01CDP PBP20250506100907.csv"), "cdp")
        self.assertEqual(instrument_kind("00LWC 300_20250506100907.csv"), "lwc")
        self.assertEqual(instrument_kind("02FM 10020250323150302.csv"), "fm")
        self.assertIsNone(instrument_kind("01CDP PBP20250506100907_PBP.csv"))
        self.assertIsNone(instrument_kind("20250506100907Shared.csv"))

    def test_campaign(self):
        rel = "Instruments/CDP/OBS_data/KAPAS/IOP_1_0213-0216/20240213/20240213090000"
        self.assertEqual(campaign_of(rel), ("KAPAS", "IOP_1_0213-0216"))
        rel = "Instruments/CDP/OBS_data/20230906 lab test/20230907/20230907001052"
        self.assertEqual(campaign_of(rel), ("20230906 lab test", ""))

    def test_drive_prefix_removed(self):
        self.assertEqual(
            normalize_rel("Instruments-20261105T070548Z-1-009\\Instruments\\CDP\\a.csv"), "Instruments/CDP/a.csv"
        )

    def test_utc_offset(self):
        self.assertEqual(parse_utc_offset("08:00:00"), timedelta(hours=8))
        self.assertEqual(parse_utc_offset("-05:30:00"), -timedelta(hours=5, minutes=30))
        self.assertEqual(parse_utc_offset(None), timedelta(hours=8))

    def test_time_crosses_midnight_with_fixed_day_of_year(self):
        table = pd.DataFrame({"end_seconds": [86399.0, 86401.0], "day_of_year": [126, 126], "year": [2025, 2025]})
        out = add_time(table, timedelta(hours=8))
        self.assertEqual(out["time_local"].iloc[0], pd.Timestamp("2025-05-06 23:59:59"))
        self.assertEqual(out["time_local"].iloc[1], pd.Timestamp("2025-05-07 00:00:01"))
        self.assertEqual(out["time_utc"].iloc[1], pd.Timestamp("2025-05-06 16:00:01"))

    def test_d11_twelve_hour_clock(self):
        self.assertEqual(d11.parse_timestamp("2025/3/29 上午 12:05:00"), pd.Timestamp("2025-03-29 00:05:00"))
        self.assertEqual(d11.parse_timestamp("2025/3/29 下午 12:05:00"), pd.Timestamp("2025-03-29 12:05:00"))
        self.assertEqual(d11.parse_timestamp("2025/3/29 下午 05:36:05"), pd.Timestamp("2025-03-29 17:36:05"))
        self.assertIsNone(d11.parse_timestamp("not a time"))

    def test_best_lag_sign(self):
        a = np.zeros(300)
        a[100:120] = 1
        b = np.roll(a, 7)
        self.assertEqual(best_lag(a, b, 20), 7)


class Flags(unittest.TestCase):
    def codes(self, **row):
        session = Session("20250506100907", "Instruments/CDP/x/2025 Cloud IOP_Lulin/20250506100907", "c", "")
        return {f["code"] for f in session_flags(row, session, Limits(site_pressure=[{"match": "Lulin", "pressure_mb": 723}]))}

    def test_pas_mismatch(self):
        self.assertIn("PAS_MISMATCH", self.codes(pas=10.0, lwc_pas=7.0))
        self.assertNotIn("PAS_MISMATCH", self.codes(pas=7.0, lwc_pas=7.0))

    def test_supply_range(self):
        self.assertIn("V24_RANGE", self.codes(v24=2.7e11))
        self.assertNotIn("V24_RANGE", self.codes(v24=24.0))

    def test_baseline_threshold(self):
        self.assertIn("BASELINE_OFFSET", self.codes(baseline=-0.2))
        self.assertNotIn("BASELINE_OFFSET", self.codes(baseline=0.02))

    def test_nan_metric_is_not_flagged(self):
        self.assertNotIn("BASELINE_OFFSET", self.codes(baseline=float("nan")))

    def test_site_pressure(self):
        self.assertIn("SITE_PRESSURE", self.codes(pressure_mb=969.0))
        self.assertNotIn("SITE_PRESSURE", self.codes(pressure_mb=722.0))

    def test_lag_needs_good_correlation(self):
        self.assertNotIn("TIME_LAG", self.codes(lag_s=30, corr_avg=0.1))
        self.assertIn("TIME_LAG", self.codes(lag_s=30, corr_avg=0.9))


class D11Files(unittest.TestCase):
    def analyse_bytes(self, content: bytes):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "Instruments" / "11D" / "site"
            folder.mkdir(parents=True)
            (folder / "a-L.dat").write_bytes(content)
            with DataSet(str(root)) as dataset:
                groups = d11.discover(dataset)
                stem = next(iter(groups))
                return d11.analyse(dataset, stem, groups[stem])

    def test_trailing_tab_in_rows_only(self):
        # 真實檔案的資料列結尾多一個 tab,標題列沒有。
        header = "<Header>\n" + "x" * 80 + "\n<Data>\n[d&t]\tP\tYear\tMon\tDay\tHr\tMin\tSec\n"
        rows = "2025/3/27 下午 05:36:05\tP\t25\t3\t27\t17\t33\t58\t\n"
        row = self.analyse_bytes((header + rows).encode("cp950"))
        self.assertEqual(row["rows"], 1)
        self.assertEqual(row["clock_offset_s"], 127.0)

    def test_placeholder_file_is_reported_as_no_rows(self):
        row = self.analyse_bytes(b"1")
        self.assertEqual(row["rows"], 0)
        codes = {f["code"] for f in d11.flags_for(row, Limits())}
        self.assertIn("D11_NO_ROWS", codes)


class Splitting(unittest.TestCase):
    def test_log_endings(self):
        stop = "11:05:14  20250507:   Recording stopped."
        start = "08:58:45  20250507:   Recording started."
        error = "*** 08:50:53  20240301:   Error 5902 occurred at CDP Initialize.vi"
        self.assertEqual(summarise_log(f"a\n{stop}\n")["log_ending"], "stopped")
        self.assertEqual(summarise_log(f"{stop}\n{start}\n")["log_ending"], "restarted")
        self.assertEqual(summarise_log(f"{stop}\n{error}\n")["log_ending"], "stopped_then_errors")
        self.assertEqual(summarise_log(f"{error}\n{error}\n"), {"log_ending": "no_stop", "comm_errors": 2})
        self.assertEqual(summarise_log("  \n")["log_ending"], "empty")

    def row(self, sid, start, end, ending, source="cdp"):
        return dict(sid=sid, start=pd.Timestamp(start), end=pd.Timestamp(end), log_ending=ending, number_source=source)

    def test_reasons_and_gaps(self):
        frame = add_split_info(pd.DataFrame([
            self.row("a", "2025-05-06 10:00:00", "2025-05-07 08:58:44", "restarted"),
            self.row("b", "2025-05-07 08:58:46", "2025-05-07 11:00:00", "stopped"),
            self.row("c", "2025-05-07 11:03:00", "2025-05-07 12:00:00", "stopped"),
            self.row("d", "2025-05-08 12:00:00", "2025-05-08 13:00:00", "no_stop"),
            self.row("e", "2025-05-09 12:00:00", "2025-05-09 13:00:00", "stopped"),
            self.row("f", "2025-05-07 09:00:00", "2025-05-07 09:30:00", "stopped", source="fm"),
        ])).set_index("sid")
        self.assertEqual(frame.loc["a", "split_reason"], "立刻開新檔")
        self.assertEqual(frame.loc["a", "next_sid"], "b")
        self.assertEqual(frame.loc["a", "gap_to_next_s"], 2)
        self.assertEqual(frame.loc["b", "split_reason"], "停止後 10 分鐘內重新記錄")
        self.assertEqual(frame.loc["c", "split_reason"], "停止記錄")
        self.assertEqual(frame.loc["d", "split_reason"], "未正常結束")
        self.assertEqual(frame.loc["e", "split_reason"], "停止記錄(之後沒有資料)")
        self.assertTrue(pd.isna(frame.loc["e", "gap_to_next_s"]))
        # FM 不算進 CDP 的接續
        self.assertEqual(frame.loc["f", "split_reason"], "停止記錄(之後沒有資料)")

    def test_overlap_is_not_a_continuation(self):
        frame = add_split_info(pd.DataFrame([
            self.row("a", "2025-05-06 10:00:00", "2025-05-06 12:00:00", "stopped"),
            self.row("b", "2025-05-06 11:00:00", "2025-05-06 13:00:00", "stopped"),
        ])).set_index("sid")
        self.assertTrue(pd.isna(frame.loc["a", "gap_to_next_s"]))

    def test_missing_log(self):
        frame = add_split_info(pd.DataFrame([dict(sid="a", start=pd.Timestamp("2025-05-06"), end=pd.Timestamp("2025-05-06 01:00"), number_source="cdp")]))
        self.assertEqual(frame.loc[0, "split_reason"], "無紀錄檔")

    def test_flags(self):
        session = Session("20250506100907", "x/20250506100907", "c", "", instruments={"cdp": object()})
        codes = lambda **row: {f["code"] for f in session_flags(row, session, Limits())}
        self.assertIn("ABNORMAL_END", codes(log_ending="no_stop"))
        self.assertNotIn("ABNORMAL_END", codes(log_ending="stopped"))
        self.assertIn("NO_LOG", codes())
        severity = {f["code"]: f["severity"] for f in session_flags({"log_ending": "stopped", "comm_errors": 3, "gaps": 1, "max_gap_s": 60.0}, session, Limits())}
        self.assertEqual(severity["COMM_ERRORS"], "warn")


class EndToEnd(unittest.TestCase):
    def test_full_run_on_zip_and_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "data"
            root.mkdir()
            write_dataset(root)
            out = Path(temp) / "out"
            args = argparse.Namespace(
                data=str(root), out=str(out), config=None, match=None, export=True, skip_11d=False
            )
            self.assertEqual(run(args), 0)

            sessions = pd.read_csv(out / "sessions.csv", dtype={"sid": str})
            self.assertEqual(len(sessions), 1)  # 壓縮檔與解壓資料夾的同一次量測只算一次
            row = sessions.iloc[0]
            self.assertEqual(row["sid"], SID)
            self.assertEqual(row["campaign"], "2025 Cloud IOP_Lulin")
            self.assertEqual(row["pas"], 10)
            self.assertEqual(row["lwc_pas"], 7)
            self.assertAlmostEqual(row["baseline"], 0.1, places=3)
            self.assertAlmostEqual(row["corr_1s"], 1.0, places=2)
            self.assertEqual(row["lag_s"], 0)
            self.assertEqual(row["gaps"], 0)
            self.assertEqual(row["log_resets"], 1)

            codes = set(pd.read_csv(out / "flags.csv")["code"])
            self.assertTrue({"PAS_MISMATCH", "V24_RANGE", "V8_RANGE", "BASELINE_OFFSET"} <= codes)
            self.assertNotIn("READ_ERROR", codes)

            d11_row = pd.read_csv(out / "d11_files.csv").iloc[0]
            self.assertEqual(d11_row["rows"], 2)
            self.assertAlmostEqual(d11_row["clock_offset_s"], 126.5, places=1)
            self.assertIn("D11_CLOCK_OFFSET", set(pd.read_csv(out / "d11_flags.csv")["code"]))

            page = (out / "report.html").read_text(encoding="utf-8")
            self.assertIn(SID, page)
            aligned = pd.read_csv(out / "aligned" / f"{SID}.csv.gz")
            self.assertEqual(len(aligned), 900)
            self.assertEqual(aligned["time_local"].iloc[0], "2025-05-06 23:59:50")
            self.assertEqual(aligned["time_local"].iloc[-1], "2025-05-07 00:14:49")
            self.assertEqual(aligned["time_utc"].iloc[0], "2025-05-06 15:59:50")

    def test_corrupt_file_does_not_stop_scan(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "data"
            folder = root / "Instruments/CDP/OBS_data/test/20250101/20250101000000"
            folder.mkdir(parents=True)
            (folder / "00CDP PBP20250101000000.csv").write_text("no marker here\n", encoding="utf-8")
            out = Path(temp) / "out"
            args = argparse.Namespace(
                data=str(root), out=str(out), config=None, match=None, export=False, skip_11d=True
            )
            self.assertEqual(run(args), 0)
            self.assertIn("READ_ERROR", set(pd.read_csv(out / "flags.csv")["code"]))


if __name__ == "__main__":
    unittest.main()
