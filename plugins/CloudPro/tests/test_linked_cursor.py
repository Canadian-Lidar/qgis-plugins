"""Linked cursor (CloudPro 2.1.0): the point under the mouse in one view is marked in the other two - a cross on
the same point, or a dashed vertical line where the view does not hold it.

    QT_QPA_PLATFORM=offscreen python3 CloudPro/tests/test_linked_cursor.py      (from the plugins folder)
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
from CloudPro.core import chain, importer as IM                   # noqa: E402
from CloudPro.core.pointset import to_box_frame                    # noqa: E402

dcs, p = _job.fresh_job("link")
_job.publish_sbet(dcs, _job.realtime_track(dcs.survey_clraw_files()))
p.data["override"] = {"source": "recorded"}
IM.import_chain(p, chain.resolve(dcs, override=p.data["override"]), IM.ImportOptions(min_speed=0.3))
p.save()
p.store.close()
import CloudPro                                                    # noqa: E402
plugin = CloudPro.classFactory(iface)
plugin.initGui()
win = plugin.show()
plugin.panel.resize(1600, 950)
win.open_project(dcs.path)
harness.settle(4)
win.v3d.async_still = False
for v in (win.plan, win.slice, win.v3d):
    v.repaint()
harness.settle(2)
ps = win.ps

# a point that is in the current slice: hover it in the Slice view
along, pos, th, _ = win._slice()
sl = win.slice
j = len(sl.idx) // 2
i = int(sl.idx[j])
x, y = sl._sx[j], sl._sy[j]
win._hover_at("slice", *sl.to_view(x, y))
win._update_linked()
pt = win.linked_point("slice", *sl.to_view(x, y))
assert pt is not None and pt[2] is not None
assert sl.linked is None                                           # not in the view the mouse is in
assert win.plan.linked[0] == "plus" and win.v3d.linked[0] == "plus"
px, py = win.plan.to_screen(win.plan.linked[1], win.plan.linked[2])
assert abs(win.plan.linked[1] - pt[0]) < 1e-6 and abs(win.plan.linked[2] - pt[1]) < 1e-6
print("Slice → Plan and 3D: cross at", tuple(round(c, 2) for c in pt))

# hover the Plan over a point outside the slice: the Slice shows a vertical line at its place
b = win.box
u, w = to_box_frame(ps.x, ps.y, b["cx"], b["cy"], b["bearing"])
off = np.flatnonzero(np.abs(u - pos) > th * 3 if not along else np.abs(w - pos) > th * 3)
k = int(off[len(off) // 2])
win._set_linked("plan", (float(ps.x[k]), float(ps.y[k]), float(ps.z[k])))
assert win.slice.linked[0] == "line", win.slice.linked
win._set_linked("3d", pt)                                          # a point in the slice: a line too (Sean)
assert win.slice.linked[0] == "line"
win._set_linked("plan", (float(ps.x[k]), float(ps.y[k]), float(ps.z[k])))
assert win.v3d.linked[0] == "plus" and win.plan.linked is None
print("Plan (outside the slice) → Slice: vertical line; 3D: cross")

# Plan over empty ground (no height): vertical lines in the Slice and the 3D view
win._set_linked("plan", (b["cx"], b["cy"], None))
assert win.slice.linked[0] == "line" and win.v3d.linked[0] == "line"
for v in (win.plan, win.slice, win.v3d):
    v.repaint()
# 3D hover picks the point under the mouse
win._set_linked(None, None)
v3 = win.v3d
sx, sy, zc = v3._screen_cache()
vis = np.flatnonzero((zc > 0.05) & (sx > 50) & (sx < v3.width() - 50) & (sy > 50) & (sy < v3.height() - 50))
jj = int(vis[len(vis) // 2])
pt3 = win.linked_point("3d", float(sx[jj]), float(sy[jj]))
assert pt3 is not None
win._set_linked("3d", pt3)
assert win.plan.linked[0] == "plus" and v3.linked is None
win._hover_off()
assert win.plan.linked is None and win.slice.linked is None and win.v3d.linked is None
plugin.panel.grab()
win._set_linked("slice", pt)
harness.settle(2)
out = os.environ.get("LINKED_SHOT")
if out:
    plugin.panel.grab().save(out)
win.shutdown()
plugin.unload()
print("test_linked_cursor: OK")
