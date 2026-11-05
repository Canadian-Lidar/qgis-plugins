#!/usr/bin/env python3
"""Older MapPro captures: one Start/Stop = one project folder with its own SBET (the Certus was logged
only during each capture). AlignPro takes one SBET per capture and uses each for its own capture.

  python3 tests/multi_sbet_test.py SIM_FOLDER WORK_FOLDER
SIM_FOLDER: output of tools/simulate_patch.py (two or more capture folders + sbet_YYYYMMDD.out + truth.json).
Each capture folder is put in its own project folder (A/, B/ ...), and the simulated SBET is cut into one
file per capture, with nothing in between. Runs without QGIS; dcs_core beside the alignpro folder."""
import json
import os
import shutil
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from dcs_core.project import DcsProject  # noqa: E402
from alignpro.core import clraw, trajectory  # noqa: E402
from alignpro.core.project import Project  # noqa: E402

SIM, WORK = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
fails = []


def check(ok, msg):
    print("  %s  %s" % ("ok  " if ok else "FAIL", msg))
    if not ok:
        fails.append(msg)


truth = json.load(open(os.path.join(SIM, "truth.json")))
caps = sorted(d for d in os.listdir(SIM) if os.path.isdir(os.path.join(SIM, d)))
assert len(caps) >= 2, "need two captures"
caps = caps[:2]
shutil.rmtree(WORK, ignore_errors=True)
folders = []
for k, c in zip("AB", caps):                       # MapPro: each Start/Stop made its own project folder
    f = os.path.join(WORK, "MapPro_%s" % k)
    shutil.copytree(os.path.join(SIM, c), os.path.join(f, c))
    folders.append(f)

print("1. one SBET per capture, cut from the simulated trajectory, nothing between them")
sbet_src = [os.path.join(SIM, f) for f in os.listdir(SIM) if f.startswith("sbet_")][0]
spans = []
for f in folders:
    files = clraw.find_clraw([f])
    cf = clraw.ClrawFile(files[0])
    ev = {e.get("event"): e.get("t") for e in cf.events()}
    spans.append((float(ev["clraw_start"]), float(ev["clraw_stop"])))
full = trajectory.read_sbet(sbet_src, near_unix=spans[0][0])
sbets = []
for (t0, t1), f in zip(spans, folders):
    part = full.window(t0, t1, pad=4.0)            # the Certus logged from just before Start to just after Stop
    p = os.path.join(f, "sbet_%s.out" % os.path.basename(f).lower())
    trajectory.write_sbet(p, part)
    sbets.append(p)
gap = (spans[0][1] + 4.0, spans[1][0] - 4.0)
check(gap[1] > gap[0], "gap between the two SBETs: %.1f s" % (gap[1] - gap[0]))

print("2. one AlignPro project for both captures (folder A + Add folder B), both SBETs")
dcs = DcsProject.create(folders[0], "MapPro_patch", plugin="AlignPro")
p = Project.open_any(dcs.path)
p.cfg["files"] = [folders[1]]
p.cfg["trajectory"] = {"type": "sbet", "paths": list(sbets), "path": sbets[0], "date": "", "leap": 18}
p.save()
p.load()
check(len(p.datas) == 2, "two captures read (%s)" % ", ".join(d["info"]["capture"] for d in p.datas))
m = p.traj_info.get("match") or []
check(len(m) == 2 and all(len(x["lines"]) == 1 for x in m) and m[0]["lines"] != m[1]["lines"],
      "each SBET covers its own capture: %s" % "; ".join("%s → %s" % (os.path.basename(x["path"]), x["lines"])
                                                          for x in m))
check(p.traj.segments is not None and len(p.traj.segments) == 2, "merged trajectory in 2 pieces")
check(not p.traj.inside([sum(gap) / 2])[0] and p.traj.inside([sum(spans[0]) / 2])[0],
      "no trajectory in the gap (never interpolated across it)")
check(not [w for w in p.warnings if "SBET" in w or "covered" in w], "no coverage warning (%s)" % p.warnings)
check(all(ln.n > 0 for ln in p.lines), "every pass georeferenced (%s points)" % [ln.n for ln in p.lines])
r = p.calibrate()
a = r["advised"]
err = max(abs(a[k] - v) for k, v in zip(("roll", "pitch", "heading"), truth["mount_true"]))
check(err < 0.01, "advised %.4f %.4f %.4f vs true %.4f %.4f %.4f (max diff %.4f°)" % (
    a["roll"], a["pitch"], a["heading"], *truth["mount_true"], err))
adv_two = np.array([a["roll"], a["pitch"], a["heading"]])

print("3. the same with the SBETs listed the other way round")
p.cfg["trajectory"]["paths"] = list(reversed(sbets))
p.save()
p.load()
r = p.calibrate()
a = r["advised"]
d = np.abs(np.array([a["roll"], a["pitch"], a["heading"]]) - adv_two).max()
check(d < 1e-6, "order does not matter (max diff %.2e°)" % d)

print("4. only capture A's SBET: capture B is reported, not georeferenced with a wrong trajectory")
p.cfg["trajectory"]["paths"] = [sbets[0]]
p.save()
p.load()
w = [x for x in p.warnings if "not covered" in x]
check(len(w) == 1, "warning: %s" % (w[0] if w else p.warnings))
cov = [ln.n for ln in p.lines]
check(min(cov) == 0 and max(cov) > 0, "capture B has no points without its SBET (%s)" % cov)

print("5. one SBET for the whole drive (single file, older setting \"path\") still works")
p.cfg["trajectory"] = {"type": "sbet", "path": sbet_src, "date": "", "leap": 18}
p.save()
p.load()
check(p.traj.segments is None and not [x for x in p.warnings if "covered" in x], "single SBET: one piece, no warning")
r = p.calibrate()
a = r["advised"]
d = np.abs(np.array([a["roll"], a["pitch"], a["heading"]]) - adv_two).max()
check(d < 0.005, "same calibration as with the two per-capture SBETs (max diff %.4f°)" % d)

print("\nMULTI SBET TEST %s" % ("OK" if not fails else "FAILED: %d" % len(fails)))
sys.exit(1 if fails else 0)
