"""Light-weight time-series plots drawn with QPainter (no matplotlib needed).

TimePlot: several series against GPS time, shared zoom between plots,
crosshair with values, click to pick a time, shaded bands (e.g. GNSS gaps)
and a coloured status strip.
"""
import bisect
import datetime
import math

from qgis.PyQt.QtCore import QPointF, QRectF, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from qgis.PyQt.QtWidgets import QSizePolicy, QWidget

from ..core import geo
from . import theme


from dcs_core.widgets import align_flags as _af  # noqa: E402

AXIS = QColor("#5d857c")
GRID = QColor(29, 51, 48, 200)


def _nice_step(span, target):
    if span <= 0:
        return 1.0
    raw = span / max(1, target)
    p = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * p:
            return m * p
    return 10 * p


class Series:
    def __init__(self, label, x, y, color, width=1.4, unit=""):
        self.label, self.x, self.y = label, x, y
        self.color = QColor(color)
        self.width = width
        self.unit = unit
        self.visible = True


class TimePlot(QWidget):
    timePicked = pyqtSignal(float)
    xRangeChanged = pyqtSignal(float, float)

    def __init__(self, title="", unit="", parent=None, symmetric=False, logy=False):
        super().__init__(parent)
        self.title = title
        self.unit = unit
        self.series = []
        self.bands = []          # (x0, x1, QColor)
        self.strip = None        # (x list, values list, {value: QColor})
        self.x0 = self.x1 = None
        self.full = None
        self.yfix = None
        self.symmetric = symmetric
        self.hover_x = None
        self.marker_x = None
        self.leap = 18
        self._drag = None
        self.setMouseTracking(True)
        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

    # ------------------------------------------------------------------
    def clear(self):
        self.series, self.bands, self.strip = [], [], None
        self.full = None
        self.update()

    def add(self, label, x, y, color, width=1.4):
        s = Series(label, x, y, color, width, self.unit)
        self.series.append(s)
        self._auto_full()
        return s

    def set_strip(self, x, values, colors):
        self.strip = (x, values, colors)
        self._auto_full()

    def add_band(self, x0, x1, color):
        self.bands.append((x0, x1, QColor(color)))

    def _auto_full(self):
        xs = [s.x[0] for s in self.series if len(s.x)] + [s.x[-1] for s in self.series if len(s.x)]
        if self.strip and len(self.strip[0]):
            xs += [self.strip[0][0], self.strip[0][-1]]
        if xs:
            self.full = (min(xs), max(xs))
            if self.x0 is None:
                self.x0, self.x1 = self.full
        self.update()

    def set_xrange(self, x0, x1, emit=False):
        if x1 - x0 < 1.0:
            mid = 0.5 * (x0 + x1)
            x0, x1 = mid - 0.5, mid + 0.5
        self.x0, self.x1 = x0, x1
        self.update()
        if emit:
            self.xRangeChanged.emit(x0, x1)

    def reset_zoom(self):
        if self.full:
            self.set_xrange(self.full[0], self.full[1], True)

    def set_marker(self, x):
        self.marker_x = x
        self.update()

    # ------------------------------------------------------------------
    def _plot_rect(self):
        r = self.rect()
        return QRectF(58, 22, max(10, r.width() - 70), max(10, r.height() - 22 - 34 - (12 if self.strip else 0)))

    def _yrange(self):
        if self.yfix:
            return self.yfix
        lo, hi = math.inf, -math.inf
        for s in self.series:
            if not s.visible or not len(s.x):
                continue
            i0 = max(0, bisect.bisect_left(s.x, self.x0) - 1)
            i1 = min(len(s.x), bisect.bisect_right(s.x, self.x1) + 1)
            step = max(1, (i1 - i0) // 4000)
            for i in range(i0, i1, step):
                v = s.y[i]
                if v is None or v != v or abs(v) > 1e12:
                    continue
                lo = min(lo, v); hi = max(hi, v)
        if lo == math.inf:
            return 0.0, 1.0
        if self.symmetric:
            m = max(abs(lo), abs(hi), 1e-6)
            return -m * 1.08, m * 1.08
        if hi - lo < 1e-9:
            hi += 0.5; lo -= 0.5
        pad = 0.06 * (hi - lo)
        return lo - pad, hi + pad

    def _tx(self, x, pr):
        return pr.left() + (x - self.x0) / (self.x1 - self.x0) * pr.width()

    def _ty(self, y, pr, yr):
        return pr.bottom() - (y - yr[0]) / (yr[1] - yr[0]) * pr.height()

    def _time_label(self, x, step):
        t = geo.gps_to_utc(x)
        if step >= 60:
            return t.strftime("%H:%M")
        return t.strftime("%H:%M:%S")

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # theme colours, not the palette: QGIS on Windows resets widget palettes
        fg = QColor(theme.TEXT)
        p.fillRect(self.rect(), QColor(theme.SURF))
        pr = self._plot_rect()
        f = QFont(self.font())
        if f.pointSizeF() > 0:
            f.setPointSizeF(max(7.0, f.pointSizeF() * 0.9))
        else:
            f.setPixelSize(max(10, int(f.pixelSize() * 0.9)))
        p.setFont(f)
        p.setPen(fg)
        title = self.title + ("  [%s]" % self.unit if self.unit else "")
        p.drawText(QRectF(pr.left(), 2, pr.width(), 18), _af(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), title)
        if self.x0 is None or not (self.series or self.strip):
            p.setPen(AXIS)
            p.drawText(pr, _af(Qt.AlignmentFlag.AlignCenter), "No data yet")
            return
        yr = self._yrange()
        # bands
        for b0, b1, col in self.bands:
            if b1 < self.x0 or b0 > self.x1:
                continue
            xa, xb = self._tx(max(b0, self.x0), pr), self._tx(min(b1, self.x1), pr)
            p.fillRect(QRectF(xa, pr.top(), max(1.0, xb - xa), pr.height()), col)
        # grid + axes
        p.setPen(QPen(GRID, 1))
        ystep = _nice_step(yr[1] - yr[0], max(2, int(pr.height() / 36)))
        y = math.ceil(yr[0] / ystep) * ystep
        while y <= yr[1]:
            yy = self._ty(y, pr, yr)
            p.setPen(QPen(GRID, 1))
            p.drawLine(QPointF(pr.left(), yy), QPointF(pr.right(), yy))
            p.setPen(AXIS)
            txt = ("%.4f" % y).rstrip("0").rstrip(".") if abs(ystep) < 1 else "%g" % y
            p.drawText(QRectF(0, yy - 8, pr.left() - 4, 16), _af(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), txt)
            y += ystep
        span = self.x1 - self.x0
        xstep = _nice_step(span, max(2, int(pr.width() / 90)))
        for nice in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800):
            if nice >= xstep:
                xstep = nice
                break
        x = math.ceil(self.x0 / xstep) * xstep
        while x <= self.x1:
            xx = self._tx(x, pr)
            p.setPen(QPen(GRID, 1))
            p.drawLine(QPointF(xx, pr.top()), QPointF(xx, pr.bottom()))
            p.setPen(AXIS)
            p.drawText(QRectF(xx - 40, pr.bottom() + 2, 80, 14), _af(Qt.AlignmentFlag.AlignHCenter), self._time_label(x, xstep))
            x += xstep
        p.setPen(AXIS)
        p.drawRect(pr)
        # status strip
        if self.strip:
            sx, sv, cols = self.strip
            sr = QRectF(pr.left(), pr.bottom() + 17, pr.width(), 8)
            i0 = max(0, bisect.bisect_left(sx, self.x0) - 1)
            i1 = min(len(sx), bisect.bisect_right(sx, self.x1) + 1)
            step = max(1, (i1 - i0) // int(max(1, pr.width())))
            for i in range(i0, i1, step):
                xa = self._tx(sx[i], pr)
                xb = self._tx(sx[min(i + step, len(sx) - 1)], pr)
                xa, xb = max(xa, pr.left()), min(xb, pr.right())
                if xb > xa:
                    p.fillRect(QRectF(xa, sr.top(), xb - xa + 0.5, sr.height()), cols.get(sv[i], AXIS))
        # series
        p.setClipRect(pr)
        for s in self.series:
            if not s.visible or len(s.x) < 1:
                continue
            path = self._path(s, pr, yr)
            p.setPen(QPen(s.color, s.width))
            p.drawPath(path)
        p.setClipping(False)
        # legend
        lx = pr.right()
        for s in reversed(self.series):
            w = p.fontMetrics().horizontalAdvance(s.label) + 22
            lx -= w
            p.fillRect(QRectF(lx, 8, 12, 3), s.color)
            p.setPen(fg if s.visible else AXIS)
            p.drawText(QRectF(lx + 15, 0, w, 18), _af(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), s.label)
        # marker and hover
        for xm, col in ((self.marker_x, QColor("#f5a623")), (self.hover_x, QColor("#5d857c"))):
            if xm is None or xm < self.x0 or xm > self.x1:
                continue
            xx = self._tx(xm, pr)
            p.setPen(QPen(col, 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(xx, pr.top()), QPointF(xx, pr.bottom()))
        if self.hover_x is not None and self.x0 <= self.hover_x <= self.x1:
            lines = [geo.gps_to_utc(self.hover_x).strftime("%H:%M:%S.%f")[:-4] + " UTC"]
            for s in self.series:
                if not s.visible or not len(s.x):
                    continue
                i = min(len(s.x) - 1, max(0, bisect.bisect_left(s.x, self.hover_x)))
                if abs(s.x[i] - self.hover_x) < max(2.0, (self.x1 - self.x0) / 200):
                    lines.append("%s: %s" % (s.label, _fmt(s.y[i])))
            fm = p.fontMetrics()
            w = max(fm.horizontalAdvance(t) for t in lines) + 10
            h = fm.height() * len(lines) + 6
            xx = self._tx(self.hover_x, pr) + 8
            if xx + w > pr.right():
                xx -= w + 16
            box = QRectF(xx, pr.top() + 4, w, h)
            bg = QColor(theme.SURF)
            bg.setAlpha(225)
            p.fillRect(box, bg)
            p.setPen(AXIS)
            p.drawRect(box)
            p.setPen(fg)
            for k, t in enumerate(lines):
                p.drawText(QRectF(box.left() + 5, box.top() + 3 + k * fm.height(), w, fm.height()),
                           _af(Qt.AlignmentFlag.AlignLeft), t)

    def _path(self, s, pr, yr):
        path = QPainterPath()
        i0 = max(0, bisect.bisect_left(s.x, self.x0) - 1)
        i1 = min(len(s.x), bisect.bisect_right(s.x, self.x1) + 1)
        n = i1 - i0
        cols = max(1, int(pr.width()))
        started = False
        lastx = None
        if n > 3 * cols:
            # min/max per pixel column
            per = n / cols
            for c in range(cols):
                a = i0 + int(c * per)
                b = min(i1, i0 + int((c + 1) * per))
                vals = [v for v in s.y[a:b] if v is not None and v == v]
                if not vals:
                    started = False
                    continue
                xx = pr.left() + c
                ya, yb = self._ty(min(vals), pr, yr), self._ty(max(vals), pr, yr)
                if not started:
                    path.moveTo(xx, ya)
                    started = True
                else:
                    path.lineTo(xx, ya)
                path.lineTo(xx, yb)
            return path
        for i in range(i0, i1):
            v = s.y[i]
            if v is None or v != v:
                started = False
                continue
            xx = self._tx(s.x[i], pr)
            yy = self._ty(v, pr, yr)
            gap = lastx is not None and s.x[i] - lastx > 5.0
            if not started or gap:
                path.moveTo(xx, yy)
                started = True
            else:
                path.lineTo(xx, yy)
            lastx = s.x[i]
        return path

    # ------------------------------------------------------------------
    def _xat(self, px):
        pr = self._plot_rect()
        return self.x0 + (px - pr.left()) / pr.width() * (self.x1 - self.x0)

    def mouseMoveEvent(self, ev):
        pos = ev.position() if hasattr(ev, "position") else ev.pos()
        if self._drag is not None and self.x0 is not None:
            dx = self._xat(self._drag[0]) - self._xat(pos.x())
            self.set_xrange(self._drag[1] + dx, self._drag[2] + dx, True)
            return
        if self.x0 is not None and self._plot_rect().contains(pos):
            self.hover_x = self._xat(pos.x())
        else:
            self.hover_x = None
        self.update()

    def leaveEvent(self, ev):
        self.hover_x = None
        self.update()

    def mousePressEvent(self, ev):
        pos = ev.position() if hasattr(ev, "position") else ev.pos()
        if ev.button() == Qt.MouseButton.LeftButton and self.x0 is not None:
            self._drag = (pos.x(), self.x0, self.x1)
            self._press = pos.x()

    def mouseReleaseEvent(self, ev):
        pos = ev.position() if hasattr(ev, "position") else ev.pos()
        if self._drag is not None and abs(pos.x() - self._press) < 3 and self._plot_rect().contains(pos):
            x = self._xat(pos.x())
            self.marker_x = x
            self.timePicked.emit(x)
            self.update()
        self._drag = None

    def mouseDoubleClickEvent(self, ev):
        self.reset_zoom()

    def wheelEvent(self, ev):
        if self.x0 is None:
            return
        pos = ev.position() if hasattr(ev, "position") else ev.pos()
        d = ev.angleDelta().y()
        if d == 0:
            return
        k = 0.8 if d > 0 else 1.25
        xc = self._xat(pos.x())
        x0 = xc - (xc - self.x0) * k
        x1 = xc + (self.x1 - xc) * k
        if self.full:
            span = self.full[1] - self.full[0]
            x0 = max(x0, self.full[0] - 0.05 * span)
            x1 = min(x1, self.full[1] + 0.05 * span)
        self.set_xrange(x0, x1, True)


def _fmt(v):
    if v is None:
        return "-"
    a = abs(v)
    if a >= 1000:
        return "%.1f" % v
    if a >= 10:
        return "%.2f" % v
    if a >= 0.1:
        return "%.3f" % v
    return "%.4f" % v


class Donut(QWidget):
    """Share of solution types."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parts = []          # (label, value, QColor)
        self.setMinimumSize(150, 150)

    def set_parts(self, parts):
        self.parts = [(l, float(v), QColor(c)) for l, v, c in parts if float(v) > 0]
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        s = min(self.width(), self.height()) - 10
        r = QRectF((self.width() - s) / 2, (self.height() - s) / 2, s, s)
        tot = sum(v for _, v, _ in self.parts)
        if tot <= 0:
            p.setPen(QPen(AXIS, 10))
            p.drawEllipse(r.adjusted(8, 8, -8, -8))
            return
        start = 90 * 16
        for label, v, col in self.parts:
            span = -int(round(v / tot * 360 * 16))
            p.setPen(QPen(col, max(8, s * 0.12), Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
            p.drawArc(r.adjusted(s * 0.08, s * 0.08, -s * 0.08, -s * 0.08), start, span)
            start += span
        main = max(self.parts, key=lambda q: q[1])
        from . import theme
        p.setPen(main[2])
        p.setFont(theme.font("mono", max(14, int(s * 0.16)), 500))
        p.drawText(r.adjusted(0, -s * 0.12, 0, 0), _af(Qt.AlignmentFlag.AlignCenter), "%.0f%%" % (100 * main[1] / tot))
        p.setPen(QColor(theme.DIM))
        p.setFont(theme.font("ui", max(10, int(s * 0.08)), 600, 112))
        p.drawText(r.adjusted(0, s * 0.16, 0, 0), _af(Qt.AlignmentFlag.AlignCenter), main[0].upper())
