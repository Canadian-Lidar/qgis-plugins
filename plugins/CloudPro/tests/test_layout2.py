"""CloudPro 2.0.0 layout (Sean, 10 Nov 2026): header steps and tools, project menu with CloudPro's menu, views that
never float, one legend with a right-click menu, whole slices inside the Inspection Area, whole-metre areas.

    QT_QPA_PLATFORM=offscreen python3 CloudPro/tests/test_layout2.py      (from the plugins folder)
"""
import os
import sys
import tempfile

PLUGINS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(PLUGINS, "guides"))
sys.path.insert(0, PLUGINS)
import harness                                                    # noqa: E402

app, iface = harness.start(PLUGINS, tempfile.mkdtemp())
from CloudPro.tests import _job                                    # noqa: E402
from CloudPro.core import chain, importer as IM                   # noqa: E402
from CloudPro.ui import viewpanes                                  # noqa: E402
from CloudPro.qt import QtCore, QtWidgets                          # noqa: E402

# whole slices, centred, never past the edges
assert viewpanes.slice_plan(37, 2) == (18, 2.0, -17.0)            # 18 slices of 2 m, 0.5 m left at each end
assert viewpanes.slice_plan(36, 1) == (36, 1.0, -17.5)
assert viewpanes.slice_plan(3, 8) == (1, 3.0, 0.0)                # wider than the area: one slice, the area's width
n, w, c0 = viewpanes.slice_plan(61, 4)
assert c0 - w / 2 >= -30.5 and c0 + (n - 1) * w + w / 2 <= 30.5

dcs, p = _job.fresh_job("lay2")
_job.publish_sbet(dcs, _job.realtime_track(dcs.survey_clraw_files()))
p.data["override"] = {"source": "recorded"}
IM.import_chain(p, chain.resolve(dcs, override=p.data["override"]), IM.ImportOptions(min_speed=0.3))
p.save()
p.store.close()
import CloudPro                                                    # noqa: E402
plugin = CloudPro.classFactory(iface)
plugin.initGui()
win = plugin.show()
win.open_project(dcs.path)
harness.settle(4)

# header: steps, tools right of the project box, the menu in the project box
h = win.header
assert [k for k, *_ in h.steps.items] == ["georef", "clean", "publish"]
assert [t for _k, t, *_ in h.steps.items] == ["Georeference", "Clean & classify", "Finalize"]
assert h.steps.isVisible() and h.steps.current in ("clean", "publish")
assert win.b_pan.geometry().left() > win.btn_project.geometry().right()
names = [a.text() for a in win.btn_project.menu().actions()]
assert any(t.startswith("Finalize for the job") for t in names) and any(t.startswith("Georeference") for t in names)
assert not any("ublish" in t for t in names)
assert not win.b_info.isVisible() and not win.cmb_eline.isVisible() and not win.sp_size.isVisible()
got = []
win.show_dock = lambda k: got.append(k)
h.steps.clicked.emit("clean")
assert got == ["filters"]
print("header: steps, tools right of the project box, CloudPro's menu in the project box")

# views never float
for d in win.docks.values():
    assert not d.isFloating()
    assert not (d.features() & QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetFloatable)
print("views and panels: no floating")

# legend menu: colour, scale, ramp, link, sizes (Plan also grid, cell, brush)
menus, last = {}, []
from CloudPro.ui import mainwindow as mw                          # noqa: E402
mw.exec_ = lambda m, *a: last.append([x.text().split("  ·")[0] for x in m.actions() if x.text()])
for k in ("plan", "slice", "3d"):
    win._legend_menu(k, QtCore.QPoint(0, 0))
    menus[k] = last.pop()
assert menus["plan"] == ["Colour", "Colour scale", "Colour ramp", "Link colours", "Grid", "Cell size", "Point size",
                         "Brush size"], menus["plan"]
assert menus["slice"] == ["Colour", "Colour scale", "Colour ramp", "Link colours", "Point size"], menus["slice"]
print("legend menus:", menus["plan"])

# 2.0.3: each view's ramp and point size are its own; the inputs line never pops up as a window
win.set_lut("slice", "Viridis")
win.set_point_size("3d", 5)
harness.settle(2)
assert win.luts == {"plan": "Rainbow", "slice": "Viridis", "3d": "Rainbow"}, win.luts
assert (win.plan.point_size, win.slice.point_size, win.v3d.point_size) == (2, 2, 5)
assert win.plan.legend["lut"] == "Rainbow" and win.slice.legend["lut"] == "Viridis"
win.panes["3d"].link.setCurrentIndex(win.panes["3d"].link.findData("slice"))
harness.settle(2)
assert win._lut_of("3d") == "Viridis" and win.v3d.legend["lut"] == "Viridis"
win.panes["3d"].link.setCurrentIndex(0)
win.refresh_chain()
harness.settle(2)
assert win.lbl_inputs.parent() is win and not win.lbl_inputs.isVisible()
assert not [w for w in QtWidgets.QApplication.topLevelWidgets() if isinstance(w, QtWidgets.QLabel) and w.isVisible()]
print("per-view colour ramp and point size; linked view follows; no stray inputs window")

# whole-metre Inspection Area; slices follow the width, stay inside, keep their place
b = dict(win.box)
b.update(length=37.4, width=20.6)
win.set_box(b, load=True)
harness.settle(2)
assert (win.box["length"], win.box["width"]) == (37.0, 21.0)
sp = win.slice_pane
sp.set_width(2.0)
win._slice()
assert sp.count() == 18 and sp.lbl.text().endswith("/ 18")
win.step("sec_last")
along, pos, th, _ = win._slice()
assert sp.index == 17 and abs(pos + th / 2 - 18.0) < 1e-9          # ends 0.5 m inside the 37 m area
win.step("sec_next")
assert sp.index == 17                                              # never past the edge
win.step("sec_first")
assert sp.index == 0
sp.go(9)
c = sp.centre()
sp.set_width(4.0)
assert sp.count() == 9 and abs(sp.centre() - c) <= 2.0
print("slices: whole, centred, inside the area; width change keeps the place; area in whole metres")
win.shutdown()
plugin.unload()
print("test_layout2: OK")
