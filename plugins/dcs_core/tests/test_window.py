"""Headless tests of dcs_core.window.PluginWindow with real Qt and a stand-in QGIS main window.

Run from the folder that holds dcs_core/, with PyQt5 (or inside QGIS's Python):
    QT_QPA_PLATFORM=offscreen python3 -m pytest dcs_core/tests/test_window.py
Outside QGIS a small stand-in maps qgis.PyQt to PyQt5.
"""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    import qgis.PyQt  # noqa: F401
except ImportError:                       # stand-in outside QGIS
    PyQt5 = pytest.importorskip("PyQt5")
    from PyQt5 import QtCore, QtGui, QtWidgets, sip
    q = types.ModuleType("qgis")
    qp = types.ModuleType("qgis.PyQt")
    q.PyQt = qp
    for n, m in (("QtCore", QtCore), ("QtGui", QtGui), ("QtWidgets", QtWidgets), ("sip", sip)):
        setattr(qp, n, m)
        sys.modules["qgis.PyQt." + n] = m
    sys.modules["qgis"] = q
    sys.modules["qgis.PyQt"] = qp

from qgis.PyQt.QtCore import QSettings, Qt  # noqa: E402
from qgis.PyQt.QtWidgets import QApplication, QLabel, QMainWindow, QToolButton  # noqa: E402

APP = QApplication.instance() or QApplication([])


class Iface:
    def __init__(self):
        self.mw = QMainWindow()
        self.mw.setCentralWidget(QLabel("map"))
        self.mw.resize(1600, 1000)
        self.mw.show()

    def mainWindow(self):              # noqa: N802
        return self.mw

    def addDockWidget(self, area, dock):    # noqa: N802
        self.mw.addDockWidget(area, dock)

    def removeDockWidget(self, dock):       # noqa: N802
        self.mw.removeDockWidget(dock)


@pytest.fixture()
def env(tmp_path):
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path))
    QApplication.setOrganizationName("dcs-test")
    QApplication.setApplicationName("dcs-test-%d" % id(tmp_path))
    from dcs_core import window
    iface = Iface()
    yield iface, window
    iface.mw.close()
    iface.mw.deleteLater()
    APP.processEvents()


def make(window, iface, key="pospro", title="PosPro"):
    w = window.PluginWindow(iface, key, title, size=(900, 600), dock_size=420)
    w.setWidget(QLabel("content"))
    return w


def settle():
    for _ in range(3):
        APP.processEvents()


def test_opens_floating_by_default_and_never_docks_by_drag(env):
    iface, window = env
    w = make(window, iface)
    w.show_window()
    settle()
    assert w.window_mode() == "float" and w.property(window.WINDOWED)
    assert w.isVisible() and w.width() <= 900 and w.allowedAreas() == window.NO_AREAS
    assert iface.mw.dockWidgetArea(w) == window.NO_AREAS or w.isFloating()


@pytest.mark.parametrize("side", ["left", "right", "top", "bottom"])
def test_docks_only_when_chosen_and_stays_locked(env, side):
    iface, window = env
    w = make(window, iface)
    w.show_window()
    w.dock_at(side)
    settle()
    assert w.window_mode() == side and not w.isFloating() and not w.property(window.WINDOWED)
    assert iface.mw.dockWidgetArea(w) == window.AREAS[side]
    assert w.allowedAreas() == window.NO_AREAS            # dragging cannot re-dock it elsewhere
    w.float_window()
    settle()
    assert w.window_mode() == "float" and w.isVisible()


def test_remembers_dock_side_after_restart(env):
    iface, window = env
    w = make(window, iface)
    w.show_window()
    w.dock_at("left")
    settle()
    w.save_state()
    w.release_window()
    settle()
    assert QSettings().value("dcs/pospro/window/mode") == "left"
    w2 = make(window, iface)
    w2.show_window()
    settle()
    assert w2.window_mode() == "left" and iface.mw.dockWidgetArea(w2) == window.AREAS["left"]
    w2.release_window()


def test_remembers_floating_place_and_size(env):
    iface, window = env
    w = make(window, iface)
    w.show_window()
    settle()
    w.setGeometry(150, 120, 700, 500)
    settle()
    w.save_state()
    geo = w.geometry()
    w.release_window()
    settle()
    w2 = make(window, iface)
    w2.show_window()
    settle()
    assert w2.window_mode() == "float"
    assert abs(w2.geometry().width() - geo.width()) <= 2 and abs(w2.geometry().x() - geo.x()) <= 2
    w2.release_window()


