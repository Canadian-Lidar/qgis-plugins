#!/usr/bin/env python3
"""Recreate every figure of the AlignPro user guide from scratch, from the real AlignPro windows.

    QT_QPA_PLATFORM=offscreen python3 shots.py

Runs the whole patch test workflow on /jobs/alignpro/Job_Oct20 from a new project: Data (SBET file),
Installation, Quality, Pairs, Calibration, Apply, Analyze, Report, Data View, publishing. The old
Processing folder and the harness settings are removed first. Figures go to fig/ next to this file.

Stand-in fixes (harness only, the plugin is not changed):
- QGIS tasks are run from the event loop (as in QGIS) instead of inside addTask, so AlignPro's
  `self.job` bookkeeping behaves as in QGIS; idle() waits for each job.
- Map layers need a real QGIS map canvas: a stub keeps only the grid summary the sidebar shows.
- Message boxes and file dialogs answer by themselves (message boxes can be grabbed on the way).
"""
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(WORK, "guides"))
FIG = os.path.join(HERE, "fig")
JOB = "/jobs/alignpro/Job_Oct20"
SETTINGS = "/jobs/alignpro/settings"
SBET = os.path.join(JOB, "sbet_20261020.out")

# ------------------------------------------------------------------ fresh start
shutil.rmtree(os.path.join(JOB, "Processing"), ignore_errors=True)
shutil.rmtree(SETTINGS, ignore_errors=True)
os.makedirs(SETTINGS)
if not os.path.exists(SBET):
    shutil.copyfile("/jobs/alignpro/sbet_20261020.out", SBET)
shutil.rmtree(FIG, ignore_errors=True)
os.makedirs(FIG)

import harness  # noqa: E402

app, iface = harness.start(WORK, SETTINGS)
from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: E402
from PIL import Image  # noqa: E402


def fig(name):
    return os.path.join(FIG, name)


def crop(name, box):
    """Crop a 2x figure; box in logical pixels (left, top, right, bottom)."""
    p = fig(name)
    im = Image.open(p)
    im.crop(tuple(int(v * 2) for v in box)).save(p)


# ------------------------------------------------------------------ dialogs answer by themselves
MB = QtWidgets.QMessageBox
mb_grab = {}            # text fragment -> figure name


def _mb_exec(self):
    self.adjustSize()
    self.show()
    harness.settle()
    for frag, name in list(mb_grab.items()):
        if frag in self.text():
            harness.grab(self, fig(name))
            del mb_grab[frag]
    self.hide()
    return MB.Yes if (self.standardButtons() & MB.Yes) else MB.Ok


MB.exec = _mb_exec
MB.exec_ = _mb_exec
answers = {"open": "", "save": "", "dir": ""}
QtWidgets.QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (answers["open"], ""))
QtWidgets.QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (answers["save"], ""))
QtWidgets.QFileDialog.getExistingDirectory = staticmethod(lambda *a, **k: answers["dir"])
QtGui.QDesktopServices.openUrl = staticmethod(lambda *a: True)

import dcs_core.dialogs as dd  # noqa: E402


def _new_project(parent=None, plugin="DCS", default_name=""):
    d = dd.NewProjectDialog(parent, plugin, default_name)
    d.job.setText(JOB)
    d.name.setText("Job_Oct20")
    d.resize(640, 260)
    d.show()
    harness.settle()
    harness.grab(d, fig("new_project.png"))
    d.create()
    d.hide()
    return d.project


dd.new_project = _new_project

# ------------------------------------------------------------------ tasks from the event loop
from qgis.core import QgsApplication  # noqa: E402

_tm = QgsApplication.taskManager()
_sync = type(_tm).addTask


def _later(t):
    QtCore.QTimer.singleShot(0, lambda: _sync(_tm, t))
    return 1


_tm.addTask = _later

# ------------------------------------------------------------------ map layers stub
from alignpro.core import engine as _eng  # noqa: E402
from alignpro.ui import maplayers as ml  # noqa: E402


class StubLayers(ml.MapLayers):
    def update_all(self, project, zoom=False):
        self.update_grid(project)

    def update_grid(self, project, attr=None):
        g = _eng.grid_stats(project.lines, float(project.settings.get("grid_cell_m", 0.5)))
        project.grid_summary = _eng.grid_summary(g)

    def update_tracks(self, project):
        pass

    def update_boxes(self, project):
        pass

    def update_patches(self, project):
        pass

    def update_aoi(self, project):
        pass

    def remove_all(self):
        pass


