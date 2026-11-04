"""Shared UI pieces: Qt5/Qt6 compatibility, theme, background tasks, a light plot widget."""
import math
import os
import sys
import traceback

from qgis.core import Qgis, QgsApplication, QgsMessageLog, QgsTask
from qgis.PyQt.QtCore import QPointF, QRectF, Qt, pyqtSignal
from qgis.PyQt.QtGui import QBrush, QColor, QFont, QPainter, QPen, QPolygonF
from qgis.PyQt.QtWidgets import (QHBoxLayout, QHeaderView, QLabel, QMessageBox, QSizePolicy,
                                 QTableWidget, QTableWidgetItem, QWidget)

try:
    from qgis.PyQt.QtGui import QAction  # Qt6
except ImportError:  # pragma: no cover
    from qgis.PyQt.QtWidgets import QAction  # Qt5

from . import theme

TAG = "AlignPro"

# Mapper palette (ui/theme.py, shared with PosPro)
C_BG = theme.BG
C_PANEL = theme.SURF
C_TEXT = theme.TEXT
C_MUTED = theme.DIM
C_GREEN = theme.GREEN
C_AMBER = theme.AMBER
C_RED = theme.RED
C_BLUE = theme.BLUE
BOX_COLORS = {"roll": theme.RED, "pitch": theme.ACCENT, "heading": theme.BLUE, "profile": theme.AMBER}
AXIS = QColor(theme.DIM)
GRID = QColor(theme.GRID)



def log(msg, level=Qgis.Info):
    QgsMessageLog.logMessage(str(msg), TAG, level)
    if os.environ.get("ALIGNPRO_DEBUG"):
        sys.stderr.write("[AlignPro] %s\n" % msg)


def scoped(enum_owner, name):
    """Qt5/Qt6 scoped-enum lookup."""
    return getattr(enum_owner, name)


def info(parent, text, title=TAG):
    theme.msg_info(parent, title, text)


def warn(parent, text, title=TAG):
    theme.msg_warn(parent, title, text)


def ask(parent, text, title=TAG):
    return theme.msg_question(parent, title, text) == QMessageBox.StandardButton.Yes


def themed(w):
    """Mapper look on a dialog or panel opened from AlignPro (Fusion + dark sheet + painted controls)."""
    theme.load_fonts()
    w.setPalette(theme.palette())
    w.setStyleSheet(theme.qss())
    theme.polish(w)
    theme.mapperize(w)
    return w


# ------------------------------------------------------------------ tasks
class Job(QgsTask):
    """Runs fn(progress, cancelled) on a QGIS worker thread; on_done(result, error) on the main thread."""

    def __init__(self, title, fn, on_done, on_progress=None):
        super().__init__(title, QgsTask.Flag.CanCancel)
        self.fn = fn
        self.on_done = on_done
        self.on_progress = on_progress
        self.result_value = None
        self.error = None
        self.message = ""
        if on_progress:
            self.progressChanged.connect(lambda v: on_progress(v, self.message))

    def run(self):
        try:
            def prog(f, msg=""):
                self.message = msg
                self.setProgress(max(0.0, min(100.0, f * 100.0)))
            self.result_value = self.fn(prog, self.isCanceled)
            return True
        except Exception as e:  # noqa: BLE001
            self.error = "%s\n\n%s" % (e, traceback.format_exc())
            return False

    def finished(self, ok):
        try:
            self.on_done(self.result_value if ok else None, None if ok else (self.error or "Cancelled"))
        except Exception:  # noqa: BLE001
            log(traceback.format_exc(), Qgis.Critical)


_jobs = []


def run_job(title, fn, on_done, on_progress=None):
    j = Job(title, fn, on_done, on_progress)
    _jobs.append(j)
    j.taskCompleted.connect(lambda: _jobs.remove(j) if j in _jobs else None)
    j.taskTerminated.connect(lambda: _jobs.remove(j) if j in _jobs else None)
    QgsApplication.taskManager().addTask(j)
    return j


# ------------------------------------------------------------------ table
def make_table(headers, stretch_last=True):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    t.setAlternatingRowColors(True)
    h = t.horizontalHeader()
    h.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    if stretch_last:
        h.setStretchLastSection(True)
    t.verticalHeader().setDefaultSectionSize(20)
    return t


