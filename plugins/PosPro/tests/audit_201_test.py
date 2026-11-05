"""PosPro 2.0.1 audit fixes, end to end on the simulated mapper job (guides/pospro/simjob.py):

- the handover trajectory is at the IMU centre even with an output point set (was at the output point,
  labelled IMU centre, so AlignPro/CloudPro added the lidar offset twice)
- the frame comes from the run (PPP: ITRF2020; PPK: the base's frame) and is not stored back
- older GNSS-only solutions are not used to seed a tight-coupled run when the base changed
- a cancelled/failed read of the rover files is not reused; time crop applies in real-time mode
- review edits made while another job runs are applied when it finishes
- exclusion windows fit the engine's limit; leap seconds around a leap; an inverted crop

    QT_QPA_PLATFORM=offscreen python3 -m PosPro.tests.audit_201_test
"""
import datetime
import json
import os
import struct
import sys
import tempfile

PLUGINS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(PLUGINS, "guides"))
sys.path.insert(0, os.path.join(PLUGINS, "guides", "pospro"))
sys.path.insert(0, PLUGINS)


def unit_checks():
    from PosPro.core import geo, review
    # leap seconds: the UTC instant just before and after 2017-01-01 round-trips
    for d in (datetime.datetime(2016, 12, 31, 23, 59, 59), datetime.datetime(2017, 1, 1, 0, 0, 0),
              datetime.datetime(2017, 1, 1, 0, 0, 5)):
        assert geo.gps_to_utc(geo.utc_to_gps(d)) == d, d
    # exclusions: 300 windows become 256, covering everything they covered
    wins = [(i * 10.0, i * 10.0 + 2.0 + (i % 3)) for i in range(300)]
    fit = review.fit_windows(wins)
    assert len(fit) == 256
    for a, b in wins:
        assert any(x <= a and b <= y for x, y in fit), (a, b)
    assert review.fit_windows(wins[:5]) == [tuple(w) for w in wins[:5]]
    print("leap seconds, exclusion windows: OK")


