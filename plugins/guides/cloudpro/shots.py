#!/usr/bin/env python3
"""Recreate every figure of the CloudPro user guide into guides/cloudpro/fig/.

    QT_QPA_PLATFORM=offscreen python3 guides/cloudpro/shots.py

Uses the real CloudPro and DCS Core widgets through guides/harness.py on the job
/jobs/cloudpro/Job_Oct20. The job's processing project is first restored from
/jobs/cloudpro/_pristine (PosPro and AlignPro published, CloudPro georeferenced,
cleaned and published once), so every run starts from the same state. The script
then works through the real workflow: update the georeferencing (edits carried
over), navigate, edit, preview and accept filters, open the properties and
publish.

Modal dialogs and menus are shown, grabbed and answered by the hooks below
(they replace QDialog.exec / QMenu.exec for the screenshots only); nothing in
the plugin is changed.
"""
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.dirname(os.path.dirname(HERE))          # folder holding cloudpro/, dcs_core/, guides/
FIG = os.path.join(HERE, "fig")
JOB = "/jobs/cloudpro/Job_Oct20"
NAME = "PatchTest_Oct20"
PROJ_DIR = os.path.join(JOB, "Processing", NAME)
PROJ = os.path.join(PROJ_DIR, NAME + ".dcsproj")
PRISTINE = "/jobs/cloudpro/_pristine/" + NAME
SETTINGS = "/jobs/cloudpro/settings"

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(WORK, "guides"))


# ----------------------------------------------------------------------------- the job's starting state
def restore():
    if not os.path.isdir(PRISTINE):
        raise SystemExit("missing %s (a copy of the job's processing project)" % PRISTINE)
    shutil.rmtree(PROJ_DIR, ignore_errors=True)
    subprocess.check_call(["cp", "-a", PRISTINE, PROJ_DIR])
    shutil.rmtree(SETTINGS, ignore_errors=True)
    os.makedirs(SETTINGS, exist_ok=True)
    os.makedirs(FIG, exist_ok=True)


restore()
import harness                                          # noqa: E402

app, iface = harness.start(WORK, SETTINGS)
from PyQt5 import QtCore, QtGui, QtWidgets              # noqa: E402
from PIL import Image                                   # noqa: E402

Qt = QtCore.Qt
MADE = []


def out(name):
    p = os.path.join(FIG, name)
    MADE.append(name)
    return p


def flush():
    """Process events, including the deferred deletes a running QGIS event loop would carry out
    (QCoreApplication.processEvents alone leaves replaced tables on screen)."""
    harness.settle(6)
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
    harness.settle(4)


def grab(w, name, scale=2):
    flush()
    return harness.grab(w, out(name), scale)


def grab_union(ws, name, top=None):
    """The bounding box of several parts of the window (e.g. the Plan and Slice windows together)."""
    top = top or panel
    flush()
    tmp = os.path.join(FIG, "_whole.png")
    harness.grab(top, tmp)
    ps = [(w.mapTo(top, QtCore.QPoint(0, 0)), w) for w in ws]
    x0 = min(p.x() for p, w in ps)
    y0 = min(p.y() for p, w in ps)
    x1 = max(p.x() + w.width() for p, w in ps)
    y1 = max(p.y() + w.height() for p, w in ps)
    Image.open(tmp).crop((2 * x0, 2 * y0, 2 * x1, 2 * y1)).save(out(name))
    os.remove(tmp)


def grab_in(w, name, top=None):
    """A part of the CloudPro window, cut out of a grab of the whole window, so it keeps the window's
    backgrounds (a part rendered on its own has transparent ones)."""
    top = top or panel
    flush()
    tmp = os.path.join(FIG, "_whole.png")
    harness.grab(top, tmp)
    p = w.mapTo(top, QtCore.QPoint(0, 0))
    Image.open(tmp).crop((2 * p.x(), 2 * p.y(), 2 * (p.x() + w.width()), 2 * (p.y() + w.height()))).save(out(name))
    os.remove(tmp)