def cell(text, color=None, align_right=True, bg=None, tip=None):
    it = QTableWidgetItem("" if text is None else str(text))
    if align_right:
        it.setTextAlignment(int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))
    if color:
        it.setForeground(QBrush(QColor(color)))
    if bg:
        it.setBackground(QBrush(QColor(bg)))
    if tip:
        it.setToolTip(tip)
    return it


def fmt(v, f="%.2f"):
    return "-" if v is None or (isinstance(v, float) and math.isnan(v)) else f % v


# ------------------------------------------------------------------ plot
class PlotWidget(QWidget):
    """Minimal XY plot: lines and markers, auto range, wheel zoom, drag pan, double-click reset."""
    clicked = pyqtSignal(float, float)

    def __init__(self, xlabel="", ylabel="", parent=None):
        super().__init__(parent)
        self.xlabel, self.ylabel = xlabel, ylabel
        self.series = []        # (xs, ys, color, style 'line'|'dots'|'both', label)
        self.vlines = []        # (x, color)
        self.hlines = []
        self.view = None
        self._drag = None
        self.setMinimumHeight(160)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.title = ""

    def clear(self):
        self.series, self.vlines, self.hlines = [], [], []
        self.view = None
        self.update()

    def add(self, xs, ys, color="#3b82f6", style="line", label=""):
        self.series.append((list(xs), list(ys), color, style, label))
        self.view = None
        self.update()

    def _range(self):
        if self.view:
            return self.view
        xs = [x for s in self.series for x in s[0] if x is not None and math.isfinite(x)]
        ys = [y for s in self.series for y in s[1] if y is not None and math.isfinite(y)]
        if not xs:
            return (0, 1, 0, 1)
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        if x1 - x0 < 1e-12:
            x0, x1 = x0 - 1, x1 + 1
        if y1 - y0 < 1e-12:
            y0, y1 = y0 - 1, y1 + 1
        px, py = (x1 - x0) * 0.04, (y1 - y0) * 0.08
        return (x0 - px, x1 + px, y0 - py, y1 + py)

    def _area(self):
        return QRectF(52, 18 if self.title else 8, max(10, self.width() - 62), max(10, self.height() - 44))

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(theme.SURF))
        a = self._area()
        p.fillRect(a, QColor(theme.VIEW_BG))
        x0, x1, y0, y1 = self._range()
        fx = lambda x: a.left() + (x - x0) / (x1 - x0) * a.width()   # noqa: E731
        fy = lambda y: a.bottom() - (y - y0) / (y1 - y0) * a.height()  # noqa: E731
        txt = AXIS
        grid = GRID
        p.setFont(theme.font("mono", 10))
        for v in _ticks(x0, x1, 6):
            X = fx(v)
            p.setPen(QPen(grid))
            p.drawLine(QPointF(X, a.top()), QPointF(X, a.bottom()))
            p.setPen(QPen(txt))
            p.drawText(QRectF(X - 40, a.bottom() + 2, 80, 14), int(Qt.AlignmentFlag.AlignHCenter), _tick_label(v, x1 - x0))
        for v in _ticks(y0, y1, 5):
            Y = fy(v)
            p.setPen(QPen(grid))
            p.drawLine(QPointF(a.left(), Y), QPointF(a.right(), Y))
            p.setPen(QPen(txt))
            p.drawText(QRectF(0, Y - 7, a.left() - 4, 14), int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                       _tick_label(v, y1 - y0))
        p.setPen(QPen(QColor(theme.BORDER)))
        p.drawRect(a)
        p.setFont(theme.font("ui", 11, 700, 108))
        p.setPen(QPen(QColor(theme.TEXT)))
        if self.title:
            p.drawText(QRectF(a.left(), 0, a.width(), 16), int(Qt.AlignmentFlag.AlignHCenter), self.title.upper())
        p.setPen(QPen(txt))
        p.drawText(QRectF(a.left(), self.height() - 14, a.width(), 14), int(Qt.AlignmentFlag.AlignHCenter),
                   self.xlabel.upper())
        p.save()
        p.translate(10, a.center().y())
        p.rotate(-90)
        p.drawText(QRectF(-a.height() / 2, -9, a.height(), 14), int(Qt.AlignmentFlag.AlignHCenter), self.ylabel.upper())
        p.restore()
        p.setClipRect(a)
        for x, c in self.vlines:
            p.setPen(QPen(QColor(c), 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(fx(x), a.top()), QPointF(fx(x), a.bottom()))
        for y, c in self.hlines:
            p.setPen(QPen(QColor(c), 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(a.left(), fy(y)), QPointF(a.right(), fy(y)))
        for xs, ys, c, style, _ in self.series:
            col = QColor(c)
            if style in ("line", "both"):
                p.setPen(QPen(col, 1.6))
                poly = QPolygonF()
                for x, y in zip(xs, ys):
                    if x is None or y is None or not (math.isfinite(x) and math.isfinite(y)):
                        if poly.size() > 1:
                            p.drawPolyline(poly)
                        poly = QPolygonF()
                        continue
                    poly.append(QPointF(fx(x), fy(y)))
                if poly.size() > 1:
                    p.drawPolyline(poly)
            if style in ("dots", "both"):
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(col))
                r = 3.0 if style == "both" else 1.6
                for x, y in zip(xs, ys):
                    if x is not None and y is not None and math.isfinite(x) and math.isfinite(y):
                        p.drawEllipse(QPointF(fx(x), fy(y)), r, r)
        p.setClipping(False)
        lx = a.left() + 6
        for xs, ys, c, style, label in self.series:
            if label:
                p.setPen(QPen(QColor(c)))
                p.drawText(QPointF(lx, a.top() + 12), "■ " + label)
                lx += p.fontMetrics().horizontalAdvance("■ " + label) + 10
        p.end()

    def wheelEvent(self, ev):
        x0, x1, y0, y1 = self._range()
        k = 0.85 if ev.angleDelta().y() > 0 else 1 / 0.85
        a = self._area()
        pos = evpos(ev)
        mx = x0 + (pos.x() - a.left()) / a.width() * (x1 - x0)
        my = y1 - (pos.y() - a.top()) / a.height() * (y1 - y0)
        mods = ev.modifiers()
        if mods & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier):
            self.view = (x0, x1, my - (my - y0) * k, my + (y1 - my) * k)
        else:
            self.view = (mx - (mx - x0) * k, mx + (x1 - mx) * k, my - (my - y0) * k, my + (y1 - my) * k)
        self.update()

    def mousePressEvent(self, ev):
        self._drag = (evpos(ev), self._range())

    def mouseMoveEvent(self, ev):
        if self._drag and ev.buttons():
            p0, (x0, x1, y0, y1) = self._drag
            a = self._area()
            dx = (evpos(ev).x() - p0.x()) / a.width() * (x1 - x0)
            dy = (evpos(ev).y() - p0.y()) / a.height() * (y1 - y0)
            self.view = (x0 - dx, x1 - dx, y0 + dy, y1 + dy)
            self.update()

    def mouseReleaseEvent(self, ev):
        if self._drag and (evpos(ev) - self._drag[0]).manhattanLength() < 3:
            x0, x1, y0, y1 = self._range()
            a = self._area()
            self.clicked.emit(x0 + (evpos(ev).x() - a.left()) / a.width() * (x1 - x0),
                              y1 - (evpos(ev).y() - a.top()) / a.height() * (y1 - y0))
        self._drag = None

    def mouseDoubleClickEvent(self, _ev):
        self.view = None
        self.update()


