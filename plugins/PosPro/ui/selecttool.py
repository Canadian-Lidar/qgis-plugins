"""Map tools for Trajectory Review: select part of the trajectory with a
rectangle (drag), a polygon (click the corners, right-click or double-click
to finish) or a lasso (drag a free shape). Emits the shape as a QgsGeometry
in the map canvas CRS; Esc cancels."""
from qgis.core import QgsGeometry, QgsPointXY, QgsRectangle, QgsWkbTypes
from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor

try:
    POLY = QgsWkbTypes.GeometryType.PolygonGeometry
except AttributeError:          # QGIS 4: Qgis.GeometryType
    from qgis.core import Qgis
    POLY = Qgis.GeometryType.Polygon


class SelectTool(QgsMapTool):
    selected = pyqtSignal(object)      # QgsGeometry, canvas CRS

    def __init__(self, canvas, mode="rectangle"):
        super().__init__(canvas)
        self.canvas = canvas
        self.mode = mode
        self.band = QgsRubberBand(canvas, POLY)
        self.band.setColor(QColor(0, 229, 170, 50))
        self.band.setStrokeColor(QColor(0, 229, 170, 230))
        self.band.setWidth(2)
        self.pts = []
        self.dragging = False
        self.setCursor(Qt.CursorShape.CrossCursor)

    def _pt(self, e):
        return self.toMapCoordinates(e.pos())

    def _show(self, extra=None):
        self.band.reset(POLY)
        pts = self.pts + ([extra] if extra is not None else [])
        if self.mode == "rectangle" and len(pts) >= 2:
            r = QgsRectangle(pts[0], pts[-1])
            pts = [QgsPointXY(r.xMinimum(), r.yMinimum()), QgsPointXY(r.xMaximum(), r.yMinimum()),
                   QgsPointXY(r.xMaximum(), r.yMaximum()), QgsPointXY(r.xMinimum(), r.yMaximum())]
        for p in pts:
            self.band.addPoint(p, False)
        self.band.show()
        self.band.updatePosition()

    def canvasPressEvent(self, e):      # noqa: N802
        if e.button() == Qt.MouseButton.RightButton:
            self._finish()
            return
        if self.mode in ("rectangle", "lasso"):
            self.pts = [self._pt(e)]
            self.dragging = True
        else:
            self.pts.append(self._pt(e))
            self._show()

    def canvasMoveEvent(self, e):       # noqa: N802
        if self.mode == "lasso" and self.dragging:
            self.pts.append(self._pt(e))
            self._show()
        elif self.mode == "rectangle" and self.dragging:
            self._show(self._pt(e))
        elif self.mode == "polygon" and self.pts:
            self._show(self._pt(e))

    def canvasReleaseEvent(self, e):    # noqa: N802
        if self.mode == "rectangle" and self.dragging:
            self.pts.append(self._pt(e))
            self.dragging = False
            self._finish()
        elif self.mode == "lasso" and self.dragging:
            self.dragging = False
            self._finish()

    def canvasDoubleClickEvent(self, e):    # noqa: N802
        if self.mode == "polygon":
            self._finish()

    def keyPressEvent(self, e):         # noqa: N802
        if e.key() == Qt.Key.Key_Escape:
            self.pts = []
            self.band.reset(POLY)

    def _finish(self):
        pts = list(self.pts)
        self.pts = []
        self.band.reset(POLY)
        if self.mode == "rectangle" and len(pts) >= 2:
            g = QgsGeometry.fromRect(QgsRectangle(pts[0], pts[-1]))
        elif len(pts) >= 3:
            g = QgsGeometry.fromPolygonXY([pts + [pts[0]]])
        else:
            return
        if not g.isEmpty():
            self.selected.emit(g)

    def deactivate(self):
        self.band.reset(POLY)
        super().deactivate()
