"""Panel title bar for AlignPro (DCS Core's dark DockTitle) with docking only on request.

Right-click the title for: Dock right / left / bottom / top, Float, Float as a window, Maximise,
Close. Dragging moves the panel and never docks it (docksafe.lock). Double-click floats a docked
panel; on a floating panel it does nothing. The float button floats a docked panel and, on a
floating one, opens the same menu."""
from qgis.PyQt.QtCore import QPoint
from qgis.PyQt.QtWidgets import QMenu

from dcs_core.widgets import DockTitle

from . import docksafe, theme


class LockedDockTitle(DockTitle):
    def __init__(self, dock, title, iface, float_size=(1100, 800), window_size=(1400, 950)):
        super().__init__(dock, title)
        self.iface = iface
        self.float_size = float_size
        self.window_size = window_size
        self.setToolTip("Right-click for docking and window options. Dragging moves the panel; it docks only "
                        "when you choose Dock right / left / bottom / top.")
        docksafe.lock(dock)

    # ------------------------------------------------------------- menu
    def build_menu(self):
        d = self.dock
        m = theme.dark_menu(QMenu(self))
        cur = docksafe.area_of(self.iface, d)
        for label, area in docksafe.DOCK_CHOICES:
            a = m.addAction(label, lambda ar=area: docksafe.dock_to(self.iface, d, ar))
            a.setCheckable(True)
            a.setChecked(cur == area)
        m.addSeparator()
        windowed = bool(d.property(docksafe.WINDOWED))
        a = m.addAction("Float", lambda: docksafe.float_panel(self.iface, d, self.float_size))
        a.setCheckable(True)
        a.setChecked(d.isFloating() and not windowed)
        a = m.addAction("Float as a window", lambda: docksafe.float_window(self.iface, d, self.window_size))
        a.setCheckable(True)
        a.setChecked(windowed and not d.isMaximized())
        m.addAction("Restore window size" if (windowed and d.isMaximized()) else "Maximise",
                    lambda: docksafe.toggle_maximised(self.iface, d))
        m.addSeparator()
        m.addAction("Close", d.close)
        return m

    def show_menu(self, global_pos):
        self.build_menu().exec(global_pos)

    def contextMenuEvent(self, ev):          # noqa: N802
        self.show_menu(ev.globalPos())
        ev.accept()

    # ------------------------------------------------------------- mouse
    def mousePressEvent(self, ev):           # noqa: N802
        pos = ev.position().toPoint() if hasattr(ev, "position") else ev.pos()
        if self._hit(pos) == "float":
            if self.dock.isFloating() or self.dock.property(docksafe.WINDOWED):
                r = self._btns()["float"]
                self.show_menu(self.mapToGlobal(QPoint(int(r.left()), int(r.bottom()) + 2)))
            else:
                docksafe.float_panel(self.iface, self.dock, self.float_size)
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseDoubleClickEvent(self, ev):     # noqa: N802
        # Qt's default double-click re-docks a floating panel; only floating is allowed here
        if not (self.dock.isFloating() or self.dock.property(docksafe.WINDOWED)):
            docksafe.float_panel(self.iface, self.dock, self.float_size)
        ev.accept()