def evpos(ev):
    """Mouse position as QPointF on Qt5 and Qt6."""
    f = getattr(ev, "position", None)
    if f is not None:
        try:
            return f()
        except TypeError:
            pass
    return ev.localPos() if hasattr(ev, "localPos") else QPointF(ev.pos())


def _ticks(a, b, n):
    span = b - a
    if not (math.isfinite(a) and math.isfinite(b)) or span <= 0 or span < 1e-12 * max(abs(a), abs(b), 1.0):
        return []
    raw = span / n
    mag = 10 ** math.floor(math.log10(raw))
    step = min((s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw), default=raw)
    t = math.ceil(a / step) * step
    out = []
    while t <= b + 1e-12 and len(out) < 50:
        out.append(t)
        t += step
    return out


def _tick_label(v, span):
    if span == 0:
        return "%g" % v
    d = max(0, min(6, int(-math.floor(math.log10(span / 5))) + 1))
    return ("%." + str(d) + "f") % v


def hbox(*widgets, stretch=True):
    w = QWidget()
    l = QHBoxLayout(w)
    l.setContentsMargins(0, 0, 0, 0)
    for x in widgets:
        if isinstance(x, str):
            x = QLabel(x)
        l.addWidget(x)
    if stretch:
        l.addStretch(1)
    return w
