#!/usr/bin/env python3
"""Recreate every figure of the PosPro user guide from scratch with the real PosPro windows.

    QT_QPA_PLATFORM=offscreen python3 guides/pospro/shots.py

Starts from an empty settings folder and no project: creates the DCS project in
/jobs/pospro/Job_Oct20 through the New DCS project dialog, reads the captures,
processes them (real-time mode), runs Trajectory Review and an export, and grabs each
page, menu and dialog on the way. Figures go to guides/pospro/fig/. Re-runnable.
"""
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GUIDES = os.path.dirname(HERE)
WORK = os.path.dirname(GUIDES)
JOB = "/jobs/pospro/Job_Oct20"
SETTINGS = "/jobs/pospro/settings"
FIG = os.path.join(HERE, "fig")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, GUIDES)
import harness  # noqa: E402

# ---------------------------------------------------------------- clean start
shutil.rmtree(os.path.join(JOB, "Processing"), ignore_errors=True)
shutil.rmtree(SETTINGS, ignore_errors=True)
os.makedirs(SETTINGS, exist_ok=True)
shutil.rmtree(FIG, ignore_errors=True)
os.makedirs(FIG, exist_ok=True)

app, iface = harness.start(WORK, SETTINGS)

from PIL import Image  # noqa: E402
from qgis.PyQt.QtCore import QPoint, Qt  # noqa: E402
from qgis.PyQt.QtWidgets import QDialog, QMessageBox  # noqa: E402
import qgis.core  # noqa: E402


def fig(name):
    return os.path.join(FIG, name)


# ---------------------------------------------------------------- stand-in fixes (harness QGIS only)
def _to_proj(self):
    return {"EPSG:2953": "+proj=sterea +lat_0=46.5 +lon_0=-66.5 +k=0.999912 +x_0=2500000 +y_0=7500000 "
                         "+ellps=GRS80 +units=m +no_defs"}.get(self.authid(), "+proj=longlat +ellps=GRS80")


qgis.core.QgsCoordinateReferenceSystem.toProj = _to_proj

# Modal boxes: grab them as they open, then answer. BOX_PLAN is consumed in order.
BOX_PLAN = []


def _box_exec(self):
    name, answer = BOX_PLAN.pop(0) if BOX_PLAN else (None, QMessageBox.StandardButton.Yes)
    if name:
        self.adjustSize()
        self.show()
        harness.settle(8)
        harness.grab(self, fig(name))
        self.hide()
    return int(answer)


QMessageBox.exec = _box_exec
QMessageBox.exec_ = _box_exec

DIALOG_PLAN = {}            # window title -> (figure name, size to show it at, or None)


def _dlg_exec(self):
    name, sz = DIALOG_PLAN.get(self.windowTitle(), (None, None))
    if name:
        if sz:
            self.resize(*sz)        # as if the user made the dialog smaller
        self.show()
        harness.settle(8)
        harness.grab(self, fig(name))
        self.hide()
    return 0


QDialog.exec = _dlg_exec
QDialog.exec_ = _dlg_exec

# ---------------------------------------------------------------- plugin
pp = harness.load("pospro", iface)
pp.toggle(True)
win = pp.dock
W_FULL = 1180


def size(w, h):
    win.resize(w, h)
    harness.settle(10)


def rect_in(widget, ref):
    """Rectangle of widget inside ref (logical pixels)."""
    tl = widget.mapTo(ref, QPoint(0, 0))
    return tl.x(), tl.y(), widget.width(), widget.height()


def crop(src, dst, box, pad=0):
    """box in logical pixels (x, y, w, h); images are 2x."""
    im = Image.open(src)
    x, y, w, h = box
    im.crop((max(0, 2 * (x - pad)), max(0, 2 * (y - pad)), min(im.width, 2 * (x + w + pad)),
             min(im.height, 2 * (y + h + pad)))).save(dst)


