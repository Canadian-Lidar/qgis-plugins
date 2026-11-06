"""Pan/zoom 2-D point view used for the Plan, Profile and Section views.

Mouse: wheel = zoom at the cursor, Shift+wheel = vertical exaggeration
(profile/section), middle drag (or left drag in Pan) = pan, double click =
centre here.  Keys: arrows pan by the navigation step, +/- zoom, Home = zoom
all, Esc = cancel, Backspace = remove the last vertex, Enter = finish a
polygon or line.

Selection tools hand a boolean mask of the shown points to the main window,
which applies the active edit (reject / accept / classify).
"""
import math
import time

import numpy as np

from ..qt import QtCore, QtGui, QtWidgets, Qt, Signal, evpos, mods, exec_
from . import render, theme

TOOLS = ("pan", "rect", "poly", "lasso", "brush", "above", "below", "measure", "pick")


def nice_step(span, target=8):
    raw = span / max(target, 1)
    if raw <= 0:
        return 1.0
    p = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if m * p >= raw:
            return m * p
    return 10 * p


class CloudView2D(QtWidgets.QWidget):
    selectionMade = Signal(object, str)          # mask over shown points, tool name
    pointPicked = Signal(int)                    # index into the model point set (-1 none)
    cursorMoved = Signal(float, float)           # view coordinates (a, b)
    viewChanged = Signal()
    measured = Signal(str)
    keyNav = Signal(str)                         # navigation requests handled by the main window
    toolRequested = Signal(str)                  # a tool chosen in the right-click menu

    def __init__(self, kind="plan", parent=None):
        super().__init__(parent)
        self.kind = kind
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(200, 140)
        self.a = np.zeros(0)
        self.b = np.zeros(0)
        self.key = np.zeros(0)
        self.col = np.zeros(0, np.uint32)
        self.idx = np.zeros(0, np.int64)
        self.scale = 10.0                          # px per metre (horizontal)
        self.ve = 1.0                              # vertical exaggeration
        self.ca = 0.0
        self.cb = 0.0
        self.tool = "pan"
        self.point_size = 2
        self.lod_max = 700_000
        self.pan_step = 1.0
        self.a_offset = 0.0                         # added to labels (world coordinates)
        self.b_offset = 0.0
        self.a_label = "E"
        self.b_label = "N"
        self.brush_px = 18
        self.overlays = []                          # callables(painter, view)
        self.title = ""
        self._img = None
        self._img_key = None
        self._dirty = True
        self._interacting = False
        self._drag = None
        self._poly = []
        self._cursor = None
        self._measure = []
        self._sx = None
        self._sy = None
        self._hover = -1
        self._lod_timer = QtCore.QTimer(self)
        self._lod_timer.setSingleShot(True)
        self._lod_timer.timeout.connect(self._end_interaction)
        self.empty_text = "No points loaded"
        self.transparent = False             # points drawn over an underlay (plan view)
        self.menu_provider = None            # callable(menu, a, b, view) adding the main window's items
        self.highlight = None                # optional callable(painter, view) drawn over the points

    # ------------------------------------------------------------------ data
    def set_points(self, a, b, key, col, idx, fit=False):
        self.a, self.b, self.key, self.col, self.idx = a, b, key, col, idx
        if fit:
            self.zoom_all()
        self._dirty = True
        self.update()

    def set_colours(self, col):
        self.col = col
        self._dirty = True
        self.update()

    def zoom_all(self):
        if len(self.a) == 0:
            return
        a0, a1 = np.percentile(self.a, [0.5, 99.5]) if len(self.a) > 1000 else (self.a.min(), self.a.max())
        b0, b1 = np.percentile(self.b, [0.5, 99.5]) if len(self.b) > 1000 else (self.b.min(), self.b.max())
        self.ca, self.cb = (a0 + a1) / 2, (b0 + b1) / 2
        W, H = max(self.width(), 50), max(self.height(), 50)
        sa = (W - 40) / max(a1 - a0, 0.5)
        sb = (H - 40) / max((b1 - b0) * self.ve, 0.5)
        self.scale = max(1e-3, min(sa, sb))
        self._dirty = True
        self.viewChanged.emit()
        self.update()

    def center_on(self, a, b):
        self.ca, self.cb = a, b
        self._dirty = True
        self.viewChanged.emit()
        self.update()

    # ------------------------------------------------------------ transforms
    def to_screen(self, a, b):
        W, H = self.width(), self.height()
        return (a - self.ca) * self.scale + W / 2.0, H / 2.0 - (b - self.cb) * self.scale * self.ve

    def to_view(self, x, y):
        W, H = self.width(), self.height()
        return (x - W / 2.0) / self.scale + self.ca, (H / 2.0 - y) / (self.scale * self.ve) + self.cb

    # ------------------------------------------------------------- rendering
    def _render(self):
        W, H = self.width(), self.height()
        n = len(self.a)
        if n == 0:
            self._img = None
            self._sx = self._sy = None
            return
        m = n
        if self._interacting and n > self.lod_max:
            m = self.lod_max
        a, b, key, col = self.a[:m], self.b[:m], self.key[:m], self.col[:m]
        t0 = time.perf_counter()
        sx, sy = self.to_screen(a, b)
        buf = render.rasterise(sx, sy, key, col, W, H, self.point_size,
                               bg=0x00000000 if self.transparent else 0xFF09100F)
        if m > 200_000:              # budget while panning / zooming: ~30 frames/s on this machine
            self.lod_max = int(min(8e6, max(250_000, m / max(time.perf_counter() - t0, 1e-3) * 0.033)))
        self._buf = np.ascontiguousarray(buf)
        self._img = QtGui.QImage(self._buf.data, W, H, 4 * W, QtGui.QImage.Format.Format_ARGB32)
        if m == n:
            self._sx, self._sy = sx, sy
        else:
            self._sx = self._sy = None

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtGui.QColor(*theme.VIEW_BG))
        if self._dirty or self._img is None or self._img.width() != self.width() or \
                self._img.height() != self.height():
            self._render()
            self._dirty = False
        self.underlay(p)
        if self._img is not None:
            p.drawImage(0, 0, self._img)
        elif not self.has_underlay():
            p.setPen(QtGui.QColor(theme.DIM))
            p.setFont(theme.ui_font(14))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.empty_text)
        self._draw_grid(p)
        for ov in self.overlays:
            try:
                ov(p, self)
            except Exception:
                pass
        self._draw_tool(p)
        self._draw_title(p)
        if getattr(self, "legend", None):
            from . import legend
            legend.paint(p, self, self.legend, x=50, y=10)
        p.end()

    def underlay(self, p):
        pass

    def has_underlay(self):
        return False

    def _draw_title(self, p):
        if not self.title:
            return
        p.setFont(theme.ui_font(12))
        txt = self.title
        fm = p.fontMetrics()
        w = fm.horizontalAdvance(txt) + 14
        p.fillRect(QtCore.QRectF(6, 6, w, 20), QtGui.QColor(46, 50, 55, 210))
        p.setPen(QtGui.QColor(theme.ACCENT))
        p.drawText(QtCore.QRectF(6, 6, w, 20), Qt.AlignmentFlag.AlignCenter, txt)

    def _draw_grid(self, p):
        W, H = self.width(), self.height()
        a0, b1 = self.to_view(0, 0)
        a1, b0 = self.to_view(W, H)
        sa = nice_step(a1 - a0, max(3, W // 110))
        sb = nice_step(b1 - b0, max(3, H // 70))
        pen = QtGui.QPen(QtGui.QColor(160, 169, 177, 45))
        p.setPen(pen)
        p.setFont(theme.mono(10))
        ga = math.ceil((a0 + self.a_offset) / sa) * sa - self.a_offset
        while ga <= a1:
            x, _ = self.to_screen(ga, 0)
            p.drawLine(QtCore.QPointF(x, 0), QtCore.QPointF(x, H))
            ga += sa
        gb = math.ceil((b0 + self.b_offset) / sb) * sb - self.b_offset
        while gb <= b1:
            _, y = self.to_screen(0, gb)
            p.drawLine(QtCore.QPointF(0, y), QtCore.QPointF(W, y))
            gb += sb
        # labels
        p.setPen(QtGui.QColor(theme.DIM))
        dec_a = max(0, -int(math.floor(math.log10(sa)))) if sa < 1 else 0
        dec_b = max(0, -int(math.floor(math.log10(sb)))) if sb < 1 else 0
        ga = math.ceil((a0 + self.a_offset) / sa) * sa - self.a_offset
        while ga <= a1:
            x, _ = self.to_screen(ga, 0)
            p.drawText(QtCore.QPointF(x + 2, H - 4), "%.*f" % (dec_a, ga + self.a_offset))
            ga += sa
        gb = math.ceil((b0 + self.b_offset) / sb) * sb - self.b_offset
        while gb <= b1:
            _, y = self.to_screen(0, gb)
            p.drawText(QtCore.QPointF(3, y - 2), "%.*f" % (dec_b, gb + self.b_offset))
            gb += sb
        # scale bar
        L = nice_step((a1 - a0) / 4.0, 1)
        px = L * self.scale
        x0, y0 = W - px - 16, H - 18
        p.setPen(QtGui.QPen(QtGui.QColor(theme.BRIGHT), 2))
        p.drawLine(QtCore.QPointF(x0, y0), QtCore.QPointF(x0 + px, y0))
        p.drawLine(QtCore.QPointF(x0, y0 - 4), QtCore.QPointF(x0, y0 + 4))
        p.drawLine(QtCore.QPointF(x0 + px, y0 - 4), QtCore.QPointF(x0 + px, y0 + 4))
        p.drawText(QtCore.QPointF(x0, y0 - 6), ("%g m" % L) + ("  VE x%.1f" % self.ve if self.ve != 1 else ""))

    def _draw_tool(self, p):
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        acc = QtGui.QColor(theme.ACCENT)
        if self._poly:
            pen = QtGui.QPen(acc, 1.5, Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.setBrush(QtGui.QColor(0, 229, 170, 30) if self.tool in ("rect", "poly", "lasso") else
                       Qt.BrushStyle.NoBrush)
            pts = [QtCore.QPointF(*q) for q in self._poly]
            if self.tool == "rect" and len(pts) == 2:
                p.drawRect(QtCore.QRectF(pts[0], pts[1]).normalized())
            elif self.tool in ("above", "below"):
                if self._cursor is not None:
                    pts = pts + [self._cursor]
                p.drawPolyline(QtGui.QPolygonF(pts))
                # shade the side that will be edited
                if len(pts) >= 2:
                    sh = QtGui.QPolygonF(pts + ([QtCore.QPointF(pts[-1].x(), 0), QtCore.QPointF(pts[0].x(), 0)]
                                                if self.tool == "above" else
                                                [QtCore.QPointF(pts[-1].x(), self.height()),
                                                 QtCore.QPointF(pts[0].x(), self.height())]))
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(QtGui.QColor(232, 72, 85, 40))
                    p.drawPolygon(sh)
            else:
                if self.tool == "poly" and self._cursor is not None:
                    pts = pts + [self._cursor]
                p.drawPolygon(QtGui.QPolygonF(pts))
        if self.tool == "brush" and self._cursor is not None:
            p.setPen(QtGui.QPen(acc, 1))
            p.setBrush(QtGui.QColor(0, 229, 170, 40))
            p.drawEllipse(self._cursor, self.brush_px, self.brush_px)
        if self._measure:
            p.setPen(QtGui.QPen(QtGui.QColor(theme.AMBER), 1.5))
            pts = [QtCore.QPointF(*self.to_screen(*q)) for q in self._measure]
            if self._cursor is not None and len(self._measure) == 1:
                pts.append(self._cursor)
            if len(pts) >= 2:
                p.drawLine(pts[0], pts[1])
                (a0, b0) = self._measure[0]
                a1, b1 = self.to_view(pts[1].x(), pts[1].y()) if len(self._measure) == 1 else self._measure[1]
                txt = self.measure_text(a0, b0, a1, b1)
                p.setFont(theme.mono(11))
                r = QtCore.QRectF(pts[1].x() + 8, pts[1].y() + 8, 220, 46)
                p.fillRect(r, QtGui.QColor(46, 50, 55, 230))
                p.setPen(QtGui.QColor(theme.BRIGHT))
                p.drawText(r.adjusted(5, 2, 0, 0), Qt.AlignmentFlag.AlignLeft, txt)
            for q in pts[:2]:
                p.drawEllipse(q, 3, 3)
        if self._hover >= 0 and self._sx is not None and self._hover < len(self._sx):
            x, y = self._sx[self._hover], self._sy[self._hover]
            p.setPen(QtGui.QPen(QtGui.QColor(theme.BRIGHT), 1))
            p.drawLine(QtCore.QPointF(x - 7, y), QtCore.QPointF(x + 7, y))
            p.drawLine(QtCore.QPointF(x, y - 7), QtCore.QPointF(x, y + 7))

    def measure_text(self, a0, b0, a1, b1):
        da, db = a1 - a0, b1 - b0
        if self.kind == "plan":
            d = math.hypot(da, db)
            brg = (math.degrees(math.atan2(da, db)) + 360) % 360
            return "dist %.3f m\nbearing %.2f deg" % (d, brg)
        d = math.hypot(da, db)
        sl = db / da * 100 if abs(da) > 1e-6 else float("inf")
        return "dH %.3f  dV %+.3f m\nslope %.1f%%  dist %.3f" % (abs(da), db, sl, d)

    # ------------------------------------------------------------- interaction
    def _begin_interaction(self):
        self._interacting = True
        self._lod_timer.start(220)

    def _end_interaction(self):
        self._interacting = False
        self._dirty = True
        self.update()

    def wheelEvent(self, e):
        d = e.angleDelta().y()
        if d == 0:
            return
        f = 1.25 ** (d / 120.0)
        pos = evpos(e)
        sh, ct, _ = mods(e)
        if sh and self.kind != "plan":
            self.ve = float(np.clip(self.ve * f, 0.1, 50))
        else:
            a, b = self.to_view(pos.x(), pos.y())
            self.scale = float(np.clip(self.scale * f, 1e-3, 1e5))
            a2, b2 = self.to_view(pos.x(), pos.y())
            self.ca += a - a2
            self.cb += b - b2
        self._begin_interaction()
        self._dirty = True
        self.viewChanged.emit()
        self.update()

    def mouseDoubleClickEvent(self, e):
        if self.tool in ("poly", "above", "below") and self._poly:
            self._finish()
            return
        pos = evpos(e)
        self.center_on(*self.to_view(pos.x(), pos.y()))

    def mousePressEvent(self, e):
        pos = evpos(e)
        btn = e.button()
        self.setFocus()
        if btn == Qt.MouseButton.MiddleButton or (btn == Qt.MouseButton.LeftButton and self.tool == "pan"):
            self._drag = ("pan", pos, self.ca, self.cb)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if btn == Qt.MouseButton.RightButton:
            if self.tool in ("poly", "above", "below") and self._poly:
                self._poly.append((pos.x(), pos.y()))
                self._finish()
            else:
                self._context_menu(e)
            return
        if btn != Qt.MouseButton.LeftButton:
            return
        if self.tool == "rect":
            self._poly = [(pos.x(), pos.y()), (pos.x(), pos.y())]
            self._drag = ("rect",)
        elif self.tool == "lasso":
            self._poly = [(pos.x(), pos.y())]
            self._drag = ("lasso",)
        elif self.tool in ("poly", "above", "below"):
            self._poly.append((pos.x(), pos.y()))
        elif self.tool == "brush":
            self._poly = [(pos.x(), pos.y())]
            self._drag = ("brush",)
        elif self.tool == "measure":
            q = self.to_view(pos.x(), pos.y())
            if len(self._measure) != 1:
                self._measure = [q]
            else:
                self._measure.append(q)
                a0, b0 = self._measure[0]
                self.measured.emit(self.measure_text(a0, b0, *q).replace("\n", "   "))
        elif self.tool == "pick":
            self.pointPicked.emit(self.pick(pos.x(), pos.y()))
        self.update()

    def mouseMoveEvent(self, e):
        pos = evpos(e)
        self._cursor = pos
        a, b = self.to_view(pos.x(), pos.y())
        self.cursorMoved.emit(a, b)
        if self._drag:
            k = self._drag[0]
            if k == "pan":
                _, p0, ca, cb = self._drag
                self.ca = ca - (pos.x() - p0.x()) / self.scale
                self.cb = cb + (pos.y() - p0.y()) / (self.scale * self.ve)
                self._begin_interaction()
                self._dirty = True
                self.viewChanged.emit()
            elif k == "rect":
                self._poly[1] = (pos.x(), pos.y())
            elif k in ("lasso", "brush"):
                last = self._poly[-1]
                if abs(last[0] - pos.x()) + abs(last[1] - pos.y()) > 2:
                    self._poly.append((pos.x(), pos.y()))
        elif self.tool == "pick" or self.tool == "pan":
            if self._sx is not None and len(self._sx) < 3_000_000:
                h = self.pick(pos.x(), pos.y(), 5)
                if h != self._hover:
                    self._hover = h
        self.update()

    def mouseReleaseEvent(self, e):
        if self._drag:
            k = self._drag[0]
            self._drag = None
            self.unsetCursor()
            if k in ("rect", "lasso", "brush"):
                self._finish()
            elif k == "pan":
                self._end_interaction()

    def leaveEvent(self, e):
        self._cursor = None
        self._hover = -1
        self.update()

    def keyPressEvent(self, e):
        k = e.key()
        sh, ct, alt = mods(e)
        K = Qt.Key
        if k == K.Key_Escape:
            self._poly = []
            self._measure = []
            self.update()
        elif k == K.Key_Backspace and self._poly:
            self._poly.pop()
            self.update()
        elif k in (K.Key_Return, K.Key_Enter) and self._poly:
            self._finish()
        elif k in (K.Key_Plus, K.Key_Equal):
            self.scale *= 1.25
            self._dirty = True
            self.viewChanged.emit()
            self.update()
        elif k == K.Key_Minus:
            self.scale /= 1.25
            self._dirty = True
            self.viewChanged.emit()
            self.update()
        elif k == K.Key_Home:
            self.zoom_all()
        elif k in (K.Key_Left, K.Key_Right, K.Key_Up, K.Key_Down) and not sh and not ct:
            st = self.pan_step
            da = {K.Key_Left: -st, K.Key_Right: st}.get(k, 0)
            db = {K.Key_Up: st, K.Key_Down: -st}.get(k, 0)
            self.ca += da
            self.cb += db
            self._dirty = True
            self.viewChanged.emit()
            self.update()
        else:
            name = {K.Key_PageUp: "next", K.Key_PageDown: "prev", K.Key_BracketRight: "next",
                    K.Key_BracketLeft: "prev", K.Key_Period: "next", K.Key_Comma: "prev"}.get(k)
            big = {K.Key_BraceRight: "next", K.Key_BraceLeft: "prev", K.Key_Greater: "next", K.Key_Less: "prev"}.get(k)
            ctrl = bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier)
            if big:
                self.keyNav.emit(big + "_big")
            elif name:
                self.keyNav.emit(name + ("_big" if (sh or ctrl) else ""))
            elif sh and k in (K.Key_Left, K.Key_Right, K.Key_Up, K.Key_Down):
                self.keyNav.emit({K.Key_Left: "box_left", K.Key_Right: "box_right", K.Key_Up: "box_fwd",
                                  K.Key_Down: "box_back"}[k])
            else:
                super().keyPressEvent(e)

    def _context_menu(self, e):
        """Right click: the main window adds its items (selection, Inspection Area, slice) through
        menu_provider(menu, a, b); the tools follow."""
        pos = evpos(e)
        va, vb = self.to_view(pos.x(), pos.y())
        m = QtWidgets.QMenu(self)
        if self.menu_provider is not None:
            self.menu_provider(m, va, vb, self)
            m.addSeparator()
        tm = m.addMenu("Tool")
        acts = {}
        for t, lab in (("pan", "Pan"), ("rect", "Rectangle"), ("poly", "Polygon"), ("lasso", "Lasso"),
                       ("brush", "Brush"), ("above", "Above a line"), ("below", "Below a line"),
                       ("measure", "Measure"), ("pick", "Point information")):
            a = tm.addAction(lab)
            a.setCheckable(True)
            a.setChecked(self.tool == t)
            acts[a] = t
        za = m.addAction("Zoom to the points")
        theme.dark_menu(m)
        theme.dark_menu(tm)
        g = e.globalPosition().toPoint() if hasattr(e, "globalPosition") else e.globalPos()
        a = exec_(m, g)
        if a in acts:
            self.toolRequested.emit(acts[a])
        elif a is za:
            self.zoom_all()

    def toolChangedByMenu(self, t):
        pass

    def set_tool(self, t):
        self.tool = t
        self._poly = []
        self._measure = []
        self.setCursor(Qt.CursorShape.CrossCursor if t not in ("pan",) else Qt.CursorShape.OpenHandCursor)
        self.update()

    # --------------------------------------------------------------- selection
    def pick(self, x, y, radius=8):
        if self._sx is None or len(self._sx) == 0:
            return -1
        d2 = (self._sx - x) ** 2 + (self._sy - y) ** 2
        j = int(np.argmin(d2))
        if d2[j] > radius * radius:
            return -1
        return j

    def _finish(self):
        poly = self._poly
        tool = self.tool
        self._poly = []
        self.update()
        if not poly:
            return
        mask = self.selection_mask(poly, tool)
        if mask is not None and mask.any():
            self.selectionMade.emit(mask, tool)

    def selection_mask(self, poly, tool):
        n = len(self.a)
        if n == 0:
            return None
        sx, sy = self.to_screen(self.a, self.b)
        W, H = self.width(), self.height()
        if tool in ("above", "below"):
            if len(poly) < 2:
                return None
            vx = np.array([self.to_view(px, py)[0] for px, py in poly])
            vy = np.array([self.to_view(px, py)[1] for px, py in poly])
            o = np.argsort(vx)
            vx, vy = vx[o], vy[o]
            inside = (self.a >= vx[0]) & (self.a <= vx[-1])
            lb = np.interp(self.a, vx, vy)
            return inside & ((self.b > lb) if tool == "above" else (self.b < lb))
        img = QtGui.QImage(W, H, QtGui.QImage.Format.Format_Grayscale8)
        img.fill(0)
        p = QtGui.QPainter(img)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)
        if tool == "brush":
            p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255), 2 * self.brush_px, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            if len(poly) == 1:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QtGui.QColor(255, 255, 255))
                p.drawEllipse(QtCore.QPointF(*poly[0]), self.brush_px, self.brush_px)
            else:
                p.drawPolyline(QtGui.QPolygonF([QtCore.QPointF(*q) for q in poly]))
        else:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QtGui.QColor(255, 255, 255))
            if tool == "rect" and len(poly) >= 2:
                p.drawRect(QtCore.QRectF(QtCore.QPointF(*poly[0]), QtCore.QPointF(*poly[1])).normalized())
            elif len(poly) >= 3:
                p.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(*q) for q in poly]))
            else:
                p.end()
                return None
        p.end()
        ptr = img.constBits()
        if hasattr(ptr, "setsize"):
            ptr.setsize(img.sizeInBytes() if hasattr(img, "sizeInBytes") else img.byteCount())
        arr = np.frombuffer(ptr, np.uint8).reshape(H, img.bytesPerLine())[:, :W]
        ix = sx.astype(np.int64)
        iy = sy.astype(np.int64)
        ok = (ix >= 0) & (iy >= 0) & (ix < W) & (iy < H)
        mask = np.zeros(n, bool)
        mask[ok] = arr[iy[ok], ix[ok]] > 0
        return mask

    def resizeEvent(self, e):
        self._dirty = True
        super().resizeEvent(e)
