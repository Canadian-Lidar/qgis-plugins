"""Trajectory editor: plan view and height profile of the processed trajectory, linked both ways.

Hover or click in one view and the other shows the same moment; select in either (rectangle, lasso,
polygon in plan, a time range in the profile) and both highlight it; zoom the plan and the profile
follows the time of what is in view. The selection is then fixed with the Trajectory Review actions
(core/review.py): interpolate (smooth curve or straight line, horizontal and/or vertical), bridge with
the IMU (GNSS excluded, processed again), mark poor, accept or reject.
"""
import math

import numpy as np

from qgis.PyQt.QtCore import QPointF, QRectF, QTimer, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QPainter, QPen, QPolygonF
from qgis.PyQt.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QHBoxLayout, QLabel,
                                 QListWidget, QListWidgetItem, QPushButton, QSizePolicy, QSplitter,
                                 QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget)

from dcs_core.window import PluginWindow
from dcs_core.widgets import align_flags as _af

from ..core import editor, geo, results, review
from . import theme
from .plots import TimePlot

STATE_COLOURS = {editor.STATE_GOOD: theme.GREEN, editor.STATE_OPEN: theme.RED, editor.STATE_INTERP: theme.VIOLET,
                 editor.STATE_POOR: theme.AMBER, editor.STATE_EXCL: theme.BLUE, editor.STATE_ACCEPT: "#8ef0cf"}
SEL = QColor("#ffd84d")
ANT1, ANT2, ANT_MEAS = "#00e5aa", "#3ab4f2", "#ff9a3c"
HOVER = QColor("#e3e7ea")
MARK = QColor("#f5a623")