def test_maximise_and_reset(env):
    iface, window = env
    w = make(window, iface)
    w.show_window()
    w.maximise()
    settle()
    assert w.window_mode() == "max"
    w.save_state()
    assert QSettings().value("dcs/pospro/window/mode") == "max"
    w.dock_at("bottom")
    w.reset_window()
    settle()
    assert w.window_mode() == "float" and not w.isMaximized() and w.width() <= 900
    w.release_window()


def test_three_plugins_together(env):
    iface, window = env
    a = make(window, iface, "pospro", "PosPro")
    b = make(window, iface, "alignpro", "AlignPro")
    c = make(window, iface, "cloudpro", "CloudPro")
    for x in (a, b, c):
        x.show_window()
    b.dock_at("right")
    c.dock_at("bottom")
    settle()
    assert (a.window_mode(), b.window_mode(), c.window_mode()) == ("float", "right", "bottom")
    for x in (a, b, c):
        x.release_window()
    settle()
    assert not window.alive(a) or not a.isVisible()


def test_menu_title_and_header_button(env):
    iface, window = env
    w = make(window, iface)
    w.show_window()
    names = [x.text() for x in w.window_menu().actions() if x.text()]
    assert names[:2] == ["Floating window", "Maximise"]
    assert {"Dock right", "Dock left", "Dock bottom", "Dock top", "Reset window", "Close"} <= set(names)
    b = QToolButton()
    holder = w.attach_menu(b)
    holder.aboutToShow.emit()
    assert len([x for x in holder.actions() if x.text()]) == len(names)
    w.setWindowTitle("CloudPro - Job 7")
    assert w.titleBarWidget().dock.windowTitle() == "CloudPro - Job 7"
    closed = []
    w.closed.connect(lambda: closed.append(1))
    w.close()
    assert closed == [1]
    w.release_window()


def test_release_while_windowed_or_docked_is_safe(env):
    iface, window = env
    for side in ("float", "right"):
        w = make(window, iface)
        w.show_window()
        if side != "float":
            w.dock_at(side)
        settle()
        w.release_window()
        settle()
    assert True


def test_mouse_on_a_window_never_reaches_the_dock_drag_code(env):
    """QGIS 4.2.3 / Qt 6 on Windows crashed (QDockWidgetLayout::minimumTitleWidth) when a mouse
    event reached a windowed panel's QDockWidget::event. As a window the title bar moves the
    window itself and the panel swallows the mouse; docked, Qt's own handling stays."""
    from qgis.PyQt.QtCore import QEvent, QPoint, QPointF
    from qgis.PyQt.QtGui import QMouseEvent
    iface, window = env
    w = make(window, iface)
    w.show_window()
    settle()
    assert w.property(window.WINDOWED)

    def ev(kind, local, glob, buttons):
        return QMouseEvent(kind, QPointF(local), QPointF(glob), Qt.MouseButton.LeftButton, buttons,
                           Qt.KeyboardModifier.NoModifier)
    t = w.titleBarWidget()
    start = w.frameGeometry().topLeft()
    g0 = t.mapToGlobal(QPoint(60, 10))
    APP.sendEvent(t, ev(QEvent.Type.MouseButtonPress, QPoint(60, 10), g0, Qt.MouseButton.LeftButton))
    APP.sendEvent(t, ev(QEvent.Type.MouseMove, QPoint(100, 40), g0 + QPoint(40, 30), Qt.MouseButton.LeftButton))
    APP.sendEvent(t, ev(QEvent.Type.MouseButtonRelease, QPoint(100, 40), g0 + QPoint(40, 30),
                        Qt.MouseButton.NoButton))
    settle()
    moved = w.frameGeometry().topLeft() - start
    assert (moved.x(), moved.y()) == (40, 30)
    # clicks the content leaves unhandled, straight on the panel: taken by PluginWindow.event
    for kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseMove, QEvent.Type.MouseButtonRelease,
                 QEvent.Type.MouseButtonDblClick):
        e = ev(kind, QPoint(200, 200), w.mapToGlobal(QPoint(200, 200)), Qt.MouseButton.LeftButton)
        assert w.event(e) is True and e.isAccepted()
    # docked: not windowed, so the events go to QDockWidget as before
    w.dock_at("right")
    settle()
    assert not w.property(window.WINDOWED)
    w.release_window()
    settle()