# ------------------------------------------------------------------ start AlignPro
ap = harness.load("alignpro", iface)
ap.toggle(True)
d = ap.dock
d.layers = StubLayers(iface)
W = 1400


def idle():
    harness.settle()
    harness.wait(lambda: d.job is None, 3600)
    harness.settle(10)


def size(h=900, w=W):
    d.resize(w, h)
    harness.settle()


def page(i, name, h=900, top=None, bottom=None, scroll=0, content=True):
    """Grab the window on step page i. content: crop to the page itself (no title bar, header or
    sidebar), down to the end of its content. Else an optional vertical crop (logical px)."""
    d.goto_page(i)
    size(h)
    sa = d.stack.currentWidget()
    sa.verticalScrollBar().setValue(scroll)
    harness.settle()
    harness.grab(d, fig(name))
    if content:
        st = d.stack.mapTo(d, QtCore.QPoint(0, 0))
        pg_ = d.pages[i]
        end = st.y() + pg_.layout().sizeHint().height() - scroll + 4
        crop(name, (st.x() - 6, st.y() - 4, W - 4, min(end, st.y() + sa.height())))
    elif top is not None or bottom is not None:
        crop(name, (0, top or 0, W, bottom or h))


def with_menu(menu, button, name, pad=24):
    """The window's top with a menu open under its button (the menu grabbed as it pops up)."""
    harness.grab(d, fig("_win.png"))
    harness.grab_menu(menu, fig("_menu.png"))
    win, mi = Image.open(fig("_win.png")), Image.open(fig("_menu.png"))
    os.remove(fig("_win.png"))
    os.remove(fig("_menu.png"))
    b = button.mapTo(d, QtCore.QPoint(0, button.height()))
    x = min(b.x() * 2, win.width - mi.width - 8)
    y = b.y() * 2 + 4
    win.paste(mi, (x, y))
    left = min(x - pad * 2, win.width - 1400)
    win.crop((left, 0, win.width, y + mi.height + pad * 2)).save(fig(name))


size()
harness.grab(d, fig("welcome.png"))                                            # no project

# ------------------------------------------------------------------ 1 new project, Data
mb_grab["Found 2 capture"] = "found_captures.png"
d.new_project()
idle()
pg = d.pg_data
answers["open"] = SBET
pg.traj._browse()                                                              # Browse... picks the SBET
pg.read()
idle()

# ------------------------------------------------------------------ before calibration
page(1, "installation.png", 960)

# settings dialog: the Passes and pairs, and Patches tabs
from alignpro.ui import dialogs as apdlg  # noqa: E402
from alignpro.ui.common import themed  # noqa: E402

sd = apdlg.SettingsDialog(d.p.settings, d)
themed(sd)
sd.resize(600, 440)
sd.show()
tabs = sd.findChild(QtWidgets.QTabWidget)
for k, nm in ((1, "passes"), (2, "patches")):
    tabs.setCurrentIndex(k)
    harness.settle()
    harness.grab(sd, fig("settings_%s.png" % nm))
sd.hide()

# free profile window (the box comes from Place boxes automatically)
d.auto_boxes()
box = dict(d.p.cfg["boxes"]["pitch"])
d._profile(dict(box))
harness.settle()
prof = [w for w in QtWidgets.QApplication.topLevelWidgets() if w.windowTitle() == "AlignPro – Profile"
        and w.isVisible()][0]
prof.resize(960, 440)
harness.grab(prof, fig("free_profile.png"))
prof.close()

# ------------------------------------------------------------------ 5 calibration
d.autopatch()
idle()
dv = d.view_dock
dv.resize(1600, 950)
harness.settle(20)
idle()                                         # full-density points for the boxes (background job)
harness.settle(20)
harness.grab(dv, fig("dataview_advised.png"))                                  # opens after Auto calibrate
page(4, "calibration.png", 760)
crop("calibration.png", (0, 0, 1205, 524))

# combinations right-click menu
tbl = d.pg_cal.tbl
tbl.selectRow(0)
menu_pos = tbl.visualItemRect(tbl.item(0, 2)).center()
_menu_exec = QtWidgets.QMenu.exec


def _grab_menu(self, *a):
    self.adjustSize()
    self.popup(QtCore.QPoint(200, 200))
    harness.settle()
    harness.grab(self, fig("combi_menu.png"))
    self.hide()
    return None


QtWidgets.QMenu.exec = _grab_menu
d.pg_cal._menu(menu_pos)
QtWidgets.QMenu.exec = _menu_exec

