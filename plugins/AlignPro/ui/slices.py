"""Roll / Pitch / Heading slice views and the free profile view."""
import numpy as np
from qgis.PyQt.QtCore import QPointF, QRectF, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from qgis.PyQt.QtWidgets import (QButtonGroup, QCheckBox, QHBoxLayout, QLabel, QRadioButton, QSplitter,
                                 QToolButton, QVBoxLayout, QWidget)

from . import theme
from .common import AXIS, BOX_COLORS, GRID, _tick_label, _ticks, evpos

TITLES = {"roll": "Roll  \u00b7  across the wall, height", "pitch": "Pitch  \u00b7  through the wall, height",
          "heading": "Heading  \u00b7  plan view along the wall", "profile": "Profile"}


class SliceView(QWidget):
    cursor = pyqtSignal(str, float)          # box kind, along distance
    redraw_requested = pyqtSignal(str)

    def __init__(self, kind, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.data = []          # [(color, along, y)]
        self.view = None
        self.equal = False
        self.plan = kind == "heading"
        self._drag = None
        self.cursor_x = None
        self.note = ""
        self.setMinimumSize(200, 120)
        self.setMouseTracking(True)
        b = QToolButton(self)
        b.setText("Redraw box…")
        b.setAutoRaise(True)
        b.setToolTip("Drag a new box for this slice on the map")
        b.clicked.connect(lambda: self.redraw_requested.emit(self.kind))
        self.btn = b

    def resizeEvent(self, ev):
        self.btn.setFixedHeight(17)
        self.btn.adjustSize()
        self.btn.resize(self.btn.width() + 16, 17)
        self.btn.move(self.width() - self.btn.width() - 8, 1)
        super().resizeEvent(ev)

    def set_points(self, series, plan=None, note=""):
        """series: [(color, along ndarray, y ndarray)]; y = height (profile) or across (plan)."""
        self.data = series
        if plan is not None:
            self.plan = plan
        self.note = note
        self.view = None
        self.update()

    def _range(self):
        if self.view:
            return self.view
        xs = [s[1] for s in self.data if len(s[1])]
        ys = [s[2] for s in self.data if len(s[2])]
        if not xs:
            return (0, 1, 0, 1)
        x = np.concatenate(xs)
        y = np.concatenate(ys)
        x0, x1 = float(np.percentile(x, 0.1)), float(np.percentile(x, 99.9))
        y0, y1 = float(np.percentile(y, 0.2)), float(np.percentile(y, 99.8))
        if x1 - x0 < 0.1:
            x0, x1 = x0 - 1, x1 + 1
        if y1 - y0 < 0.1:
            m = (y0 + y1) / 2
            y0, y1 = m - 0.5, m + 0.5
        px, py = (x1 - x0) * 0.03, (y1 - y0) * 0.06
        return (x0 - px, x1 + px, y0 - py, y1 + py)

    def _area(self):
        return QRectF(46, 22, max(10, self.width() - 54), max(10, self.height() - 42))

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(theme.SURF))
        a = self._area()
        x0, x1, y0, y1 = self._range()
        if self.equal and not self.view:
            sx = (x1 - x0) / a.width()
            sy = (y1 - y0) / a.height()
            s = max(sx, sy)
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            x0, x1 = cx - s * a.width() / 2, cx + s * a.width() / 2
            y0, y1 = cy - s * a.height() / 2, cy + s * a.height() / 2
        self._cur = (x0, x1, y0, y1)
        txt = AXIS
        grid = GRID
        p.setFont(theme.font("mono", 10))
        fx = lambda v: a.left() + (v - x0) / (x1 - x0) * a.width()   # noqa: E731
        fy = lambda v: a.bottom() - (v - y0) / (y1 - y0) * a.height()  # noqa: E731
        for v in _ticks(x0, x1, 7):
            p.setPen(QPen(grid))
            p.drawLine(QPointF(fx(v), a.top()), QPointF(fx(v), a.bottom()))
            p.setPen(QPen(txt))
            p.drawText(QRectF(fx(v) - 30, a.bottom() + 1, 60, 14), int(Qt.AlignmentFlag.AlignHCenter),
                       _tick_label(v, x1 - x0))
        for v in _ticks(y0, y1, 5):
            p.setPen(QPen(grid))
            p.drawLine(QPointF(a.left(), fy(v)), QPointF(a.right(), fy(v)))
            p.setPen(QPen(txt))
            p.drawText(QRectF(0, fy(v) - 7, a.left() - 3, 14),
                       int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), _tick_label(v, y1 - y0))
        col = QColor(BOX_COLORS.get(self.kind, "#888"))
        p.setPen(QPen(col, 1.5))
        p.drawRect(a)
        p.setFont(theme.font("ui", 12, 700, 110))
        p.setPen(QPen(col))
        p.drawText(QRectF(a.left(), 1, a.width() - 110, 17), int(Qt.AlignmentFlag.AlignLeft),
                   TITLES.get(self.kind, self.kind).upper())
        if self.note:
            p.setFont(theme.font("mono", 10))
            p.setPen(QPen(QColor(theme.DIM)))
            w0 = theme.font("ui", 12, 700, 110)
            from qgis.PyQt.QtGui import QFontMetrics
            off = QFontMetrics(w0).horizontalAdvance(TITLES.get(self.kind, self.kind).upper()) + 12
            p.drawText(QRectF(a.left() + off, 3, a.width() - off - 110, 16), int(Qt.AlignmentFlag.AlignLeft),
                       self.note)
        # end-of-box marker, as on the map
        tri = QPolygonF([QPointF(a.right() - 10, a.top() + 2), QPointF(a.right() - 2, a.top() + 2),
                         QPointF(a.right() - 6, a.top() + 9)])
        p.setBrush(col)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPolygon(tri)
        p.setClipRect(a)
        # interleave the lines in small batches so no line hides the others
        batches = []
        for c, xs, ys in self.data:
            X = a.left() + (xs - x0) / (x1 - x0) * a.width()
            Y = a.bottom() - (ys - y0) / (y1 - y0) * a.height()
            k = max(1, len(X) // 2000)
            for j in range(k):
                batches.append((j / k, c, X[j::k], Y[j::k]))
        batches.sort(key=lambda b: b[0])
        pens = {}
        for _o, c, X, Y in batches:
            if c not in pens:
                pens[c] = QPen(QColor(c), 1.8)
            p.setPen(pens[c])
            p.drawPoints(QPolygonF([QPointF(float(u), float(v)) for u, v in zip(X, Y)]))
        if self.cursor_x is not None:
            p.setPen(QPen(QColor(theme.BRIGHT), 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(fx(self.cursor_x), a.top()), QPointF(fx(self.cursor_x), a.bottom()))
        p.setClipping(False)
        p.end()

    def _to_data(self, pos):
        a = self._area()
        x0, x1, y0, y1 = getattr(self, "_cur", self._range())
        return (x0 + (pos.x() - a.left()) / a.width() * (x1 - x0),
                y1 - (pos.y() - a.top()) / a.height() * (y1 - y0))

    def wheelEvent(self, ev):
        x0, x1, y0, y1 = getattr(self, "_cur", self._range())
        k = 0.85 if ev.angleDelta().y() > 0 else 1 / 0.85
        mx, my = self._to_data(evpos(ev))
        if ev.modifiers() & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier):
            self.view = (x0, x1, my - (my - y0) * k, my + (y1 - my) * k)
        else:
            self.view = (mx - (mx - x0) * k, mx + (x1 - mx) * k, my - (my - y0) * k, my + (y1 - my) * k)
        self.update()

    def mousePressEvent(self, ev):
        self._drag = (evpos(ev), getattr(self, "_cur", self._range()))

    def mouseMoveEvent(self, ev):
        pos = evpos(ev)
        if self._drag and ev.buttons():
            p0, (x0, x1, y0, y1) = self._drag
            a = self._area()
            dx = (pos.x() - p0.x()) / a.width() * (x1 - x0)
            dy = (pos.y() - p0.y()) / a.height() * (y1 - y0)
            self.view = (x0 - dx, x1 - dx, y0 + dy, y1 + dy)
            self.update()
        else:
            x, y = self._to_data(pos)
            self.setToolTip("along %.2f m, %s %.3f m" % (x, "across" if self.plan else "height", y))
            self.cursor.emit(self.kind, x)

    def mouseReleaseEvent(self, _ev):
        self._drag = None

    def mouseDoubleClickEvent(self, _ev):
        self.view = None
        self.update()


class SlicePanel(QWidget):
    """Three slice views plus the angle selector (current / advised / selected combination)."""
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        bar = QHBoxLayout()
        bar.setSpacing(14)
        k = QLabel("ANGLES")
        k.setObjectName("ppKey")
        bar.addWidget(k)
        self.r_cur = QRadioButton("Current")
        self.r_adv = QRadioButton("Advised")
        self.r_sel = QRadioButton("Combination")
        self.r_pre = QRadioButton("Recorded")
        self.r_cur.setChecked(True)
        self.grp = QButtonGroup(self)
        for b in (self.r_cur, self.r_adv, self.r_sel, self.r_pre):
            self.grp.addButton(b)
            bar.addWidget(b)
            b.toggled.connect(lambda on: on and self.changed.emit())
        self.chk_combi = QCheckBox("Combination's lines only")
        self.chk_combi.toggled.connect(lambda _on: self.changed.emit())
        bar.addWidget(self.chk_combi)
        bar.addStretch(1)
        self.lbl = QLabel("")
        self.lbl.setObjectName("ppVal")
        bar.addWidget(self.lbl)
        lay.addLayout(bar)
        sp = QSplitter(Qt.Orientation.Vertical)
        sp.setHandleWidth(6)
        self.views = {k: SliceView(k) for k in ("roll", "pitch", "heading")}
        sp.addWidget(self.views["roll"])
        low = QSplitter(Qt.Orientation.Horizontal)
        low.setHandleWidth(6)
        low.addWidget(self.views["pitch"])
        low.addWidget(self.views["heading"])
        sp.addWidget(low)
        lay.addWidget(sp, 1)

    def mode(self):
        if self.r_adv.isChecked():
            return "advised"
        if self.r_sel.isChecked():
            return "selected"
        if self.r_pre.isChecked():
            return "recorded"
        return "current"
