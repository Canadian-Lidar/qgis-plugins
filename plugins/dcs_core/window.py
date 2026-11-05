"""One window for every DCS plugin: floating by default, docked only when the user chooses it.

PluginWindow is a QDockWidget that
  - opens as a floating window (minimise, maximise, close) centred on the QGIS window's screen,
  - never docks because it was dragged: docking happens only from its Window menu
    (the title bar's right-click menu, its float button, or a plugin's header button),
  - remembers per plugin how it was left (floating with its size and place, maximised, or docked
    left / right / top / bottom with its size) in QSettings dcs/<key>/window/..., and comes back
    the same way, also after QGIS restarts,
  - has "Reset window" to go back to the default floating window.

Plugin code: win.show_window() to open it, win.window_menu() / win.attach_menu(button) for a header
button, win.release_window() in unload, win.closed when the user closes it.

The functions below the class are the same operations for any QDockWidget. They came from
AlignPro's docksafe.py, which found that on Qt 6 (QGIS 4) a panel given window buttons must
never go through QMainWindow.removeDockWidget (QGIS crashes): release() handles unloading.
"""
from qgis.PyQt.QtCore import QEvent, QPoint, QRect, QRectF, QSettings, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QColor, QGuiApplication, QPainter, QPen
from qgis.PyQt.QtWidgets import QDockWidget, QMenu

from . import theme
from .widgets import DockTitle, align_flags

WINDOWED = "apWindowed"
NO_AREAS = Qt.DockWidgetArea.NoDockWidgetArea
ALL_AREAS = Qt.DockWidgetArea.AllDockWidgetAreas
AREAS = {"left": Qt.DockWidgetArea.LeftDockWidgetArea, "right": Qt.DockWidgetArea.RightDockWidgetArea,
         "top": Qt.DockWidgetArea.TopDockWidgetArea, "bottom": Qt.DockWidgetArea.BottomDockWidgetArea}
DOCK_CHOICES = [("Dock right", AREAS["right"]), ("Dock left", AREAS["left"]),
                ("Dock bottom", AREAS["bottom"]), ("Dock top", AREAS["top"])]
MODES = ("float", "max", "left", "right", "top", "bottom")


def _area_name(area):
    for k, v in AREAS.items():
        if v == area:
            return k
    return None