# ---------------------------------------------------------------------------
# plan view
# ---------------------------------------------------------------------------
class PlanView(QWidget):
    hovered = pyqtSignal(int)
    picked = pyqtSignal(int)
    selectedMask = pyqtSignal(object)
    viewChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.track = None
        self.shown = None            # bool mask of samples in the shown sections
        self.sel = None              # bool mask of selected samples
        self.colour_by = "state"
        self.tool = "pan"
        self.hover_i = self.mark_i = -1
        self.cx = self.cy = 0.0      # centre east, north (m)
        self.scale = 1.0             # pixels per metre
        self.basemap = None          # (QImage, (e0, n0, e1, n1))
        self.antennas = None         # {"gps1": (n, e), "gps2": (n, e)} from the installation, or None
        self.measured = None         # (n, e) arrays: GPS2 as measured by the antenna check
        self.show_dir = True
        self._drag = None
        self._shape = []
        self.setMouseTracking(True)
        self.setMinimumSize(300, 200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    # -------------------------------------------------------------- data / view
    def set_track(self, track):
        self.track = track
        self.shown = np.ones(len(track.t), dtype=bool) if track and track.ok else None
        self.sel = np.zeros(len(track.t), dtype=bool) if track and track.ok else None
        self.update()

    def fit(self, mask=None):
        tr = self.track
        if not tr or not tr.ok:
            return
        m = self.shown if mask is None else mask
        if m is None or not m.any():
            m = np.ones(len(tr.t), dtype=bool)
        e, n = tr.fe[m], tr.fn[m]
        e0, e1, n0, n1 = float(e.min()), float(e.max()), float(n.min()), float(n.max())
        self.cx, self.cy = 0.5 * (e0 + e1), 0.5 * (n0 + n1)
        w, h = max(50, self.width() - 40), max(50, self.height() - 40)
        self.scale = min(w / max(e1 - e0, 2.0), h / max(n1 - n0, 2.0))
        self.update()
        self.viewChanged.emit()

    def to_screen(self, e, n):
        return (self.width() * 0.5 + (e - self.cx) * self.scale, self.height() * 0.5 - (n - self.cy) * self.scale)

    def to_world(self, x, y):
        return self.cx + (x - self.width() * 0.5) / self.scale, self.cy - (y - self.height() * 0.5) / self.scale

    def extent(self):
        e0, n1 = self.to_world(0, 0)
        e1, n0 = self.to_world(self.width(), self.height())
        return e0, n0, e1, n1

    def in_view(self):
        tr = self.track
        e0, n0, e1, n1 = self.extent()
        m = (tr.fe >= e0) & (tr.fe <= e1) & (tr.fn >= n0) & (tr.fn <= n1)
        return m & self.shown

    def colours(self):
        tr = self.track
        if self.colour_by == "fix":
            return tr.status, {s: QColor(c) for s, c in results.STATUS_COLOURS.items()}
        return tr.state, {s: QColor(c) for s, c in STATE_COLOURS.items()}

    # -------------------------------------------------------------- painting
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.fillRect(self.rect(), QColor(theme.VIEW_BG))
        tr = self.track
        if not tr or not tr.ok:
            p.setPen(QColor(theme.DIM))
            p.drawText(self.rect(), _af(Qt.AlignmentFlag.AlignCenter), "Process the project to edit its trajectory")
            return
        if self.basemap is not None:
            img, (be0, bn0, be1, bn1) = self.basemap
            x0, y0 = self.to_screen(be0, bn1)
            x1, y1 = self.to_screen(be1, bn0)
            p.setOpacity(0.55)
            p.drawImage(QRectF(x0, y0, x1 - x0, y1 - y0), img)
            p.setOpacity(1.0)
        self._grid(p)
        vis = self.in_view()
        idx = np.nonzero(vis)[0]
        if len(idx):
            # what the edits moved: the trajectory as processed, faint red
            ch = tr.changed() & vis
            if ch.any():
                self._dots(p, tr.oe[ch], tr.on[ch], QColor(232, 72, 85, 150), 2.2)
            vals, cols = self.colours()
            if self.antennas:
                for key, col in (("gps1", ANT1), ("gps2", ANT2)):
                    an, ae = self.antennas[key]
                    self._thin(p, idx, ae, an, QColor(col))
            self._lines(p, idx, vals, cols, tr.fe, tr.fn)
            if self.show_dir:
                self._chevrons(p, idx)
            if self.measured is not None:
                mn, me = self.measured
                x0, y0 = self.to_screen(me, mn)
                ins = (x0 >= 0) & (x0 <= self.width()) & (y0 >= 0) & (y0 <= self.height())
                if ins.any():
                    self._dots(p, me[ins], mn[ins], QColor(ANT_MEAS), 3.2)
            if self.sel is not None:
                s = self.sel & vis
                if s.any():
                    self._dots(p, tr.fe[s], tr.fn[s], SEL, 4.0)
        if self.antennas:
            for i in (self.mark_i, self.hover_i):
                if 0 <= i < len(tr.t):
                    self._vehicle(p, i)
        for i, col, r in ((self.mark_i, MARK, 6.0), (self.hover_i, HOVER, 5.0)):
            if 0 <= i < len(tr.t):
                x, y = self.to_screen(tr.fe[i], tr.fn[i])
                p.setPen(QPen(col, 2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(QPointF(x, y), r, r)
        if self._shape:
            p.setPen(QPen(SEL, 1.5, Qt.PenStyle.DashLine))
            p.setBrush(QColor(255, 216, 77, 40))
            if self.tool == "rectangle" and len(self._shape) == 2:
                (ax, ay), (bx, by) = self._shape
                p.drawRect(QRectF(min(ax, bx), min(ay, by), abs(bx - ax), abs(by - ay)))
            else:
                p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in self._shape]))
        self._scalebar(p)
        self._legend(p)

    def _lines(self, p, idx, vals, cols, ex, ny):
        tr = self.track
        x, y = self.to_screen(ex[idx], ny[idx])
        xi, yi = np.round(x).astype(np.int64), np.round(y).astype(np.int64)
        v = vals[idx]
        brk = np.ones(len(idx), dtype=bool)
        if len(idx) > 1:
            jump = np.diff(idx) > 1
            gapt = np.diff(tr.t[idx]) > 2.0
            same = (np.diff(xi) == 0) & (np.diff(yi) == 0) & (np.diff(v) == 0)
            keep = np.ones(len(idx), dtype=bool)
            keep[1:] = ~same | jump | gapt
            brk[1:] = jump | gapt
            x, y, v, brk = x[keep], y[keep], v[keep], brk[keep]
        width = 2.4
        start = 0
        n = len(x)
        for k in range(1, n + 1):
            if k == n or brk[k] or v[k] != v[start]:
                if k - start >= 1:
                    col = QColor(cols.get(int(v[start]), QColor(theme.DIM)))
                    pts = [QPointF(float(x[j]), float(y[j])) for j in range(start, k)]
                    if k < n and not brk[k]:
                        pts.append(QPointF(float(x[k]), float(y[k])))       # join the colour runs
                    p.setPen(QPen(col, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                                  Qt.PenJoinStyle.RoundJoin))
                    if len(pts) == 1:
                        p.drawPoint(pts[0])
                    else:
                        p.drawPolyline(QPolygonF(pts))
                start = k

    def _thin(self, p, idx, ex, ny, col):
        """A thin line through the visible samples (antenna tracks)."""
        x, y = self.to_screen(ex[idx], ny[idx])
        if len(idx) < 2:
            return
        brk = np.ones(len(idx), dtype=bool)
        brk[1:] = (np.diff(idx) > 1) | (np.diff(self.track.t[idx]) > 2.0)
        xi, yi = np.round(x).astype(np.int64), np.round(y).astype(np.int64)
        keep = np.ones(len(idx), dtype=bool)
        keep[1:] = (np.diff(xi) != 0) | (np.diff(yi) != 0) | brk[1:]
        x, y, brk = x[keep], y[keep], brk[keep]
        p.setPen(QPen(col, 1.2))
        start = 0
        for k in range(1, len(x) + 1):
            if k == len(x) or brk[k]:
                if k - start > 1:
                    p.drawPolyline(QPolygonF([QPointF(float(x[j]), float(y[j])) for j in range(start, k)]))
                start = k

    def _chevrons(self, p, idx, spacing=90.0):
        """Direction of travel: a chevron every `spacing` pixels along the visible track."""
        tr = self.track
        if len(idx) < 3:
            return
        x, y = self.to_screen(tr.fe[idx], tr.fn[idx])
        brk = np.zeros(len(idx), dtype=bool)
        brk[1:] = (np.diff(idx) > 1) | (np.diff(tr.t[idx]) > 2.0)
        d = np.hypot(np.diff(x), np.diff(y))
        d[brk[1:]] = 0.0
        cum = np.concatenate(([0.0], np.cumsum(d)))
        marks = np.nonzero(np.diff(np.floor((cum + spacing * 0.5) / spacing)) > 0)[0] + 1
        if len(marks) > 400:
            return
        p.setPen(QPen(QColor(theme.BRIGHT), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                      Qt.PenJoinStyle.RoundJoin))
        for k in marks:
            a, b = max(0, k - 3), min(len(x) - 1, k + 3)
            dx, dy = x[b] - x[a], y[b] - y[a]
            L = math.hypot(dx, dy)
            if L < 1e-6 or brk[a + 1:b + 1].any():
                continue
            ux, uy = dx / L, dy / L
            cx, cy = float(x[k]), float(y[k])
            s = 6.0
            p.drawPolyline(QPolygonF([QPointF(cx - s * ux - s * 0.8 * uy, cy - s * uy + s * 0.8 * ux),
                                      QPointF(cx, cy),
                                      QPointF(cx - s * ux + s * 0.8 * uy, cy - s * uy - s * 0.8 * ux)]))

    def _vehicle(self, p, i):
        """At sample i: IMU, GPS1 and GPS2 (as installed) joined, and the direction of travel."""
        tr = self.track
        ox, oy = self.to_screen(tr.fe[i], tr.fn[i])
        for key, col in (("gps1", ANT1), ("gps2", ANT2)):
            an, ae = self.antennas[key]
            ax, ay = self.to_screen(ae[i], an[i])
            p.setPen(QPen(QColor(col), 1.5))
            p.drawLine(QPointF(ox, oy), QPointF(ax, ay))
            p.setBrush(QColor(col))
            p.drawEllipse(QPointF(ax, ay), 4.5, 4.5)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawText(QRectF(ax + 7, ay - 8, 50, 16), _af(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                       "GPS1" if key == "gps1" else "GPS2")

    def _dots(self, p, ex, ny, col, r):
        x, y = self.to_screen(ex, ny)
        xi, yi = np.round(x).astype(np.int64), np.round(y).astype(np.int64)
        if len(xi) > 1:
            keep = np.ones(len(xi), dtype=bool)
            keep[1:] = (np.diff(xi) != 0) | (np.diff(yi) != 0)
            x, y = x[keep], y[keep]
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        for a, b in zip(x[:20000], y[:20000]):
            p.drawEllipse(QPointF(float(a), float(b)), r * 0.5, r * 0.5)

    def _grid(self, p):
        e0, n0, e1, n1 = self.extent()
        span = max(e1 - e0, n1 - n0)
        step = 10 ** math.floor(math.log10(max(span / 5.0, 1e-3)))
        for m in (1, 2, 5, 10):
            if span / (step * m) <= 8:
                step *= m
                break
        p.setPen(QPen(QColor(theme.GRID), 1))
        e = math.floor(e0 / step) * step
        while e <= e1:
            x, _ = self.to_screen(e, 0)
            p.drawLine(QPointF(x, 0), QPointF(x, self.height()))
            e += step
        n = math.floor(n0 / step) * step
        while n <= n1:
            _, y = self.to_screen(0, n)
            p.drawLine(QPointF(0, y), QPointF(self.width(), y))
            n += step

    def _scalebar(self, p):
        target = self.width() / 6.0 / self.scale
        step = 10 ** math.floor(math.log10(max(target, 1e-3)))
        for m in (1, 2, 5):
            if step * m >= target * 0.6:
                step *= m
                break
        L = step * self.scale
        x0, y0 = 14, self.height() - 16
        p.setPen(QPen(QColor(theme.TEXT), 2))
        p.drawLine(QPointF(x0, y0), QPointF(x0 + L, y0))
        p.drawLine(QPointF(x0, y0 - 4), QPointF(x0, y0 + 2))
        p.drawLine(QPointF(x0 + L, y0 - 4), QPointF(x0 + L, y0 + 2))
        txt = ("%g m" % step) if step >= 1 else ("%g cm" % (step * 100))
        p.drawText(QRectF(x0 + L + 6, y0 - 9, 80, 16), _af(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), txt)
        # north arrow
        p.drawText(QRectF(self.width() - 26, 6, 20, 16), _af(Qt.AlignmentFlag.AlignCenter), "N")
        p.drawLine(QPointF(self.width() - 16, 24), QPointF(self.width() - 16, 40))

    def _legend(self, p):
        if self.colour_by == "fix":
            items = [(results.STATUS[s], results.STATUS_COLOURS[s]) for s in (6, 5, 4, 3, 2, 1)]
        else:
            items = [(editor.STATE_LABEL[s], STATE_COLOURS[s]) for s in sorted(STATE_COLOURS)]
        items.append(("As processed (moved)", "#e84855"))
        if self.antennas:
            items += [("GPS1 primary (installed)", ANT1), ("GPS2 secondary (installed)", ANT2)]
        if self.measured is not None:
            items.append(("GPS2 measured (antenna check)", ANT_MEAS))
        fm = p.fontMetrics()
        w = max(fm.horizontalAdvance(t) for t, _ in items) + 30
        h = fm.height() * len(items) + 8
        box = QRectF(8, 8, w, h)
        bg = QColor(theme.SURF)
        bg.setAlpha(215)
        p.fillRect(box, bg)
        for k, (t, c) in enumerate(items):
            yy = box.top() + 4 + k * fm.height()
            p.fillRect(QRectF(box.left() + 6, yy + fm.height() * 0.35, 14, 4), QColor(c))
            p.setPen(QColor(theme.TEXT))
            p.drawText(QRectF(box.left() + 26, yy, w, fm.height()), _af(Qt.AlignmentFlag.AlignLeft), t)

    # -------------------------------------------------------------- mouse
    def _pos(self, ev):
        q = ev.position() if hasattr(ev, "position") else ev.pos()
        return q.x(), q.y()

    def nearest(self, x, y, radius=12.0):
        tr = self.track
        if not tr or not tr.ok:
            return -1
        vis = np.nonzero(self.in_view())[0]
        if not len(vis):
            return -1
        sx, sy = self.to_screen(tr.fe[vis], tr.fn[vis])
        d = (sx - x) ** 2 + (sy - y) ** 2
        k = int(np.argmin(d))
        return int(vis[k]) if d[k] <= radius * radius else -1

    def mousePressEvent(self, ev):
        x, y = self._pos(ev)
        if ev.button() == Qt.MouseButton.MiddleButton or (ev.button() == Qt.MouseButton.LeftButton and self.tool == "pan"):
            self._drag = (x, y, self.cx, self.cy, x, y)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if ev.button() == Qt.MouseButton.LeftButton:
            if self.tool == "polygon":
                self._shape.append((x, y))
            else:
                self._shape = [(x, y), (x, y)] if self.tool == "rectangle" else [(x, y)]
            self.update()
        elif ev.button() == Qt.MouseButton.RightButton and self.tool == "polygon":
            self._finish_shape()

    def mouseMoveEvent(self, ev):
        x, y = self._pos(ev)
        if self._drag is not None:
            dx, dy = x - self._drag[0], y - self._drag[1]
            self.cx = self._drag[2] - dx / self.scale
            self.cy = self._drag[3] + dy / self.scale
            self.update()
            return
        if self._shape and ev.buttons() & Qt.MouseButton.LeftButton:
            if self.tool == "rectangle":
                self._shape[1] = (x, y)
            elif self.tool == "lasso":
                lx, ly = self._shape[-1]
                if (lx - x) ** 2 + (ly - y) ** 2 > 9:
                    self._shape.append((x, y))
            self.update()
            return
        i = self.nearest(x, y)
        if i != self.hover_i:
            self.hover_i = i
            self.hovered.emit(i)
            self.update()

    def mouseReleaseEvent(self, ev):
        x, y = self._pos(ev)
        if self._drag is not None:
            moved = abs(x - self._drag[4]) + abs(y - self._drag[5]) > 3
            self._drag = None
            self.setCursor(Qt.CursorShape.OpenHandCursor if self.tool == "pan" else Qt.CursorShape.CrossCursor)
            if moved:
                self.viewChanged.emit()
            else:
                i = self.nearest(x, y)
                if i >= 0:
                    self.mark_i = i
                    self.picked.emit(i)
                    self.update()
            return
        if ev.button() == Qt.MouseButton.LeftButton and self.tool in ("rectangle", "lasso"):
            self._finish_shape()

    def mouseDoubleClickEvent(self, ev):
        if self.tool == "polygon":
            self._finish_shape()
        elif self.tool == "pan":
            self.fit()

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key.Key_Escape:
            self._shape = []
            self.update()
        else:
            super().keyPressEvent(ev)

    def wheelEvent(self, ev):
        d = ev.angleDelta().y()
        if not d:
            return
        x, y = self._pos(ev)
        e, n = self.to_world(x, y)
        k = 1.25 if d > 0 else 0.8
        self.scale = min(5000.0, max(1e-4, self.scale * k))
        self.cx = e - (x - self.width() * 0.5) / self.scale
        self.cy = n + (y - self.height() * 0.5) / self.scale
        self.update()
        self.viewChanged.emit()

    def _finish_shape(self):
        shape, self._shape = self._shape, []
        self.update()
        tr = self.track
        if not tr or not tr.ok:
            return
        sx, sy = self.to_screen(tr.fe, tr.fn)
        if self.tool == "rectangle":
            if len(shape) < 2:
                return
            (ax, ay), (bx, by) = shape
            if abs(ax - bx) < 3 or abs(ay - by) < 3:
                return
            m = (sx >= min(ax, bx)) & (sx <= max(ax, bx)) & (sy >= min(ay, by)) & (sy <= max(ay, by))
        else:
            if len(shape) < 3:
                return
            if len(shape) > 300:
                shape = shape[::int(math.ceil(len(shape) / 300.0))]
            m = editor.inside_polygon(sx, sy, shape)
        self.selectedMask.emit(m & self.shown)


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------
class ProfilePlot(TimePlot):
    """Height against time; in select mode a drag picks a time range instead of panning."""
    hoveredTime = pyqtSignal(object)
    rangeSelected = pyqtSignal(float, float)

    def __init__(self, parent=None):
        super().__init__("Height", "m", parent)
        self.select_mode = False
        self.sel_windows = []
        self._sel_drag = None
        self.setMinimumHeight(170)

    def paintEvent(self, ev):
        super().paintEvent(ev)
        if self.x0 is None:
            return
        p = QPainter(self)
        pr = self._plot_rect()
        wins = list(self.sel_windows)
        if self._sel_drag is not None:
            a, b = self._sel_drag
            wins.append((min(a, b), max(a, b)))
        for a, b in wins:
            if b < self.x0 or a > self.x1:
                continue
            xa, xb = self._tx(max(a, self.x0), pr), self._tx(min(b, self.x1), pr)
            c = QColor(SEL)
            c.setAlpha(55)
            p.fillRect(QRectF(xa, pr.top(), max(2.0, xb - xa), pr.height()), c)
            p.setPen(QPen(SEL, 1))
            p.drawLine(QPointF(xa, pr.top()), QPointF(xa, pr.bottom()))
            p.drawLine(QPointF(xb, pr.top()), QPointF(xb, pr.bottom()))

    def mousePressEvent(self, ev):
        if self.select_mode and ev.button() == Qt.MouseButton.LeftButton and self.x0 is not None:
            pos = ev.position() if hasattr(ev, "position") else ev.pos()
            x = self._xat(pos.x())
            self._sel_drag = (x, x)
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._sel_drag is not None:
            pos = ev.position() if hasattr(ev, "position") else ev.pos()
            self._sel_drag = (self._sel_drag[0], self._xat(pos.x()))
            self.update()
            return
        super().mouseMoveEvent(ev)
        self.hoveredTime.emit(self.hover_x)

    def mouseReleaseEvent(self, ev):
        if self._sel_drag is not None:
            a, b = self._sel_drag
            self._sel_drag = None
            self.update()
            if abs(b - a) > 1e-3:
                self.rangeSelected.emit(min(a, b), max(a, b))
            return
        super().mouseReleaseEvent(ev)

    def leaveEvent(self, ev):
        super().leaveEvent(ev)
        self.hoveredTime.emit(None)


# ---------------------------------------------------------------------------
# the window
# ---------------------------------------------------------------------------
class TrajectoryEditor(PluginWindow):
    def __init__(self, dock):
        super().__init__(dock.iface, "pospro_editor", "PosPro – Trajectory editor", size=(1500, 940),
                         object_name="PosProEditor", min_size=(760, 480))
        self.dock = dock
        self.track = None
        self.sel_windows = []
        self._busy = False
        w = QWidget()
        theme.apply(w)
        outer = QVBoxLayout(w)
        outer.setContentsMargins(6, 4, 6, 4)
        outer.setSpacing(4)
        outer.addLayout(self._toolbar())
        outer.addLayout(self._actionbar())
        split = QSplitter(Qt.Orientation.Horizontal)
        views = QSplitter(Qt.Orientation.Vertical)
        self.plan = PlanView()
        self.profile = ProfilePlot()
        views.addWidget(self.plan)
        views.addWidget(self.profile)
        views.setStretchFactor(0, 3)
        views.setStretchFactor(1, 2)
        views.setSizes([560, 340])
        split.addWidget(views)
        split.addWidget(self._side())
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        split.setCollapsible(0, False)
        split.setSizes([1120, 360])
        outer.addWidget(split, 1)
        self.status = QLabel("")
        self.status.setObjectName("ppHint")
        outer.addWidget(self.status)
        self.setWidget(w)
        theme.apply_dock(self)
        theme.polish(w)
        # linking
        self.plan.hovered.connect(self._plan_hover)
        self.plan.picked.connect(self._plan_pick)
        self.plan.selectedMask.connect(self._plan_selected)
        self.plan.viewChanged.connect(self._plan_view_changed)
        self.profile.hoveredTime.connect(self._profile_hover)
        self.profile.timePicked.connect(self._profile_pick)
        self.profile.rangeSelected.connect(self._profile_selected)
        self.profile.xRangeChanged.connect(self._profile_range)
        self._bm_timer = QTimer(self)
        self._bm_timer.setSingleShot(True)
        self._bm_timer.setInterval(350)
        self._bm_timer.timeout.connect(self._render_basemap)
        self._bm_job = None

    # -------------------------------------------------------------- layout
    def _toolbar(self):
        h = QHBoxLayout()
        self.tools = QButtonGroup(self)
        self.tools.setExclusive(True)
        for key, label, tip in (("pan", "Pan", "Drag to move, wheel to zoom, click a point to show it in both views"),
                                ("rectangle", "Rectangle", "Drag a rectangle over the bad points in the plan view"),
                                ("lasso", "Lasso", "Drag around the bad points in the plan view"),
                                ("polygon", "Polygon", "Click the corners; right-click or double-click to finish"),
                                ("time", "Time range", "Drag across the profile to select a time range")):
            b = QToolButton()
            b.setText(label)
            b.setToolTip(tip)
            b.setCheckable(True)
            b.setProperty("tool", key)
            self.tools.addButton(b)
            h.addWidget(b)
            if key == "pan":
                b.setChecked(True)
        self.tools.buttonClicked.connect(self._tool_changed)
        h.addSpacing(12)
        for label, fn, tip in (("Whole job", self.zoom_all, "Show everything in the shown sections"),
                               ("Zoom to selection", self.zoom_selection, "Fit both views to the selection")):
            b = QPushButton(label)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            h.addWidget(b)
        h.addStretch(1)
        h.addWidget(QLabel("Colour"))
        self.colour = QComboBox()
        self.colour.addItem("Review state", "state")
        self.colour.addItem("Solution type", "fix")
        self.colour.currentIndexChanged.connect(self._colour_changed)
        h.addWidget(self.colour)
        self.c_dir = QCheckBox("Direction")
        self.c_dir.setChecked(True)
        self.c_dir.setToolTip("Chevrons along the track pointing the way the vehicle drove")
        self.c_dir.toggled.connect(self._dir_toggled)
        h.addWidget(self.c_dir)
        self.c_ant = QCheckBox("Antennas")
        self.c_ant.setToolTip("Draw where GPS1 (primary) and GPS2 (secondary) were along the track, from the "
                              "offsets on the Installation page; hover or click a point to see the vehicle there")
        self.c_ant.toggled.connect(self._ant_toggled)
        h.addWidget(self.c_ant)
        b = QPushButton("Check antennas")
        b.setToolTip("Measure where GPS2 really was relative to GPS1 while driving straight, and compare with the "
                     "installation (finds antennas plugged in the wrong way round)")
        b.clicked.connect(self._check_antennas)
        h.addWidget(b)
        self.bm = QCheckBox("Map layers underneath")
        self.bm.setToolTip("Draw the layers of the QGIS map (basemap, roads) under the plan view")
        self.bm.toggled.connect(self._bm_toggled)
        h.addWidget(self.bm)
        return h

    def _actionbar(self):
        h = QHBoxLayout()
        self.b_interp = QPushButton("Interpolate")
        self.b_interp.setObjectName("ppPrimary")
        self.b_interp.setToolTip("Replace the selected positions between the good points on each side")
        self.b_interp.clicked.connect(lambda: self.decide("interpolate"))
        h.addWidget(self.b_interp)
        self.method = QComboBox()
        self.method.addItem("Smooth curve", "smooth")
        self.method.addItem("Straight line", "straight")
        self.method.setToolTip("Smooth curve: leaves and meets the good points at their own speed and direction "
                               "(follows bends). Straight line: a straight line between them.")
        h.addWidget(self.method)
        self.c_h = QCheckBox("Horizontal")
        self.c_v = QCheckBox("Vertical")
        self.c_h.setChecked(True)
        self.c_v.setChecked(True)
        h.addWidget(self.c_h)
        h.addWidget(self.c_v)
        h.addSpacing(10)
        self.b_bridge = QPushButton("Bridge with IMU")
        self.b_bridge.setToolTip("Leave the GNSS out here and let the IMU carry the trajectory (processed again). "
                                 "Best for longer stretches: the IMU knows the turns and bumps.")
        self.b_bridge.clicked.connect(lambda: self.decide("exclude"))
        h.addWidget(self.b_bridge)
        for label, action, tip in (("Mark poor", "poor", "Keep it, but raise its σ so later stages know"),
                                   ("Accept", "accept", "Looked at and fine as it is"),
                                   ("Reject", "reject", "Mark it bad, to fix later (listed as open)")):
            b = QPushButton(label)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, a=action: self.decide(a))
            h.addWidget(b)
            setattr(self, "b_" + action, b)
        h.addSpacing(10)
        self.b_clearsel = QPushButton("Clear selection")
        self.b_clearsel.clicked.connect(self.clear_selection)
        h.addWidget(self.b_clearsel)
        self.b_undo = QPushButton("Undo")
        self.b_undo.clicked.connect(self.undo)
        h.addWidget(self.b_undo)
        h.addStretch(1)
        self.b_reproc = QPushButton("Process again")
        self.b_reproc.setObjectName("ppPrimary")
        self.b_reproc.setToolTip("Apply the IMU bridges: process the trajectory again")
        self.b_reproc.clicked.connect(self.dock.run_processing)
        h.addWidget(self.b_reproc)
        return h

    def _side(self):
        side = QWidget()
        v = QVBoxLayout(side)
        v.setContentsMargins(4, 0, 0, 0)
        k = QLabel("SECTIONS")
        k.setObjectName("ppKey")
        v.addWidget(k)
        self.sections = QListWidget()
        self.sections.itemChanged.connect(self._sections_changed)
        self.sections.itemDoubleClicked.connect(self._section_zoom)
        v.addWidget(self.sections, 2)
        r = QHBoxLayout()
        for label, fn in (("Show all", lambda: self._check_sections(True)),
                          ("Show none", lambda: self._check_sections(False)),
                          ("Show selected", self._show_selected_sections)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            r.addWidget(b)
        v.addLayout(r)
        k = QLabel("REVIEW LIST")
        k.setObjectName("ppKey")
        v.addWidget(k)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["#", "Start (UTC)", "Duration", "Decision"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._row_selected)
        v.addWidget(self.table, 3)
        r = QHBoxLayout()
        b = QPushButton("Find anomalies")
        b.setToolTip("Mark everything that misses the limits set on the Review page")
        b.clicked.connect(self.find)
        r.addWidget(b)
        r.addStretch(1)
        v.addLayout(r)
        side.setMinimumWidth(280)
        side.setMaximumWidth(460)
        return side

    # -------------------------------------------------------------- data
    @property
    def project(self):
        return self.dock.project

    def reload(self, keep_view=True):
        """Read the trajectory again (after processing or applied edits)."""
        p = self.project
        old = self.track
        self.track = editor.Track(p.work_dir) if p else None
        tr = self.track
        if tr is None or not tr.ok:
            self.plan.set_track(None)
            self.profile.clear()
            self.status.setText("Process the project first: the editor works on the processed trajectory.")
            return
        tr.set_states(p)
        shown = self.plan.shown if (keep_view and old is not None and self.plan.shown is not None and
                                    len(self.plan.shown) == len(tr.t)) else None
        view = (self.plan.cx, self.plan.cy, self.plan.scale) if keep_view and old is not None else None
        self.plan.set_track(tr)
        if shown is not None:
            self.plan.shown = shown
        self._fill_sections(keep=shown is not None)
        self._set_profile(keep_range=view is not None)
        if view:
            self.plan.cx, self.plan.cy, self.plan.scale = view
            self.plan.update()
        else:
            QTimer.singleShot(0, self.plan.fit)
        self._apply_selection_windows(self.sel_windows)
        self._ant_toggled(self.c_ant.isChecked())
        if getattr(self.dock, "antenna_check", None):
            self.set_antenna_check(self.dock.antenna_check)
        self.fill_table()
        self._update_buttons()

    def refresh_states(self):
        if self.track and self.track.ok:
            self.track.set_states(self.project)
            self._set_profile(keep_range=True)
            self.plan.update()
        self.fill_table()
        self._update_buttons()

    def _set_profile(self, keep_range=False):
        tr = self.track
        pl = self.profile
        x0, x1 = pl.x0, pl.x1
        pl.clear()
        t = tr.t.tolist()
        pl.add("Height", t, tr.fh.tolist(), theme.BRIGHT, 1.3)
        ch = tr.changed() | (tr.state == editor.STATE_OPEN)
        ch &= (np.abs(tr.fh - tr.oh) > 1e-4) | (tr.state == editor.STATE_OPEN)
        if ch.any():
            pl.add("As processed", t, [h if c else None for h, c in zip(tr.oh.tolist(), ch.tolist())],
                   "#e84855", 1.2)
        cols = {s: QColor(c) for s, c in results.STATUS_COLOURS.items()}
        pl.set_strip(t, tr.status.tolist(), cols)
        st = tr.state
        if len(st):
            edges = np.nonzero(np.diff(st))[0] + 1
            starts = np.concatenate(([0], edges))
            ends = np.concatenate((edges, [len(st)]))
            for a, b in zip(starts, ends):
                s = int(st[a])
                if s == editor.STATE_GOOD:
                    continue
                c = QColor(STATE_COLOURS[s])
                c.setAlpha(46)
                pl.add_band(float(tr.t[a]), float(tr.t[b - 1]), c)
        if keep_range and x0 is not None:
            pl.set_xrange(x0, x1)
        else:
            pl.reset_zoom()

    def _fill_sections(self, keep=False):
        tr = self.track
        checked = {}
        if keep:
            for i in range(self.sections.count()):
                it = self.sections.item(i)
                checked[i] = it.checkState() == Qt.CheckState.Checked
        self.sections.blockSignals(True)
        self.sections.clear()
        for i, (a, b) in enumerate(tr.sections):
            it = QListWidgetItem("Section %d   %s–%s UTC   %s" % (
                i + 1, geo.gps_to_utc(a).strftime("%H:%M:%S"), geo.gps_to_utc(b).strftime("%H:%M:%S"),
                geo.fmt_duration(b - a)))
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if checked.get(i, True) else Qt.CheckState.Unchecked)
            it.setData(Qt.ItemDataRole.UserRole, (a, b))
            self.sections.addItem(it)
        self.sections.blockSignals(False)
        self._sections_changed()

    def _sections_changed(self, *_):
        tr = self.track
        if not tr or not tr.ok:
            return
        m = np.zeros(len(tr.t), dtype=bool)
        for i in range(self.sections.count()):
            it = self.sections.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                a, b = it.data(Qt.ItemDataRole.UserRole)
                m[tr.span(a - 0.01, b + 0.01)] = True
        self.plan.shown = m
        self.plan.update()

    def _check_sections(self, on):
        self.sections.blockSignals(True)
        for i in range(self.sections.count()):
            self.sections.item(i).setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        self.sections.blockSignals(False)
        self._sections_changed()
        if on:
            self.zoom_all()

    def _show_selected_sections(self):
        rows = {i.row() for i in self.sections.selectedIndexes()}
        if not rows:
            return
        self.sections.blockSignals(True)
        for i in range(self.sections.count()):
            self.sections.item(i).setCheckState(Qt.CheckState.Checked if i in rows else Qt.CheckState.Unchecked)
        self.sections.blockSignals(False)
        self._sections_changed()
        self.zoom_all()

    def _section_zoom(self, it):
        a, b = it.data(Qt.ItemDataRole.UserRole)
        self._zoom_time(a, b)

    # -------------------------------------------------------------- review list
    def fill_table(self):
        p = self.project
        if not p:
            return
        r = review.state(p)
        rows = []
        seen = {}
        for a in r["anomalies"]:
            key = (round(a["t0"], 1), round(a["t1"], 1))
            if key in seen:
                k = seen[key]
                first = rows[k][0].split(" ")[0]
                more = int(rows[k][0].split("+")[1]) + 1 if "+" in rows[k][0] else 1
                rows[k] = ("%s +%d" % (first, more),) + rows[k][1:]
                continue
            seen[key] = len(rows)
            rows.append((a["id"], a["t0"], a["t1"], review.edit_for(p, a["t0"], a["t1"])))
        for e in r["edits"]:
            if not any(x[3] is e for x in rows):
                rows.append((e["id"], e["t0"], e["t1"], e))
        rows.sort(key=lambda x: x[1])
        self.rows = rows
        cols = {"exclude": theme.BLUE, "poor": theme.AMBER, "interpolate": theme.VIOLET, "accept": theme.GREEN,
                "reject": theme.RED}
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for i, (rid, t0, t1, e) in enumerate(rows):
            dec = review.ACTION_LABEL[e["action"]] if e else "open"
            if e and e["action"] == "interpolate":
                dec = review.METHODS.get(e.get("method", "straight"), "").capitalize()
            elif e and e["action"] == "exclude":
                dec = "IMU bridge"
            for c, val in enumerate((rid, geo.gps_to_utc(t0).strftime("%H:%M:%S"), "%.1f s" % (t1 - t0), dec)):
                it = QTableWidgetItem(val)
                if c == 3:
                    it.setForeground(QColor(cols.get(e["action"], theme.TEXT) if e else theme.RED))
                self.table.setItem(i, c, it)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.blockSignals(False)

    def _row_selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        wins = [(self.rows[r][1], self.rows[r][2]) for r in rows if r < len(self.rows)]
        if not wins:
            return
        self._apply_selection_windows(wins)
        a, b = min(w[0] for w in wins), max(w[1] for w in wins)
        self._zoom_time(a, b, pad=0.6)

    def find(self):
        if not self.dock.qc:
            return
        n = len(review.detect(self.project))
        self.dock.save()
        self.dock.pages_review().fill()
        self.refresh_states()
        self.dock.toast("%d anomal%s found" % (n, "y" if n == 1 else "ies"), "ok" if not n else "warn")

    # -------------------------------------------------------------- selection
    def _apply_selection_windows(self, wins):
        self.sel_windows = list(wins)
        tr = self.track
        if tr and tr.ok:
            m = np.zeros(len(tr.t), dtype=bool)
            for a, b in wins:
                m[tr.span(a, b)] = True
            self.plan.sel = m
        self.profile.sel_windows = self.sel_windows
        self.plan.update()
        self.profile.update()
        self._update_buttons()

    def _plan_selected(self, mask):
        wins = editor.windows_from_mask(self.track.t, mask)
        if not wins:
            self.dock.toast("No part of the trajectory inside the selection", "warn")
            return
        self._apply_selection_windows(wins)

    def _profile_selected(self, a, b):
        self._apply_selection_windows([(a, b)])

    def clear_selection(self):
        self._apply_selection_windows([])

    def _update_buttons(self):
        n = len(self.sel_windows)
        mode = (self.project.cfg["processing"].get("mode") if self.project else "") or ""
        for b in (self.b_interp, self.b_poor, self.b_accept, self.b_reject, self.b_clearsel):
            b.setEnabled(n > 0 and not self._busy)
        self.b_bridge.setEnabled(n > 0 and mode in ("tc", "lc") and not self._busy)
        self.b_bridge.setVisible(mode in ("tc", "lc"))
        pending = bool(self.project) and review.exclusions_pending(self.project)
        self.b_reproc.setVisible(pending)
        self.b_undo.setEnabled(bool(self.project) and bool(review.state(self.project)["undo"]))
        if n:
            dur = sum(b - a for a, b in self.sel_windows)
            txt = "Selected: %d window%s, %.1f s. " % (n, "" if n == 1 else "s", dur)
        else:
            txt = "Select bad points with Rectangle, Lasso or Polygon in the plan view, or Time range in the profile. "
        if pending:
            txt += "IMU bridges are waiting: press Process again to apply them."
        self.status.setText(txt)

    # -------------------------------------------------------------- decisions
    def decide(self, action):
        if not self.sel_windows or not self.project:
            return
        p = self.project
        tr = self.track
        if action == "interpolate" and not (self.c_h.isChecked() or self.c_v.isChecked()):
            self.dock.toast("Tick Horizontal, Vertical or both", "warn")
            return
        for a, b in self.sel_windows:
            r = review.state(p)
            # a decision on exactly this window replaces the earlier one
            r["edits"] = [e for e in r["edits"] if not (abs(e["t0"] - a) < 0.01 and abs(e["t1"] - b) < 0.01)]
            review.add_edit(p, max(float(tr.t[0]), a), min(float(tr.t[-1]), b), action, source="manual",
                            horizontal=self.c_h.isChecked(), vertical=self.c_v.isChecked(),
                            method=self.method.currentData())
        self.dock.save()
        long_ = [b - a for a, b in self.sel_windows if b - a > 10.0]
        mode = self.project.cfg["processing"].get("mode")
        if action == "interpolate" and long_ and mode in ("tc", "lc"):
            self.dock.toast("Interpolated %.0f s: for stretches this long, Bridge with IMU usually follows the road "
                            "better (the IMU knows the turns and bumps)." % max(long_), "warn")
        self._apply_selection_windows([])         # show the result, not the selection
        self._busy = action in ("poor", "interpolate")
        self._update_buttons()
        self.dock.pages_review().after_edit(action in ("poor", "interpolate"))
        if action not in ("poor", "interpolate"):
            self.refresh_states()

    def undo(self):
        if review.undo(self.project):
            self.dock.save()
            self._busy = True
            self._update_buttons()
            self.dock.pages_review().after_edit(True)

    def edits_applied(self):
        """Called when the background apply of poor / interpolate edits has finished."""
        self._busy = False
        self.reload(keep_view=True)

    # -------------------------------------------------------------- linking
    def _plan_hover(self, i):
        tr = self.track
        self.profile.hover_x = float(tr.t[i]) if i >= 0 else None
        self.profile.update()
        if i >= 0:
            self._status_at(i)

    def _plan_pick(self, i):
        t = float(self.track.t[i])
        self.profile.set_marker(t)
        x0, x1 = self.profile.x0, self.profile.x1
        if x0 is not None and not (x0 <= t <= x1):
            half = 0.5 * (x1 - x0)
            self.profile.set_xrange(t - half, t + half)

    def _profile_hover(self, t):
        tr = self.track
        if not tr or not tr.ok:
            return
        i = tr.index_at(t) if t is not None else -1
        self.plan.hover_i = i
        self.plan.update()
        if i >= 0:
            self._status_at(i)

    def _profile_pick(self, t):
        tr = self.track
        i = tr.index_at(t)
        self.plan.mark_i = i
        x, y = self.plan.to_screen(tr.fe[i], tr.fn[i])
        if not (0 <= x <= self.plan.width() and 0 <= y <= self.plan.height()):
            self.plan.cx, self.plan.cy = float(tr.fe[i]), float(tr.fn[i])
            self._plan_view_changed()
        self.plan.update()

    def _profile_range(self, x0, x1):
        """The profile was zoomed or panned: the plan view follows when the stretch is off screen."""
        tr = self.track
        if not tr or not tr.ok:
            return
        sl = tr.span(x0, x1)
        m = np.zeros(len(tr.t), dtype=bool)
        m[sl] = True
        m &= self.plan.shown
        if not m.any():
            return
        vis = self.plan.in_view() & m
        if vis.sum() < 0.6 * m.sum():
            self.plan.fit(m)

    def _plan_view_changed(self):
        """The plan was zoomed or panned: the profile shows the time of what is in view."""
        tr = self.track
        if tr and tr.ok:
            idx = np.nonzero(self.plan.in_view())[0]
            if len(idx):
                a, b = float(tr.t[idx[0]]), float(tr.t[idx[-1]])
                if b - a >= 1.0:
                    self.profile.set_xrange(a - 0.02 * (b - a), b + 0.02 * (b - a))
        if self.bm.isChecked():
            self._bm_timer.start()

    def _zoom_time(self, a, b, pad=0.25):
        tr = self.track
        if not tr or not tr.ok:
            return
        span = max(b - a, 2.0)
        a, b = a - pad * span, b + pad * span
        m = np.zeros(len(tr.t), dtype=bool)
        m[tr.span(a, b)] = True
        if m.any():
            self.plan.fit(m)
        self.profile.set_xrange(a, b)

    def zoom_all(self):
        self.plan.fit()
        tr = self.track
        if tr and tr.ok and self.plan.shown is not None and self.plan.shown.any():
            idx = np.nonzero(self.plan.shown)[0]
            self.profile.set_xrange(float(tr.t[idx[0]]), float(tr.t[idx[-1]]))

    def zoom_selection(self):
        if self.sel_windows:
            self._zoom_time(min(a for a, _ in self.sel_windows), max(b for _, b in self.sel_windows))

    def _status_at(self, i):
        tr = self.track
        lat, lon = tr.lat_lon_deg(i)
        txt = "%s UTC   %.7f, %.7f   h %.3f m   %s" % (
            geo.gps_to_utc(float(tr.t[i])).strftime("%H:%M:%S.%f")[:-4], lat, lon, tr.fh[i],
            results.STATUS.get(int(tr.status[i]), ""))
        if tr.state[i] != editor.STATE_GOOD:
            txt += "   · %s" % editor.STATE_LABEL[int(tr.state[i])]
        self.status.setText(txt)

    # -------------------------------------------------------------- tools, colours, basemap
    def _tool_changed(self, b):
        key = b.property("tool")
        self.profile.select_mode = key == "time"
        self.plan.tool = "pan" if key == "time" else key
        self.plan.setCursor(Qt.CursorShape.OpenHandCursor if self.plan.tool == "pan" else Qt.CursorShape.CrossCursor)

    def _dir_toggled(self, on):
        self.plan.show_dir = bool(on)
        self.plan.update()

    def _ant_toggled(self, on):
        tr = self.track
        if on and tr and tr.ok and self.project:
            inst = self.project.cfg["installation"]
            self.plan.antennas = {"gps1": tr.antenna(inst.get("lever_antenna") or [0, 0, 0]),
                                  "gps2": tr.antenna(inst.get("lever_antenna2") or [0, 0, 0])}
        else:
            self.plan.antennas = None
        self.plan.update()

    def _check_antennas(self):
        from . import antennacheck
        antennacheck.run(self.dock, self)

    def set_antenna_check(self, res):
        """Show GPS2 as measured: GPS1 (installed) + the measured baseline, turned with the direction of
        travel at each epoch."""
        tr = self.track
        pts = res.get("points") or []
        if not tr or not tr.ok or not pts or res.get("basis") != "course":
            self.plan.measured = None
            self.plan.update()
            return
        inst = self.project.cfg["installation"]
        g1n, g1e = tr.antenna(inst.get("lever_antenna") or [0, 0, 0])
        t = np.array([q[0] for q in pts])
        fwd = np.array([q[1] for q in pts])
        stb = np.array([q[2] for q in pts])
        k = np.clip(np.searchsorted(tr.t, t), 0, len(tr.t) - 1)
        c = tr.course[k]
        n = g1n[k] + fwd * np.cos(c) - stb * np.sin(c)
        e = g1e[k] + fwd * np.sin(c) + stb * np.cos(c)
        self.plan.measured = (n, e)
        if not self.c_ant.isChecked():
            self.c_ant.setChecked(True)
        self.plan.update()

    def _colour_changed(self, *_):
        self.plan.colour_by = self.colour.currentData()
        self.plan.update()

    def _bm_toggled(self, on):
        if on:
            self._render_basemap()
        else:
            self.plan.basemap = None
            self.plan.update()

    def _render_basemap(self):
        """Draw the QGIS map's own layers (not PosPro's) for the plan's extent. The plan's metres are linear
        in latitude and longitude, so a map drawn in geographic coordinates lines up exactly."""
        tr = self.track
        if not tr or not tr.ok or not self.bm.isChecked():
            return
        try:
            from qgis.core import (QgsCoordinateReferenceSystem, QgsMapRendererParallelJob, QgsMapSettings,
                                   QgsRectangle)
            from qgis.PyQt.QtCore import QSize
            from . import maplayers
            e0, n0, e1, n1 = self.plan.extent()
            k_n, k_e = tr._k
            lon0, lat0 = math.degrees(tr.lon0), math.degrees(tr.lat0)
            box = (lon0 + math.degrees(e0 / k_e), lat0 + math.degrees(n0 / k_n),
                   lon0 + math.degrees(e1 / k_e), lat0 + math.degrees(n1 / k_n))
            mine = set()
            g = maplayers.group(self.project, create=False)
            if g is not None:
                mine = {lyr.layerId() for lyr in g.findLayers()}
            layers = [lyr for lyr in self.dock.iface.mapCanvas().layers() if lyr.id() not in mine]
            if not layers:
                self.dock.toast("No other layers in the QGIS map to draw underneath", "warn")
                return
            ms = QgsMapSettings()
            ms.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
            ms.setLayers(layers)
            ms.setOutputSize(QSize(max(64, self.plan.width()), max(64, self.plan.height())))
            ms.setExtent(QgsRectangle(*box))
            ms.setBackgroundColor(QColor(theme.VIEW_BG))
            ve = ms.visibleExtent()
            local = (math.radians(ve.xMinimum() - lon0) * k_e, math.radians(ve.yMinimum() - lat0) * k_n,
                     math.radians(ve.xMaximum() - lon0) * k_e, math.radians(ve.yMaximum() - lat0) * k_n)
            if self._bm_job is not None:
                try:
                    self._bm_job.cancelWithoutBlocking()
                except Exception:       # noqa: BLE001
                    pass
            job = QgsMapRendererParallelJob(ms)

            def done():
                if job is self._bm_job and self.bm.isChecked():
                    self.plan.basemap = (job.renderedImage(), local)
                    self.plan.update()
            job.finished.connect(done)
            self._bm_job = job
            job.start()
        except Exception as e:      # noqa: BLE001 - the plan works without it
            self.dock.log("warning", "map layers under the editor: %s" % e)

    def show_editor(self):
        self.reload(keep_view=self.track is not None)
        self.show_window()
