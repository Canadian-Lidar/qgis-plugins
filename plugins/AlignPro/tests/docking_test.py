"""Panels dock only when asked: right-click title menu (Dock right/left/bottom/top), never by dragging.
Run: QT_QPA_PLATFORM=offscreen python3 tests/docking_test.py"""
import faulthandler
import os
import sys

faulthandler.enable()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from qgis.core import QgsApplication  # noqa: E402
from qgis.gui import QgsMapCanvas, QgsMessageBar  # noqa: E402
from qgis.PyQt.QtCore import QEvent, QPoint, QPointF, QSettings, Qt  # noqa: E402
from qgis.PyQt.QtGui import QMouseEvent  # noqa: E402
from qgis.PyQt.QtWidgets import QApplication, QDockWidget, QLabel, QMainWindow, QMenu, QToolBar  # noqa: E402

app = QgsApplication([], True)
app.initQgis()
QSettings().remove("AlignPro/recent")


class Iface:
    def __init__(self):
        self.mw = QMainWindow()
        self.mw.setGeometry(0, 0, 1400, 900)
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


def spin(n=10):
    for _ in range(n):
        app.processEvents()


def check(c, msg):
    print(("  ok    " if c else "  FAIL  ") + msg)
    if not c:
        sys.exit(1)


def mouse(w, typ, pos, buttons):
    btn = Qt.MouseButton.LeftButton if typ != QEvent.Type.MouseMove else Qt.MouseButton.NoButton
    gp = w.mapToGlobal(pos)
    ev = QMouseEvent(typ, QPointF(pos), QPointF(gp), btn, buttons, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(w, ev)


def drag(title, to_global):
    """Press on the title bar, move in steps to a point, release (as a user dragging the panel)."""
    start = QPoint(40, title.height() // 2)
    mouse(title, QEvent.Type.MouseButtonPress, start, Qt.MouseButton.LeftButton)
    g0 = title.mapToGlobal(start)
    for k in range(1, 21):
        g = g0 + (to_global - g0) * k / 20
        mouse(title, QEvent.Type.MouseMove, title.mapFromGlobal(g), Qt.MouseButton.LeftButton)
        spin(2)
    mouse(title, QEvent.Type.MouseButtonRelease, title.mapFromGlobal(to_global), Qt.MouseButton.NoButton)
    spin()


import alignpro  # noqa: E402
from alignpro.ui import docksafe  # noqa: E402

iface = Iface()
iface.mw.show()
plug = alignpro.classFactory(iface)
plug.initGui()
plug.toggle(True)
spin()
d, v = plug.dock, plug.dock.view_dock
mw = iface.mw
iface.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, v)
spin()
print("1. locked, but docked on start")
for dk in (d, v):
    check(dk.allowedAreas() == docksafe.NO_AREAS, "%s: no drag-docking areas" % dk.objectName())
check(mw.dockWidgetArea(d) == Qt.DockWidgetArea.RightDockWidgetArea, "main panel docked right on start")

print("2. right-click title menu")
t = d.titleBarWidget()
m = t.build_menu()
labels = [a.text() for a in m.actions() if a.text()]
check(labels == ["Dock right", "Dock left", "Dock bottom", "Dock top", "Float", "Float as a window", "Maximise",
                 "Close"], "menu: %s" % labels)
check([a.isChecked() for a in m.actions() if a.text() == "Dock right"] == [True], "current area ticked")
for label, area in docksafe.DOCK_CHOICES:
    next(a for a in t.build_menu().actions() if a.text() == label).trigger()
    spin()
    check(mw.dockWidgetArea(d) == area and not d.isFloating() and d.allowedAreas() == docksafe.NO_AREAS,
          "%s docks there and stays locked" % label)
next(a for a in t.build_menu().actions() if a.text() == "Float").trigger()
spin()
check(d.isFloating(), "Float")

print("3. double-click / float button on a floating panel do not dock")
t.mouseDoubleClickEvent(QMouseEvent(QEvent.Type.MouseButtonDblClick, QPointF(40, 10), QPointF(40, 10),
                                    Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                                    Qt.KeyboardModifier.NoModifier))
spin()
check(d.isFloating(), "double-click leaves it floating")
shown = []
t.show_menu = lambda gp: shown.append(gp)
r = t._btns()["float"]
t.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(r.center()), QPointF(r.center()),
                              Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
spin()
del t.show_menu
check(d.isFloating() and shown, "float button opens the menu instead of docking")

print("4. dragging a floating panel over the main window edges (only meaningful where the platform performs\n   drag-docking: see the control line; the protection is allowedAreas = NoDockWidgetArea)")
d.setGeometry(500, 300, 500, 400)
spin()
for name, gp in (("right edge", QPoint(1390, 450)), ("left edge", QPoint(8, 450)), ("bottom edge", QPoint(700, 890))):
    drag(d.titleBarWidget(), mw.mapToGlobal(gp))
    check(d.isFloating(), "AlignPro panel dragged to the %s: still floating" % name)

# control: an ordinary panel with drag-docking on, dragged the same way, to show the drag reaches Qt
ctl = QDockWidget("control", mw)
ctl.setWidget(QLabel("x"))
ctl.setTitleBarWidget(QLabel("control title"))
ctl.titleBarWidget().setMinimumHeight(24)
mw.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, ctl)
ctl.setFloating(True)
ctl.setGeometry(500, 300, 300, 200)
spin()
drag(ctl.titleBarWidget(), mw.mapToGlobal(QPoint(1390, 450)))
print("  info  ordinary unlocked panel after the same drag: %s" % ("docked" if not ctl.isFloating() else
                                                                  "floating (drag not simulated by this platform)"))

print("5. window modes, then docking from the menu")
next(a for a in t.build_menu().actions() if a.text() == "Float as a window").trigger()
spin()
check(d.property(docksafe.WINDOWED) and d.isFloating(), "Float as a window")
next(a for a in t.build_menu().actions() if a.text() == "Maximise").trigger()
spin()
check(d.isMaximized(), "Maximise")
next(a for a in t.build_menu().actions() if a.text() == "Dock left").trigger()
spin()
check(mw.dockWidgetArea(d) == Qt.DockWidgetArea.LeftDockWidgetArea and not d.property(docksafe.WINDOWED)
      and d.allowedAreas() == docksafe.NO_AREAS, "Dock left from a maximised window")
d.show_dataview("float")
spin()
check(v.property(docksafe.WINDOWED), "Data View as a window (window button)")
d.show_dataview("dock")
spin()
check(mw.dockWidgetArea(v) == Qt.DockWidgetArea.BottomDockWidgetArea and v.allowedAreas() == docksafe.NO_AREAS,
      "Data View docked at the bottom (window button), still locked")
d.dock_side(Qt.DockWidgetArea.RightDockWidgetArea)
spin()
check(mw.dockWidgetArea(d) == Qt.DockWidgetArea.RightDockWidgetArea, "Dock on the right (window button)")
plug.unload()
spin()
print("DOCKING TEST OK")