# ----------------------------------------------------------------------------- the window
class PluginWindow(QDockWidget):
    """The standard window of a DCS plugin. key is the plugin's registry key ("pospro", ...);
    size is the default floating size; dock_size the default width (left/right) or height
    (top/bottom) when docked."""
    closed = pyqtSignal()

    def __init__(self, iface, key, title, parent=None, size=(1400, 950), dock_size=None, object_name=None,
                 min_size=(520, 360)):
        super().__init__(title, parent if parent is not None else iface.mainWindow())
        self.iface = iface
        self.key = key
        self.default_size = (int(size[0]), int(size[1]))
        self.dock_size = dock_size
        self._placed = False
        self._restoring = False
        self.setObjectName(object_name or "Dcs%sWindow" % key.title().replace("_", ""))
        self.setMinimumSize(*min_size)
        self.setTitleBarWidget(WindowTitle(self))
        lock(self)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(600)
        self._save_timer.timeout.connect(self.save_state)
        self.dockLocationChanged.connect(lambda *_: self._save_soon())
        self.windowTitleChanged.connect(lambda *_: self.titleBarWidget().update())

    # ------------------------------------------------------------------ settings
    def _k(self, name):
        return "dcs/%s/window/%s" % (self.key, name)

    def saved_mode(self):
        m = QSettings().value(self._k("mode"), "float")
        return m if m in MODES else "float"

    def window_mode(self):
        """'left' / 'right' / 'top' / 'bottom' when docked, 'max' when maximised, else 'float'."""
        a = area_of(self.iface, self)
        if a is not None:
            return _area_name(a) or "right"
        if self.property(WINDOWED) and self.isMaximized():
            return "max"
        return "float"

    def save_state(self):
        if self._restoring or not alive(self) or not self._placed:
            return
        s = QSettings()
        m = self.window_mode()
        s.setValue(self._k("mode"), m)
        if m in ("float", "max"):
            s.setValue(self._k("geometry"), self.saveGeometry())
        else:
            s.setValue(self._k("dock_extent"), self.width() if m in ("left", "right") else self.height())

    def _save_soon(self):
        if self._placed and not self._restoring:
            self._save_timer.start()

    def clear_state(self):
        s = QSettings()
        for n in ("mode", "geometry", "dock_extent"):
            s.remove(self._k(n))

    # ------------------------------------------------------------------ placing
    def show_window(self):
        """Show the window: the first time where it was left last time (default: floating)."""
        if not self._placed:
            self._restore()
        else:
            if self.property(WINDOWED) or self.isFloating() or area_of(self.iface, self) is not None:
                self.show()
            else:
                self._float_default()
            self.raise_()
            self.activateWindow()

    def _restore(self):
        self._restoring = True
        try:
            m = self.saved_mode()
            if m in AREAS:
                self.dock_at(m, save=False)
                ext = QSettings().value(self._k("dock_extent"))
                if ext:
                    self._resize_docked(int(ext))
            else:
                geo = QSettings().value(self._k("geometry"))
                self.float_window(save=False)
                if geo is not None and self.restoreGeometry(geo) and _on_a_screen(self.geometry()):
                    if m == "max":
                        self.showMaximized()
                else:
                    self._center()
        finally:
            self._restoring = False
            self._placed = True
        self.show()
        self.raise_()
        self.activateWindow()

    def set_mode(self, m):
        if m == "float":
            self.float_window()
        elif m == "max":
            self.maximise()
        elif m in AREAS:
            self.dock_at(m)

    def float_window(self, save=True):  # noqa: D401
        """Floating window with minimise / maximise / close."""
        if self.mainwin_has_none() and not self.isFloating() and not self.property(WINDOWED):
            self.iface.addDockWidget(AREAS["right"], self)
        was = bool(self.property(WINDOWED))
        make_window(self)
        if self.isMaximized():
            self.showNormal()
        if not was and not self._restoring:
            self._center()
        self._placed = True
        self.raise_()
        self.activateWindow()
        if save:
            self._save_soon()

    def _float_default(self):
        self.float_window(save=False)
        self._center()
        self._save_soon()

    def maximise(self):
        """Maximise the floating window (a second call restores its size)."""
        if self.property(WINDOWED) and self.isMaximized():
            self.showNormal()
        else:
            if not self.property(WINDOWED):
                self.float_window(save=False)
            self.showMaximized()
        self.raise_()
        self._save_soon()

    def dock_at(self, side, save=True):
        """Dock left / right / top / bottom: the only way this window docks."""
        dock_to(self.iface, self, AREAS[side])
        self._placed = True
        if not self._restoring and self.dock_size:
            self._resize_docked(self.dock_size)
        if save:
            self._save_soon()

    def reset_window(self):
        """Back to the default: a floating window of the default size, centred."""
        self.clear_state()
        if self.property(WINDOWED) and self.isMaximized():
            self.showNormal()
        self.float_window(save=False)
        self._center()
        self._save_soon()

    def _resize_docked(self, ext):
        mw = self.iface.mainWindow()
        orient = Qt.Orientation.Horizontal if self.window_mode() in ("left", "right") else Qt.Orientation.Vertical
        try:
            mw.resizeDocks([self], [int(ext)], orient)
        except (AttributeError, RuntimeError, TypeError):
            pass

    def _center(self):
        w, h = self.default_size
        scr = None
        try:
            scr = self.iface.mainWindow().screen()
        except (AttributeError, RuntimeError):
            pass
        scr = scr or self.screen() or QGuiApplication.primaryScreen()
        if scr is None:
            self.resize(w, h)
            return
        g = scr.availableGeometry()
        w, h = min(w, int(g.width() * 0.9)), min(h, int(g.height() * 0.88))
        self.setGeometry(g.x() + (g.width() - w) // 2, g.y() + (g.height() - h) // 2, w, h)

    def mainwin_has_none(self):
        try:
            return self.iface.mainWindow().dockWidgetArea(self) == NO_AREAS
        except RuntimeError:
            return True

    # ------------------------------------------------------------------ menu
    def window_menu(self, parent=None):
        """The Window menu: Floating, Maximise, Dock right / left / bottom / top, Reset window."""
        m = theme.dark_menu(QMenu(parent or self))
        cur = self.window_mode()
        a = m.addAction("Floating window", self.float_window)
        a.setCheckable(True)
        a.setChecked(cur == "float")
        m.addAction("Restore window size" if cur == "max" else "Maximise", self.maximise)
        m.addSeparator()
        for label, area in DOCK_CHOICES:
            side = _area_name(area)
            a = m.addAction(label, lambda s=side: self.dock_at(s))
            a.setCheckable(True)
            a.setChecked(cur == side)
        m.addSeparator()
        m.addAction("Reset window", self.reset_window)
        m.addAction("Close", self.close)
        return m

    def attach_menu(self, button, extras=()):
        """Give a header tool button the Window menu (rebuilt each time it opens); extras =
        [(label, callable)] added at the end."""
        from qgis.PyQt.QtWidgets import QToolButton
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        holder = theme.dark_menu(QMenu(button))

        def fill():
            holder.clear()
            for act in self.window_menu(holder).actions():
                holder.addAction(act)
            if extras:
                holder.addSeparator()
                for label, fn in extras:
                    holder.addAction(label, fn)
        holder.aboutToShow.connect(fill)
        button.setMenu(holder)
        button.setToolTip("Window: floating, maximise, dock left / right / top / bottom, reset")
        return holder

    # ------------------------------------------------------------------ events
    _MOUSE = (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease, QEvent.Type.MouseMove,
              QEvent.Type.MouseButtonDblClick)

    def event(self, ev):                   # noqa: N802
        # As a window (make_window) the panel is moved by its frame and by WindowTitle. Mouse
        # events that reach the panel itself (a child left them unhandled) must not go to
        # QDockWidget's title-bar drag code: on Qt 6 / Windows it expects a dock title bar this
        # window does not have and crashes (QDockWidgetLayout::minimumTitleWidth, access
        # violation; QGIS 4.2.3, 5 Nov 2026).
        if ev.type() in self._MOUSE and self.property(WINDOWED):
            ev.accept()
            return True
        return super().event(ev)

    def moveEvent(self, ev):               # noqa: N802
        super().moveEvent(ev)
        if self.isFloating() or self.property(WINDOWED):
            self._save_soon()

    def resizeEvent(self, ev):             # noqa: N802
        super().resizeEvent(ev)
        self._save_soon()

    def closeEvent(self, ev):              # noqa: N802
        self.save_state()
        super().closeEvent(ev)
        self.closed.emit()

    # names used by AlignPro 1.2 and PosPro 1.3 (and their tests)
    def toggle_maximised(self):
        self.maximise()

    def dock_side(self, area):
        self.dock_at(_area_name(area) or "right")

    def release_window(self):
        """For the plugin's unload: remember the state, then remove the window safely."""
        try:
            self._save_timer.stop()
            self.save_state()
        except RuntimeError:
            pass
        release(self.iface, self)


# ----------------------------------------------------------------------------- title bar
class WindowTitle(DockTitle):
    """Dark title bar: right-click (or the float button) for the Window menu. Dragging moves the
    window and never docks it. Double-click floats a docked window."""

    def __init__(self, dock):
        super().__init__(dock, dock.windowTitle())
        self._drag = None
        self.setToolTip("Right-click for the Window menu: floating, maximise, dock left / right / top / "
                        "bottom, reset. Dragging moves the window; it docks only when you choose.")

    def paintEvent(self, ev):              # noqa: N802
        # the title as written (PosPro, AlignPro, CloudPro), like the header; other panels stay upper case
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.fillRect(self.rect(), QColor(theme.BG))
        p.setPen(QPen(QColor(theme.BORDER), 1))
        p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        p.setPen(QColor(theme.TEXT))
        p.setFont(theme.font("ui", 13, 700, 104))
        p.drawText(QRectF(10, 0, self.width() - 70, self.height()),
                   align_flags(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self.dock.windowTitle())
        p.end()
        self.title = ""
        self._paint_buttons()

    def _menu(self):
        d = self.dock
        if isinstance(d, PluginWindow):
            return d.window_menu(self)
        return _plain_menu(d, self)

    def contextMenuEvent(self, ev):        # noqa: N802
        self._menu().exec(ev.globalPos())
        ev.accept()

    def mousePressEvent(self, ev):         # noqa: N802
        pos = ev.position().toPoint() if hasattr(ev, "position") else ev.pos()
        if self._hit(pos) == "float":
            if self.dock.isFloating() or self.dock.property(WINDOWED):
                r = self._btns()["float"]
                self._menu().exec(self.mapToGlobal(QPoint(int(r.left()), int(r.bottom()) + 2)))
            elif isinstance(self.dock, PluginWindow):
                self.dock.float_window()
            else:
                float_window(self.dock.iface if hasattr(self.dock, "iface") else None, self.dock)
            ev.accept()
            return
        if self.dock.property(WINDOWED):
            # a window moves itself; Qt's dock drag must never run on it (see PluginWindow.event)
            if self._hit(pos) == "close":
                self.dock.close()
            elif not self.dock.isMaximized():
                self._drag = _gpos(ev) - self.dock.frameGeometry().topLeft()
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):          # noqa: N802
        if self.dock.property(WINDOWED):
            pos = ev.position().toPoint() if hasattr(ev, "position") else ev.pos()
            h = self._hit(pos)
            if h != self.hover:
                self.hover = h
                self.update()
            if self._drag is not None and (ev.buttons() & Qt.MouseButton.LeftButton):
                self.dock.move(_gpos(ev) - self._drag)
            ev.accept()
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):       # noqa: N802
        if self.dock.property(WINDOWED):
            self._drag = None
            ev.accept()
            return
        super().mouseReleaseEvent(ev)

    def mouseDoubleClickEvent(self, ev):   # noqa: N802
        if not (self.dock.isFloating() or self.dock.property(WINDOWED)):
            if isinstance(self.dock, PluginWindow):
                self.dock.float_window()
        ev.accept()


