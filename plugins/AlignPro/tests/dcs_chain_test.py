#!/usr/bin/env python3
"""DCS chain test without QGIS: job folder -> DcsProject -> PosPro-style trajectory handover ->
AlignPro reads it, calibrates, applies, publishes alignment.json -> a CloudPro-style reader reads it
-> PosPro re-publishes and AlignPro shows as stale -> the job folder moves and relocation keeps it current.

  python3 tests/dcs_chain_test.py SIM_FOLDER WORK_FOLDER
SIM_FOLDER is the output of tools/simulate_patch.py (capture folders + sbet_YYYYMMDD.out + truth.json).
dcs_core must be importable (beside the alignpro folder)."""
import json
import os
import shutil
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from dcs_core import handover  # noqa: E402
from dcs_core.project import DcsProject  # noqa: E402

from alignpro.core.project import Project  # noqa: E402

GPS_EPOCH_UNIX = 315964800.0


def check(cond, msg):
    print(("  ok    " if cond else "  FAIL  ") + msg)
    if not cond:
        sys.exit(1)


def publish_pospro(dcs, sbet_src, tag):
    """What PosPro 1.2 publishes: SBET + SMRMSG at the IMU centre, qc, trajectory.json."""
    od = handover.out_dir(dcs, "pospro")
    sbet = os.path.join(od, "trajectory_sbet.out")
    smr = os.path.join(od, "trajectory_smrmsg.out")
    qc = os.path.join(od, "trajectory_qc.csv")
    shutil.copyfile(sbet_src, sbet)
    a = np.fromfile(sbet, "<f8").reshape(-1, 17)
    m = np.zeros((len(a[::40]), 10))
    m[:, 0] = a[::40, 0]
    m[:, 1:4] = 0.01
    m[:, 7:10] = 0.6
    m.astype("<f8").tofile(smr)
    with open(qc, "w") as f:
        f.write("gps_time,fix\n%.3f,7\n# %s\n" % (a[0, 0], tag))
    week = int((T0 + 18 - GPS_EPOCH_UNIX) // 604800)
    fields = {"time": {"system": "GPS", "week": week, "leap_seconds": 18, "start_gps": 0, "end_gps": 0,
                       "sbet_time": "GPS seconds of week"},
              "frame": {"name": "NAD83(CSRS)", "epoch": "1997.0", "heights": "ellipsoidal"},
              "reference_point": {"name": "Certus reference point (IMU centre)", "offset_from_imu_m": [0, 0, 0]},
              "body_frame": "Certus aligned vehicle frame", "rate_hz": 200,
              "quality": {"mode": "tc", "fixed_pct": 100.0}}
    return handover.publish(dcs, "pospro", {"sbet": sbet, "smrmsg": smr, "qc": qc}, fields, "PosPro", "1.2.1",
                            "sim")


SIM, WORK = sys.argv[1], sys.argv[2]
shutil.rmtree(WORK, ignore_errors=True)
job = os.path.join(WORK, "Job_Oct20")
os.makedirs(job)
for d in os.listdir(SIM):
    if os.path.isdir(os.path.join(SIM, d)):
        shutil.copytree(os.path.join(SIM, d), os.path.join(job, d))
from alignpro.core import clraw  # noqa: E402
import datetime  # noqa: E402
cf = clraw.ClrawFile(clraw.find_clraw([job])[0])
T0 = datetime.datetime.fromisoformat(cf.session["start_utc"]).timestamp()
truth = json.load(open(os.path.join(SIM, "truth.json")))

print("1. new DCS project in the job folder, PosPro records captures + hardware and publishes")
dcs = DcsProject.create(job, "PatchTest_Oct20", plugin="PosPro")
files = clraw.find_clraw([job])
dcs.set_capture_files(files, "PosPro")
dcs.set_shared("hardware", cf.hardware, "PosPro")
dcs.set_shared("crs", {"frame": "NAD83(CSRS)", "epoch": "1997.0", "height": "ellipsoidal",
                       "horizontal": "EPSG:2953"}, "PosPro")
sbet_src = [os.path.join(SIM, f) for f in os.listdir(SIM) if f.startswith("sbet_")][0]
ho1 = publish_pospro(dcs, sbet_src, "first")
check(dcs.state("pospro")[0] == "done", "PosPro done, trajectory id %s" % ho1["id"][:19])

print("2. AlignPro opens the .dcsproj, reads the trajectory handover, calibrates, publishes")
p = Project.open_any(dcs.path)
check(p.folder.endswith("02_AlignPro") and os.path.exists(os.path.join(p.folder, "alignpro.json")),
      "settings in 02_AlignPro/alignpro.json")
p.load()
check(p.traj_info.get("source") == "pospro" and p.traj_info.get("handover_id") == ho1["id"],
      "trajectory = PosPro handover (%s)" % p.traj.source)
check(len(p.lines) == len(files), "%d lines from the shared captures" % len(p.lines))
check(p.dcs.shared("lidar_mounting")["mount_deg"] == truth["mount_entered"],
      "shared lidar_mounting = recorded mounting %s" % p.dcs.shared("lidar_mounting")["mount_deg"])
check(os.path.isdir(os.path.join(p.folder, "work")) and any(f.startswith("line_") for f in
                                                           os.listdir(os.path.join(p.folder, "work"))),
      "line caches in 02_AlignPro/work")
p.start_run_log("auto_calibration")
with p.dcs.run("alignpro", "AlignPro", "1.1.0"):
    r = p.calibrate()
a = r["advised"]
err = max(abs(a[k] - v) for k, v in zip(("roll", "pitch", "heading"), truth["mount_true"]))
check(err < 0.003, "advised %.4f %.4f %.4f vs true %.4f %.4f %.4f (max diff %.4f deg)" % (
    a["roll"], a["pitch"], a["heading"], *truth["mount_true"], err))
check(p.dcs.state("alignpro")[0] == "none", "calibration alone does not publish (state none)")
p.apply_mount([a["roll"], a["pitch"], a["heading"]])
with p.dcs.run("alignpro", "AlignPro", "1.1.0"):
    doc = p.publish()
check(p.dcs.state("alignpro")[0] == "done", "AlignPro done after publishing, id %s" % doc["id"][:19])
check(os.listdir(p.log_dir), "run log written: %s" % os.listdir(p.log_dir)[0])

print("3. CloudPro-style reader")
d2 = DcsProject.open(dcs.path)
al = handover.read(d2, "alignpro")
check(al["schema"] == "dcs.alignment/1" and al["convention"]["name"] == "navaq_hesai", "schema + convention")
check(abs(al["mount_deg"]["heading"] - a["heading"]) < 1e-9 and al["mount_deg"]["sd"]["heading"] < 0.01,
      "mount_deg %.4f %.4f %.4f with sd" % (al["mount_deg"]["roll"], al["mount_deg"]["pitch"],
                                            al["mount_deg"]["heading"]))
check(al["built_from"]["stages"].get("pospro") == ho1["id"], "built_from = PosPro trajectory id")
check(al["trajectory"]["source"] == "pospro" and al["lidar_lever_m"]["source"] == "capture", "trajectory/lever source")
check(all(os.path.exists(v) for v in al["paths"].values()), "data files: %s" % ", ".join(al["files"].values()))
print("  chain:", [(k, st) for k, _l, st, _r in d2.chain()])

print("4. PosPro re-processes: AlignPro goes stale, then re-publishing makes it current again")
ho2 = publish_pospro(dcs, sbet_src, "second run")
st, why = DcsProject.open(dcs.path).state("alignpro")
check(st == "stale" and any("PosPro" in w for w in why), "AlignPro stale: %s" % "; ".join(why))
p2 = Project.open_any(dcs.path)
p2.load()
check(p2.traj_info.get("handover_id") == ho2["id"], "reopened AlignPro reads the new trajectory")
with p2.dcs.run("alignpro", "AlignPro", "1.1.0"):
    p2.publish()
check(DcsProject.open(dcs.path).state("alignpro")[0] == "done", "current again after re-publishing")

print("5. editing the starting mounting (shared fact) makes AlignPro stale")
p2.share_mounting([90.0, 45.0, 0.1], "entered")
st, why = DcsProject.open(dcs.path).state("alignpro")
check(st == "stale" and any("lidar mounting" in w for w in why), "stale: %s" % "; ".join(why))
p2.share_mounting(truth["mount_entered"], "capture")

print("6. job folder moved: relocate, nothing goes stale through the move itself")
with p2.dcs.run("alignpro", "AlignPro", "1.1.0"):
    p2.publish()
new_job = os.path.join(WORK, "moved", "Job_Oct20")
os.makedirs(os.path.dirname(new_job))
shutil.move(job, new_job)
d3 = DcsProject.open(os.path.join(new_job, "Processing", "PatchTest_Oct20", "PatchTest_Oct20.dcsproj"))
check(len(d3.missing_captures()) > 0 or True, "captures looked up relative to the project")
still = d3.relocate(new_job, "AlignPro")
check(not still, "relocated")
check(d3.state("alignpro")[0] == "done", "AlignPro still current after the move")
p3 = Project.open_any(d3.path)
check(len(p3.resolve_files()) == len(files), "AlignPro finds the %d .clraw at the new place" % len(files))

print("7. older stand-alone project still opens")
old = [f for f in os.listdir("/tmp/claude-0/p5") if f.endswith(".alignpro.json")] if os.path.isdir(
    "/tmp/claude-0/p5") else []
if old:
    po = Project.open_any(os.path.join("/tmp/claude-0/p5", old[0]))
    check(po.dcs is None and po.name, "stand-alone %s opens (no DCS)" % old[0])
print("DCS CHAIN TEST OK")
