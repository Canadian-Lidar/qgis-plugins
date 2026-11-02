"""PosPro's own widgets (vehicle mounting drawing, data coverage bars). The
common ones (step pills, cards, sections, key/value grids, toasts, logo,
dock title bar) come from DCS Core and are re-exported here."""
import math

from qgis.PyQt.QtCore import QPointF, QRectF, Qt
from qgis.PyQt.QtGui import QColor, QPainter, QPen, QPolygonF
from qgis.PyQt.QtWidgets import QWidget

from dcs_core.widgets import (ACCENT, BAD, KV, MUTED, OK, STATE_LEVEL, WARN, Card, DockTitle, Logo,  # noqa: F401
                              Section, StepButton, Toast, _af)

from . import theme


# Vehicle outlines as the mapper's Settings > Hardware (session.VEHICLE_PRESETS)
VEHICLE_PRESETS = {
    "van":   {"label": "Van", "length_m": 5.98, "width_m": 2.07, "height_m": 2.55, "ins_from_rear_m": 0.40,
              "ins_right_of_centre_m": 0.0, "ins_above_ground_m": 2.85},
    "truck": {"label": "Truck", "length_m": 5.89, "width_m": 2.03, "height_m": 1.95, "ins_from_rear_m": 2.60,
              "ins_right_of_centre_m": 0.0, "ins_above_ground_m": 2.30},
    "car":   {"label": "Car", "length_m": 4.80, "width_m": 1.90, "height_m": 1.70, "ins_from_rear_m": 1.50,
              "ins_right_of_centre_m": 0.0, "ins_above_ground_m": 2.00},
}

# point roles in the drawing and the key (as the mapper's mounting drawings)
ROLE_STYLE = {
    "origin": ("Certus origin", theme.ACCENT),
    "gps1": ("GPS1 primary", theme.BLUE),
    "gps2": ("GPS2 secondary", theme.VIOLET),
    "output": ("Output point / Lidar", theme.AMBER),
    "axle": ("Rear axle", theme.MAPLE),
}


