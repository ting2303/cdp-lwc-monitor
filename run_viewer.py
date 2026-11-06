"""從任何資料夾都能啟動檢視器。"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.chdir(HERE)

from cpd_lwc_qc.viewer import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
