"""Antennas and direction of travel in the Trajectory editor, and the antenna check window (harness).

    QT_QPA_PLATFORM=offscreen python3 PosPro/tests/antennas_ui_test.py [figure folder]
"""
import math
import os
import random
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGINS = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(PLUGINS, "guides"))
import harness  # noqa: E402

FIG = sys.argv[1] if len(sys.argv) > 1 else None
app, iface = harness.start(PLUGINS, tempfile.mkdtemp())
import numpy as np  # noqa: E402
from PosPro.core import antennas, review  # noqa: E402
from PosPro.core.project import Project  # noqa: E402
from PosPro.tests import simtraj  # noqa: E402
from PosPro.ui import antennacheck  # noqa: E402

L1, L2 = [0.082, 0.495, -0.112], [0.082, -0.493, -0.112]
plugin = harness.load("PosPro", iface)
plugin.toggle(True)
dock = plugin.dock
job = tempfile.mkdtemp()
p = Project(os.path.join(job, "Shirley.pospro.json"))
p.cfg["name"] = "Shirley"
p.cfg["processing"]["mode"] = "tc"
p.cfg["installation"]["lever_antenna"] = L1
p.cfg["installation"]["lever_antenna2"] = L2
os.makedirs(p.work_dir, exist_ok=True)
p.save()
t0, _ = simtraj.make(p.work_dir)
review.state(p)
review.detect(p)
dock.set_project(p)
dock.open_editor()
ed = plugin.editor
ed.resize(1500, 940)
ed.widget().resize(1500, 940)
harness.settle(10)
tr = ed.track

# antennas along the track: GPS1 0.495 m to starboard of the IMU, GPS2 0.493 m to port
ed.c_ant.setChecked(True)
g1n, g1e = ed.plan.antennas["gps1"]
k = len(tr.t) // 2
c = tr.course[k]
stb = -(g1n[k] - tr.fn[k]) * math.sin(c) + (g1e[k] - tr.fe[k]) * math.cos(c)
print("GPS1 starboard of the IMU along the track: %.3f m" % stb)
assert abs(stb - 0.495) < 0.01

# a swapped drive along the simulated road: receiver 0 sits on the port antenna, Certus heading turned 180
random.seed(2)
sols, rt = [], []
for i in range(0, len(tr.t), 25):
    ci = float(tr.course[i])
    b = [-(L2[j] - L1[j]) for j in range(3)]
    sols.append((float(tr.t[i]), b[0] * math.sin(ci) + b[1] * math.cos(ci) + random.gauss(0, .004),
                 b[0] * math.cos(ci) - b[1] * math.sin(ci) + random.gauss(0, .004), -b[2], 1))
    rt.append((float(tr.t[i]), 11 * math.cos(ci), 11 * math.sin(ci), ci + math.pi, 0.001))
res = antennas.analyse(sols, rt, L1, L2)
print(res["verdict"], res["measured"])
assert res["verdict"] == "swapped"
dock.antenna_check = res
ed.set_antenna_check(res)
mn, me = ed.plan.measured
k2 = int(np.searchsorted(tr.t, res["points"][len(res["points"]) // 2][0]))
ck = tr.course[min(k2, len(tr.t) - 1)]
side = -(mn[len(mn) // 2] - tr.fn[k2]) * math.sin(ck) + (me[len(me) // 2] - tr.fe[k2]) * math.cos(ck)
print("measured GPS2 starboard of the IMU: %.3f m (installed: -0.493)" % side)
assert side > 1.3

# zoom in on a stretch with the vehicle drawn at a point
k = int(np.searchsorted(tr.t, t0 + 60))
ed.plan.cx, ed.plan.cy, ed.plan.scale = float(tr.fe[k]), float(tr.fn[k]), 60.0
ed.plan.mark_i = k
ed._plan_view_changed()
if FIG:
    harness.grab(ed.widget(), os.path.join(FIG, "editor_antennas.png"))
    ed.c_ant.setChecked(False)
    ed.plan.measured = None
    ed.plan.mark_i = -1
    ed.plan.cx, ed.plan.cy, ed.plan.scale = float(tr.fe[k]), float(tr.fn[k]), 1.2
    ed.plan.update()
    harness.grab(ed.plan, os.path.join(FIG, "editor_direction.png"))
d = antennacheck.AntennaDialog(res, dock, on_swap=lambda: None)
d.resize(640, 700)
if FIG:
    harness.grab(d, os.path.join(FIG, "antenna_check.png"))
print("OK")
