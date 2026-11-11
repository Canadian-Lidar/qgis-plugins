"""AlignPro's Pairs page with CloudPro's calibration findings (AlignPro 1.8.0): the "From the point cloud" group lists
the pairs, says what they point at, and "Use these lines and their overlap" keeps only that pair's passes and sets
an area of interest along the stretch they share.

    QT_QPA_PLATFORM=offscreen python3 AlignPro/tests/cloudcheck_ui_test.py [figure folder]   (from the plugins folder)
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGINS = os.path.dirname(os.path.dirname(HERE))
FIG = sys.argv[1] if len(sys.argv) > 1 else None
sys.path.insert(0, PLUGINS)
sys.path.insert(0, os.path.join(PLUGINS, "guides"))

# AlignPro loads as "alignpro" (as QGIS installs it): a lower-case folder of links
LC = tempfile.mkdtemp(prefix="lc_")
for name in os.listdir(PLUGINS):
    src = os.path.join(PLUGINS, name)
    if os.path.isdir(src) and not name.startswith("."):
        os.symlink(src, os.path.join(LC, name.lower() if name in ("AlignPro", "CloudPro", "PosPro") else name))

import faulthandler  # noqa: E402
faulthandler.dump_traceback_later(500, repeat=False)
import harness  # noqa: E402

app, iface = harness.start(LC, tempfile.mkdtemp())

# no one is here to answer questions: every message box says what it asked and gets "No" / its default
from qgis.PyQt import QtWidgets  # noqa: E402

_MB = QtWidgets.QMessageBox


def _say(*a, **k):
    print("DIALOG:", [str(x)[:80] for x in a[1:3]])
    return _MB.StandardButton.No


for _n in ("question", "information", "warning", "critical"):
    setattr(_MB, _n, staticmethod(_say))
_MB.exec = lambda self: (print("DIALOG:", self.text()[:80]), _MB.StandardButton.No)[1]
# AlignPro's background jobs run as QGIS tasks: start them from the event loop (as guides/alignpro/shots.py does)
from qgis.core import QgsApplication  # noqa: E402
from qgis.PyQt import QtCore  # noqa: E402

_tm = QgsApplication.taskManager()
_sync = type(_tm).addTask
_tm.addTask = lambda t: (QtCore.QTimer.singleShot(0, lambda: _sync(_tm, t)), 1)[1]
from AlignPro.tests import cloudcheck_test as CT  # noqa: E402

if not os.path.isdir(CT.SRC):
    print("SKIP: %s not here" % CT.SRC)
    sys.exit(0)
tmp, job, r = CT.prepare()

ap = harness.load("alignpro", iface)
ap.toggle(True)
d = ap.dock
from alignpro.ui import maplayers as ml  # noqa: E402


class StubLayers(ml.MapLayers):
    def update_all(self, project, zoom=False):
        pass

    def update_grid(self, project, attr=None):
        pass

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


d.layers = StubLayers(iface)


def idle():
    harness.settle()
    harness.wait(lambda: d.job is None, 3600)
    harness.settle(10)


d.open_project(os.path.join(job, "Job_Oct20.dcsproj"))
idle()
d.resize(1400, 1000)
d.goto_page(3)
idle()
pg = d.pg_pairs
assert not pg.cloud_group.isHidden(), "From the point cloud group hidden"
n = pg.cloud_tbl.rowCount()
assert n == len(r.pairs), (n, len(r.pairs))
row = next(i for i in range(n) if pg.cloud_tbl.item(i, 7).text() == "Calibration")
pg.cloud_tbl.selectRow(row)
harness.settle()
assert "latency" in pg.cloud_why.text() and pg.b_cloud.isEnabled(), pg.cloud_why.text()
print("Pairs page: %d pairs from the point cloud; row %d: %s" % (n, row, pg.cloud_tbl.item(row, 0).text()))
if FIG:
    sa = d.stack.currentWidget()
    harness.grab(d, os.path.join(FIG, "alignpro_cloud.png"))
pg.b_cloud.click()
idle()
used = [ln.name for ln in d.p.lines if ln.use]
assert len(used) == 2 and d.p.cfg.get("aoi") and d.p.settings.get("use_aoi"), (used, bool(d.p.cfg.get("aoi")))
assert d.p.P is not None and len(d.p.P) >= 6
print("used: passes %s, area of interest set, %d patches" % (", ".join(used), len(d.p.P)))
ap.unload()
import shutil  # noqa: E402
shutil.rmtree(tmp, ignore_errors=True)
print("OK")
