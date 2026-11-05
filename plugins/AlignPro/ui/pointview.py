"""Fast point canvas: numpy rasterises hundreds of thousands of points into a QImage, QPainter draws the
axes and overlays. Used for the plan, profile and section views of the Data View."""
import math

import numpy as np
from qgis.PyQt.QtCore import QPointF, QRectF, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QImage, QPainter, QPen, QPolygonF
from qgis.PyQt.QtWidgets import QSizePolicy, QWidget

from . import theme
from .common import _tick_label, _ticks, evpos

GHOST = (120, 140, 136)


def _rgb(c):
    q = QColor(c)
    return q.red(), q.green(), q.blue()


def height_colors(z, lo, hi):
    """Blue-green-yellow-red ramp, (N,3) uint8."""
    t = np.clip((z - lo) / max(hi - lo, 1e-6), 0, 1)
    stops = np.array([[59, 130, 246], [0, 229, 170], [245, 166, 35], [232, 72, 85]], float)
    f = t * (len(stops) - 1)
    i = np.minimum(f.astype(int), len(stops) - 2)
    w = (f - i)[:, None]
    return (stops[i] * (1 - w) + stops[i + 1] * w).astype(np.uint8)


class PointCanvas(QWidget):
    """x, y data in metres; equal aspect (plan) or free vertical scale (profiles).
    Wheel zoom (Shift/Ctrl: vertical only), left-drag pan, double-click fit.
    Right-drag draws a line (lineDrawn), a left click without drag emits clicked."""
    lineDrawn = pyqtSignal(float, float, float, float)
    clicked = pyqtSignal(float, float)
    hovered = pyqtSignal(float, float)
    activated = pyqtSignal()             # any mouse press (the Data View makes this view the active one)

    def __init__(self, title, xlabel, ylabel, equal=False, parent=None):
        super().__init__(parent)
        self.title, self.xlabel, self.ylabel = title, xlabel, ylabel
        self.equal = equal
        self.x = self.y = None
        self.rgb = None
        self.gx = self.gy = None
        self.view = None                 # (x0, x1, y0, y1)
        self.overlays = []               # callables(painter, to_screen)
        self.note = ""
        self.accent = theme.ACCENT
        self.active = False
        self.empty_text = ""
        self._ver = 0
        self.point_px = 2
        self._drag = None
        self._rdrag = None
        self.setMouseTracking(True)
        self.setMinimumSize(220, 160)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    # ------------------------------------------------------------ data
    def set_data(self, x, y, rgb, gx=None, gy=None, keep_view=True):
        self.x, self.y, self.rgb = x, y, rgb
        self.gx, self.gy = gx, gy
        self._ver += 1
        if not keep_view or self.view is None:
            self.fit()
        self.update()

    def fit(self):
        if self.x is None or not len(self.x):
            self.view = None
            return
        x0, x1 = np.percentile(self.x, [0.2, 99.8])
        y0, y1 = np.percentile(self.y, [0.2, 99.8])
        if x1 - x0 < 0.2:
            x0, x1 = x0 - 1, x1 + 1
        if y1 - y0 < 0.2:
            m = (y0 + y1) / 2
            y0, y1 = m - 0.5, m + 0.5
        px, py = (x1 - x0) * 0.04, (y1 - y0) * 0.06
        self.view = (float(x0 - px), float(x1 + px), float(y0 - py), float(y1 + py))

    def set_view(self, x0, x1, y0, y1):
        self.view = (x0, x1, y0, y1)
        self.update()

    def _area(self):
        return QRectF(52, 22, max(10, self.width() - 60), max(10, self.height() - 50))

    def _range(self):
        a = self._area()
        x0, x1, y0, y1 = self.view or (0, 1, 0, 1)
        if self.equal:
            s = max((x1 - x0) / a.width(), (y1 - y0) / a.height())
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            x0, x1 = cx - s * a.width() / 2, cx + s * a.width() / 2
            y0, y1 = cy - s * a.height() / 2, cy + s * a.height() / 2
        return x0, x1, y0, y1

    def to_screen(self, x, y):
        a = self._area()
        x0, x1, y0, y1 = self._range()
        return QPointF(a.left() + (x - x0) / (x1 - x0) * a.width(), a.bottom() - (y - y0) / (y1 - y0) * a.height())

    def to_data(self, pos):
        a = self._area()
        x0, x1, y0, y1 = self._range()
        return (x0 + (pos.x() - a.left()) / a.width() * (x1 - x0),
                y1 - (pos.y() - a.top()) / a.height() * (y1 - y0))

    # ----------------------------------------------------------- paint
    def _raster(self, W, H, x0, x1, y0, y1):
        key = (self._ver, W, H, x0, x1, y0, y1, self.point_px)
        if getattr(self, "_rkey", None) == key:
            return self._rimg
        img = np.empty((H, W, 4), np.uint8)
        r, g, b = _rgb(theme.VIEW_BG)
        img[:] = (b, g, r, 255)          # Format_RGB32 is B, G, R, A in memory
        flat = img.view(np.uint32).reshape(-1)
        n = self.point_px

        def pack(c):
            c = np.asarray(c, np.uint32)
            return (c[..., 2] | (c[..., 1] << 8) | (c[..., 0] << 16) | np.uint32(0xFF000000)).astype(np.uint32)

        def splat(xs, ys, cols):
            if xs is None or not len(xs):
                return
            px = ((xs - x0) * (W / (x1 - x0))).astype(np.int32)
            py = ((y1 - ys) * (H / (y1 - y0))).astype(np.int32)
            m = (px >= 0) & (px < W - n + 1) & (py >= 0) & (py < H - n + 1)
            c = pack(cols) if cols.ndim == 1 else pack(cols[m])
            i = py[m] * W + px[m]
            for dy in range(n):
                for dx in range(n):
                    flat[i + (dy * W + dx)] = c
        splat(self.gx, self.gy, np.array(GHOST, np.uint8))
        if self.rgb is not None:
            splat(self.x, self.y, self.rgb)
        self._rkey, self._rimg = key, img
        return img

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(theme.BG))
        a = self._area()
        x0, x1, y0, y1 = self._range()
        W, H = int(a.width()), int(a.height())
        if W > 2 and H > 2 and self.view is not None:
            buf = self._raster(W, H, x0, x1, y0, y1)
            self._buf = buf
            img = QImage(buf.data, W, H, W * 4, QImage.Format.Format_RGB32)
            p.drawImage(QPointF(a.left(), a.top()), img)
        else:
            p.fillRect(a, QColor(theme.VIEW_BG))
        grid = QColor(theme.GRID)
        p.setFont(theme.font("mono", 10))
        for v in _ticks(x0, x1, max(2, int(a.width() / 75))):
            X = a.left() + (v - x0) / (x1 - x0) * a.width()
            p.setPen(QPen(grid))
            p.drawLine(QPointF(X, a.top()), QPointF(X, a.bottom()))
            p.setPen(QColor(theme.DIM))
            p.drawText(QRectF(X - 40, a.bottom() + 2, 80, 14), int(Qt.AlignmentFlag.AlignHCenter), _tick_label(v, x1 - x0))
        for v in _ticks(y0, y1, max(2, int(a.height() / 45))):
            Y = a.bottom() - (v - y0) / (y1 - y0) * a.height()
            p.setPen(QPen(grid))
            p.drawLine(QPointF(a.left(), Y), QPointF(a.right(), Y))
            p.setPen(QColor(theme.DIM))
            p.drawText(QRectF(0, Y - 7, a.left() - 4, 14),
                       int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), _tick_label(v, y1 - y0))
        p.setPen(QPen(QColor(self.accent), 2.6 if self.active else 1.2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(a)
        p.setFont(theme.font("ui", 12, 700, 110))
        p.setPen(QColor(self.accent))
        p.drawText(QRectF(a.left(), 2, a.width(), 18), int(Qt.AlignmentFlag.AlignLeft), self.title.upper())
        tw = p.fontMetrics().horizontalAdvance(self.title.upper())
        p.setFont(theme.font("mono", 10))
        if self.note and tw + p.fontMetrics().horizontalAdvance(self.note) + 24 < a.width():
            p.setFont(theme.font("mono", 10))
            p.setPen(QColor(theme.DIM))
            p.drawText(QRectF(a.left(), 3, a.width() - 4, 16), int(Qt.AlignmentFlag.AlignRight), self.note)
        p.setFont(theme.font("ui", 10, 600, 106))
        p.setPen(QColor(theme.DIM))
        p.drawText(QRectF(a.left(), self.height() - 14, a.width(), 14), int(Qt.AlignmentFlag.AlignHCenter),
                   self.xlabel.upper())
        p.save()
        p.translate(11, a.center().y())
        p.rotate(-90)
        p.drawText(QRectF(-a.height() / 2, -8, a.height(), 14), int(Qt.AlignmentFlag.AlignHCenter), self.ylabel.upper())
        p.restore()
        p.setClipRect(a)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for ov in self.overlays:
            ov(p, self.to_screen)
        if self.empty_text and (self.x is None or not len(self.x)):
            p.setFont(theme.font("ui", 11, 600, 104))
            p.setPen(QColor(theme.DIM))
            p.drawText(a.adjusted(16, 16, -16, -16), int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap),
                       self.empty_text)
        if self._rdrag:
            p.setPen(QPen(QColor(theme.AMBER), 1.5, Qt.PenStyle.DashLine))
            p.drawLine(self._rdrag[0], self._rdrag[1])
        if not self.equal:
            sx = (x1 - x0) / a.width()
            sy = (y1 - y0) / a.height()
            if sy > 0:
                p.setClipping(False)
                p.setFont(theme.font("mono", 10))
                p.setPen(QColor(theme.DIM))
                p.drawText(QRectF(a.right() - 120, a.bottom() - 16, 116, 14), int(Qt.AlignmentFlag.AlignRight),
                           "V ×%.1f" % (sx / sy))
        p.end()

    # ----------------------------------------------------------- mouse
    def wheelEvent(self, ev):
        if self.view is None:
            return
        x0, x1, y0, y1 = self._range()
        k = 0.8 if ev.angleDelta().y() > 0 else 1.25
        mx, my = self.to_data(evpos(ev))
        if ev.modifiers() & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier):
            self.equal = False
            self.view = (x0, x1, my - (my - y0) * k, my + (y1 - my) * k)
        else:
            self.view = (mx - (mx - x0) * k, mx + (x1 - mx) * k, my - (my - y0) * k, my + (y1 - my) * k)
        self.update()

    def mousePressEvent(self, ev):
        pos = evpos(ev)
        self.activated.emit()
        if ev.button() == Qt.MouseButton.RightButton:
            self._rdrag = [pos, pos]
        else:
            self._drag = (pos, self._range(), False)

    def mouseMoveEvent(self, ev):
        pos = evpos(ev)
        if self._rdrag:
            self._rdrag[1] = pos
            self.update()
            return
        if self._drag and ev.buttons():
            p0, (x0, x1, y0, y1), _ = self._drag
            a = self._area()
            dx = (pos.x() - p0.x()) / a.width() * (x1 - x0)
            dy = (pos.y() - p0.y()) / a.height() * (y1 - y0)
            if abs(pos.x() - p0.x()) + abs(pos.y() - p0.y()) > 3:
                self._drag = (p0, (x0, x1, y0, y1), True)
            self.view = (x0 - dx, x1 - dx, y0 + dy, y1 + dy)
            self.update()
        else:
            x, y = self.to_data(pos)
            self.hovered.emit(x, y)

    def mouseReleaseEvent(self, ev):
        pos = evpos(ev)
        if self._rdrag:
            a, b = self.to_data(self._rdrag[0]), self.to_data(pos)
            self._rdrag = None
            self.update()
            if math.hypot(b[0] - a[0], b[1] - a[1]) > 0.3:
                self.lineDrawn.emit(a[0], a[1], b[0], b[1])
            return
        if self._drag and not self._drag[2]:
            x, y = self.to_data(pos)
            self.clicked.emit(x, y)
        self._drag = None

    def mouseDoubleClickEvent(self, _ev):
        self.fit()
        self.update()
