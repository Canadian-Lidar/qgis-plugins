"""Colour scale per view: automatic over the Inspection Area (default), automatic over the whole project, or a
fixed minimum / maximum; set from a right-click on the scale bar; a linked view uses the scale of the view it
follows; the scales are saved with the project.

    QT_QPA_PLATFORM=offscreen python3 CloudPro/tests/test_colour_scale.py      (from the plugins folder)
"""
import os
import sys
import tempfile

import numpy as np

PLUGINS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(PLUGINS, "guides"))
sys.path.insert(0, PLUGINS)
import harness                                                    # noqa: E402

app, iface = harness.start(PLUGINS, tempfile.mkdtemp())
from CloudPro.tests import _job                                    # noqa: E402
import CloudPro                                                    # noqa: E402
from CloudPro.core import chain, importer as IM                   # noqa: E402
from CloudPro.qt import QtCore, QtGui, Qt                         # noqa: E402

dcs, p = _job.fresh_job("scale")
IM.import_chain(p, chain.resolve(dcs), IM.ImportOptions())
p.store.close()
plugin = CloudPro.classFactory(iface)
plugin.initGui()
win = plugin.show()
win.open_project(dcs.path)
harness.settle(20)
views = {"plan": win.plan, "slice": win.slice, "3d": win.v3d}
for k in views:
    win.panes[k].colour.setCurrentText("Height")
harness.settle(10)


def leg(k):
    L = views[k].legend
    return round(L["lo"], 6), round(L["hi"], 6), L.get("note", "")


area = win._range("Height")
assert all(leg(k)[:2] == tuple(round(v, 6) for v in area) for k in views)
print("default: all views automatic over the Inspection Area", area)

# whole project
win.set_scale("3d", "Height", ("project",))
harness.settle(5)
pr = win._project_range("Height")
assert leg("3d") == (round(pr[0], 6), round(pr[1], 6), "project") and pr != area
assert leg("plan")[:2] == tuple(round(v, 6) for v in area)
print("3D automatic over the project", tuple(round(v, 2) for v in pr), "(Plan unchanged)")

# fixed
lo, hi = area[0] + 2.0, area[0] + 6.0
win.set_scale("plan", "Height", ("manual", lo, hi))
harness.settle(5)
assert leg("plan") == (round(lo, 6), round(hi, 6), "fixed")
c_plan = win._colours("Height", None, "plan")
c_area = win._colours("Height", None, "slice")
assert (c_plan != c_area).any()
print("Plan fixed %.2f to %.2f: its colours change, the Slice's do not" % (lo, hi))

# linking overrides the view's own setting
win.set_scale("slice", "Height", ("project",))
win.panes["slice"].link.setCurrentIndex(win.panes["slice"].link.findData("plan"))
harness.settle(5)
assert leg("slice") == leg("plan"), (leg("slice"), leg("plan"))
win.panes["slice"].link.setCurrentIndex(0)
harness.settle(5)
assert leg("slice")[2] == "project"
print("Slice linked to the Plan uses the Plan's fixed scale; unlinked, its own (project) again")

# right-click on the scale bar opens the menu for that view
calls = []
win._scale_menu = lambda k, g: calls.append(k)
for k, v in views.items():
    v.repaint()
    harness.settle(2)
    r = v.legend_rect
    assert r is not None, k
    pt = QtCore.QPointF(r.center())
    gp = QtCore.QPointF(v.mapToGlobal(pt.toPoint()))
    ev = QtGui.QMouseEvent(QtCore.QEvent.Type.MouseButtonPress, pt, gp, Qt.MouseButton.RightButton,
                           Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier)
    v.mousePressEvent(ev)
assert calls == ["plan", "slice", "3d"], calls
print("right-click on each scale bar opens its menu")

# the min / max dialog works in shown units (elevations for heights)
from CloudPro.ui.dialogs import ScaleDialog   # noqa: E402
d = ScaleDialog(win, "Plan", "Height", lo, hi, z_offset=100.0)
assert abs(d.w_lo.value() - (lo + 100)) < 0.01 and all(abs(a - b) < 0.006 for a, b in zip(d.values(), (lo, hi)))
d = ScaleDialog(win, "Plan", "Time", 1.7e9, 1.7e9 + 600)
assert d.values() == (1.7e9, 1.7e9 + 600)
print("dialog: heights as elevations, times in UTC")

# saved with the project
win.save_project(quiet=True) if hasattr(win, "save_project") else win.save(quiet=True)
win.open_project(dcs.path)
harness.settle(10)
assert win.scales["plan"]["Height"][0] == "manual" and win.scales["3d"]["Height"] == ("project",)
print("scales saved with the project")
win.shutdown()
print("test_colour_scale: OK")
