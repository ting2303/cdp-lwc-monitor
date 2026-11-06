"""Per-session size histograms for every CDP / FM file outside the big field campaigns."""
import io
import os
import pickle
import re
import sys
import zipfile

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
pd.set_option("display.width", 240, "display.max_rows", 200, "display.max_columns", 30, "display.max_colwidth", 48, "display.float_format", lambda x: f"{x:.3g}")
D = os.path.dirname(os.path.abspath(__file__))
idx = pickle.load(open(os.path.join(D, "idx.pkl"), "rb"))
zc = {}


def op(p):
    z = idx[p][0]
    if z not in zc:
        zc[z] = zipfile.ZipFile(z)
    return zc[z].open(p)


print("--- files or folders that mention beads or sizes")
pat = re.compile(r"玻璃|bead|glass|\d+\s*um|\d+\s*µm|pas\s*\d|speed\s*\d|PSL", re.I)
names = sorted({p for p in idx if p.startswith("Instruments") and pat.search(p)})
for p in names[:60]:
    print(" ", p)
print("--- screenshot folders")
for p in sorted(p for p in idx if "螢幕擷取" in p or "collection_figure" in p):
    print(" ", p, idx[p][1])

print("--- notes typed into logs")
seen = 0
for p in sorted(idx):
    if p.startswith(("Instruments", "DMT/PADS Data")) and re.search(r"/Log\d+\.txt$", p):
        text = op(p).read().decode("cp950", "replace")
        for line in text.splitlines():
            m = re.match(r"\d\d:\d\d:\d\d\s+\d+:\s+(.*)", line)
            body = m.group(1) if m else line
            if body.strip() and not re.search(r"Instrument|Saving|Acquisition|Recording|PADS program|Loading|Reading|Changes to|Display|Enabling|Disabling", body):
                print(" ", p.split("/")[-1], "|", body[:120])
                seen += 1
print("log notes:", seen)

print("--- config descriptions")
for p in sorted(idx):
    if re.search(r"/Original\d+\.ini$", p):
        text = op(p).read().decode("cp950", "replace")
        m = re.search(r"^Config Description=(.+)$", text, re.M)
        if m and m.group(1).strip():
            print(" ", p.split("/")[-1], m.group(1)[:120])

rows = []
hists = {}
pat_file = re.compile(r"^\d\d(CDP PBP|FM 100)(\d{14})\.csv$")
for p in sorted(idx):
    if not p.startswith(("Instruments", "DMT/PADS Data")):
        continue
    folder, _, name = p.rpartition("/")
    m = pat_file.match(name)
    if not m:
        continue
    if "/KAPAS/" in p or "2025 Cloud IOP_Lulin" in p:
        continue
    stream = io.TextIOWrapper(op(p), encoding="cp950", errors="replace", newline="")
    header = []
    while True:
        line = stream.readline()
        if not line or line.startswith("****"):
            break
        header.append(line.rstrip())
    text = "\n".join(header)
    sizes = [float(x) for x in re.search(r"^Sizes=<\d+>(.*)$", text, re.M).group(1).split(",")]
    area = re.search(r"^Sample Area \(mm\^2\)=(.*)$", text, re.M)
    thresh = re.search(r"^Thresholds=(.*)$", text, re.M)
    df = pd.read_csv(stream, on_bad_lines="skip", low_memory=False, skipinitialspace=True)
    df.columns = [c.strip() for c in df.columns]
    bins = [c for c in df.columns if re.fullmatch(r"(CDP|Fog Monitor) Bin \d+", c)]
    counts = df[bins].apply(pd.to_numeric, errors="coerce").fillna(0).to_numpy()
    kind = "cdp" if "CDP" in m.group(1) else "fm"
    sid = m.group(2)
    per_sec = counts.sum(axis=1)
    rows.append(dict(sid=sid, kind=kind, path=folder, secs=len(df), active=int((per_sec > 0).sum()), total=int(per_sec.sum()), max_per_sec=int(per_sec.max()) if len(per_sec) else 0, area=area.group(1) if area else "", pas=pd.to_numeric(df.get("Applied PAS (m/s)"), errors="coerce").median(), sizes=len(sizes)))
    hists[(sid, kind, folder)] = dict(sizes=sizes, sum=counts.sum(axis=0), per_sec=per_sec, counts=counts, thresholds=thresh.group(1) if thresh else "", time=pd.to_numeric(df["End Seconds"], errors="coerce").to_numpy())
pickle.dump(hists, open(os.path.join(D, "cal_hists.pkl"), "wb"))
t = pd.DataFrame(rows)
t["camp"] = t.path.str.split("/").str[1:4].str.join("/")
print("--- sessions", len(t), t.kind.value_counts().to_dict())
print(t[["sid", "kind", "camp", "secs", "active", "total", "max_per_sec", "pas", "area"]].to_string())