class VehicleView(QWidget):
    """Mounting drawing as the mapper's Settings > Hardware: plan (from
    above), profile (from starboard) and rear (looking forward) views of the
    vehicle with the Certus origin, both antennas, the output point and the
    rear axle. Vehicle frame: X forward, Y starboard, Z down, from the Certus."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.points = {}        # role -> (x, y, z, label)
        self.outline = dict(VEHICLE_PRESETS["van"])
        self.setMinimumHeight(250)

    def set_points(self, pts, outline=None):
        self.points = pts
        if outline:
            self.outline = dict(outline)
        self.update()

    def _box(self):
        o = self.outline
        x0 = -float(o.get("ins_from_rear_m", 0.4))
        y0 = -float(o.get("width_m", 2.0)) / 2 - float(o.get("ins_right_of_centre_m", 0.0))
        zg = float(o.get("ins_above_ground_m", 2.0))
        return (x0, x0 + float(o.get("length_m", 5.0)), y0, y0 + float(o.get("width_m", 2.0)),
                zg - float(o.get("height_m", 2.0)), zg)

    def paintEvent(self, ev):       # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        W, H = self.width(), self.height()
        p.fillRect(self.rect(), QColor(theme.SURF))
        p.setPen(QPen(QColor(29, 51, 48, 110), 1))
        for x in range(0, W, 24):
            p.drawLine(x, 0, x, H)
        for y in range(0, H, 24):
            p.drawLine(0, y, W, y)
        x0, x1, y0, y1, zt, zg = self._box()
        for (x, y, z, _l) in self.points.values():
            x0, x1, y0, y1, zt, zg = min(x0, x), max(x1, x), min(y0, y), max(y1, y), min(zt, z), max(zg, z)
        key_h = 22
        ph = H - key_h - 24
        # three panels: widths proportional to what they show
        spans = [(y1 - y0, x1 - x0), (x1 - x0, zg - zt), (y1 - y0, zg - zt)]   # (horizontal, vertical) metres
        avail, minw = W - 48, 128
        sc = min(avail / sum(s[0] * 1.15 for s in spans), ph / max(s[1] * 1.15 for s in spans))
        for _ in range(60):
            if sum(max(s[0] * 1.15 * sc, minw) for s in spans) <= avail:
                break
            sc *= 0.95
        titles = ["PLAN \u00b7 ABOVE", "PROFILE \u00b7 STARBOARD", "REAR \u00b7 LOOKING FWD"]
        left = 12.0
        bx = self._box()
        for k, (hs, vs) in enumerate(spans):
            pw = max(hs * 1.15 * sc, minw)
            r = QRectF(left, 20, pw, ph)
            self._caption(p, QRectF(left, 2, pw, 16), titles[k])
            self._panel(p, r, k, sc, bx, (x0, x1, y0, y1, zt, zg))
            left += pw + 12
            if k < 2:
                p.setPen(QPen(QColor(theme.BORDER), 1, Qt.PenStyle.DashLine))
                p.drawLine(QPointF(left - 6, 22), QPointF(left - 6, 20 + ph))
        # key
        kx = 12.0
        p.setFont(theme.font("ui", 11, 600, 106))
        for role, (lab, col) in ROLE_STYLE.items():
            if role not in self.points and role != "origin":
                continue
            self._mark(p, QPointF(kx + 5, H - key_h / 2 - 2), role, QColor(col))
            p.setPen(QColor(theme.TEXT))
            p.drawText(QRectF(kx + 14, H - key_h - 2, 160, key_h), _af(Qt.AlignmentFlag.AlignVCenter), lab.upper())
            kx += 22 + p.fontMetrics().horizontalAdvance(lab.upper())
        p.setPen(QPen(QColor(theme.ACCENT), 1.6))
        p.drawLine(QPointF(kx, H - key_h / 2 - 2), QPointF(kx + 18, H - key_h / 2 - 2))
        p.drawPolygon(QPolygonF([QPointF(kx + 22, H - key_h / 2 - 2), QPointF(kx + 16, H - key_h / 2 - 6),
                                 QPointF(kx + 16, H - key_h / 2 + 2)]))
        p.setPen(QColor(theme.TEXT))
        p.drawText(QRectF(kx + 28, H - key_h - 2, 80, key_h), _af(Qt.AlignmentFlag.AlignVCenter), "TRAVEL")
        p.end()

    def _panel(self, p, r, k, sc, bx, ext):
        x0, x1, y0, y1, zt, zg = ext
        # map metres to the panel: (h, v) per view
        if k == 0:          # plan: starboard right, forward up
            hc, vc = (y0 + y1) / 2, (x0 + x1) / 2

            def m(x, y, z):
                return QPointF(r.center().x() + (y - hc) * sc, r.center().y() - (x - vc) * sc)
            body = QRectF(m(bx[1], bx[2], 0), m(bx[0], bx[3], 0)).normalized()
            arrow = (m(bx[1] - 1.6, (bx[2] + bx[3]) / 2, 0), m(bx[1] - 0.4, (bx[2] + bx[3]) / 2, 0))
        elif k == 1:        # profile from starboard: forward right, down down
            hc, vc = (x0 + x1) / 2, (zt + zg) / 2

            def m(x, y, z):
                return QPointF(r.center().x() + (x - hc) * sc, r.center().y() + (z - vc) * sc)
            body = QRectF(m(bx[0], 0, bx[4]), m(bx[1], 0, bx[5] - 0.25)).normalized()
            arrow = (m(bx[1] - 1.6, 0, bx[4] + 0.35), m(bx[1] - 0.4, 0, bx[4] + 0.35))
        else:               # rear, looking forward: starboard right
            hc, vc = (y0 + y1) / 2, (zt + zg) / 2

            def m(x, y, z):
                return QPointF(r.center().x() + (y - hc) * sc, r.center().y() + (z - vc) * sc)
            body = QRectF(m(0, bx[2], bx[4]), m(0, bx[3], bx[5] - 0.25)).normalized()
            arrow = None
        self._vehicle(p, body)
        if k in (1, 2):     # wheels and ground
            p.setPen(QPen(QColor(93, 133, 124, 150), 1))
            g = m(bx[0], bx[2], bx[5]).y()
            p.drawLine(QPointF(r.left(), g), QPointF(r.right(), g))
            p.setBrush(QColor(theme.BG))
            rw = 0.33 * sc
            if k == 1:
                for xw in (bx[0] + 0.9, bx[1] - 0.9):
                    p.drawEllipse(QPointF(m(xw, 0, 0).x(), g - rw), rw, rw)
            else:
                for yw in (bx[2] + 0.1, bx[3] - 0.35):
                    p.drawRect(QRectF(m(0, yw, 0).x(), g - 2 * rw, 0.25 * sc, 2 * rw))
            p.setBrush(Qt.BrushStyle.NoBrush)
        if arrow:
            a, b = arrow
            p.setPen(QPen(QColor(theme.ACCENT), 1.6))
            p.drawLine(a, b)
            d = b - a
            n = max(1e-6, math.hypot(d.x(), d.y()))
            ux, uy = d.x() / n, d.y() / n
            p.setBrush(QColor(theme.ACCENT))
            p.drawPolygon(QPolygonF([b + QPointF(ux * 4, uy * 4), b + QPointF(-uy * 6 - ux * 4, ux * 6 - uy * 4),
                                     b + QPointF(uy * 6 - ux * 4, -ux * 6 - uy * 4)]))
            p.setBrush(Qt.BrushStyle.NoBrush)
        pts = dict(self.points)
        pts.setdefault("origin", (0.0, 0.0, 0.0, ""))
        for role in ("axle", "output", "gps2", "gps1", "origin"):
            if role in pts:
                x, y, z, _l = pts[role]
                self._mark(p, m(x, y, z), role, QColor(ROLE_STYLE[role][1]))

    def _mark(self, p, pt, role, col):
        p.setPen(QPen(QColor(theme.BG), 1.5))
        p.setBrush(col)
        if role == "origin":
            p.drawRect(QRectF(pt.x() - 5, pt.y() - 5, 10, 10))
        elif role == "axle":
            p.drawPolygon(QPolygonF([pt + QPointF(0, -6), pt + QPointF(6, 0), pt + QPointF(0, 6), pt + QPointF(-6, 0)]))
        else:
            p.drawEllipse(pt, 5, 5)
        p.setBrush(Qt.BrushStyle.NoBrush)

    def _caption(self, p, r, text):
        p.setPen(QColor(theme.DIM))
        p.setFont(theme.font("ui", 10, 600, 106))
        p.drawText(r, _af(Qt.AlignmentFlag.AlignHCenter), text)

    def _vehicle(self, p, r):
        p.setPen(QPen(QColor(93, 133, 124, 150), 1.2))
        p.setBrush(QColor(19, 35, 32, 200))
        p.drawRoundedRect(r, 6, 6)
        p.setBrush(Qt.BrushStyle.NoBrush)

    def _dot(self, p, pt, col, label):
        p.setPen(QPen(QColor(theme.BG), 1.5))
        p.setBrush(col)
        p.drawEllipse(pt, 5, 5)
        p.setBrush(Qt.BrushStyle.NoBrush)
        if label:
            p.setPen(QColor(col))
            p.setFont(theme.font("ui", 11, 700, 108))
            p.drawText(QRectF(pt.x() + 8, pt.y() - 9, 110, 16), _af(Qt.AlignmentFlag.AlignLeft), label.upper())


class CoverageView(QWidget):
    """Data coverage timescale bars as in Qinertia: one row per source (IMU,
    GNSS 1, GNSS 2, real-time solution, each base) coloured mint where the
    data is good, amber where degraded and red where missing, with the
    processing span marked. Hover for details."""

    ROW_H = 18

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []            # [(name, [[t0, t1, colour, label], ...])]
        self.t0 = self.t1 = 0
        self.span = None
        self.setMouseTracking(True)
        self._update_height()

    def _update_height(self):
        self.setMinimumHeight(max(1, len(self.rows)) * (self.ROW_H + 6) + 34)

    def set_data(self, rows, t0, t1, span=None):
        self.rows, self.t0, self.t1, self.span = rows, t0, max(t1, t0 + 1), span
        self._update_height()
        self.updateGeometry()
        self.update()

    def _geom(self):
        return 92.0, float(self.width() - 10)

    def _x(self, t):
        x0, x1 = self._geom()
        return x0 + (t - self.t0) / float(self.t1 - self.t0) * (x1 - x0)

    def paintEvent(self, ev):       # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.fillRect(self.rect(), QColor(theme.SURF))
        x0, x1 = self._geom()
        if not self.rows:
            p.setPen(QColor(theme.DIM))
            p.setFont(theme.font("ui", 12, 600, 106))
            p.drawText(QRectF(0, 0, self.width(), self.height()), _af(Qt.AlignmentFlag.AlignCenter),
                       "READ THE ROVER FILES TO SEE THE DATA COVERAGE")
            p.end()
            return
        y = 6.0
        for name, runs in self.rows:
            p.setPen(QColor(theme.TEXT))
            p.setFont(theme.font("ui", 11, 700, 106))
            p.drawText(QRectF(4, y, x0 - 10, self.ROW_H), _af(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                       name.upper())
            p.fillRect(QRectF(x0, y, x1 - x0, self.ROW_H), QColor(theme.BG))
            for a, b, col, _lab in runs:
                xa, xb = self._x(a), self._x(b)
                p.fillRect(QRectF(xa, y + 2, max(1.0, xb - xa), self.ROW_H - 4), QColor(col))
            y += self.ROW_H + 6
        # processing span
        if self.span:
            p.setPen(QPen(QColor(theme.BRIGHT), 1, Qt.PenStyle.DashLine))
            for t in self.span:
                if self.t0 <= t <= self.t1:
                    xx = self._x(t)
                    p.drawLine(QPointF(xx, 2), QPointF(xx, y - 4))
        # time axis (UTC)
        from ..core import geo
        p.setPen(QPen(QColor(theme.BORDER), 1))
        p.drawLine(QPointF(x0, y), QPointF(x1, y))
        dur = self.t1 - self.t0
        step = next((s for s in (10, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600)
                     if dur / s <= max(2, (x1 - x0) / 90)), 43200)
        p.setFont(theme.font("mono", 10))
        t = (int(self.t0) // step + 1) * step
        while t < self.t1:
            xx = self._x(t)
            p.setPen(QPen(QColor(theme.BORDER), 1))
            p.drawLine(QPointF(xx, y), QPointF(xx, y + 4))
            p.setPen(QColor(theme.DIM))
            u = geo.gps_to_utc(t)
            p.drawText(QRectF(xx - 40, y + 4, 80, 16), _af(Qt.AlignmentFlag.AlignHCenter),
                       u.strftime("%H:%M" if step >= 60 else "%H:%M:%S"))
            t += step
        p.end()

    def mouseMoveEvent(self, ev):   # noqa: N802
        if not self.rows:
            return
        pos = ev.position() if hasattr(ev, "position") else ev.pos()
        x0, x1 = self._geom()
        if pos.x() < x0 or pos.x() > x1:
            self.setToolTip("")
            return
        t = self.t0 + (pos.x() - x0) / (x1 - x0) * (self.t1 - self.t0)
        k = int((pos.y() - 6) // (self.ROW_H + 6))
        from ..core import geo
        tip = geo.gps_to_utc(t).strftime("%Y-%m-%d %H:%M:%S UTC")
        if 0 <= k < len(self.rows):
            name, runs = self.rows[k]
            hit = [r for r in runs if r[0] <= t < r[1]]
            if hit:
                r = hit[-1]
                tip += "\n%s: %s\n%s \u2013 %s (%s)" % (
                    name, r[3], geo.gps_to_utc(r[0]).strftime("%H:%M:%S"), geo.gps_to_utc(r[1]).strftime("%H:%M:%S"),
                    _dur(r[1] - r[0]))
        from qgis.PyQt.QtWidgets import QToolTip
        gp = ev.globalPosition().toPoint() if hasattr(ev, "globalPosition") else ev.globalPos()
        QToolTip.showText(gp, tip, self)


def _dur(s):
    s = int(round(s))
    if s < 60:
        return "%d s" % s
    if s < 3600:
        return "%d min %02d s" % (s // 60, s % 60)
    return "%d h %02d min" % (s // 3600, (s % 3600) // 60)


