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
v3.nav_rotate("left")
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
win.shutdown()
print("test_view_nav: OK")
