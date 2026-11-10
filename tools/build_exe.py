"""打包即時面板。需要 numpy 與 pyinstaller;建議在乾淨的虛擬環境執行。

python tools/build_exe.py [暫存資料夾] [輸出資料夾]
打包前請先關閉正在執行的面板,否則預設的 dist 資料夾會被鎖住。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parents[1]
NAME = "CDP_LWC_11D_Panel"


def main() -> int:
    work = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp())
    dist = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "dist"
    PyInstaller.__main__.run([
        "--noconfirm", "--clean", "--onedir", "--console",
        "--name", NAME,
        "--distpath", str(dist),
        "--workpath", str(work / "build"),
        "--specpath", str(work / "build"),
        "--add-data", f"{ROOT / 'cpd_lwc_qc' / 'viewer.html'};cpd_lwc_qc",
        "--add-data", f"{ROOT / 'cpd_lwc_qc' / 'normal_reference.json'};cpd_lwc_qc",
        "--exclude-module", "pandas",
        "--exclude-module", "matplotlib",
        "--exclude-module", "scipy",
        "--exclude-module", "tkinter",
        "--paths", str(ROOT),
        str(ROOT / "panel.py"),
    ])
    for name in ("使用說明.txt", "設定開機自動啟動.bat", "取消開機自動啟動.bat", "模擬展示.bat", "雲端檢查.bat"):
        shutil.copy(ROOT / "tools" / name, dist / NAME / name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
