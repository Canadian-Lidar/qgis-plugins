"""Safe handling of AlignPro's dock panels on QGIS 3 and QGIS 4 (Qt 6).

"Float as a window" and "Maximise" give a floating panel normal window buttons by setting
Qt.Window flags on the QDockWidget. Qt 6 then no longer keeps the panel's place in the main window's
dock layout consistent, and QMainWindow.removeDockWidget() on such a panel crashes QGIS (access
violation in QLayout::removeWidget), for example when QGIS closes and unloads the plugin.

So: panels made into windows are marked; on unload they are hidden and deleted without
removeDockWidget (the main window's layout drops a deleted child by itself); before docking a panel
again its flags are reset to an ordinary dock panel."""
from qgis.PyQt.QtCore import Qt

WINDOWED = "apWindowed"
NO_AREAS = Qt.DockWidgetArea.NoDockWidgetArea
ALL_AREAS = Qt.DockWidgetArea.AllDockWidgetAreas
DOCK_CHOICES = [("Dock right", Qt.DockWidgetArea.RightDockWidgetArea),
                ("Dock left", Qt.DockWidgetArea.LeftDockWidgetArea),
                ("Dock bottom", Qt.DockWidgetArea.BottomDockWidgetArea),
                ("Dock top", Qt.DockWidgetArea.TopDockWidgetArea)]


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
    """No docking by dragging: a moved panel stays where it is put. Docking happens only through
    dock_to (the title-bar right-click menu or the window button)."""
    dock.setAllowedAreas(NO_AREAS)


def area_of(iface, dock):
    """Where the panel is docked, or None when it floats."""
    if dock.isFloating() or dock.property(WINDOWED):
        return None
    a = iface.mainWindow().dockWidgetArea(dock)
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
        dock_to(iface, dock, Qt.DockWidgetArea.RightDockWidgetArea)
        dock.setFloating(True)
        dock.setGeometry(geo)
    elif not dock.isFloating():
        dock.setFloating(True)
        _center(dock, *size)
    dock.show()
    dock.raise_()


def float_window(iface, dock, size=(1400, 950)):
    """A floating window with minimise / maximise / close buttons."""
    if iface.mainWindow().dockWidgetArea(dock) == NO_AREAS and not dock.isFloating():
        iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
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
        iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
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
