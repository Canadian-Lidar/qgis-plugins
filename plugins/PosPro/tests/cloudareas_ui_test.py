"""Problem areas from the point cloud (PosPro 2.2.0): CloudPro's problem_areas.json is listed in Trajectory Review
(C rows, found by "Point cloud"), drawn in the Trajectory editor (plan and profile), can be decided like any window,
counts as open while it belongs to the current trajectory, and is marked as from an earlier trajectory once PosPro
has finalized another one.

    QT_QPA_PLATFORM=offscreen python3 PosPro/tests/cloudareas_ui_test.py [figure folder]
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGINS = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(PLUGINS, "guides"))
import harness  # noqa: E402

FIG = sys.argv[1] if len(sys.argv) > 1 else None
app, iface = harness.start(PLUGINS, tempfile.mkdtemp())
from PosPro.core import cloudareas, review  # noqa: E402
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
n_found = len(review.state(p)["anomalies"])

# CloudPro's file, as a DCS job would have it in 03_CloudPro/out (the DCS lookups stubbed)
out = os.path.join(job, "03_CloudPro", "out")
os.makedirs(out)
fp = os.path.join(out, "problem_areas.json")
ids = {"now": "sha256:current"}
cloudareas.path = lambda project: fp
cloudareas._pospro_id = lambda project: ids["now"]
a0, a1 = t0 + 150.0, t0 + 153.5
wk = int(a0 // 604800)
json.dump({"schema": "dcs.problem_areas/1", "measured_utc": "2026-11-10 21:30:00 UTC", "tolerance_m": 0.03,
           "trajectory": {"source": "pospro", "handover_id": "sha256:current"},
           "summary": "Lines agree within 3 cm, except 1 local problem area (Line 3).",
           "areas": [{"id": 1, "line": "Line 3", "lines": ["Line 1", "Line 3"], "e": 1.0, "n": 2.0, "area_m2": 140,
                      "median_m": 0.09, "max_m": 0.17, "gps_week": wk,
                      "gps_sow": [a0 - wk * 604800, a1 - wk * 604800]}]}, open(fp, "w"))

dock.set_project(p)
harness.settle()
rp = dock.page["review"]
rp.refresh()
c_rows = [it for it in rp.all_items if str(it["id"]).startswith("C")]
assert len(c_rows) == 1, [it["id"] for it in rp.all_items]
c = c_rows[0]
th = review.state(p)["thresholds"]
assert abs(c["t0"] - (a0 - th["extend_before_s"])) < 1e-6 and abs(c["t1"] - (a1 + th["extend_after_s"])) < 1e-6
assert c["what"] == "Point cloud" and "17 cm, Line 3" in c["worst"], c
assert "problem area" in rp.pending.text()
assert len(review.state(p)["anomalies"]) == n_found               # not stored in the review
print("Review: %s listed, found by %s, worst %s" % (c["id"], c["what"], c["worst"]))
rp.show_filter.setCurrentIndex(rp.show_filter.findData("cloud"))
assert [it["id"] for it in rp.items] == ["C1"]
rp.show_filter.setCurrentIndex(0)

# the editor: open in plan and profile, listed, selectable
dock.open_editor()
ed = plugin.editor
ed.resize(1500, 940)
ed.widget().resize(1500, 940)
harness.settle(10)
tr = ed.track
assert ed.profile.cloud_windows and ed.profile.cloud_windows[0][0] == "C1"
assert ed.plan.cloud_spans and (tr.state[tr.span(a0, a1)] == 1).all(), "open in the editor"
row = [i for i, r in enumerate(ed.rows) if str(r[0]).startswith("C1")][0]
ed.table.selectRow(row)
harness.settle()
w0, w1 = ed.sel_windows[0]
assert abs(w0 - c["t0"]) < 1e-6 and abs(w1 - c["t1"]) < 1e-6
ed.plan.repaint()
ed.profile.repaint()
if FIG:
    harness.grab(ed.widget(), os.path.join(FIG, "editor_cloud.png"))
print("editor: C1 drawn in the plan and the profile, selected from the list")

# decided like any window: marked poor, then no longer open
rp.refresh()
i = [k for k, it in enumerate(rp.items) if it["id"] == "C1"][0]
rp.table.selectRow(i)
rp.decide("poor")
harness.settle(10)
e = review.edit_for(p, c["t0"], c["t1"])
assert e is not None and e["action"] == "poor" and e.get("source") == "cloud", e
print("decided: C1 marked poor (source %s)" % e["source"])

# 2.2.1: opened from CloudPro's line separation (DCS Core registry.focus): Trajectory Review, C rows, C1 chosen
dock.goto_page(0)
rp.show_filter.setCurrentIndex(0)
rp.table.clearSelection()
plugin.focus({"kind": "cloud_area", "id": "C1"})
harness.settle(4)
assert dock.stack.currentIndex() == dock.index_of("review") and rp.show_filter.currentData() == "cloud"
sel = rp.table.selectionModel().selectedRows()
assert sel and rp.items[sel[0].row()]["id"] == "C1", sel
assert not dock.focus_cloud("C9")
print("focus from CloudPro: Trajectory Review, point cloud rows, C1 chosen")

# PosPro finalizes another trajectory: the area is from an earlier one and no longer counts as open
ids["now"] = "sha256:newer"
rp.refresh()
c = [it for it in rp.all_items if it["id"] == "C1"][0]
assert "earlier trajectory" in c["worst"] and "earlier trajectory" in rp.pending.text()
assert not any(a.get("current", True) for a in review.listed(p) if a["kind"] == "cloud")
print("after another trajectory: shown as from an earlier trajectory")
print("OK")
