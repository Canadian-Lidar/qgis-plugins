"""AlignPro's own widgets (vehicle mounting drawing). The common ones (step pills, cards, sections,
key/value grids, toasts, logo, dock title bar) come from DCS Core and are re-exported here."""
import math

from qgis.PyQt.QtCore import QPointF, QRectF, Qt
from qgis.PyQt.QtGui import QColor, QPainter, QPen, QPolygonF
from qgis.PyQt.QtWidgets import QWidget

from dcs_core.widgets import (ACCENT, BAD, KV, MUTED, OK, STATE_LEVEL, WARN, Card, DockTitle, Logo,  # noqa: F401
                              Section, StepButton, Toast, align_flags as _af)

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
        p.setPen(QPen(QColor(58, 63, 69, 110), 1))
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
            p.setPen(QPen(QColor(160, 169, 177, 150), 1))
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
        p.setPen(QPen(QColor(160, 169, 177, 150), 1.2))
        p.setBrush(QColor(54, 59, 65, 200))
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