# ----------------------------------------------------------------------------- GDAL stand-in
# QGIS ships GDAL; this Python does not. Without it CloudPro falls back to an ESRI ASCII ground
# grid, which a QGIS user never sees. A minimal GTiff writer stands in for osgeo.gdal / osr.
def _install_gdal():
    import types
    import numpy as np
    try:
        import osgeo  # noqa: F401
        return
    except ImportError:
        pass
    gdal, osr = types.ModuleType("osgeo.gdal"), types.ModuleType("osgeo.osr")

    class Band:
        def __init__(self, ds):
            self.ds = ds

        def SetNoDataValue(self, v):            # noqa: N802
            pass

        def WriteArray(self, a):                # noqa: N802
            self.ds.a = np.asarray(a, np.float32)

    class DS:
        def __init__(self, path):
            self.path, self.a = path, None

        def SetGeoTransform(self, g):           # noqa: N802
            pass

        def SetProjection(self, w):             # noqa: N802
            pass

        def GetRasterBand(self, i):             # noqa: N802
            return Band(self)

        def FlushCache(self):                   # noqa: N802
            if self.a is not None:
                Image.fromarray(self.a, mode="F").save(self.path, format="TIFF")

    class Driver:
        def Create(self, path, *a, **k):        # noqa: N802
            return DS(path)

    gdal.GetDriverByName = lambda name: Driver()
    gdal.GDT_Float32 = 6
    gdal.Open = lambda p: None
    pkg = types.ModuleType("osgeo")
    pkg.gdal, pkg.osr = gdal, osr
    sys.modules.update({"osgeo": pkg, "osgeo.gdal": gdal, "osgeo.osr": osr})


_install_gdal()


def crop(name, box):
    """Crop fig/name to box (x0, y0, x1, y1) in widget pixels (the PNG is 2x)."""
    p = os.path.join(FIG, name)
    im = Image.open(p)
    im.crop(tuple(int(v * 2) for v in box)).save(p)


def side_by_side(name, parts, gap=24):
    """Several PNGs in fig/ (deleted afterwards) put next to each other, top aligned."""
    ims = [Image.open(os.path.join(FIG, p)).convert("RGBA") for p in parts]
    w = sum(i.width for i in ims) + gap * (len(ims) - 1)
    h = max(i.height for i in ims)
    o = Image.new("RGBA", (w, h), (255, 255, 255, 0))
    x = 0
    for i in ims:
        o.paste(i, (x, 0))
        x += i.width + gap
    o.save(out(name))
    for p in parts:
        os.remove(os.path.join(FIG, p))
        MADE.remove(p)


# ----------------------------------------------------------------------------- modal hooks
PLAN = []          # what the next modal dialogs / menus should do, in order


def expect(kind, name=None, result=None, setup=None, choose=None, open_sub=None):
    PLAN.append(dict(kind=kind, name=name, result=result, setup=setup, choose=choose, open_sub=open_sub))


def _next(kind, what):
    if not PLAN:
        print("!! unexpected %s: %s" % (kind, what))
        return None
    p = PLAN.pop(0)
    if p["kind"] != kind:
        print("!! expected %s, got %s: %s" % (p["kind"], kind, what))
    return p


