"""The user guide shipped in the plugin, tested on an install from the zip.

    QT_QPA_PLATFORM=offscreen python3 tests/guide_test.py PLUGINS_DIR

PLUGINS_DIR holds alignpro/ (unzipped from alignpro-<version>.zip) and dcs_core/, as a clean QGIS
plugins folder would. Checks: the guide for the installed version is found and opens (? button, project
box menu, Help > Plugins), the Help entry goes away on unload, the newest guide opens when the exact
one is missing, and a message shows when there is none."""
import glob
import os
import shutil
import sys

PLUGINS = os.path.abspath(sys.argv[1])
sys.path.insert(0, PLUGINS)

from qgis.core import QgsApplication  # noqa: E402
from qgis.gui import QgsMapCanvas, QgsMessageBar  # noqa: E402
from qgis.PyQt.QtCore import QSettings  # noqa: E402
from qgis.PyQt.QtGui import QDesktopServices  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow, QMenu, QMessageBox, QToolBar, QToolButton  # noqa: E402

app = QgsApplication([], True)
app.initQgis()
QSettings().remove("AlignPro/recent")

opened, told = [], []
QDesktopServices.openUrl = staticmethod(lambda url: opened.append(url.toLocalFile()) or True)
QMessageBox.warning = staticmethod(lambda *a, **k: told.append(("warn", a[2])))
QMessageBox.information = staticmethod(lambda *a, **k: told.append(("info", a[2])))


class Bar(QgsMessageBar):
    def pushMessage(self, title, text, level=None, duration=0):  # noqa: N802
        told.append(("bar", text))


class Iface:
    def __init__(self):
        self.mw = QMainWindow()
        self.mw.resize(1300, 850)
        self.menu = QMenu("Plugins")
        self.help = QMenu("Plugins help")
        self.canvas = QgsMapCanvas(self.mw)
        self.mw.setCentralWidget(self.canvas)
        self.bar = Bar()
        self.tb = QToolBar()

    def mainWindow(self): return self.mw
    def pluginMenu(self): return self.menu
    def pluginHelpMenu(self): return self.help
    def mapCanvas(self): return self.canvas
    def messageBar(self): return self.bar
    def addToolBarIcon(self, a): self.tb.addAction(a)
    def removeToolBarIcon(self, a): self.tb.removeAction(a)
    def addDockWidget(self, area, d): self.mw.addDockWidget(area, d)
    def removeDockWidget(self, d): self.mw.removeDockWidget(d)
    def addPluginToMenu(self, t, a): self.menu.addAction(a)
    def removePluginMenu(self, t, a): self.menu.removeAction(a)


def spin(n=6):
    for _ in range(n):
        app.processEvents()


import alignpro  # noqa: E402
from alignpro import guide  # noqa: E402

root = os.path.dirname(os.path.abspath(alignpro.__file__))
assert root == os.path.join(PLUGINS, "alignpro"), "loaded from %s, not the clean install" % root
version = guide.plugin_version(root)
docs = os.path.join(root, "docs")
want = os.path.join(docs, "AlignPro_User_Guide_%s.pdf" % version)
print("installed AlignPro", version, "from", root)
print("guides in docs/:", [os.path.basename(p) for p in glob.glob(os.path.join(docs, "*.pdf"))])

# 1 the guide for this version is found
path, exact = guide.guide_path(root)
assert path == want and exact, (path, exact)
with open(path, "rb") as f:
    assert f.read(5) == b"%PDF-", "not a PDF"
print("1 found", os.path.basename(path))

iface = Iface()
iface.mw.show()
plug = alignpro.classFactory(iface)
plug.initGui()

# 2 Help > Plugins > AlignPro user guide
helps = [a for a in iface.help.actions() if a.text() == "AlignPro user guide"]
assert len(helps) == 1, [a.text() for a in iface.help.actions()]
helps[0].trigger()
assert opened == [want], opened
print("2 Help > Plugins > AlignPro user guide opens it")

# 3 the ? button beside the window button, same style
plug.toggle(True)
spin()
d = plug.dock
assert d.hbtn.text() == "?" and d.hbtn.objectName() == d.wbtn.objectName() == "ppWinBtn"
assert d.hbtn.size() == d.wbtn.size(), (d.hbtn.size(), d.wbtn.size())
assert d.hbtn.parentWidget() is d.wbtn.parentWidget()
lay = d.wbtn.parentWidget().layout()
idx = [lay.itemAt(i).widget() for i in range(lay.count())]
assert idx.index(d.hbtn) + 1 == idx.index(d.wbtn), "the ? button is not beside the window button"
assert d.hbtn.isVisible() and d.hbtn.mapTo(d, d.hbtn.rect().topRight()).x() > d.width() * 0.8, "not at the top right"
d.hbtn.click()
assert opened[-1] == want and len(opened) == 2
print("3 ? button at the top right opens it")

# 4 project box menu
acts = [a for a in d.pbox.menu().actions() if a.text() == "User guide (this version)"]
assert len(acts) == 1
acts[0].trigger()
assert opened[-1] == want and len(opened) == 3
assert not told, told
print("4 project menu > User guide (this version) opens it")

# 5 fallback: exact guide missing -> newest one in docs/, with a note
hold = want + ".hold"
os.rename(want, hold)
older = [os.path.join(docs, "AlignPro_User_Guide_1.0.9.pdf"), os.path.join(docs, "AlignPro_User_Guide_1.1.0.pdf")]
for p in older:
    shutil.copy(hold, p)
assert guide.guide_path(root) == (older[1], False), guide.guide_path(root)
d.open_guide()
assert opened[-1] == older[1] and told and "1.1.0" in told[-1][1], (opened, told)
print("5 fallback opens the newest:", os.path.basename(opened[-1]), "|", told[-1][1])

# 6 none at all -> a message, nothing opened
for p in older:
    os.remove(p)
os.rename(hold, hold + ".none")
n = len(opened)
assert guide.guide_path(root) == (None, False)
plug.open_guide()
assert len(opened) == n and "not installed" in told[-1][1], told[-1]
print("6 no guide: message:", told[-1][1])
os.rename(hold + ".none", want)

# 7 unload removes the Help entry and the panel
plug.unload()
spin()
assert not [a for a in iface.help.actions() if a.text() == "AlignPro user guide"], "Help entry left after unload"
assert plug.help_action is None and plug.dock is None
print("7 unload removes Help > Plugins > AlignPro user guide")

# 8 load again (plugin reload) gives exactly one entry
plug2 = alignpro.classFactory(iface)
plug2.initGui()
assert len([a for a in iface.help.actions() if a.text() == "AlignPro user guide"]) == 1
plug2.unload()
assert not iface.help.actions()
print("8 reload: one entry, removed again")
print("GUIDE TEST PASSED")
