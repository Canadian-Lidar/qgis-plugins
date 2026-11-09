"""View navigation: zoom-to-Inspection-Area buttons on all three views, zoom to extents on Plan, the 3-D view
turning around the Inspection Area's centre (also after the area moves) and 22.5 degree arrow clicks.

    QT_QPA_PLATFORM=offscreen python3 CloudPro/tests/test_view_nav.py      (from the plugins folder)
"""
import math
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

dcs, p = _job.fresh_job("nav")
IM.import_chain(p, chain.resolve(dcs), IM.ImportOptions())
p.store.close()
plugin = CloudPro.classFactory(iface)
plugin.initGui()
win = plugin.show()
win.open_project(dcs.path)
harness.settle(20)
v3, b = win.v3d, win.box

# 3-D: the orbit centre is the area's centre
assert np.allclose(v3.center[:2], [b["cx"], b["cy"]]), (v3.center, b)
assert abs(v3.center[2] - v3._zmid()) < 1e-6
print("3D centre = Inspection Area centre (%.2f, %.2f, %.2f)" % tuple(v3.center))

# arrows: 22.5 degrees per click
y0, p0 = v3.yaw, v3.pitch
v3.nav_rotate("right")
assert abs(math.degrees(v3.yaw - y0) - 22.5) < 1e-9
v3.nav_rotate("up")
assert abs(math.degrees(p0 - v3.pitch) - 22.5) < 1e-9
print("arrows: 22.5 degrees per click")

# pan away, then the button brings the centre back and frames the area
v3.center = v3.center + [40.0, -25.0, 3.0]
v3.dist = 900.0
win.v3d_pane.zoom_area_btn.click()
harness.settle(2)
r = math.hypot(b["length"], b["width"]) / 2
assert np.allclose(v3.center[:2], [b["cx"], b["cy"]]) and r / math.tan(v3.fov / 2) <= v3.dist <= 1.5 * r / math.tan(v3.fov / 2)
print("3D zoom to Inspection Area: centre back, distance %.1f m" % v3.dist)

# moving the area moves the orbit centre with it
nb = dict(b, cx=b["cx"] + 20.0, cy=b["cy"] + 5.0)
win.set_box(nb, load=True)
harness.settle(20)
assert np.allclose(v3.center[:2], [nb["cx"], nb["cy"]]), v3.center
print("3D centre follows the moved area")

# plan: zoom to area and to extents
pl = win.plan
win.plan_pane.zoom_ext_btn.click()
s_ext = pl.scale
x0, y0, x1, y1 = p.store.extent() if p.store.extent() else win.project.store.extent()
win.plan_pane.zoom_area_btn.click()
assert abs(pl.ca - nb["cx"]) < 1e-6 and abs(pl.cb - nb["cy"]) < 1e-6 and pl.scale > s_ext
print("plan: zoom to extents (scale %.3f) and to the area (scale %.3f)" % (s_ext, pl.scale))

# slice: zoom to area frames the width (cross-section) or the length (profile)
sl = win.slice
sl.scale *= 0.05
win.slice_pane.zoom_area_btn.click()
W = max(sl.width(), 50)
span = (W - 40) / sl.scale
half = nb["length"] / 2 if win.slice_pane.along else nb["width"] / 2
assert abs(sl.ca) < 1e-6 and span >= 2 * half - 1e-6, (sl.ca, span, half)
print("slice: zoom to the area (%.1f m across)" % span)

# dragging right (and the right arrow) moves the near side of the cloud right, as if holding it
from CloudPro.qt import QtCore, QtGui, Qt                        # noqa: E402
eye_dir, fwd, right, up = v3._basis()
near = v3.center + eye_dir * (v3.dist * 0.3)                     # a point between the centre and the camera
x_before = float(v3.project(near[None, :])[0][0])
v3._drag = ("orbit", QtCore.QPointF(100, 100), v3.yaw, v3.pitch)
ev = QtGui.QMouseEvent(QtCore.QEvent.Type.MouseMove, QtCore.QPointF(160, 100), QtCore.QPointF(160, 100),
                       Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
v3.mouseMoveEvent(ev)
v3._drag = None
x_after = float(v3.project(near[None, :])[0][0])
assert x_after > x_before, (x_before, x_after)
x0 = float(v3.project(near[None, :])[0][0])
v3.nav_rotate("right")
assert float(v3.project(near[None, :])[0][0]) > x0
print("3D drag right / right arrow: the near side moves right (%.0f -> %.0f px)" % (x_before, x_after))

# edit mode colours of the selection shapes, white Inspection Area, magenta rejects, titles, cell list, tabs
from CloudPro.ui import render                                    # noqa: E402
for mode, rgb in (("reject", (255, 59, 59)), ("accept", (46, 204, 64)), ("select", (160, 160, 160))):
    win.set_mode(mode)
    for v in (win.plan, win.slice, v3):
        c = render.edit_qcolor(v.edit_mode)
        assert (c.red(), c.green(), c.blue()) == rgb, (mode, v)
assert render.REJECT_RGB == (255, 0, 255) and win._box_lines3d()[0][1] == "#ffffff"
assert [win.panes[k].title.text() for k in ("plan", "slice", "3d")] == ["PLAN", "SLICE", "3D"]
assert [win.cb_cell.itemData(i) for i in range(win.cb_cell.count())] == list(pl.CELL_CHOICES)
win.cb_cell.setCurrentIndex(win.cb_cell.findData(5.0))
assert pl.cell == 5.0
from CloudPro.qt import QtWidgets                                 # noqa: E402
assert win.tabPosition(Qt.DockWidgetArea.RightDockWidgetArea) == QtWidgets.QTabWidget.TabPosition.South
win._toggle_rej(True)
win.refresh_views()
for k in ("plan", "slice", "3d"):
    L = win.panes[k].view.legend
    assert L is None or all(t != "Rejected" for _c, t in L.get("extra", [])), k
print("selection colours by mode, white area, magenta rejects, plain titles, cell list, tabs at the bottom")
win.shutdown()
print("test_view_nav: OK")
