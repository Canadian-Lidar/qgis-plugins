#!/usr/bin/env python3
"""Recreate every figure of the PosPro user guide from scratch with the real PosPro windows.

    QT_QPA_PLATFORM=offscreen python3 guides/pospro/shots.py

Starts from an empty settings folder and no project: writes a simulated mapper 2.10 job (simjob.py: an
8-minute drive with a float spell, a DGNSS jump and a short GNSS gap, plus a base RINEX file), creates the
DCS project through the New DCS project dialog, reads the navigation recording, adds the base, processes
(real-time mode), checks the antennas, uses the Trajectory editor and Trajectory Review, and exports,
grabbing each page, menu and dialog on the way. Figures go to guides/pospro/fig/. Re-runnable.
"""
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GUIDES = os.path.dirname(HERE)
WORK = os.path.dirname(GUIDES)
ROOT = os.environ.get("POSPRO_GUIDE_JOB", os.path.join(os.path.expanduser("~"), "pospro_guide_job"))
JOB = os.path.join(ROOT, "Shirley_Ave")
SETTINGS = os.path.join(ROOT, "settings")
FIG = os.path.join(HERE, "fig")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, GUIDES)
import harness  # noqa: E402

# ---------------------------------------------------------------- clean start: a fresh simulated job
sys.path.insert(0, HERE)
import simjob  # noqa: E402
shutil.rmtree(ROOT, ignore_errors=True)
SIM = simjob.make(ROOT)
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
pp = harness.load("PosPro", iface)
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
    """The whole page k (its key, e.g. "base"; content only, no sidebar), at its full height."""
    win.goto_page(k)
    pg = win.page[k]
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
    d.name.setText("Shirley_Ave")
    d.resize(640, 260)
    d.show()
    harness.settle(8)
    harness.grab(d, fig("new_project.png"))
    d.create()
    d.hide()
    return d.project


dialogs.new_project = new_project
size(W_FULL, 900)
# a mapper 2.10 job: PosPro asks navigation or survey lines; grab it and answer Navigation
from dcs_core import theme as dtheme  # noqa: E402
_choice = dtheme.msg_choice


def _msg_choice(parent, title, text, choices, default=0):
    mb = QMessageBox(QMessageBox.Icon.Question, title, text, QMessageBox.StandardButton.NoButton, parent)
    for i, label in enumerate(choices):
        mb.addButton(label, QMessageBox.ButtonRole.RejectRole if i == len(choices) - 1 else
                     QMessageBox.ButtonRole.AcceptRole)
    BOX_PLAN.insert(0, ("choose_rover.png", QMessageBox.StandardButton.Ok))
    mb.exec()
    return 0


dtheme.msg_choice = _msg_choice
import PosPro.ui.dock as _dockmod  # noqa: E402
_dockmod.theme.msg_choice = _msg_choice
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
pg = page_shot("data", "_data.png")
p0 = win.page["data"]
_, by, _, bh = rect_in(p0.b_read, p0)
crop(fig("_data.png"), fig("data_files.png"), (0, 0, p0.width(), by + bh + 6))
cov_section = p0.cov.parentWidget().parentWidget()
_, cy, _, _ = rect_in(cov_section, p0)
crop(fig("_data.png"), fig("data_coverage.png"), (0, cy - 4, p0.width(), p0.height() - cy))
trim(fig("data_coverage.png"))


# ---------------------------------------------------------------- 4. installation
p2 = win.page["install"]
from PosPro.ui.widgets import Section  # noqa: E402
for s in p2.findChildren(Section):
    s.btn.setChecked(True)
page_shot("install", "installation.png")

# antenna check: the simulated drive has no consistent raw GNSS, so the check is shown on an analysis of a
# drive like it with the antennas plugged in the wrong way round (as tests/antennas_ui_test.py does)
import math  # noqa: E402
import random  # noqa: E402
from PosPro.core import antennas as _ant  # noqa: E402
from PosPro.ui import antennacheck as _antc  # noqa: E402
_inst = win.project.cfg["installation"]
_l1, _l2 = _inst["lever_antenna"], _inst["lever_antenna2"]
random.seed(3)
_sols, _rt = [], []
for _i in range(800):
    _t, _c = 1000.0 + _i * 0.125, math.radians(35 + 25 * math.sin(_i / 300.0))
    _b = [-(_l2[j] - _l1[j]) for j in range(3)]
    _sols.append((_t, _b[0] * math.sin(_c) + _b[1] * math.cos(_c) + random.gauss(0, .004),
                  _b[0] * math.cos(_c) - _b[1] * math.sin(_c) + random.gauss(0, .004), -_b[2], 1))
    _rt.append((_t, 11 * math.cos(_c), 11 * math.sin(_c), _c + math.pi, 0.001))
ANT_RES = _ant.analyse(_sols, _rt, _l1, _l2)
_d = _antc.AntennaDialog(ANT_RES, win, on_swap=lambda: None)
_d.resize(640, 600)
_d.show()
harness.settle(8)
harness.grab(_d, fig("antenna_check.png"))
_d.hide()

# ---------------------------------------------------------------- 3. base: add the base RINEX, approximate position
win.goto_page("base")
pb = win.page["base"]
pb.add_base_file([os.path.join(SIM["base"], "RCH10010.26O")])
harness.settle(6)
page_shot("base", "base.png")