def _gpos(ev):
    return ev.globalPosition().toPoint() if hasattr(ev, "globalPosition") else ev.globalPos()


def _plain_menu(d, parent):
    m = theme.dark_menu(QMenu(parent))
    iface = getattr(d, "iface", None)
    for label, area in DOCK_CHOICES:
        m.addAction(label, lambda ar=area: dock_to(iface, d, ar))
    m.addSeparator()
    m.addAction("Floating window", lambda: float_window(iface, d))
    m.addAction("Close", d.close)
    return m


def _on_a_screen(rect):
    for s in QGuiApplication.screens():
        g = s.availableGeometry()
        if g.intersects(QRect(rect)) and g.intersected(QRect(rect)).width() > 120:
            return True
    return False


# ----------------------------------------------------------------------------- any QDockWidget
def alive(obj):
    if obj is None:
        return False
    try:
        from qgis.PyQt import sip
    except ImportError:         # pragma: no cover
        import sip
    try:
        return not sip.isdeleted(obj)
    except TypeError:
        return True


def lock(dock):
    """No docking by dragging: a moved panel stays where it is put. Docking happens only through dock_to."""
    dock.setAllowedAreas(NO_AREAS)


def area_of(iface, dock):
    """Where the panel is docked, or None when it floats."""
    if dock.isFloating() or dock.property(WINDOWED):
        return None
    try:
        a = iface.mainWindow().dockWidgetArea(dock)
    except (AttributeError, RuntimeError):
        return None
    return None if a == NO_AREAS else a


