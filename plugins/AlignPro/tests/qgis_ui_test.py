"""Headless QGIS test of the AlignPro plugin: menu, project load, every page, AutoPatch, apply, analyze,
report, LAS, line toggle, unload. Screenshots of each page go to OUTDIR.
On a DCS project built from simulated captures (tests/dcs_fixture.py): chain bar, publishing, staleness.
Run:  QT_QPA_PLATFORM=offscreen python3 tests/qgis_ui_test.py SIM_FOLDER WORK_FOLDER OUTDIR"""
import faulthandler
import os
import struct
import sys
import time

import numpy as np

faulthandler.dump_traceback_later(500, exit=True)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from qgis.core import QgsApplication, QgsProject  # noqa: E402
from qgis.gui import QgsMapCanvas, QgsMessageBar  # noqa: E402
from qgis.PyQt.QtCore import QSettings, Qt  # noqa: E402
from qgis.PyQt.QtWidgets import QAction, QFileDialog, QMainWindow, QMenu, QToolBar  # noqa: E402

app = QgsApplication([], True)
app.initQgis()
QSettings().remove("AlignPro/recent")


class Iface:
    def __init__(self):
        self.mw = QMainWindow()
        self.mw.resize(1600, 1000)
        self.menu = QMenu("Plugins")
        self.canvas = QgsMapCanvas(self.mw)
        self.mw.setCentralWidget(self.canvas)
        self.bar = QgsMessageBar()
        self.tb = QToolBar()

    def mainWindow(self): return self.mw
    def pluginMenu(self): return self.menu
    def pluginHelpMenu(self):
        if not hasattr(self, "_help"):
            from qgis.PyQt.QtWidgets import QMenu as _M
            self._help = _M("Plugins help")
        return self._help
    def mapCanvas(self): return self.canvas
    def messageBar(self): return self.bar
    def addToolBarIcon(self, a): self.tb.addAction(a)
    def removeToolBarIcon(self, a): self.tb.removeAction(a)
    def addDockWidget(self, area, d): self.mw.addDockWidget(area, d)
    def removeDockWidget(self, d): self.mw.removeDockWidget(d)
    def addPluginToMenu(self, t, a): self.menu.addAction(a)
    def removePluginMenu(self, t, a): self.menu.removeAction(a)


def wait(dock, timeout=600):
    t = time.time()
    while dock.job is not None and time.time() - t < timeout:
        app.processEvents()
        time.sleep(0.02)
    for _ in range(30):
        app.processEvents()
    assert dock.job is None, "job timed out"


def menu_path(menu):
    out = []
    for a in menu.actions():
        if a.menu():
            out += [a.text() + " > " + p for p in menu_path(a.menu())]
        else:
            out.append(a.text())
    return out