def main():
    import harness
    import simjob
    app, iface = harness.start(PLUGINS, tempfile.mkdtemp())
    from dcs_core.project import DcsProject
    from PosPro.core import pipeline, review
    from PosPro.core.project import Project
    from PosPro.ui.dock import find_captures
    unit_checks()

    J = tempfile.mkdtemp()
    made = simjob.make(J)
    job = made["project"]
    pp = harness.load("PosPro", iface)
    pp.toggle(True)
    win = pp.dock
    dcs = DcsProject.create(job, "Shirley_Ave", plugin="PosPro")
    p = Project.from_dcs(dcs)
    p.cfg["rover"]["files"] = find_captures(job)["nav"]
    p.cfg["processing"]["mode"] = "rt"
    p.cfg["installation"]["lever_output"] = [0.5, 0.3, -0.4]     # an output point (e.g. the lidar)
    p.save()
    win.set_project(p)

    # rover read: the ok marker is written last; a missing marker with a .reading flag means "read again"
    win.run_extract(force=True)
    harness.settle(15)
    ex = os.path.join(p.work_dir, "extract")
    assert os.path.exists(os.path.join(ex, "ok.json")) and not os.path.exists(os.path.join(ex, ".reading"))
    sig = p.cfg["rover"]["extract_files"]
    assert pipeline._extract_ok(os.path.join(ex, "ok.json"), sig)
    os.remove(os.path.join(ex, "ok.json"))
    open(os.path.join(ex, ".reading"), "w").close()
    assert not pipeline._extract_ok(os.path.join(ex, "ok.json"), sig), "an unfinished read was trusted"
    win.run_extract(force=True)
    harness.settle(15)
    print("rover read cache marker: OK")

    # time crop in real-time mode
    span = p.capture_span()
    p.cfg["rover"]["time_start"] = span[0] + 60
    p.cfg["rover"]["time_end"] = span[0] + 300
    p.save()
    win.run_processing()
    harness.settle(20)
    assert win.qc, "processing failed"
    t = win.qc["gps_time"]
    assert t[0] >= span[0] + 59.9 and t[-1] <= span[0] + 300.1, (t[0] - span[0], t[-1] - span[0])
    print("real-time trajectory inside the crop: %.0f-%.0f s" % (t[0] - span[0], t[-1] - span[0]))

    # handover at the IMU centre: SBET position equals the trajectory (no output lever applied)
    doc = json.load(open(os.path.join(p.out_dir, "trajectory.json")))
    assert doc["reference_point"]["offset_from_imu_m"] == [0.0, 0.0, 0.0]
    import math
    with open(os.path.join(p.out_dir, "trajectory_sbet.out"), "rb") as fh:
        rec = struct.unpack("<17d", fh.read(136))
    i = min(range(len(t)), key=lambda k: abs((t[k] % 604800) - rec[0]))
    dn = (math.degrees(rec[1]) - win.qc["lat_deg"][i]) * 111111
    assert abs(rec[0] - t[i] % 604800) < 1e-3 and abs(dn) < 0.01, (rec[0], t[i] % 604800, dn)
    assert doc["frame"]["name"].startswith("Certus real-time")
    print("handover SBET at the IMU centre with an output point set: OK")

    # frame from the run, not stored: PPP says ITRF2020, PPK the base's frame, nothing written back
    p.cfg["processing"]["mode"] = "lc"
    p.cfg["processing"]["positioning"] = "ppp"
    assert p.run_frame()["name"] == "ITRF2020"
    p.cfg["processing"]["positioning"] = "ppk"
    p.cfg["bases"] = [{"id": "B1", "name": "RCH1", "frame": "NAD83(CSRS)", "epoch": "2010.0",
                       "xyz": [1760000.0, -4078000.0, 4561800.0], "pos_source": "csrs", "files": []}]
    p.cfg["base_selected"] = "B1"
    assert p.run_frame() == {"name": "NAD83(CSRS)", "epoch": "2010.0"}
    p.cfg["bases"][0].update(frame="ITRF2020", epoch="2026.84")
    assert p.run_frame()["name"] == "ITRF2020"
    assert p.cfg["frame"]["name"] == "NAD83(CSRS)", "the run frame was stored in the project"
    print("frame from the run: OK")

    # GNSS-only seeds: used only while the inputs match
    gdir = p.wpath("gnss", "x")[:-2]
    for f in ("fwd.pos", "bwd.pos"):
        open(os.path.join(gdir, f), "w").close()
    with open(os.path.join(gdir, "inputs.json"), "w") as fh:
        json.dump({"sig": pipeline._gnss_sig(p)}, fh)
    p.cfg["processing"]["mode"] = "tc"
    conf = open(pipeline.ins_conf(p, 1, [])).read()
    assert "file.gnss_fwd" in conf, "current GNSS-only solutions were not used"
    p.cfg["bases"][0]["xyz"] = [1760001.0, -4078000.0, 4561800.0]          # the base position changed
    conf = open(pipeline.ins_conf(p, 1, [])).read()
    assert "file.gnss_fwd" not in conf, "stale GNSS-only solutions would seed tight coupling"
    assert "lever.output = 0, 0, 0" in conf
    print("stale GNSS-only seeds left out; trajectory made at the IMU: OK")

    # review edits while a job runs are applied when it finishes
    p.cfg["processing"].update(mode="rt", positioning="ppk")
    p.cfg["bases"] = []
    p.save()
    busy = object()
    win.task = busy                            # a job is running (the test harness runs tasks at once) ...
    t0 = win.qc["gps_time"][100]
    review.add_edit(p, t0, t0 + 5, "poor")     # ... when the user marks a stretch poor
    p.save()
    win.page["review"].after_edit(True)
    assert win._edits_pending, "the edit was not queued"
    win.task = None
    win.run_extract()                          # the running job finishes: the queued edit is applied
    harness.settle(25)
    assert not win._edits_pending and win.task is None
    qc = win.qc
    k = min(range(len(qc["gps_time"])), key=lambda j: abs(qc["gps_time"][j] - t0 - 2))
    assert qc["sd_n"][k] > 0.05, "the edit made during the job was not applied (sd %.3f)" % qc["sd_n"][k]
    print("edit made during another job applied afterwards: OK")

    # unload while a job runs: the job is cancelled, no callback runs, the stage lock is released
    class Job:
        cancelled = False

        class done:
            @staticmethod
            def disconnect():
                pass

        def cancel(self):
            self.cancelled = True

    class Lock:
        released = None

        def __exit__(self, *a):
            Lock.released = a[1]
    job_ = Job()
    win.task, win._run_lock = job_, Lock()
    pp.unload()
    assert job_.cancelled and Lock.released is not None and win._closing
    print("unload during a job: cancelled, lock released: OK")
    print("OK")


if __name__ == "__main__":
    main()