def trim(path, margin=24):
    """Cut the empty page background below the last control."""
    im = Image.open(path).convert("RGBA")
    px = im.load()
    bg = px[im.width - 3, im.height - 3]
    last = 0
    for yy in range(im.height - 1, -1, -1):
        row = [px[xx, yy] for xx in range(0, im.width, 3)]
        if any(sum(abs(a - b) for a, b in zip(c[:3], bg[:3])) > 12 for c in row):
            last = yy
            break
    im.crop((0, 0, im.width, min(im.height, last + margin))).save(path)
    return path


def page_shot(k, name, height=None):
    """The whole page k (content only, no sidebar), at its full height."""
    win.goto_page(k)
    pg = win.pages[k]
    need = height or (pg.sizeHint().height() + 200)
    size(W_FULL, max(900, need))
    harness.settle(10)
    return trim(harness.grab(pg, fig(name)))


def strip_tmp():
    for f in os.listdir(FIG):
        if f.startswith("_"):
            os.remove(fig(f))


# ---------------------------------------------------------------- 1. new project
from dcs_core import dialogs  # noqa: E402


def new_project(parent=None, plugin="DCS", default_name=""):
    d = dialogs.NewProjectDialog(parent, plugin, default_name)
    d.job.setText(JOB)
    d.name.setText("Job_Oct20")
    d.resize(640, 260)
    d.show()
    harness.settle(8)
    harness.grab(d, fig("new_project.png"))
    d.create()
    d.hide()
    return d.project


dialogs.new_project = new_project
size(W_FULL, 900)
BOX_PLAN.append(("found_captures.png", QMessageBox.StandardButton.Yes))
win.new_project()
harness.settle(10)


# ---------------------------------------------------------------- 2. read the files
win.run_extract(force=True)
harness.settle(10)

# project menu, window menu
size(W_FULL, 860)
harness.grab_menu(win.header.project_btn.menu(), fig("project_menu.png"))
wm = win.window_menu(win.header.window_btn)
harness.grab_menu(wm, fig("window_menu.png"))

# Data page: rover list and buttons, contents box, coverage + ephemeris
pg = page_shot(0, "_data.png")
p0 = win.pages[0]
_, by, _, bh = rect_in(p0.b_read, p0)
crop(fig("_data.png"), fig("data_files.png"), (0, 0, p0.width(), by + bh + 6))
cov_section = p0.cov.parentWidget().parentWidget()
_, cy, _, _ = rect_in(cov_section, p0)
crop(fig("_data.png"), fig("data_coverage.png"), (0, cy - 4, p0.width(), p0.height() - cy))
trim(fig("data_coverage.png"))

# ---------------------------------------------------------------- 3. base page (no base yet)
page_shot(1, "base.png")

# ---------------------------------------------------------------- 4. installation
p2 = win.pages[2]
from pospro.ui.widgets import Section  # noqa: E402
for s in p2.findChildren(Section):
    s.btn.setChecked(True)
page_shot(2, "installation.png")

# ---------------------------------------------------------------- 5. processing settings (tightly coupled)
p3 = win.pages[3]
for s in p3.findChildren(Section):
    s.btn.setChecked(True)
page_shot(3, "processing.png")

# choose the real-time mode
for b in p3.mode.buttons():
    if b.property("key") == "rt":
        b.setChecked(True)
p3.store()
win.goto_page(3)
harness.settle(6)
mg = p3.mode.buttons()[0].parentWidget()
harness.grab(mg, fig("processing_rt.png"))

# ---------------------------------------------------------------- 6. process
win.run_processing()
harness.settle(10)
page_shot(4, "quality.png", 900)

# overview window and header, on the Quality page after processing
win.toast_w.hide()              # toasts hide after 6 s; the harness runs no timers
size(W_FULL, 760)
win.goto_page(4)
harness.grab(win, fig("window.png"))
x, y, w, h = rect_in(win.header, win)
crop(fig("window.png"), fig("header.png"), (x, y, w, h))