def _composite(widgets, name):
    """Several top-level widgets (a menu and its open submenu) as they sit on the screen."""
    harness.settle(6)
    imgs = []
    for w in widgets:
        tmp = os.path.join(FIG, "_tmp_%d.png" % len(imgs))
        harness.grab(w, tmp)
        imgs.append((Image.open(tmp).convert("RGBA"), w.pos()))
        os.remove(tmp)
    x0 = min(p.x() for _, p in imgs)
    y0 = min(p.y() for _, p in imgs)
    W = max(p.x() - x0 + im.width // 2 for im, p in imgs)
    H = max(p.y() - y0 + im.height // 2 for im, p in imgs)
    o = Image.new("RGBA", (2 * W, 2 * H), (0, 0, 0, 0))
    for im, p in imgs:
        o.paste(im, (2 * (p.x() - x0), 2 * (p.y() - y0)))
    o.save(out(name))


def menu_exec(self, *a):
    p = _next("menu", [x.text() for x in self.actions()][:3])
    pos = a[0] if a and isinstance(a[0], QtCore.QPoint) else QtCore.QPoint(300, 200)
    self.popup(pos)
    harness.settle(6)
    if p is None:
        self.hide()
        return None
    shown = [self]
    if p["open_sub"]:
        for act in self.actions():
            if act.text().startswith(p["open_sub"]) and act.menu() is not None:
                self.setActiveAction(act)
                r = self.actionGeometry(act)
                sub = act.menu()
                sub.popup(self.mapToGlobal(QtCore.QPoint(r.right() - 2, r.top() - 4)))
                harness.settle(6)
                shown.append(sub)
    if p["name"]:
        _composite(shown, p["name"])
    for w in reversed(shown):
        w.hide()
    if p["choose"]:
        for act in self.actions():
            if act.text().startswith(p["choose"]):
                act.trigger()
                return act
    return None


def dialog_exec(self, *a):
    p = _next("dialog", self.windowTitle() or getattr(self, "text", lambda: "")())
    self.show()
    harness.settle(10)
    if p is None:
        self.hide()
        return 0
    if p["setup"]:
        p["setup"](self)
        harness.settle(8)
    if p["name"]:
        flush()
        harness.grab(self, out(p["name"]))
    self.hide()
    r = p["result"]
    if callable(r):
        r = r(self)
    if r == "accept":
        self.setResult(1)
        return 1
    return r or 0


for cls in (QtWidgets.QMenu,):
    cls.exec = menu_exec
    cls.exec_ = menu_exec
for cls in (QtWidgets.QDialog, QtWidgets.QMessageBox, QtWidgets.QInputDialog):
    cls.exec = dialog_exec
    cls.exec_ = dialog_exec
YES = int(QtWidgets.QMessageBox.StandardButton.Yes)
OK = int(QtWidgets.QMessageBox.StandardButton.Ok)


def mouse(w, kind, x, y, button=Qt.NoButton, mods=Qt.NoModifier):
    t = {"move": QtCore.QEvent.MouseMove, "press": QtCore.QEvent.MouseButtonPress,
         "release": QtCore.QEvent.MouseButtonRelease}[kind]
    pos = QtCore.QPointF(x, y)
    ev = QtGui.QMouseEvent(t, pos, QtCore.QPointF(w.mapToGlobal(QtCore.QPoint(int(x), int(y)))), button,
                           button if kind != "release" else Qt.NoButton, mods)
    QtWidgets.QApplication.sendEvent(w, ev)
    harness.settle(3)


# ----------------------------------------------------------------------------- open CloudPro on the job
cp = harness.load("cloudpro", iface)
win = cp.show()
panel = cp.panel
win.open_project(PROJ)
harness.wait(lambda: win.project is not None and not win._busy, 600)
panel.resize(1500, 950)
harness.settle(20)
print("opened", win.project.name, len(win.ps))

# ---- header, window and project menus (shared chapters)
grab(win.header, "header.png")
harness.grab_menu(win.btn_window.menu(), out("window_menu.png"))
harness.grab_menu(win.btn_project.menu(), out("project_menu.png"))
from dcs_core.dialogs import NewProjectDialog            # noqa: E402

d = NewProjectDialog(win, "CloudPro")
d.job.setText(JOB)
d.name.setText("Street_Oct20")
d.show()
grab(d, "new_project.png")
d.close()

# ---- MENU
expect("menu", "main_menu.png")
win._main_menu()

# ---- out of date: Survey configuration changed to the recorded mounting (the Georeference dialog is cancelled)
win.show_properties()
pp = win._props
pp.tabs.setCurrentIndex(2)
pp.src.setCurrentIndex(1)
expect("dialog", "georef_recorded.png", result=0)
pp._apply_cfg()
win.refresh_chain()
harness.settle(6)
grab(win.header, "_stale_header.png")
r = win.btn_menu.geometry()
crop("_stale_header.png", (win.lbl_inputs.geometry().left() - 12, 0, r.right() + 8, win.header.height()))
os.replace(os.path.join(FIG, "_stale_header.png"), out("stale.png"))
MADE.remove("_stale_header.png")
pp.src.setCurrentIndex(0)
expect("dialog", None, result=0)
pp._apply_cfg()
pp.close()
win.refresh_chain()
assert not win.btn_update.isVisible(), "still out of date"

# ---- Update georeferencing with PosPro + AlignPro, every edit carried over
prog = {}


def georef_setup(dlg):
    pass


expect("dialog", "georef_dialog.png", result="accept")
expect("dialog", "georef_done.png", result=OK)          # the result message
win.georeference()
t0 = time.time()
harness.wait(lambda: win.prog.value() > 450 or not win._busy, 900)
if win._busy:
    strip = win.lbl_msg.parentWidget()
    grab(strip, "progress.png")
harness.wait(lambda: not win._busy, 900)
harness.settle(10)
print("georeferenced in %.0f s" % (time.time() - t0), len(win.ps))

# ---- the working Inspection Area
b = dict(win.box)
b.update(length=80.0, width=70.0)
win.set_box(b, load=True, fit=True)
win.plan.zoom_box()
win.v3d.preset("iso")
win.v3d.fit()
harness.settle(10)
print("box points", len(win.ps))

# ---- tool bar and its option lists
grab_in(win.toolbar, "toolbar.png")
expect("menu", "_opt_shape.png")
win.b_shape.show_options()
expect("menu", "_opt_line.png")
win.b_line.show_options()
expect("menu", "_opt_action.png")
win.b_action.show_options()
expect("menu", "_opt_where.png")
win.b_where.show_options()
side_by_side("tool_options.png", ["_opt_shape.png", "_opt_line.png", "_opt_action.png", "_opt_where.png"])

# ---- progress strip: the message on the left, counts, progress and Cancel on the right
im = Image.open(os.path.join(FIG, "progress.png"))
w, h = im.size
left, right = im.crop((0, 0, 620, h)), im.crop((w - 1240, 0, w, h))
o = Image.new("RGBA", (left.width + right.width, h))
o.paste(left, (0, 0))
o.paste(right, (left.width, 0))
o.save(os.path.join(FIG, "progress.png"))

# ---- progress strip: the message on the left, counts, progress and Cancel on the right
im = Image.open(os.path.join(FIG, "progress.png"))
w, h = im.size
left, right = im.crop((0, 0, 620, h)), im.crop((w - 1240, 0, w, h))
o = Image.new("RGBA", (left.width + right.width, h))
o.paste(left, (0, 0))
o.paste(right, (left.width, 0))
o.save(os.path.join(FIG, "progress.png"))

# ---- the poles in the Inspection Area (found with CloudPro's own pole filter; nothing is changed)
import numpy as np                                       # noqa: E402
from cloudpro.core import filters as F                   # noqa: E402
from cloudpro.core.pointset import rect_corners, to_box_frame   # noqa: E402

ctx = F.Ctx(win.ps, tracks=win.tracks, origin=win._origin())
res = F.make("f_poles").run(ctx, np.flatnonzero(ctx.accepted))
pidx = np.asarray(res["cls"][0]) if res.get("cls") else np.zeros(0, int)
key = np.round(win.ps.x[pidx] / 2.0).astype(int) * 100000 + np.round(win.ps.y[pidx] / 2.0).astype(int)
poles = []
for k in np.unique(key):
    m = pidx[key == k]
    if len(m) > 300:
        x, y = float(np.median(win.ps.x[m])), float(np.median(win.ps.y[m]))
        uu, vv = to_box_frame(np.array([x]), np.array([y]), win.box["cx"], win.box["cy"], win.box["bearing"])
        poles.append((abs(uu[0]) + 0.3 * abs(vv[0]), x, y, float(uu[0]), float(vv[0]), m))
poles.sort(key=lambda t: t[0])
_, PX, PY, PU, PV, best = poles[0]
ZLO, ZHI = float(win.ps.z[best].min()), float(win.ps.z[best].max())
print("poles", [(round(p[3], 1), round(p[4], 1)) for p in poles])


def zoom_slice(width_m=36.0):
    sv = win.slice
    sv.scale = sv.width() / width_m
    sv.center_on(PU if win.slice_pane.along else PV, (ZLO + ZHI) / 2 + 1.0)
    harness.settle(4)


win.slice_pane.thick.setValue(1.0)
win._slice_at(PX, PY)
zoom_slice()
grab_in(win.docks["slice"], "slice.png")
win.slice_pane._rotate()
win._slice_at(PX, PY)
zoom_slice(60.0)
grab_in(win.docks["slice"], "slice_profile.png")
win.slice_pane._rotate()
win._slice_at(PX, PY)
zoom_slice()
grab(panel, "main_window.png")

# ---- Plan view: the Inspection Area under the mouse shows its handles
cor = rect_corners(win.box["cx"], win.box["cy"], win.box["length"], win.box["width"], win.box["bearing"])
mx, my = (cor[1] + cor[2]) / 2
sx, sy = win.plan.to_screen(mx, my)
mouse(win.plan, "move", sx, sy)
grab_in(win.docks["plan"], "plan.png")

# ---- Plan grid: Coverage against a target density (the whole project, 1 m cells)
mouse(win.plan, "move", 5, 5)
win.sp_cell.setValue(1.0)
win.plan_pane.grid.setCurrentText("Coverage")
win.plan_pane.target.setValue(100)
win.plan.zoom_project()
grab_in(win.docks["plan"], "coverage.png")
win.plan_pane.grid.setCurrentText("Mean height")
win.sp_cell.setValue(0.5)
win.plan.zoom_box()
mouse(win.plan, "move", sx, sy)
cx, cy = win.plan.to_screen(win.box["cx"] - 8, win.box["cy"] + 6)
mouse(win.plan, "move", cx, cy)
expect("menu", "plan_menu.png")
mouse(win.plan, "press", cx, cy, Qt.RightButton)
mouse(win.plan, "release", cx, cy, Qt.RightButton)
SAVED_BOX = dict(win.box)
SAVED_POS = win.slice_pane.pos.value()
win.show_area_properties()
a = win._area
a.ge.setValue(PX + win._origin()[0])
a.gn.setValue(PY + win._origin()[1])
a.follow.setCurrentIndex(1)                 # follow L1: the area turns with the line, chainage shown
win.step("fwd")
win.step("back")
grab(a, "area_props.png")
a.follow.setCurrentIndex(0)
a.close()
win.set_box(SAVED_BOX, load=True)
win.slice_pane.pos.setValue(SAVED_POS)
win.plan.zoom_box()
zoom_slice()
assert len(win.ps) and int(best.max()) < len(win.ps)

# ---- 3D view and its navigation arrows
grab_in(win.docks["3d"], "view3d.png")
harness.grab(win.v3d, os.path.join(FIG, "nav_arrows.png"), scale=4)
MADE.append("nav_arrows.png")
Image.open(os.path.join(FIG, "nav_arrows.png")).crop((0, 0, 4 * 66, 4 * 66)).save(os.path.join(FIG, "nav_arrows.png"))

# ---- automatic filters: feature extraction, previewed and accepted
fp = win.filters
win.show_dock("filters")
fp.cmb.setCurrentIndex([g.name for g in fp.groups].index("Feature extraction (road and street furniture)"))
fp.cmb_f.setCurrentIndex(5)
grab_in(win.docks["filters"], "filters.png")
win.plan_pane.colour.setCurrentText("Classification")
win.v3d_pane.colour.setCurrentText("Classification")
win.slice_pane.link.setCurrentIndex(win.slice_pane.link.findData("plan"))
fp.btn_prev.setChecked(True)
harness.wait(lambda: win.preview is not None or "failed" in fp.lbl_status.text(), 900)
harness.settle(10)
print("preview:", fp.lbl_status.text())
grab(panel, "preview_features.png")
fp.btn_accept.click()
harness.settle(10)
print("accept:", fp.lbl_status.text())

# ---- colours: Plan by classification, Slice linked to the Plan, 3D by intensity
win.v3d_pane.colour.setCurrentText("Intensity")
harness.settle(10)
grab_union([win.docks["plan"], win.docks["slice"]], "colours.png")
grab_in(win.docks["3d"], "view3d_intensity.png")
win.v3d_pane.colour.setCurrentText("Classification")

# ---- a preview that rejects and re-accepts points
fp.cmb.setCurrentIndex([g.name for g in fp.groups].index("Snow and rain"))
win.plan_pane.colour.setCurrentText("Height")
fp.btn_prev.setChecked(True)
harness.wait(lambda: win.preview is not None or "failed" in fp.lbl_status.text(), 900)
harness.settle(10)
print("preview 2:", fp.lbl_status.text())
grab_in(win.docks["plan"], "preview_reject.png")
fp.btn_prev.setChecked(False)
harness.settle(6)

# ---- group menu, add-a-filter menu, help, tuning
expect("menu", "group_menu.png", open_sub="Add a ready-made group")
fp._group_menu()
expect("menu", "add_menu.png", open_sub="Features")
fp._add_menu()
fp.cmb.setCurrentIndex([g.name for g in fp.groups].index("Feature extraction (road and street furniture)"))
fp.cmb_f.setCurrentIndex(5)
from cloudpro.ui import chrome                             # noqa: E402


def full_height(d):
    d.resize(max(d.width(), 560), d.sizeHint().height())


helps = fp.findChildren(chrome.HelpButton)
expect("dialog", "filter_help.png", result=0, setup=full_height)
helps[1].show_help()
expect("dialog", "tune.png", result=0)
win.tune_filters()

# ---- manual editing in the Slice view
sv = win.slice
win.set_mode("select")
win.set_tool("rect")
x0, y0 = sv.to_screen(PV - 1.0, ZHI + 0.5)
x1, y1 = sv.to_screen(PV + 1.0, ZLO + 0.3)
mask = sv.selection_mask([(x0, y0), (x1, y1)], "rect")
win.edit_selection(sv, mask)
grab_in(win.docks["slice"], "select.png")
expect("menu", "selection_menu.png", open_sub="Selection")
mouse(sv, "press", (x0 + x1) / 2, (y0 + y1) / 2, Qt.RightButton)
win.clear_selection()
win.set_mode("reject")
# below a line, while it is drawn
win.set_tool("below")
zg = float(np.percentile(win.ps.z[np.abs(to_box_frame(win.ps.x, win.ps.y, win.box["cx"], win.box["cy"],
                                                     win.box["bearing"])[0] - PU) < 1], 5))
pts = [sv.to_screen(PV - 14, zg - 0.25), sv.to_screen(PV - 2, zg - 0.2)]
for x, y in pts:
    mouse(sv, "press", x, y, Qt.LeftButton)
    mouse(sv, "release", x, y, Qt.LeftButton)
mouse(sv, "move", *sv.to_screen(PV + 12, zg - 0.3))
grab_in(win.docks["slice"], "line_tool.png")
win.set_tool("measure")
bx, by = sv.to_screen(PV, ZLO)
tx, ty = sv.to_screen(PV, ZHI)
for x, y in ((bx, by), (tx, ty)):
    mouse(sv, "press", x, y, Qt.LeftButton)
    mouse(sv, "release", x, y, Qt.LeftButton)
grab_in(win.docks["slice"], "measure.png")
win.set_tool("pan")

# ---- Lines · Classes · Info, History (the right-hand windows made wider, as by dragging the edge)
win.resizeDocks([win.docks["plan"], win.docks["3d"]], [560, 940], Qt.Horizontal)
j = int(best[np.argmax(win.ps.z[best])])
win.show_point(j)
grab_in(win.docks["info"], "info_tab.png")
win.show_dock("history")
grab_in(win.docks["history"], "history.png")
win.resizeDocks([win.docks["plan"], win.docks["3d"]], [750, 750], Qt.Horizontal)

# ---- Project Properties
win.show_properties()
pp = win._props
for k, n, h in ((0, "props_info.png", 760), (1, "props_geo.png", 330), (2, "props_cfg.png", 760),
                (3, "props_files.png", 300)):
    pp.resize(860, h)                       # the window made as tall as its tab needs
    pp.tabs.setCurrentIndex(k)
    grab(pp, n)
pp.close()

# ---- Publish and Export
expect("dialog", "publish_dialog.png", result="accept", setup=lambda d: d.dem_on.setChecked(True))
expect("dialog", "publish_done.png", result=OK)
win.publish_dialog()
harness.wait(lambda: not win._busy, 900)
harness.settle(6)
expect("dialog", "export_dialog.png", result=0)
win.export()

# ---- the import dialog of an older stand-alone project (shown here with the job's two captures)
import glob                                              # noqa: E402
from cloudpro.ui.dialogs import ImportDialog              # noqa: E402

d = ImportDialog(win, JOB)
d._add_paths(sorted(glob.glob(os.path.join(JOB, "*", "clraw", "*.clraw"))))
d.show()
d.resize(780, 960)
grab(d, "import_dialog.png")
d.close()

print("remaining plan:", PLAN)
print("figures:", sorted(set(MADE)))
leftover = sorted(set(os.listdir(FIG)) - set(MADE))
print("not made by this run:", leftover)

import os as _os
_os._exit(0)          # Qt can crash tearing down at interpreter exit outside QGIS