def _center(dock, w, h):
    scr = dock.screen().availableGeometry() if dock.screen() else None
    if scr is not None:
        w, h = min(w, int(scr.width() * 0.9)), min(h, int(scr.height() * 0.88))
        dock.setGeometry(scr.x() + (scr.width() - w) // 2, scr.y() + (scr.height() - h) // 2, w, h)


def float_panel(iface, dock, size=(1100, 800)):
    """An ordinary floating panel (no window buttons)."""
    if dock.property(WINDOWED):
        geo = dock.geometry()
        dock_to(iface, dock, AREAS["right"])
        dock.setFloating(True)
        dock.setGeometry(geo)
    elif not dock.isFloating():
        dock.setFloating(True)
        _center(dock, *size)
    dock.show()
    dock.raise_()


def float_window(iface, dock, size=(1400, 950)):
    """A floating window with minimise / maximise / close buttons."""
    if iface is not None and iface.mainWindow().dockWidgetArea(dock) == NO_AREAS and not dock.isFloating():
        iface.addDockWidget(AREAS["right"], dock)
    was = bool(dock.property(WINDOWED))
    make_window(dock)
    if dock.isMaximized():
        dock.showNormal()
    if not was:
        _center(dock, *size)
    dock.raise_()
    dock.activateWindow()


def toggle_maximised(iface, dock):
    if dock.property(WINDOWED) and dock.isMaximized():
        dock.showNormal()
        return
    if iface.mainWindow().dockWidgetArea(dock) == NO_AREAS and not dock.isFloating():
        iface.addDockWidget(AREAS["right"], dock)
    make_window(dock)
    dock.showMaximized()
    dock.raise_()


def make_window(dock):
    """Floating panel with normal window buttons (minimise, maximise, close)."""
    if not dock.isFloating():
        dock.setFloating(True)
    dock.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.CustomizeWindowHint | Qt.WindowType.WindowTitleHint |
                        Qt.WindowType.WindowMinMaxButtonsHint | Qt.WindowType.WindowCloseButtonHint)
    dock.setProperty(WINDOWED, True)
    dock.show()