# apply the advised angles: publishes alignment.json
d.pg_cal._apply_adv()
idle()
page(4, "calibration_published.png", 1060, scroll=330)
page(0, "data.png", 1500)

# quality and pairs with the calibration applied
qc = d.pg_qc
d.goto_page(2)
for s_ in qc.findChildren(QtWidgets.QToolButton):
    if s_.objectName() == "ppSectHead":
        s_.setChecked(s_.text() in ("TIME SYNC AND RATES", "HEADING VS COURSE", "RESIDUALS"))
page(2, "quality.png", 1500)
page(3, "pairs.png", 820)

# ------------------------------------------------------------------ Data View
d.dv.load_values("applied")
d.dv.set_active("heading")                                                     # heading box active
d.dv.band.setChecked(True)
harness.settle(20)
harness.grab(dv, fig("dataview_heading.png"))
d.dv.band.setChecked(False)
d.dv.set_active("pitch")                                                       # pitch box, a trial value
d.dv.compare.setCurrentIndex(2)
d.dv.colour.setCurrentIndex(1)
d.dv.ang["pitch"].bump(+1)
d.dv.ang["pitch"].bump(+1)
d.dv._update()
harness.settle(20)
harness.grab(d.dv._split.widget(2), fig("dataview_panel.png"))
d.dv.colour.setCurrentIndex(0)
d.dv.load_values("applied")
harness.settle(10)

# misalignment as seen in the pitch box view: recorded angles, then the advised ones
import numpy as np  # noqa: E402
d.dv.compare.setCurrentIndex(0)
d.dv.colour.setCurrentIndex(0)
parts = []
for which in ("recorded", "advised"):
    d.dv.load_values(which)
    d.dv._update()
    pv = d.dv.views["pitch"]
    pv.resize(520, 420)
    y = pv.y
    hi = y > np.percentile(y, 30) + 1.0
    xm = float(np.median(pv.x[hi]))
    pv.set_view(xm - 0.35, xm + 0.35, float(np.percentile(y, 1)) - 0.3, float(np.percentile(y, 99.9)) + 0.3)
    harness.settle(5)
    nm = "_cmp_%s.png" % which
    harness.grab(pv, fig(nm))
    parts.append(Image.open(fig(nm)))
    os.remove(fig(nm))
gap = 24
im = Image.new("RGB", (sum(p.width for p in parts) + gap, max(p.height for p in parts)), (255, 255, 255))
im.paste(parts[0], (0, 0))
im.paste(parts[1], (parts[0].width + gap, 0))
im.save(fig("dataview_compare.png"))
d.dv.load_values("applied")
d.dv.compare.setCurrentIndex(2)
d.dv.set_active("heading")
dv.resize(1600, 950)
harness.settle(10)

# ------------------------------------------------------------------ 6 analyze
pa = d.pg_an
d.goto_page(5)
pa.par.setCurrentIndex([k for k, *_ in pa.PARAMS].index("heading"))
pa.rng.setValue(0.3)
pa._start()
idle()
page(5, "analyze.png", 800)

# ------------------------------------------------------------------ 7 report
pr = d.pg_rep
d.goto_page(6)
for k, v in (("vehicle", "Survey van 2"), ("operator", "M. Leblanc"), ("client", "Internal"),
             ("location", "Moncton, NB")):
    pr.fields[k].setText(v)
pr.notes.setPlainText("Patch test after the Lidar was re-mounted.")
pr.create()
idle()
page(6, "report.png", 1180)
pdfs = [f for f in os.listdir(d.p.report_dir) if f.startswith("Report_") and f.endswith(".pdf")]
if pdfs:
    import fitz
    doc = fitz.open(os.path.join(d.p.report_dir, pdfs[0]))
    pix = doc[0].get_pixmap(dpi=110)
    pix.save(fig("report_pdf.png"))
    crop("report_pdf.png", (0, 0, pix.width / 2, 360))

# ------------------------------------------------------------------ header, menus, chain
d.goto_page(4)
size(900)
harness.settle(10)
d.chain.refresh()
harness.grab(d.header, fig("header.png"))
with_menu(d.pbox.menu(), d.pbox, "project_menu.png")
with_menu(d.wbtn.menu(), d.wbtn, "window_menu.png")

# the published handover (the guide quotes an excerpt of it)
print("published:", os.path.join(d.p.folder, "out", "alignment.json"))
print("messages:", [m[1][:90] for m in iface.messages])
print("figures:", sorted(os.listdir(FIG)))
sys.stdout.flush()
os._exit(0)          # skip Qt's teardown of the stand-in QGIS objects (it can crash on exit)