# ---------------------------------------------------------------- 5. processing settings (tightly coupled)
p3 = win.page["process"]
for s in p3.findChildren(Section):
    s.btn.setChecked(True)
page_shot("process", "processing.png")

# choose the real-time mode
for b in p3.mode.buttons():
    if b.property("key") == "rt":
        b.setChecked(True)
p3.store()
win.goto_page("process")
harness.settle(6)
mg = p3.mode.buttons()[0].parentWidget()
harness.grab(mg, fig("processing_rt.png"))

# ---------------------------------------------------------------- 6. process
win.run_processing()
harness.settle(10)
page_shot("quality", "quality.png", 900)

# overview window and header, on the Quality page after processing
win.toast_w.hide()              # toasts hide after 6 s; the harness runs no timers
size(W_FULL, 760)
win.goto_page("quality")
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
p5 = win.page["review"]
win.goto_page("review")
p5.detect()
harness.settle(6)
page_shot("review", "review_found.png")

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
page_shot("review", "review.png")

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
win.page["quality"].refresh()

# ---------------------------------------------------------------- 7b. the Trajectory editor
import numpy as np  # noqa: E402
p5.undo()                                    # back to the trajectory as processed
win.save()
win.open_editor()
ed = pp.editor
ed.resize(1500, 940)
ed.widget().resize(1500, 940)
harness.settle(10)
tr = ed.track
t0 = float(tr.t[0])
# the longest place on the review list (the DGNSS spell with the sideways jump)
cands = [i for i, r in enumerate(ed.rows) if r[2] - r[1] > 10]
ed.table.selectRow(cands[-1] if cands else 0)
harness.settle(8)
harness.grab(ed.widget(), fig("editor_found.png"))
ed.method.setCurrentIndex(0)
ed.c_v.setChecked(False)
ed.decide("interpolate")
harness.settle(10)
ed.c_v.setChecked(True)
harness.grab(ed.widget(), fig("editor_fixed.png"))
# the antennas along the track, and what a check finds on a drive recorded with them swapped
ed.c_ant.setChecked(True)
_l1, _l2 = win.project.cfg["installation"]["lever_antenna"], win.project.cfg["installation"]["lever_antenna2"]
_sols, _rt = [], []
for _i in range(0, len(tr.t), 5):
    if tr.speed[_i] < 4:
        continue
    _c = float(tr.course[_i])
    _b = [-(_l2[j] - _l1[j]) for j in range(3)]
    _sols.append((float(tr.t[_i]), _b[0] * math.sin(_c) + _b[1] * math.cos(_c),
                  _b[0] * math.cos(_c) - _b[1] * math.sin(_c), -_b[2], 1))
    _rt.append((float(tr.t[_i]), tr.speed[_i] * math.cos(_c), tr.speed[_i] * math.sin(_c), _c + math.pi, 0.001))
win.antenna_check = _ant.analyse(_sols, _rt, _l1, _l2)
ed.set_antenna_check(win.antenna_check)
k = int(np.searchsorted(tr.t, t0 + 150))
ed.plan.cx, ed.plan.cy, ed.plan.scale = float(tr.fe[k]), float(tr.fn[k]), 60.0
ed.plan.mark_i = k
ed._plan_view_changed()
harness.grab(ed.widget(), fig("editor_antennas.png"))
ed.c_ant.setChecked(False)
ed.plan.measured = None
win.antenna_check = None
ed.plan.mark_i = -1
ed.colour.setCurrentIndex(1)
ed.zoom_all()
harness.grab(ed.widget(), fig("editor_fix.png"))
ed.colour.setCurrentIndex(0)
ed.hide()

# ---------------------------------------------------------------- 8. calibration
page_shot("calib", "calibration.png", 900)
BOX_PLAN.append(("calib_needs_tc.png", QMessageBox.StandardButton.Ok))
win.page["calib"].run_levers()

# ---------------------------------------------------------------- 9. export
p7 = win.page["export"]
e = win.project.cfg["export"]           # the page shows the saved export settings when it opens
e.update({"ascii": True, "gpx": True, "gpkg": False,
          "ascii_cols": "date_time_utc,lat,lon,h,roll,pitch,heading", "ascii_sep": ","})
win.save()
page_shot("export", "export.png")
p7.run()                        # writes the files (checked in the guide text, not shown)
harness.settle(6)

# ---------------------------------------------------------------- the workflow steps at the end
win.toast_w.hide()
size(W_FULL, 760)
win.goto_page("export")
harness.grab(win, fig("_end.png"))
sx, sy, sw, sh = rect_in(win.step_buttons[0].parentWidget(), win)
crop(fig("_end.png"), fig("steps.png"), (sx, sy, sw, win.step_buttons[-1].geometry().bottom() + 10))

# ---------------------------------------------------------------- a mapper 2.10 project: lines or navigation?
# /jobs210/Deck_Test is the simulated job rewritten by the mapper 2.10 writer (lines/, nav/; see make210.py)
JOB210 = os.environ.get("JOB210", "/jobs210/Deck_Test")
if os.path.isdir(JOB210):
    from PosPro.ui.dock import find_captures
    BOX_PLAN.append(("choose_rover.png", QMessageBox.StandardButton.Cancel))
    win._choose_rover_files(JOB210, find_captures(JOB210))

strip_tmp()
print("figures:", sorted(os.listdir(FIG)))
print("messages:", iface.messages)
print("export:", p7.out.text())
