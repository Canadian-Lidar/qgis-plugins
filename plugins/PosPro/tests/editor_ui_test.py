"""Trajectory editor in the guide harness: linked views, selection in both, decisions applied.

    QT_QPA_PLATFORM=offscreen python3 PosPro/tests/editor_ui_test.py [figure folder]
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGINS = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(PLUGINS, "guides"))
import harness  # noqa: E402

FIG = sys.argv[1] if len(sys.argv) > 1 else None
settings = tempfile.mkdtemp()
app, iface = harness.start(PLUGINS, settings)
import numpy as np  # noqa: E402
from PosPro.core import review  # noqa: E402
from PosPro.core.project import Project  # noqa: E402
from PosPro.tests import simtraj  # noqa: E402

plugin = harness.load("PosPro", iface)
plugin.toggle(True)
dock = plugin.dock
job = tempfile.mkdtemp()
p = Project(os.path.join(job, "Shirley.pospro.json"))
p.cfg["name"] = "Shirley"
p.cfg["processing"]["mode"] = "tc"
os.makedirs(p.work_dir, exist_ok=True)
p.save()
t0, _ = simtraj.make(p.work_dir)
review.state(p)
review.detect(p)
dock.set_project(p)
harness.settle()
print("anomalies found:", [(a["id"], round(a["t0"] - t0, 1), round(a["t1"] - t0, 1)) for a in review.state(p)["anomalies"]])

dock.open_editor()
ed = plugin.editor
ed.resize(1500, 940)
ed.widget().resize(1500, 940)
harness.settle(10)
tr = ed.track
assert tr.ok and len(tr.t) > 1000, "track loaded"
print("track: %d samples (every %d), %d section(s)" % (len(tr.t), tr.step, len(tr.sections)))
assert (tr.state == 1).any(), "open anomalies coloured"

# --- linking: hover in the plan marks the profile, and the other way round
i = len(tr.t) // 3
x, y = ed.plan.to_screen(tr.fe[i], tr.fn[i])
j = ed.plan.nearest(x, y)
ed.plan.hovered.emit(j)
assert ed.profile.hover_x is not None and abs(ed.profile.hover_x - tr.t[j]) < 1e-6
ed.profile.hoveredTime.emit(float(tr.t[i]))
assert ed.plan.hover_i == tr.index_at(float(tr.t[i]))
ed.profile.timePicked.emit(float(tr.t[i]))
assert ed.plan.mark_i == tr.index_at(float(tr.t[i]))
print("linked hover and pick: OK")

# --- clicking the first anomaly in the list selects it in both views and zooms there
ed.table.selectRow(0)
harness.settle()
assert ed.sel_windows and ed.plan.sel.any(), "row selects"
a0, a1 = ed.sel_windows[0]
assert ed.profile.x0 <= a0 and ed.profile.x1 >= a1
if FIG:
    harness.grab(ed.widget(), os.path.join(FIG, "editor_found.png"))

# --- profile range: select the height spike and interpolate (straight, vertical only)
ed.clear_selection()
ed.profile.rangeSelected.emit(t0 + 99.5, t0 + 112.5)
assert len(ed.sel_windows) == 1
ed.method.setCurrentIndex(1)
ed.c_h.setChecked(False)
ed.decide("interpolate")
harness.settle(10)
tr = ed.track
sl = tr.span(t0 + 100, t0 + 112)
spike = float(np.abs(tr.fh[sl] - tr.oh[sl]).max())
print("height spike moved by up to %.2f m" % spike)
assert spike > 1.0 and (tr.state[sl] == 2).all()

# --- plan lasso: around the sideways jump, smooth curve, horizontal only
ed.c_h.setChecked(True)
ed.c_v.setChecked(False)
ed.method.setCurrentIndex(0)
ed._zoom_time(t0 + 225, t0 + 251, pad=0.2)
harness.settle()
m = np.zeros(len(tr.t), dtype=bool)
m[tr.span(t0 + 229.5, t0 + 246.5)] = True
sx, sy = ed.plan.to_screen(tr.fe[m], tr.fn[m])
pad = 25
shape = [(sx.min() - pad, sy.min() - pad), (sx.max() + pad, sy.min() - pad), (sx.max() + pad, sy.max() + pad),
         (sx.min() - pad, sy.max() + pad)]
ed.plan.tool = "lasso"
ed.plan._shape = shape
ed.plan._finish_shape()
print("lasso windows:", [(round(a - t0, 1), round(b - t0, 1)) for a, b in ed.sel_windows])
assert ed.sel_windows
if FIG:
    harness.grab(ed.widget(), os.path.join(FIG, "editor_selected.png"))
ed.decide("interpolate")
harness.settle(10)
tr = ed.track
sl = tr.span(t0 + 231, t0 + 245)
M, N = review._radii(simtraj.LAT0)
worst = 0.0
for k in range(sl.start, sl.stop, 10):
    tn, te, _ = simtraj.truth_local(tr.t[k] - t0)
    worst = max(worst, float(np.hypot(tr.fn[k] - tn, tr.fe[k] - te)))
print("sideways jump after smooth curve: worst %.3f m from the road" % worst)
assert worst < 0.6     # 24 s of curving road bridged by a curve
if FIG:
    ed.plan.mark_i = -1
    harness.grab(ed.widget(), os.path.join(FIG, "editor_fixed.png"))

# --- review list and Review page agree
r = review.state(p)
acts = sorted(e["action"] + ":" + e.get("method", "") for e in r["edits"])
print("edits:", acts)
assert acts == ["interpolate:smooth", "interpolate:straight"]
dock.page["review"].fill()
assert any("smooth curve" in (dock.page["review"].table.item(k, 5).text() or "")
           for k in range(dock.page["review"].table.rowCount()))

# --- reject counts as open; undo takes the last edit back
ed.clear_selection()
ed.profile.rangeSelected.emit(t0 + 300, t0 + 305)
ed.decide("reject")
assert dock.page["review"].state()[0] == "warn"
ed.undo()
harness.settle(10)
assert len(review.state(p)["edits"]) == 2
ed.undo()
harness.settle(10)
assert len(review.state(p)["edits"]) == 1
print("reject / undo: OK")

# --- colour by solution type, whole job
ed.colour.setCurrentIndex(1)
ed.zoom_all()
if FIG:
    harness.grab(ed.widget(), os.path.join(FIG, "editor_fix.png"))
print("OK")