sim, workdir, outdir = sys.argv[1], sys.argv[2], sys.argv[3]
os.makedirs(outdir, exist_ok=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dcs_fixture  # noqa: E402
dcs0, SBET, T0 = dcs_fixture.make_job(sim, workdir)
proj = dcs0.path
iface = Iface()
pre = iface.menu.addMenu("Canadian Lidar")
pre.addAction("CLAM TidalCal")
import alignpro  # noqa: E402
from dcs_core import menu as cl_menu, registry  # noqa: E402
from dcs_core.project import DcsProject  # noqa: E402
plug = alignpro.classFactory(iface)
plug.initGui()
other = QAction("PosPro", iface.mw)
cl_menu.add(iface, other)
print("MENU:", menu_path(iface.menu))
assert registry.has("alignpro"), "AlignPro not registered with DCS Core"
assert menu_path(iface.menu) == ["Canadian Lidar > CLAM TidalCal", "Canadian Lidar > Data Collection Systems > AlignPro",
                                 "Canadian Lidar > Data Collection Systems > PosPro"], menu_path(iface.menu)
plug.toggle(True)
dock = plug.dock
iface.mw.show()
dock.open_project(proj)
wait(dock)
p = dock.p
assert p is not None and len(p.lines) >= 2, "project did not load"
print("loaded", len(p.lines), "lines; layers:", [QgsProject.instance().mapLayer(i).name() for i in dock.layers.ids.values()])
assert p.dcs is not None and p.traj_info.get("source") == "pospro", p.traj_info
print("trajectory:", p.traj.source, "| chain:", [(k, st) for k, _l, st, _r in dock.chain.items])
assert dock.chain_w.isVisibleTo(dock), "chain bar hidden"
print("steps:", [(b.title, b.state, b.subtitle) for b in dock.step_buttons])


def shot(name, w=1320, h=1000):
    dock.setFloating(True)
    dock.resize(w, h)
    dock.show()
    for _ in range(10):
        app.processEvents()
    dock.grab().save(os.path.join(outdir, name))


for k in range(7):
    dock.goto_page(k)
    shot("page_%d_%s.png" % (k + 1, dock.step_buttons[k].title.lower()))
dock.autopatch()
wait(dock)
r = p.cfg["results"]
print("ADVISED", r["advised"], "SDEV", r["sdev"])
st_cal = DcsProject.open(proj).state("alignpro")[0]
print("after auto calibration, AlignPro state:", st_cal)
assert st_cal == "none", "calibrating alone must not publish"
dock.goto_page(4)
shot("page_5_calibration_done.png")
# ---- Data View: AutoPatch loaded the advised angles as trial values over the applied ones (grey ghost)
dv = dock.dv


def settle():
    for _ in range(5):
        app.processEvents()
        time.sleep(0.04)
    app.processEvents()


def view_shot(name):
    dock.view_dock.setFloating(True)
    dock.view_dock.resize(1700, 920)
    dock.view_dock.show()
    settle()
    dock.view_dock.grab().save(os.path.join(outdir, name))


for i, nm in enumerate(("roll", "pitch", "heading")):
    dv.preset.setCurrentIndex(i)
    settle()
    view_shot("view_%d_%s_advised_vs_applied.png" % (i + 1, nm))
print("view error (advised trial):", dv.c_err.value.text(), "|", dv.c_err.label.text())
# manual nudging: start from recorded, step heading by +0.1 deg five times, time each live update
dv.preset.setCurrentIndex(2)
dv.compare.setCurrentIndex(1)
dv.load_values("recorded")
settle()
print("recorded:", dv.c_err.value.text())
dv.ang_step.setCurrentText("0.1")
times = []
for k in range(5):
    t0 = time.time()
    dv.ang["heading"].bump(1)
    dv._timer.stop()
    dv._update()
    dv.plan.repaint(); dv.profile_v.repaint(); dv.section_v.repaint()
    times.append(time.time() - t0)
    print("  heading %.4f -> %s" % (dv.ang["heading"].value(), dv.c_err.value.text()))
print("live update ms:", [round(t * 1000) for t in times])
view_shot("view_4_manual_heading_nudge.png")
# custom profile drawn in the plan (right-drag) and a section station click
c = np.array(dv.sub["center"])
dv._custom_line(c[0] - 15, c[1] + 2, c[0] + 15, c[1] + 2)
dv._set_station(12.0)
settle()
print("custom profile points:", dv.profile_v.note)
# apply the advised values from the Data View
dv.load_values("advised")
settle()
dv._apply()
wait(dock)
settle()
print("applied from Data View:", [round(v, 4) for v in p.cfg["mount"]], "dirty:", repr(dv.dirty.text()))
d = DcsProject.open(proj)
from dcs_core import handover  # noqa: E402
al = handover.read(d, "alignpro")
print("published alignment.json:", al["mount_deg"]["roll"], al["mount_deg"]["pitch"], al["mount_deg"]["heading"],
      "| state:", d.state("alignpro")[0], "| built_from pospro:", al["built_from"]["stages"].get("pospro", "")[:19])
assert d.state("alignpro")[0] == "done"
dock.goto_page(4)
shot("page_5_calibration_published.png")
view_shot("view_5_after_apply.png")
print("after apply: error %.2f cm" % dock.avg_error)
dock.pg_cal._copy()
print("clipboard:", app.clipboard().text())
dock.goto_page(5)
dock.pg_an.par.setCurrentIndex(0)
dock.pg_an._start()
wait(dock)
print("analyze:", dock.pg_an.best.text())
shot("page_6_analyze_done.png")
dock.goto_page(2)
shot("page_3_quality_loaded.png", 1320, 1400)
import alignpro.ui.dock as dk  # noqa: E402
dk.ask = lambda *a, **k: False
dk.warn = lambda *a, **k: print("WARN", a[1:])
pdf = os.path.join(p.report_dir, "Report_test_%s.pdf" % p.name)
assert dock.make_report(pdf, True, True, False)
print("pdf", os.path.getsize(pdf))
las = os.path.join(outdir, "out.las")
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (las, ""))
dock.export_las()
wait(dock)
with open(las, "rb") as f:
    h = f.read(227)
print("LAS points:", struct.unpack_from("<I", h, 107)[0])
dock.goto_page(6)
shot("page_7_report.png")
# PosPro re-processes: AlignPro shows as stale in its chain bar and on the Calibration page
dcs_fixture.publish_pospro(DcsProject.open(proj), SBET, T0, "second run")
dock.chain.refresh()
dock.pg_cal.refresh_publish()
print("after PosPro re-run:", [(k, st) for k, _l, st, _r in dock.chain.items], "|", dock.pg_cal.pub_warn.text()[:90])
assert DcsProject.open(proj).state("alignpro")[0] == "stale"
dock.goto_page(4)
shot("page_5_calibration_stale.png")
it = dock.pg_data.tbl.item(0, 0)
it.setCheckState(Qt.CheckState.Unchecked)
wait(dock)
print("line 0 use:", p.lines[0].use, "pairs:", len(p.pair_list))
print("steps:", [(b.title, b.state, b.subtitle) for b in dock.step_buttons])
plug.unload()
assert not registry.has("alignpro")
print("MENU after unload:", menu_path(iface.menu))
cl_menu.remove(iface, other)
print("MENU after both:", menu_path(iface.menu))
print("UI TEST OK")