def make_panel(dock):
    """Undo make_window before the panel is docked again."""
    if dock.isMaximized():
        dock.showNormal()
    if dock.property(WINDOWED):
        dock.hide()
        dock.setWindowFlags(Qt.WindowType.Widget)
        dock.setProperty(WINDOWED, False)
    if dock.isFloating():
        dock.setFloating(False)


def dock_to(iface, dock, area):
    """Dock the panel on purpose (the only way it docks: see lock)."""
    dock.setAllowedAreas(ALL_AREAS)
    try:
        make_panel(dock)
        iface.addDockWidget(area, dock)       # moves it when it is already in the layout
    finally:
        dock.setAllowedAreas(NO_AREAS)
    dock.show()
    dock.raise_()


def release(iface, dock):
    """Remove and delete a panel on unload without crashing, whatever state it is in."""
    if not alive(dock):
        return
    try:
        dock.hide()
    except RuntimeError:
        return
    mw = iface.mainWindow() if iface is not None else None
    in_layout = False
    try:
        in_layout = (mw is not None and alive(mw) and not dock.property(WINDOWED) and not dock.isFloating()
                     and dock.parentWidget() is mw
                     and mw.dockWidgetArea(dock) != Qt.DockWidgetArea.NoDockWidgetArea)
    except RuntimeError:
        in_layout = False
    if in_layout:
        try:
            iface.removeDockWidget(dock)
        except RuntimeError:
            pass
    try:
        dock.deleteLater()
    except RuntimeError:
        pass
