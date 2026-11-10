"""Headless tests of dcs_core.window.PluginWindow with real Qt and a stand-in QGIS main window.

Run from the plugins folder (the one that holds dcs_core/ and qtshim.py), on either Qt:
    QT_QPA_PLATFORM=offscreen python3 -m pytest dcs_core/tests              (QGIS's Python, else PyQt5)
    DCS_QT=6 QT_QPA_PLATFORM=offscreen python3 -m pytest dcs_core/tests     (PyQt6 stand-in; DCS_QT=5 for PyQt5)
"""
import os
import sys

import pytest  # noqa: F401

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import qtshim  # noqa: E402

QT = qtshim.install()

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
def test_never_docks(env, side):
    """1.11.0: no docking; dock_at (older plugin code) keeps the window a window."""
    iface, window = env
    w = make(window, iface)
    w.show_window()
    w.dock_at(side)
    settle()
    assert w.window_mode() == "float" and w.property(window.WINDOWED) and w.isVisible()
    assert w.allowedAreas() == window.NO_AREAS
    w.release_window()


def test_left_docked_by_an_older_version_opens_as_a_window(env):
    iface, window = env
    QSettings().setValue("dcs/pospro/window/mode", "left")
    w = make(window, iface)
    w.show_window()
    settle()
    assert w.window_mode() == "float" and w.property(window.WINDOWED)
    w.release_window()


def test_remembers_floating_place_and_size(env):
    iface, window = env
    w = make(window, iface)
    w.show_window()
    settle()
    # inside the 800 x 800 offscreen screen: Qt 6's restoreGeometry moves a window that sticks out back onto it
    w.setGeometry(50, 60, 600, 400)
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
    c.maximise()
    settle()
    assert (a.window_mode(), b.window_mode(), c.window_mode()) == ("float", "float", "max")
    for x in (a, b, c):
        x.release_window()
    settle()
    assert not window.alive(a) or not a.isVisible()


def test_menu_title_and_header_button(env):
    iface, window = env
    w = make(window, iface)
    w.show_window()
    names = [x.text() for x in w.window_menu().actions() if x.text()]
    assert names == ["Maximise", "Reset window", "Close"]          # no docking (1.11.0)
    b = QToolButton()
    holder = w.attach_menu(b)
    holder.aboutToShow.emit()
    assert len([x for x in holder.actions() if x.text()]) == len(names)
    w.setWindowTitle("CloudPro - Job 7")
    assert w.titleBarWidget().height() == 0              # the system's title bar only
    closed = []
    w.closed.connect(lambda: closed.append(1))
    w.close()
    assert closed == [1]
    w.release_window()


def test_release_while_windowed_or_docked_is_safe(env):
    iface, window = env
    for side in ("float", "max"):
        w = make(window, iface)
        w.show_window()
        if side != "float":
            w.maximise()
        settle()
        w.release_window()
        settle()
    assert True


def test_mouse_on_a_window_never_reaches_the_dock_drag_code(env):
    """QGIS 4.2.3 / Qt 6 on Windows crashed (QDockWidgetLayout::minimumTitleWidth) when a mouse
    event reached a windowed panel's QDockWidget::event: the window swallows the mouse events its content leaves
    unhandled (the system's title bar moves it)."""
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
    for kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseMove, QEvent.Type.MouseButtonRelease,
                 QEvent.Type.MouseButtonDblClick):
        e = ev(kind, QPoint(200, 200), w.mapToGlobal(QPoint(200, 200)), Qt.MouseButton.LeftButton)
        assert w.event(e) is True and e.isAccepted()
    w.release_window()
    settle()
