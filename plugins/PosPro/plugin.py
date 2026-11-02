"""QGIS entry point: toolbar button, the workflow panel and the plots panel."""
import bisect
import os

from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY, QgsProject
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor, QIcon

try:
    from qgis.PyQt.QtGui import QAction
except ImportError:          # QGIS 3 / Qt5
    from qgis.PyQt.QtWidgets import QAction

HERE = os.path.dirname(os.path.abspath(__file__))


class PosProPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.dock = None
        self.plots = None
        self.action = None
        self.marker = None

    def initGui(self):  # noqa: N802
        self.action = QAction(QIcon(os.path.join(HERE, "icon.svg")), "PosPro",
                              self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.triggered.connect(self.toggle)
        self.iface.addToolBarIcon(self.action)
        from dcs_core import menu, registry
        menu.add(self.iface, self.action)
        registry.register("pospro", self.open_project)

    def unload(self):
        try:
            from .ui import theme
            theme.remove_guard()
        except Exception:           # noqa: BLE001
            pass
        if self.action:
            self.iface.removeToolBarIcon(self.action)
            from dcs_core import menu, registry
            menu.remove(self.iface, self.action)
            registry.unregister("pospro")
        self._clear_marker()
        for d in (self.dock, self.plots):
            if d is not None:
                self.iface.removeDockWidget(d)
                d.deleteLater()
        self.dock = self.plots = None

    def toggle(self, on=True):
        if self.dock is None:
            from .ui.dock import PosProDock
            self.dock = PosProDock(self.iface, self)
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
            self.dock.visibilityChanged.connect(lambda v: self.action.setChecked(v))
        self.dock.setVisible(bool(on) or not self.dock.isVisible())
        if self.dock.isVisible():
            self.dock.raise_()

    def open_project(self, path):
        """Show PosPro on a DCS project (called from another plugin's chain bar)."""
        self.toggle(True)
        self.dock.open_path(path)

    def show_plots(self, qc):
        if self.plots is None:
            from .ui.plotsdock import PlotsDock
            self.plots = PlotsDock(self)
            self.iface.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.plots)
        self.plots.set_data(qc)
        self.plots.show()
        self.plots.raise_()

    # ------------------------------------------------------------------
    def _clear_marker(self):
        if self.marker is not None:
            try:
                self.iface.mapCanvas().scene().removeItem(self.marker)
            except Exception:       # noqa: BLE001
                pass
            self.marker = None

    def goto_time(self, t0, t1=None, from_plot=False):
        """Show the trajectory position at GPS time t0 on the map and plots."""
        qc = self.dock.qc if self.dock else None
        if not qc:
            return
        t = qc["gps_time"]
        i = min(len(t) - 1, max(0, bisect.bisect_left(t, t0)))
        from qgis.gui import QgsVertexMarker
        canvas = self.iface.mapCanvas()
        lyr = getattr(self.dock, "traj_layer", None)
        src = lyr.crs() if lyr is not None else QgsCoordinateReferenceSystem("EPSG:4617")
        tr = QgsCoordinateTransform(src, canvas.mapSettings().destinationCrs(), QgsProject.instance())
        try:
            pt = tr.transform(QgsPointXY(qc["lon_deg"][i], qc["lat_deg"][i]))
        except Exception:           # noqa: BLE001
            return
        self._clear_marker()
        m = QgsVertexMarker(canvas)
        m.setCenter(pt)
        m.setColor(QColor(230, 120, 0))
        m.setIconSize(16)
        m.setIconType(QgsVertexMarker.IconType.ICON_CIRCLE if hasattr(QgsVertexMarker, "IconType") else 3)
        m.setPenWidth(3)
        self.marker = m
        if not canvas.extent().contains(pt):
            canvas.setCenter(pt)
            canvas.refresh()
        if not from_plot:
            if self.plots is None or not self.plots.isVisible():
                self.show_plots(qc)
            self.plots.zoom_to(t0, t1)
