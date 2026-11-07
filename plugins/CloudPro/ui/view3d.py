"""Interactive 3-D point view (software renderer, works on every QGIS
install without OpenGL drivers).

Mouse: left drag = orbit around the centre (the centre of the Inspection
Area; it follows the area when it moves), Shift+left or middle drag = pan,
wheel = zoom, double click = new centre at the point under the cursor
(the Zoom to Inspection Area button or 0 / Home bring it back), right drag = edit selection (lasso or
rectangle) with the active edit.  Keys: W/S fly forward/back, A/D strafe,
Q/E down/up by the navigation step; 1 top, 2 along, 3 across, 4 oblique,
0/Home = fit; Esc cancels a selection.
"""
import math
import threading
import time

import numpy as np

from ..qt import QtCore, QtGui, QtWidgets, Qt, Signal, evpos, mods
from . import render, theme


def _project_local(L, cam):
    """Local float32 points (P - origin) -> screen x, y and camera depth (float32), one matrix product."""
    R = L @ cam["M"]
    R -= cam["t"]
    xc, yc, zc = R[:, 0], R[:, 1], R[:, 2]
    W, H = cam["W"], cam["H"]
    if cam["ortho"]:
        sx = W / 2 + xc * cam["s"]
        sy = H / 2 - yc * cam["s"]
    else:
        with np.errstate(divide="ignore", invalid="ignore"):
            inv = cam["f"] / zc
            sx = W / 2 + xc * inv
            sy = H / 2 - yc * inv
    return sx, sy, zc


def _frame(L, col, cam, m, size, edl, strength):
    """One picture of the first m points (pure numpy, safe in a worker thread)."""
    sx, sy, zc = _project_local(L[:m], cam)
    buf, depth, _nvis = render.rasterise_nearest(sx, sy, zc, col[:m], cam["W"], cam["H"], size)
    if edl:
        buf = render.eye_dome(buf, depth, strength)
    return np.ascontiguousarray(buf, np.uint32)


