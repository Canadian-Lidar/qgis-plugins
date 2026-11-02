"""Map tools: drag an inspection box / profile, click an area of interest, slice cursor marker."""
from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry, QgsPointXY, QgsProject,
                       QgsWkbTypes)
from qgis.gui import QgsMapTool, QgsRubberBand, QgsVertexMarker
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor

from .common import BOX_COLORS

try:
    _LINE = QgsWkbTypes.GeometryType.LineGeometry
    _POLY = QgsWkbTypes.GeometryType.PolygonGeometry
except AttributeError:  # pragma: no cover
    from qgis.core import Qgis
    _LINE = Qgis.GeometryType.Line
    _POLY = Qgis.GeometryType.Polygon


class DragLineTool(QgsMapTool):
    """Press, drag, release: emits the two end points in `target_crs`."""
    done = pyqtSignal(object, object)

    def __init__(self, canvas, target_crs, color="#f2a33a"):
        super().__init__(canvas)
        self.canvas = canvas
        self.crs = target_crs
        self.rb = QgsRubberBand(canvas, _LINE)
        self.rb.setColor(QColor(color))
        self.rb.setWidth(2)
        self.p0 = None
        self.setCursor(Qt.CursorShape.CrossCursor)

    def _tr(self, pt):
        t = QgsCoordinateTransform(self.canvas.mapSettings().destinationCrs(), self.crs, QgsProject.instance())
        return t.transform(pt)

    def canvasPressEvent(self, e):
        self.p0 = e.mapPoint()
        self.rb.reset(_LINE)
        self.rb.addPoint(self.p0)

    def canvasMoveEvent(self, e):
        if self.p0 is not None:
            self.rb.reset(_LINE)
            self.rb.addPoint(self.p0)
            self.rb.addPoint(e.mapPoint())

    def canvasReleaseEvent(self, e):
        if self.p0 is None:
            return
        a, b = self._tr(self.p0), self._tr(e.mapPoint())
        self.p0 = None
        self.rb.reset(_LINE)
        if a.distance(b) > 0.5:
            self.done.emit((a.x(), a.y()), (b.x(), b.y()))

    def deactivate(self):
        self.rb.reset(_LINE)
        super().deactivate()


class PolygonTool(QgsMapTool):
    """Left click adds vertices, right click (or double click) finishes. Emits lon/lat ring."""
    done = pyqtSignal(list)

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.rb = QgsRubberBand(canvas, _POLY)
        self.rb.setColor(QColor(242, 163, 58, 90))
        self.rb.setStrokeColor(QColor("#f2a33a"))
        self.rb.setWidth(2)
        self.pts = []
        self.setCursor(Qt.CursorShape.CrossCursor)

    def canvasReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.RightButton:
            self._finish()
            return
        self.pts.append(e.mapPoint())
        self.rb.addPoint(e.mapPoint())

    def canvasDoubleClickEvent(self, e):
        self._finish()

    def canvasMoveEvent(self, e):
        if self.pts:
            self.rb.reset(_POLY)
            for p in self.pts + [e.mapPoint()]:
                self.rb.addPoint(p)

    def _finish(self):
        if len(self.pts) >= 3:
            t = QgsCoordinateTransform(self.canvas.mapSettings().destinationCrs(),
                                       QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance())
            ring = [(t.transform(p).x(), t.transform(p).y()) for p in self.pts]
            self.done.emit(ring)
        self.pts = []
        self.rb.reset(_POLY)

    def deactivate(self):
        self.rb.reset(_POLY)
        self.pts = []
        super().deactivate()


class SliceCursor:
    """Marker on the map that follows the mouse in a slice view."""

    def __init__(self, canvas):
        self.canvas = canvas
        self.m = None

    def show(self, x, y, crs, kind):
        t = QgsCoordinateTransform(crs, self.canvas.mapSettings().destinationCrs(), QgsProject.instance())
        p = t.transform(QgsPointXY(x, y))
        if self.m is None:
            self.m = QgsVertexMarker(self.canvas)
            self.m.setIconType(QgsVertexMarker.IconType.ICON_CROSS if hasattr(QgsVertexMarker, "IconType")
                               else QgsVertexMarker.ICON_CROSS)
            self.m.setIconSize(14)
            self.m.setPenWidth(2)
        self.m.setColor(QColor(BOX_COLORS.get(kind, "#f2a33a")))
        self.m.setCenter(p)
        self.m.show()

    def hide(self):
        if self.m is not None:
            self.canvas.scene().removeItem(self.m)
            self.m = None
