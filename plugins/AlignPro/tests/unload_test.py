"""Unload AlignPro from every panel state (docked, floating, as a window, maximised, re-docked) as QGIS
does on exit, and check that removeDockWidget is never called on a panel turned into a window.
Run: QT_QPA_PLATFORM=offscreen python3 tests/unload_test.py"""
import faulthandler
import os
import sys

faulthandler.enable()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from qgis.core import QgsApplication  # noqa: E402
from qgis.gui import QgsMapCanvas, QgsMessageBar  # noqa: E402
from qgis.PyQt.QtCore import QSettings, Qt  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow, QMenu, QToolBar  # noqa: E402

app = QgsApplication([], True)
app.initQgis()
QSettings().remove("AlignPro/recent")
calls = []


class Iface:
    def __init__(self):
        self.mw = QMainWindow()
        self.mw.resize(1400, 900)
        self.menu = QMenu("Plugins")
        self.canvas = QgsMapCanvas(self.mw)
        self.mw.setCentralWidget(self.canvas)
        self.bar = QgsMessageBar()
        self.tb = QToolBar()

    def mainWindow(self): return self.mw
    def pluginMenu(self): return self.menu
    def mapCanvas(self): return self.canvas
    def messageBar(self): return self.bar
    def addToolBarIcon(self, a): self.tb.addAction(a)
    def removeToolBarIcon(self, a): self.tb.removeAction(a)
    def addDockWidget(self, area, d): self.mw.addDockWidget(area, d)

    def removeDockWidget(self, d):
        from alignpro.ui import docksafe
        calls.append((d.objectName(), bool(d.property(docksafe.WINDOWED))))
        assert not d.property(docksafe.WINDOWED), "removeDockWidget on a panel made into a window"
        self.mw.removeDockWidget(d)

    def addPluginToMenu(self, t, a): self.menu.addAction(a)
    def removePluginMenu(self, t, a): self.menu.removeAction(a)


def spin(n=8):
    for _ in range(n):
        app.processEvents()


import alignpro  # noqa: E402

scenarios = {
    "docked": lambda d: None,
    "view floated as window": lambda d: d.show_dataview("float"),
    "view maximised": lambda d: d.show_dataview("max"),
    "main floated as window": lambda d: d.float_window(),
    "main maximised": lambda d: d.toggle_maximised(),
    "both windows": lambda d: (d.float_window(), d.show_dataview("max")),
    "window then docked left": lambda d: (d.float_window(), d.dock_side(Qt.DockWidgetArea.LeftDockWidgetArea),
                                          d.show_dataview("float"), d.show_dataview("dock")),
    "plain floating": lambda d: (d.setFloating(True), d.view_dock.setFloating(True)),
}
for name, act in scenarios.items():
    calls.clear()
    iface = Iface()
    iface.mw.show()
    plug = alignpro.classFactory(iface)
    plug.initGui()
    plug.toggle(True)
    spin()
    iface.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, plug.dock.view_dock)
    act(plug.dock)
    spin()
    if name == "window then docked left":
        mw = iface.mw
        assert mw.dockWidgetArea(plug.dock) == Qt.DockWidgetArea.LeftDockWidgetArea and not plug.dock.isFloating()
        assert mw.dockWidgetArea(plug.dock.view_dock) == Qt.DockWidgetArea.BottomDockWidgetArea
        assert plug.dock.isVisible() and plug.dock.view_dock.isVisible()
        assert not plug.dock.property("apWindowed") and not plug.dock.view_dock.property("apWindowed")
    plug.unload()               # what QgsPluginRegistry::unloadAll does on exit
    spin()
    iface.mw.close()
    iface.mw.deleteLater()
    spin()
    print("ok   %-26s removeDockWidget calls: %s" % (name, calls))
app.exitQgis()
print("UNLOAD TEST OK")
