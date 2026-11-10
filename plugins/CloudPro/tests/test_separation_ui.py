"""Line separation in the window (CloudPro 2.2.0): the menu action runs the check as a job, the Plan view switches
to the Line separation grid with its key, the results window lists the pairs, the tolerance comes from the legend
menu, the result is kept with the project and shown out of date after the points change.

    QT_QPA_PLATFORM=offscreen python3 CloudPro/tests/test_separation_ui.py      (from the plugins folder)
"""
import os
import sys
import tempfile

PLUGINS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(PLUGINS, "guides"))
sys.path.insert(0, PLUGINS)
import harness                                                    # noqa: E402

app, iface = harness.start(PLUGINS, tempfile.mkdtemp())
from CloudPro.tests import test_separation as TS                  # noqa: E402
from CloudPro.ui.planview import SEPARATION                       # noqa: E402
from CloudPro.qt import QtCore                                     # noqa: E402

p = TS.build(tempfile.mkdtemp())                                   # the simulated street, three lines
p.store.close()
import CloudPro                                                    # noqa: E402
plugin = CloudPro.classFactory(iface)
plugin.initGui()
win = plugin.show()
plugin.panel.resize(1500, 900)
win.open_project(p.path)
harness.settle(6)


def wait_job(limit=120):
    harness.wait(lambda: not win._busy, limit)
    assert not win._busy, "job still running"
    harness.settle(4)


assert win.plan.separation is None
assert any(a.text().startswith("Check line separation") for a in win.btn_project.menu().actions())

# choosing the grid with nothing measured runs the check
win.plan_pane.grid.setCurrentText(SEPARATION)
assert win._busy and win._busy_tag == "separation"
wait_job()
r = win.plan.separation
assert r is not None and win.plan.attr == SEPARATION and win.plan.separation_current
assert r.pairs, "no overlapping surfaces found between the two lines"
w = win._sepwin
assert w is not None and w.isVisible() and w.pairs.rowCount() == len(r.pairs)
assert w.lines.rowCount() == len(r.lines) and w.lbl_verdict.text()
print("check: %d pair(s), %s patches; %s" % (len(r.pairs), "{:,}".format(r.data["patches"]), w.lbl_verdict.text()))
assert any("Line separation checked" in h.get("text", "") for h in win.project.data.get("history", []))
assert any(f.endswith("line_separation.log") for f in os.listdir(win.project.log_dir))

# drawn: tiles with patches give images; the key is painted
img = [win.plan._tile_image(k) for k in r.grids]
assert any(i is not None for i in img)
win.plan.repaint()
win.plan.grab().save(os.path.join(tempfile.gettempdir(), "cloudpro_separation_plan.png"))

# a pair or a line chosen in the window: the Plan shows only that one
w.pairs.selectRow(0)
f = win.plan.separation_filter
assert f and f[0] == "pair" and (f[1], f[2]) == (r.pairs[0]["a"], r.pairs[0]["b"]), f
assert "/" in win.plan.separation_title()
assert any(win.plan._sep_grid(k) is not None for k in r.grids)
w.lines.selectRow(0)
assert win.plan.separation_filter == ("line", r.lines[0]["id"]) and not w.pairs.selectionModel().selectedRows()
assert "against the others" in win.plan.separation_title()
w.btn_all.click()
assert win.plan.separation_filter is None
print("pair and line chosen in the window: shown alone on the Plan")

# problem areas: listed, outlined on the Plan, a click puts the Inspection Area there; the file for PosPro
areas = r.problem_areas(win.plan.tolerance)
assert areas and w.areas.rowCount() == len(areas) and "PROBLEM AREAS" in w.tabs.tabText(0)
loc = [a for a in areas if a["line"] == 0][0]
row = [i for i in range(w.areas.rowCount()) if w.areas.item(i, 0).data(QtCore.Qt.ItemDataRole.UserRole) == loc["id"]][0]
w.areas.selectRow(row)
harness.settle(4)
bx = max(loc["boxes"], key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
assert abs(win.box["cx"] - (bx[0] + bx[2]) / 2) < 1 and abs(win.box["cy"] - (bx[1] + bx[3]) / 2) < 1, (win.box, loc)
assert win.box["length"] <= 60
assert win.plan.separation_filter == ("line", 0)
assert any(a["id"] == loc["id"] for a in win.plan.problem_areas())
win.plan.repaint()
assert os.path.exists(os.path.join(win.project.out_dir, "problem_areas.json"))
print("problem areas: %d listed; #%d (Line 1) chosen: Inspection Area at E %.1f N %.1f" % (
    len(areas), loc["id"], win.box["cx"], win.box["cy"]))
w.btn_all.click()

# tolerance from the legend menu changes the key and the table colours
before = win.plan.separation_shares()
win.set_tolerance(0.01)
assert win.plan.tolerance == 0.01 and win.plan.separation_shares() != before or before[0] == 1.0
win.set_tolerance(0.03)
print("tolerance: shares at 3 cm %s" % ", ".join("%.0f%%" % (100 * s) for s in win.plan.separation_shares()[:3]))

# kept with the project; out of date after the points change
win._load_separation()
assert win.plan.separation is not None and win.plan.separation_current
win.project.lines[0]["latency_s"] = 0.01
win._load_separation()
assert not win.plan.separation_current and "OUT OF DATE" in w.lbl_info.text()
print("reloaded from the project; out of date after a georeferencing change")

# check again from the results window
w.btn_again.click()
wait_job()
assert win.plan.separation_current
print("OK")
plugin.unload()
