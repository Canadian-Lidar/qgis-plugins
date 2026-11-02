"""Build a DCS job for tests: copy the simulated captures into a job folder, create the shared project,
record captures and hardware as PosPro does and publish a PosPro-style trajectory handover."""
import datetime
import os
import shutil

import numpy as np

GPS_EPOCH_UNIX = 315964800.0


def publish_pospro(dcs, sbet_src, t0_unix, tag="first"):
    from dcs_core import handover
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
    week = int((t0_unix + 18 - GPS_EPOCH_UNIX) // 604800)
    fields = {"time": {"system": "GPS", "week": week, "leap_seconds": 18, "sbet_time": "GPS seconds of week"},
              "frame": {"name": "NAD83(CSRS)", "epoch": "1997.0", "heights": "ellipsoidal"},
              "reference_point": {"name": "Certus reference point (IMU centre)", "offset_from_imu_m": [0, 0, 0]},
              "rate_hz": 200, "quality": {"mode": "tc", "fixed_pct": 100.0}}
    return handover.publish(dcs, "pospro", {"sbet": sbet, "smrmsg": smr, "qc": qc}, fields, "PosPro", "1.2.1", "sim")


def make_job(sim, work, name="PatchTest_Oct20"):
    from dcs_core.project import DcsProject
    from alignpro.core import clraw
    shutil.rmtree(work, ignore_errors=True)
    job = os.path.join(work, "Job_Oct20")
    os.makedirs(job)
    for d in os.listdir(sim):
        if os.path.isdir(os.path.join(sim, d)):
            shutil.copytree(os.path.join(sim, d), os.path.join(job, d))
    files = clraw.find_clraw([job])
    cf = clraw.ClrawFile(files[0])
    t0 = datetime.datetime.fromisoformat(cf.session["start_utc"]).timestamp()
    dcs = DcsProject.create(job, name, plugin="PosPro")
    dcs.set_capture_files(files, "PosPro")
    dcs.set_shared("hardware", cf.hardware, "PosPro")
    sbet = [os.path.join(sim, f) for f in os.listdir(sim) if f.startswith("sbet_")][0]
    publish_pospro(dcs, sbet, t0)
    return dcs, sbet, t0