class CloudView3D(QtWidgets.QWidget):
    selectionMade = Signal(object, str)
    pointPicked = Signal(int)
    viewChanged = Signal()
    legendMenu = Signal(object)                  # right-click on the colour scale bar (global pos)
    _stillReady = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(200, 150)
        self.P = np.zeros((0, 3))
        self.col = np.zeros(0, np.uint32)
        self.idx = np.zeros(0, np.int64)
        self.center = np.zeros(3)
        self.dist = 50.0
        self.yaw = math.radians(30)        # camera azimuth (clockwise from north)
        self.pitch = math.radians(35)      # elevation above horizon
        self.roll = 0.0                    # rotation of the picture about the viewing direction
        self.nav_step = 22.5               # degrees per click on the navigation arrows
        self.nav_hold_step = 2.5           # degrees per repeat (every 40 ms) while an arrow is held
        self.area = None                   # Inspection Area (cx, cy, length, width, bearing): the orbit centre
        self._area_z = False               # the centre's height still to be taken from the next points
        self._nav_hot = None
        self._nav_timer = QtCore.QTimer(self)
        self._nav_timer.timeout.connect(self._nav_repeat)
        self.fov = math.radians(50)
        self.ortho = False
        self.zscale = 1.0
        self.point_size = 2
        self.edl = True
        self.edl_strength = 1.0
        self.lod_max = 600_000
        self.step = 1.0
        self.sel_mode = "lasso"
        self.box_bearing = 0.0
        self.overlay_lines = []          # list of (N,3) arrays + colour
        self.title = "3D"
        self._img = None
        self._dirty = True
        self._interacting = False
        self._drag = None
        self._poly = []
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._end)
        self._last_proj = None
        self._L = np.zeros((0, 3), np.float32)      # P - origin in float32: half the memory traffic
        self._origin = np.zeros(3)
        self.shown = 0
        # full-detail still frames are drawn in a worker thread so the window never waits for them;
        # while moving, a reduced frame sized for ~30 frames/s is drawn at once
        self.async_still = True
        self._gen = 0
        self._busy = False
        self._again = False
        self._stillReady.connect(self._on_still)

    # ---------------------------------------------------------------- data
    def set_points(self, P, col, idx, fit=False):
        self.P, self.col, self.idx = P, col, idx
        if len(P) and P.dtype == np.float32:
            # already local single precision (CloudPro's loaded points are millimetres from the project
            # origin): used as they are, no second copy
            self._origin = np.zeros(3)
            self._L = np.ascontiguousarray(P)
        elif len(P):
            self._origin = np.round(np.asarray(P[len(P) // 2], float), -1)
            self._L = (P - self._origin).astype(np.float32)
        else:
            self._L = np.zeros((0, 3), np.float32)
        if fit:
            self.fit()
        elif self._area_z and len(P):
            self.center = self._area_centre()
            self._area_z = False
            self.viewChanged.emit()
        self._dirty = True
        self.update()

    def set_colours(self, col):
        self.col = col
        self._dirty = True
        self.update()

    def _cam(self):
        """Camera snapshot for _project_local: local points @ M - t = camera coordinates."""
        eye_dir, fwd, right, up = self._basis()
        zs = np.array([1.0, 1.0, self.zscale])
        B = np.column_stack([right, up, fwd])
        c = (self.center - self._origin) * zs + eye_dir * self.dist
        W, H = max(1, self.width()), max(1, self.height())
        return {"M": (B * zs[:, None]).astype(np.float32), "t": (c @ B).astype(np.float32), "W": W, "H": H,
                "ortho": self.ortho, "s": (H / 2) / (self.dist * math.tan(self.fov / 2)),
                "f": (H / 2) / math.tan(self.fov / 2)}

    def set_area(self, box):
        """The Inspection Area: the orbit centre moves to its centre (keeping the angles and distance), so
        turning always happens around the area, wherever it is moved."""
        self.area = dict(box) if box else None
        if self.area is None:
            return
        self.center = self._area_centre()      # height from the points shown now, until ...
        self._area_z = True                    # ... the new area's points arrive
        self._dirty = True
        self.viewChanged.emit()
        self.update()

    def _zrange(self):
        z = self.P[:, 2]
        if len(z) > 400_000:
            z = z[::len(z) // 200_000]
        return (float(np.percentile(z, 2)), float(np.percentile(z, 98))) if len(z) > 1000 else \
            (float(z.min()), float(z.max()))

    def _zmid(self):
        z = self.P[:, 2]
        if len(z) > 400_000:
            z = z[::len(z) // 200_000]
        return float(np.median(z))

    def _area_centre(self):
        """The area's centre at the median height of its points (near street level, not mid-air)."""
        a = self.area
        return np.array([a["cx"], a["cy"], self._zmid() if len(self.P) else float(self.center[2])])

    def fit(self):
        """Zoom to the Inspection Area (to the points when there is no area): centre on it, whole area in view."""
        if self.area is not None:
            a = self.area
            self.center = self._area_centre() if len(self.P) else np.array([a["cx"], a["cy"], self.center[2]])
            self._area_z = not len(self.P)
            zr = self._zrange() if len(self.P) else (0.0, 0.0)
            dz = min((zr[1] - zr[0]) * self.zscale, max(a["length"], a["width"]))   # tall outliers: ignored
            r = math.sqrt(a["length"] ** 2 + a["width"] ** 2 + dz ** 2) / 2
            self.dist = max(2.0, r / math.tan(self.fov / 2) * 1.05)
            self._dirty = True
            self.viewChanged.emit()
            self.update()
            return
        if len(self.P) == 0:
            return
        lo = np.percentile(self.P, 1, axis=0) if len(self.P) > 1000 else self.P.min(0)
        hi = np.percentile(self.P, 99, axis=0) if len(self.P) > 1000 else self.P.max(0)
        self.center = (lo + hi) / 2
        r = np.linalg.norm(hi - lo) / 2
        self.dist = max(2.0, r / math.tan(self.fov / 2) * 1.05)
        self._dirty = True
        self.viewChanged.emit()
        self.update()

    def preset(self, name):
        b = math.radians(self.box_bearing)
        self.roll = 0.0
        if name == "top":
            self.pitch = math.radians(89.9)
            self.yaw = b + math.pi
        elif name == "along":
            self.pitch = math.radians(2)
            self.yaw = b + math.pi / 2           # look across the box: see the profile
        elif name == "across":
            self.pitch = math.radians(2)
            self.yaw = b + math.pi               # look along the box axis: see the section
        else:
            self.pitch = math.radians(35)
            self.yaw = b + math.radians(210)
        self._dirty = True
        self.viewChanged.emit()
        self.update()

    # ------------------------------------------------------------ camera
    def _basis(self):
        cp, sp = math.cos(self.pitch), math.sin(self.pitch)
        cy, sy = math.cos(self.yaw), math.sin(self.yaw)
        # camera position relative to centre (E, N, U); yaw = azimuth of the camera from the centre
        eye_dir = np.array([sy * cp, cy * cp, sp])
        fwd = -eye_dir
        right = np.cross(fwd, [0, 0, 1.0])
        nr = np.linalg.norm(right)
        right = right / nr if nr > 1e-9 else np.array([math.cos(self.yaw), -math.sin(self.yaw), 0])
        up = np.cross(right, fwd)
        if self.roll:
            c, s_ = math.cos(self.roll), math.sin(self.roll)
            right, up = right * c + up * s_, up * c - right * s_
        return eye_dir, fwd, right, up

    # ------------------------------------------------------------ navigation arrows (top left)
    NAV = 52                               # size of the control in pixels (half the 1.3 size)

    @property
    def _k(self):
        return self.NAV / 104.0             # every size below was drawn for 104 px

    def _nav_geom(self):
        """Centre and the click spots of the arrow heads: key -> (centre point, radius)."""
        n = self.NAV
        k = self._k
        ox, oy = 6, 6
        c = QtCore.QPointF(ox + n / 2, oy + n / 2)
        a = n * 0.40
        r = n * 0.33
        return c, a, r, {
            "up": (QtCore.QPointF(c.x(), c.y() - a), max(8, 13 * k)),
            "down": (QtCore.QPointF(c.x(), c.y() + a), max(8, 13 * k)),
            "left": (QtCore.QPointF(c.x() - a, c.y()), max(8, 13 * k)),
            "right": (QtCore.QPointF(c.x() + a, c.y()), max(8, 13 * k)),
            "cw": (QtCore.QPointF(c.x() + (r + 8 * k) * math.cos(math.radians(20)),
                                  c.y() - (r + 8 * k) * math.sin(math.radians(20))), max(8, 14 * k)),
            "ccw": (QtCore.QPointF(c.x() + (r + 8 * k) * math.cos(math.radians(240)),
                                   c.y() - (r + 8 * k) * math.sin(math.radians(240))), max(8, 14 * k))}

    def _nav_hit(self, pos):
        _, _, _, spots = self._nav_geom()
        for k, (pt, rad) in spots.items():
            if (pos.x() - pt.x()) ** 2 + (pos.y() - pt.y()) ** 2 <= rad * rad:
                return k
        return None

    def nav_rotate(self, key, deg=None):
        """Turn the cloud the way the arrow points: up / down (into the page), left / right, cw / ccw on screen."""
        d = math.radians(self.nav_step if deg is None else deg)
        if key == "up":
            self.pitch = max(-math.radians(89.9), self.pitch - d)
        elif key == "down":
            self.pitch = min(math.radians(89.9), self.pitch + d)
        elif key == "right":
            self.yaw -= d
        elif key == "left":
            self.yaw += d
        elif key == "cw":
            self.roll += d
        elif key == "ccw":
            self.roll -= d
        self._changed()

    def _nav_repeat(self):
        if self._nav_hot and self._nav_timer.property("held"):
            self._nav_timer.setInterval(40)
            self.nav_rotate(self._nav_hot, self.nav_hold_step)

    def _draw_nav(self, p):
        c, a, r, spots = self._nav_geom()
        k = self._k
        p.save()
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QColor(38, 41, 45, 150))
        p.drawEllipse(c, self.NAV / 2, self.NAV / 2)

        def col(base, key):
            q = QtGui.QColor(base)
            return q.lighter(150) if key == self._nav_hot else q

        def head(tip, ux, uy, colour, size=11):
            size *= k
            px, py = -uy, ux
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(colour)
            p.drawPolygon(QtGui.QPolygonF([tip, QtCore.QPointF(tip.x() - ux * size + px * size * 0.65,
                                                               tip.y() - uy * size + py * size * 0.65),
                                           QtCore.QPointF(tip.x() - ux * size - px * size * 0.65,
                                                          tip.y() - uy * size - py * size * 0.65)]))
        blue, red, green = "#3d5afe", "#e84855", "#3ddc5a"
        # blue: up / down
        for key, sgn in (("up", -1), ("down", 1)):
            q = col(blue, key)
            p.setPen(QtGui.QPen(q, 4 * k, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
            p.drawLine(QtCore.QPointF(c.x(), c.y() + sgn * 5 * k), QtCore.QPointF(c.x(), c.y() + sgn * (a - 9 * k)))
            head(QtCore.QPointF(c.x(), c.y() + sgn * (a + 3 * k)), 0, sgn, q)
        for key, sgn in (("left", -1), ("right", 1)):
            q = col(red, key)
            p.setPen(QtGui.QPen(q, 4 * k, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
            p.drawLine(QtCore.QPointF(c.x() + sgn * 5 * k, c.y()), QtCore.QPointF(c.x() + sgn * (a - 9 * k), c.y()))
            head(QtCore.QPointF(c.x() + sgn * (a + 3 * k), c.y()), sgn, 0, q)
        # green arcs: top right clockwise (12 -> 3 o'clock), bottom left anticlockwise (9 -> 6 o'clock)
        rect = QtCore.QRectF(c.x() - r - 8 * k, c.y() - r - 8 * k, 2 * r + 16 * k, 2 * r + 16 * k)
        for key, start, span, tip_ang in (("cw", 85, -70, 10), ("ccw", 175, 70, 250)):
            q = col(green, key)
            p.setPen(QtGui.QPen(q, 3.5 * k, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawArc(rect, int(start * 16), int(span * 16))
            t = math.radians(tip_ang)
            tip = QtCore.QPointF(c.x() + (r + 8 * k) * math.cos(t), c.y() - (r + 8 * k) * math.sin(t))
            ux, uy = (math.sin(t), math.cos(t)) if key == "cw" else (-math.sin(t), -math.cos(t))
            head(tip, ux, uy, q, 10)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QColor(theme.SURF))
        p.drawEllipse(c, 4 * k, 4 * k)
        p.restore()

    def project(self, P):
        """World points -> screen x, y and camera depth."""
        eye_dir, fwd, right, up = self._basis()
        Q = P - self.center
        if self.zscale != 1.0:
            Q = Q * np.array([1, 1, self.zscale])
        cam = eye_dir * self.dist
        R = Q - cam
        xc = R @ right
        yc = R @ up
        zc = R @ fwd
        W, H = self.width(), self.height()
        if self.ortho:
            s = (H / 2) / (self.dist * math.tan(self.fov / 2))
            sx = W / 2 + xc * s
            sy = H / 2 - yc * s
        else:
            f = (H / 2) / math.tan(self.fov / 2)
            with np.errstate(divide="ignore", invalid="ignore"):
                sx = W / 2 + f * xc / zc
                sy = H / 2 - f * yc / zc
        return sx, sy, zc

    def _set_image(self, buf, m):
        H, W = buf.shape
        self.shown = m
        self._buf = buf
        self._img = QtGui.QImage(self._buf.data, W, H, 4 * W, QtGui.QImage.Format.Format_ARGB32)

    def _learn(self, m, dt, still):
        """Point budgets from this machine's own frame times (the points are in random order, so the first
        m are an even sample): ~30 frames/s for moving frames (no shading), ~0.3 s for a full still frame
        (with shading, drawn by the worker). Each kind of frame sets its own budget."""
        if m > 200_000:
            rate = m / max(dt, 1e-3)
            if still:
                self.rest_max = int(min(40e6, max(2_000_000, rate * 0.3)))
            else:
                self.lod_max = int(min(8e6, max(250_000, rate * 0.033)))

    def _render(self, quick=None):
        """Draw a frame now. quick: the reduced moving-frame budget (default: while interacting)."""
        n = len(self.P)
        if n == 0 or len(self._L) != n:
            self._img = None
            self.shown = 0
            return
        if quick is None:
            quick = self._interacting
        m = min(n, self.lod_max) if quick else min(n, getattr(self, "rest_max", 12_000_000))
        t0 = time.perf_counter()
        shade = self.edl and not self._interacting
        buf = _frame(self._L, self.col, self._cam(), m, self.point_size, shade, self.edl_strength)
        if not shade or not quick:
            self._learn(m, time.perf_counter() - t0, still=not quick)
        self._set_image(buf, m)

    # ------------------------------------------------------------ still frames in a worker
    def _kick_still(self):
        self._gen += 1
        if self._busy:
            self._again = True
            return
        self._start_still()

    def _start_still(self):
        n = len(self.P)
        if n == 0 or len(self._L) != n:
            return
        self._busy, self._again = True, False
        snap = (self._gen, self._L, self.col, self._cam(), min(n, getattr(self, "rest_max", 12_000_000)),
                self.point_size, self.edl, self.edl_strength)
        threading.Thread(target=self._work, args=(snap,), daemon=True).start()

    def _work(self, snap):
        gen, L, col, cam, m, size, edl, strength = snap
        t0 = time.perf_counter()
        try:
            buf = _frame(L, col, cam, m, size, edl, strength)
        except Exception:       # noqa: BLE001  (points replaced under us: the next frame redraws)
            buf = None
        try:
            self._stillReady.emit((gen, buf, m, time.perf_counter() - t0))
        except RuntimeError:    # the view was deleted meanwhile
            pass

    def _on_still(self, res):
        gen, buf, m, dt = res
        self._busy = False
        if buf is not None and gen == self._gen and not self._interacting and \
                buf.shape == (max(1, self.height()), max(1, self.width())):
            self._learn(m, dt, still=True)
            self._set_image(buf, m)
            self.update()
        elif self._again or (gen != self._gen and not self._interacting):
            self._start_still()

    def paintEvent(self, e):
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtGui.QColor(*theme.VIEW_BG))
        if self._dirty or self._img is None or self._img.width() != self.width() or \
                self._img.height() != self.height():
            if self.async_still and not self._interacting:
                # a quick frame now (with shading), the full one from the worker when it is ready
                self._render(quick=True)
                if self.shown < min(len(self.P), getattr(self, "rest_max", 12_000_000)):
                    self._kick_still()
                else:
                    self._gen += 1          # nothing more to draw: drop any older worker result
            else:
                self._gen += 1
                self._render()
            self._dirty = False
        if self._img is not None:
            p.drawImage(0, 0, self._img)
        else:
            p.setPen(QtGui.QColor(theme.DIM))
            p.setFont(theme.ui_font(14))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No points loaded")
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        for line, colour in self.overlay_lines:
            if len(line) < 2:
                continue
            sx, sy, zc = self.project(line)
            pen = QtGui.QPen(QtGui.QColor(colour), 1.5)
            p.setPen(pen)
            for i in range(len(line) - 1):
                if zc[i] > 0.05 and zc[i + 1] > 0.05:
                    p.drawLine(QtCore.QPointF(sx[i], sy[i]), QtCore.QPointF(sx[i + 1], sy[i + 1]))
        self._draw_axes(p)
        self._draw_nav(p)
        if self._poly:
            p.setPen(QtGui.QPen(QtGui.QColor(theme.ACCENT), 1.5, Qt.PenStyle.DashLine))
            p.setBrush(QtGui.QColor(0, 229, 170, 30))
            pts = [QtCore.QPointF(*q) for q in self._poly]
            if self.sel_mode == "rect" and len(pts) >= 2:
                p.drawRect(QtCore.QRectF(pts[0], pts[-1]).normalized())
            else:
                p.drawPolygon(QtGui.QPolygonF(pts))
        p.setFont(theme.ui_font(12))
        thin = getattr(self, "shown", len(self.P)) < len(self.P)
        txt = self.title + ("  ortho" if self.ortho else "") + \
            ("  showing %d %%" % (100 * self.shown / max(1, len(self.P))) if thin else "") + ("  Z x%.1f" % self.zscale if self.zscale != 1 else "")
        txt = txt.strip()
        if txt:
            w = p.fontMetrics().horizontalAdvance(txt) + 14
            x0 = self.NAV + 16
            p.fillRect(QtCore.QRectF(x0, 6, w, 20), QtGui.QColor(46, 50, 55, 210))
            p.setPen(QtGui.QColor(theme.ACCENT))
            p.drawText(QtCore.QRectF(x0, 6, w, 20), Qt.AlignmentFlag.AlignCenter, txt)
        if getattr(self, "legend", None):
            from . import legend
            legend.paint(p, self, self.legend, x=10, y=self.NAV + 16)
        p.end()

    def _draw_axes(self, p):
        eye_dir, fwd, right, up = self._basis()
        o = QtCore.QPointF(40, self.height() - 40)
        for v, lab, c in (((1, 0, 0), "E", theme.RED), ((0, 1, 0), "N", theme.ACCENT), ((0, 0, 1), "U", theme.BLUE)):
            v = np.array(v, float)
            dx, dy = v @ right, v @ up
            q = QtCore.QPointF(o.x() + dx * 26, o.y() - dy * 26)
            p.setPen(QtGui.QPen(QtGui.QColor(c), 2))
            p.drawLine(o, q)
            p.drawText(q + QtCore.QPointF(3, 3), lab)

    # ------------------------------------------------------------ interaction
    def _begin(self):
        self._interacting = True
        self._timer.start(250)

    def _end(self):
        self._interacting = False
        self._dirty = True
        self.update()

    def _changed(self):
        self._begin()
        self._dirty = True
        self.viewChanged.emit()
        self.update()

    def wheelEvent(self, e):
        d = e.angleDelta().y()
        if not d:
            return
        sh, ct, _ = mods(e)
        if ct:
            self.zscale = float(np.clip(self.zscale * 1.15 ** (d / 120), 0.1, 50))
        else:
            self.dist = float(np.clip(self.dist / 1.2 ** (d / 120), 0.2, 1e5))
        self._changed()

    def mousePressEvent(self, e):
        pos = evpos(e)
        self.setFocus()
        if e.button() == Qt.MouseButton.RightButton and getattr(self, "legend_rect", None) is not None and \
                self.legend_rect.contains(QtCore.QPointF(pos.x(), pos.y())):
            self.legendMenu.emit(self.mapToGlobal(QtCore.QPoint(int(pos.x()), int(pos.y()))))
            return
        if e.button() == Qt.MouseButton.LeftButton:
            k = self._nav_hit(pos)
            if k:
                self._nav_hot = k
                self.nav_rotate(k)
                self._nav_timer.setProperty("held", True)
                self._nav_timer.start(350)          # hold to keep turning
                return
        sh, ct, _ = mods(e)
        if e.button() == Qt.MouseButton.RightButton:
            self._poly = [(pos.x(), pos.y())]
            self._drag = ("sel", pos)
        elif e.button() == Qt.MouseButton.MiddleButton or (e.button() == Qt.MouseButton.LeftButton and sh):
            self._drag = ("pan", pos, self.center.copy())
        elif e.button() == Qt.MouseButton.LeftButton:
            self._drag = ("orbit", pos, self.yaw, self.pitch)

    def mouseMoveEvent(self, e):
        pos = evpos(e)
        if not self._drag:
            hot = self._nav_hit(pos)
            if hot != self._nav_hot and not self._nav_timer.isActive():
                self._nav_hot = hot
                self.setCursor(Qt.CursorShape.PointingHandCursor if hot else Qt.CursorShape.ArrowCursor)
                self.setToolTip({"up": "Turn the cloud up (hold to keep turning)", "down": "Turn the cloud down",
                                 "left": "Turn the cloud left", "right": "Turn the cloud right",
                                 "cw": "Turn the picture clockwise", "ccw": "Turn the picture anticlockwise"}
                                .get(hot, ""))
                self.update()
            return
        k = self._drag[0]
        if k == "orbit":
            _, p0, y0, pt0 = self._drag
            self.yaw = y0 - (pos.x() - p0.x()) * 0.008
            self.pitch = float(np.clip(pt0 + (pos.y() - p0.y()) * 0.008, -math.radians(89.9), math.radians(89.9)))
            self._changed()
        elif k == "pan":
            _, p0, c0 = self._drag
            eye_dir, fwd, right, up = self._basis()
            s = 2 * self.dist * math.tan(self.fov / 2) / max(self.height(), 1)
            d = (-(pos.x() - p0.x()) * right + (pos.y() - p0.y()) * up) * s
            if self.zscale != 1:
                d[2] /= self.zscale
            self.center = c0 + d
            self._changed()
        elif k == "sel":
            if self.sel_mode == "rect":
                self._poly = [self._poly[0], (pos.x(), pos.y())]
            else:
                last = self._poly[-1]
                if abs(last[0] - pos.x()) + abs(last[1] - pos.y()) > 2:
                    self._poly.append((pos.x(), pos.y()))
            self.update()

    def mouseReleaseEvent(self, e):
        if self._nav_timer.isActive():
            self._nav_timer.stop()
            self._nav_timer.setProperty("held", False)
            return
        if not self._drag:
            return
        k = self._drag[0]
        self._drag = None
        if k == "sel":
            poly = self._poly
            self._poly = []
            self.update()
            if self.sel_mode == "rect" and len(poly) == 2:
                (x0, y0), (x1, y1) = poly
                poly = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            if len(poly) >= 3:
                m = self.selection_mask(poly)
                if m is not None and m.any():
                    self.selectionMade.emit(m, "3d")
        else:
            self._end()

    def mouseDoubleClickEvent(self, e):
        pos = evpos(e)
        j = self.pick(pos.x(), pos.y())
        if j >= 0:
            self.center = self.P[j].copy()
            self.pointPicked.emit(j)
            self._changed()

    FLY_KEYS = (Qt.Key.Key_W, Qt.Key.Key_S, Qt.Key.Key_A, Qt.Key.Key_D, Qt.Key.Key_Q, Qt.Key.Key_E)

    def event(self, e):
        # with the focus here, the fly keys go to keyPressEvent, not to the window's one-letter shortcuts
        if e.type() == QtCore.QEvent.Type.ShortcutOverride and e.key() in self.FLY_KEYS and \
                not (e.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)):
            e.accept()
            return True
        return super().event(e)

    def keyPressEvent(self, e):
        K = Qt.Key
        k = e.key()
        eye_dir, fwd, right, up = self._basis()
        f = np.array([fwd[0], fwd[1], 0])
        if np.linalg.norm(f) < 1e-6:
            f = np.array([math.sin(self.yaw + math.pi), math.cos(self.yaw + math.pi), 0])
        f /= np.linalg.norm(f)
        r = np.array([right[0], right[1], 0])
        r /= max(np.linalg.norm(r), 1e-9)
        st = self.step
        moves = {K.Key_W: f * st, K.Key_S: -f * st, K.Key_A: -r * st, K.Key_D: r * st,
                 K.Key_E: np.array([0, 0, st]), K.Key_Q: np.array([0, 0, -st])}
        if k in moves:
            self.center = self.center + moves[k]
            self._changed()
        elif k == K.Key_1:
            self.preset("top")
        elif k == K.Key_2:
            self.preset("along")
        elif k == K.Key_3:
            self.preset("across")
        elif k == K.Key_4:
            self.preset("iso")
        elif k in (K.Key_0, K.Key_Home):
            self.fit()
        elif k == K.Key_O:
            self.ortho = not self.ortho
            self._changed()
        elif k == K.Key_Escape:
            self._poly = []
            self._drag = None
            self.update()
        elif k in (K.Key_Plus, K.Key_Equal):
            self.dist /= 1.2
            self._changed()
        elif k == K.Key_Minus:
            self.dist *= 1.2
            self._changed()
        else:
            super().keyPressEvent(e)

    def pick(self, x, y, radius=6):
        if len(self.P) == 0:
            return -1
        sx, sy, zc = _project_local(self._L, self._cam())
        d2 = (sx - x) ** 2 + (sy - y) ** 2
        cand = np.flatnonzero((d2 < radius * radius) & (zc > 0.05))
        if len(cand) == 0:
            return -1
        return int(cand[np.argmin(zc[cand])])

    def selection_mask(self, poly):
        W, H = self.width(), self.height()
        sx, sy, zc = _project_local(self._L, self._cam())
        img = QtGui.QImage(W, H, QtGui.QImage.Format.Format_Grayscale8)
        img.fill(0)
        p = QtGui.QPainter(img)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QColor(255, 255, 255))
        p.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(*q) for q in poly]))
        p.end()
        ptr = img.constBits()
        if hasattr(ptr, "setsize"):
            ptr.setsize(img.sizeInBytes() if hasattr(img, "sizeInBytes") else img.byteCount())
        arr = np.frombuffer(ptr, np.uint8).reshape(H, img.bytesPerLine())[:, :W]
        ix = np.nan_to_num(sx, nan=-1).astype(np.int64)
        iy = np.nan_to_num(sy, nan=-1).astype(np.int64)
        ok = (ix >= 0) & (iy >= 0) & (ix < W) & (iy < H) & (zc > 0.05)
        m = np.zeros(len(self.P), bool)
        m[ok] = arr[iy[ok], ix[ok]] > 0
        return m

    def resizeEvent(self, e):
        self._dirty = True
        super().resizeEvent(e)
