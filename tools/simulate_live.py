"""用真實資料模擬儀器持續寫檔,測試面板用。不會更動原始資料。

python tools/simulate_live.py --cdp 原始CDP.csv --lwc 原始LWC.csv --d11 11D資料夾/xxx --out 測試資料夾 --seconds 600
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime
from pathlib import Path
from typing import List, Tuple


def split_pads(path: Path) -> Tuple[bytes, List[str]]:
    data = path.read_bytes()
    marker = data.index(b"****")
    end_header = data.index(b"\n", data.index(b"\n", marker) + 1) + 1
    return data[:end_header], data[end_header:].decode("ascii", errors="ignore").splitlines()


def rewrite(line: str, now: datetime) -> str:
    """把時間改成現在,其餘欄位不變。"""
    fields = line.split(",")
    seconds = now.hour * 3600 + now.minute * 60 + now.second + now.microsecond / 1e6
    fields[0] = f"{seconds:.6f}"
    fields[1] = str(now.timetuple().tm_yday)
    fields[2] = str(now.year)
    return ",".join(fields)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cdp", type=Path, required=True)
    parser.add_argument("--lwc", type=Path, required=True)
    parser.add_argument("--d11", type=Path, required=True, help="11-D 檔名前綴,例如 xxx/05070859")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=600)
    parser.add_argument("--speed", type=float, default=1.0, help="每秒寫幾筆")
    parser.add_argument("--skip", type=int, default=0, help="跳過原始資料前幾列")
    parser.add_argument("--dump", type=float, default=None, help="把 Dump Spot 改成這個值,模擬鏡窗起霧")
    args = parser.parse_args()
    now = datetime.now()
    sid = now.strftime("%Y%m%d%H%M%S")
    folder = args.out / "pads" / now.strftime("%Y%m%d") / sid
    folder.mkdir(parents=True)
    (args.out / "d11").mkdir(exist_ok=True)
    head_c, rows_c = split_pads(args.cdp)
    head_l, rows_l = split_pads(args.lwc)
    cdp = open(folder / f"01CDP PBP{sid}.csv", "wb", buffering=0)
    lwc = open(folder / f"00LWC 300_{sid}.csv", "wb", buffering=0)
    cdp.write(head_c)
    lwc.write(head_l)
    d11_files = {}
    d11_rows = {}
    for kind in ("C", "M", "L"):
        data = args.d11.with_name(f"{args.d11.name}-{kind}.dat").read_bytes()
        split = data.index(b"<Data>") + len(b"<Data>")
        split = data.index(b"\n", data.index(b"\n", split) + 1) + 1
        handle = open(args.out / "d11" / f"{sid}-{kind}.dat", "wb", buffering=0)
        handle.write(data[:split])
        d11_files[kind] = handle
        d11_rows[kind] = data[split:].split(b"\n")
    columns = head_c.decode("ascii", errors="ignore").splitlines()[-1].split(",")
    dump_index = [c.strip() for c in columns].index("Dump Spot Monitor (V)")
    print("開始寫入", folder, flush=True)
    for i in range(args.seconds):
        now = datetime.now()
        fields_c = rewrite(rows_c[args.skip + i], now).split(",")
        if args.dump is not None:
            fields_c[dump_index] = str(args.dump)
        line_c = ",".join(fields_c) + "\r\n"
        # 故意分兩次寫,測試寫到一半的行不會被讀壞
        middle = len(line_c) // 2
        cdp.write(line_c[:middle].encode())
        lwc.write((rewrite(rows_l[args.skip + i], now) + "\r\n").encode())
        time.sleep(0.05 / args.speed)
        cdp.write(line_c[middle:].encode())
        if i % 6 == 0:
            stamp = f"{now.year}/{now.month}/{now.day} {'上午' if now.hour < 12 else '下午'} {(now.hour % 12) or 12:02d}:{now.minute:02d}:{now.second:02d}"
            for kind, handle in d11_files.items():
                raw = d11_rows[kind][(args.skip // 6 + i // 6) % max(1, len(d11_rows[kind]) - 1)]
                body = raw.partition(b"\t")[2]
                handle.write(stamp.encode("cp950") + b"\t" + body + b"\n")
        time.sleep(max(0.0, 0.95 / args.speed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
