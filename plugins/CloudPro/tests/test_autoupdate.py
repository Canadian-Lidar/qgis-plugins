"""Background georeferencing (CloudPro 1.9.6, DCS Core API 11): when PosPro / AlignPro publish and the job's upstream
is current, CloudPro georeferences again with the last settings and carries the edits over - in the background when
its window is closed (the window waits and then opens the job), in the window when it shows the job.

    QT_QPA_PLATFORM=offscreen python3 CloudPro/tests/test_autoupdate.py      (from the plugins folder)
"""
import os
import sys
import tempfile
import time

import numpy as np

PLUGINS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(PLUGINS, "guides"))
sys.path.insert(0, PLUGINS)
import harness                                                    # noqa: E402

app, iface = harness.start(PLUGINS, tempfile.mkdtemp())
from CloudPro.tests import _job                                    # noqa: E402
from CloudPro.core import autoupdate, chain, importer as IM       # noqa: E402
from CloudPro.core.project import Project                         # noqa: E402
from CloudPro.core.store import REJECTED                          # noqa: E402

dcs, p = _job.fresh_job("auto")
files = dcs.survey_clraw_files()
track = _job.realtime_track(files)
_job.publish_sbet(dcs, track)
p.data["override"] = {"source": "recorded"}                       # this job does not use AlignPro
IM.import_chain(p, chain.resolve(dcs, override=p.data["override"]), IM.ImportOptions(min_speed=0.3))
p.save()
ok, why = autoupdate.decide(dcs.path, p)
assert not ok and "current" in why, why
z0 = {k: p.store.tile_array(k)["z"].astype(np.int64).copy() for k in p.store.keys()}
k0 = max(z0, key=lambda k: len(z0[k]))
a = p.store.tile_array(k0, writable=True)
a["flag"][::7] |= REJECTED                                         # a manual edit to carry over
a.flush()
n_rej = int(((a["flag"] & REJECTED) != 0).sum())
p.store.close()
print("georeferenced once; points current: %s" % why)


def higher(dz):
    """PosPro publishes a trajectory dz m higher (as after reprocessing)."""
    t2 = type(track)(track.t, track.lat, track.lon, track.h + dz, track.roll, track.pitch, track.heading)
    _job.publish_sbet(dcs, t2)


# --- the plugin, window closed: background thread, the window waits for it --------------------------------------
import CloudPro                                                    # noqa: E402
from dcs_core import registry                                     # noqa: E402
plugin = CloudPro.classFactory(iface)
plugin.initGui()
assert "cloudpro" in registry._LISTENERS
higher(0.05)
harness.settle(2)
assert plugin._bg is not None, "no background run started"
win = plugin.show()
win.open_project(dcs.path)                                          # busy: deferred
assert win.project is None or os.path.abspath(win.project.dcs.path) != os.path.abspath(dcs.path)
t0 = time.time()
while plugin._bg is not None and time.time() - t0 < 300:
    harness.settle(1)
path, res = plugin.last_background
assert not isinstance(res, Exception), res
out, rest = res
assert rest >= n_rej, (rest, n_rej)
harness.settle(10)
assert win.project is not None and os.path.abspath(win.project.dcs.path) == os.path.abspath(dcs.path), \
    "the window did not open the job after the background run"
st = win.project.store
z1 = st.tile_array(k0)["z"].astype(np.int64)
assert len(z1) == len(z0[k0]) and abs(np.median(z1 - z0[k0]) - 50) <= 1, np.median(z1 - z0[k0])
assert int(((st.tile_array(k0)["flag"] & REJECTED) != 0).sum()) == n_rej
assert any("automatically" in h["text"] for h in win.project.data.get("history", []))
ok, why = autoupdate.decide(dcs.path, win.project)
assert not ok, why
print("background run: %d lines, points 50 mm higher, %d edits carried over, the window opened the job after" % (
    len(out), rest))

# --- window shows the job: the window georeferences again itself ------------------------------------------------
higher(0.10)
harness.settle(2)
assert plugin._bg is None and win._busy, "the open window should do it"
t0 = time.time()
while win._busy and time.time() - t0 < 300:
    harness.settle(1)
harness.settle(5)
z2 = win.project.store.tile_array(k0)["z"].astype(np.int64)
assert abs(np.median(z2 - z0[k0]) - 100) <= 1, np.median(z2 - z0[k0])
assert int(((win.project.store.tile_array(k0)["flag"] & REJECTED) != 0).sum()) == n_rej
print("window open: georeferenced again in the window (100 mm), edits kept, no message box")

# --- AlignPro stale: no run (its calibration will change) -------------------------------------------------------
win.project.data["override"] = {}
win.project.save()
from dcs_core import handover                                     # noqa: E402
rep = os.path.join(handover.out_dir(dcs, "alignpro"), "alignment_report.txt")
open(rep, "w").write("test\n")
ln0 = win.project.lines[0]
handover.publish(dcs, "alignpro", {"report": rep}, {
    "convention": {"name": "navaq_hesai"},
    "mount_deg": {"roll": ln0["mount"][0] + 0.05, "pitch": ln0["mount"][1], "heading": ln0["mount"][2],
                  "source": "calibration"},
    "lidar_lever_m": {"value": ln0["lever"], "source": "capture"}, "latency_s": 0.0,
    "trajectory": {"handover_id": dcs.stage("pospro").get("handover_id")}}, "AlignPro", "test")
harness.settle(2)
assert win._busy, "AlignPro's publish (upstream current) should start an update"
t0 = time.time()
while win._busy and time.time() - t0 < 300:
    harness.settle(1)
harness.settle(3)
higher(0.15)                                                        # AlignPro is now stale
harness.settle(3)
assert not win._busy and plugin._bg is None
ok, why = autoupdate.decide(dcs.path, Project.open_any(dcs.path))
assert not ok and "AlignPro" in why, why
print("AlignPro published: updated; then PosPro published while AlignPro is stale: no run (%s)" % why)

# --- unload removes the listener ---------------------------------------------------------------------------------
win.shutdown()
plugin.unload()
assert "cloudpro" not in registry._LISTENERS
print("test_autoupdate: OK")