# quality plots window
win.show_plots()
pl = pp.plots
pl.resize(1300, 520)
harness.settle(10)
for idx, name in ((5, "plots_height.png"),):
    pl.tabs.setCurrentIndex(idx)
    harness.settle(6)
    harness.grab(pl, fig(name))
pl.tabs.setCurrentIndex(0)

# ---------------------------------------------------------------- 7. trajectory review
p5 = win.pages[5]
win.goto_page(5)
p5.val["sd_h"].setValue(0.015)      # tighter than the 0.017 m horizontal sigma of this trajectory
p5.store()
p5.detect()
harness.settle(6)
page_shot(5, "review_found.png")

items = p5.items


def select_rows(pred):
    p5.table.clearSelection()
    from qgis.PyQt.QtCore import QItemSelectionModel
    for i, it in enumerate(p5.items):
        if pred(it):
            p5.table.selectionModel().select(p5.table.model().index(i, 0),
                                             QItemSelectionModel.SelectionFlag.Select |
                                             QItemSelectionModel.SelectionFlag.Rows)
    harness.settle(4)


select_rows(lambda it: it["id"] == "A1")
p5.decide("poor")
select_rows(lambda it: it["id"] == "A2")
p5.decide("accept")
select_rows(lambda it: it["id"] == "A2")
page_shot(5, "review.png")

# undo both decisions, then a part chosen by hand from the plots (Plot view), interpolated; the edit log
p5.undo()
p5.undo()
qc = win.qc
t0 = qc["gps_time"][0]
for p_ in pl.plots.values():
    p_.set_xrange(t0 + 6, t0 + 10, False)
p5.select_plot_range()
select_rows(lambda it: it["what"] == "Selected by hand")
p5.decide("interpolate")
DIALOG_PLAN["Trajectory Review log"] = ("review_log.png", (560, 210))
p5.show_log()
for p_ in pl.plots.values():
    p_.reset_zoom()
pl.set_data(win.qc)
harness.settle(6)

# Quality page again after the review (poor part raises the sigma)
win.pages[4].refresh()

# ---------------------------------------------------------------- 8. calibration
page_shot(6, "calibration.png", 900)
BOX_PLAN.append(("calib_needs_tc.png", QMessageBox.StandardButton.Ok))
win.pages[6].run_levers()

# ---------------------------------------------------------------- 9. export
p7 = win.pages[7]
e = win.project.cfg["export"]           # the page shows the saved export settings when it opens
e.update({"ascii": True, "gpx": True, "gpkg": False,
          "ascii_cols": "date_time_utc,lat,lon,h,roll,pitch,heading", "ascii_sep": ","})
win.save()
page_shot(7, "export.png")
p7.run()                        # writes the files (checked in the guide text, not shown)
harness.settle(6)

# ---------------------------------------------------------------- the workflow steps at the end
win.toast_w.hide()
size(W_FULL, 760)
win.goto_page(7)
harness.grab(win, fig("_end.png"))
sx, sy, sw, sh = rect_in(win.step_buttons[0].parentWidget(), win)
crop(fig("_end.png"), fig("steps.png"), (sx, sy, sw, win.step_buttons[-1].geometry().bottom() + 10))

# ---------------------------------------------------------------- a mapper 2.10 project: lines or navigation?
# /jobs210/Deck_Test is the simulated job rewritten by the mapper 2.10 writer (lines/, nav/; see make210.py)
JOB210 = os.environ.get("JOB210", "/jobs210/Deck_Test")
if os.path.isdir(JOB210):
    from pospro.ui.dock import find_captures
    BOX_PLAN.append(("choose_rover.png", QMessageBox.StandardButton.Cancel))
    win._choose_rover_files(JOB210, find_captures(JOB210))

strip_tmp()
print("figures:", sorted(os.listdir(FIG)))
print("messages:", iface.messages)
print("export:", p7.out.text())
