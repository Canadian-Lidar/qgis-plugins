"""Vector icons drawn with QPainter (no image files, crisp at any size,
coloured by state). icon(name, size, colour) -> QIcon; paint(p, name, rect, colour)."""
import math

from ..qt import QtCore, QtGui, Qt

P = QtCore.QPointF
R = QtCore.QRectF

NAMES = ("hand", "rect", "poly", "lasso", "brush", "above", "below", "ruler", "info", "undo", "redo", "eye",
         "eye_off", "reject", "accept", "select", "classify", "filter", "inside", "outside", "menu", "props",
         "help", "prev", "next", "rotate", "full", "zoomall", "top", "along", "across", "iso", "plus", "minus",
         "copy", "up", "down", "edl", "map", "lines", "cross", "legend", "link", "float", "close")


def paint(p, name, r, col, width=1.6):
    """Draw icon `name` into rect r (QRectF) with colour col (QColor)."""
    p.save()
    p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
    s = min(r.width(), r.height())
    p.translate(r.center().x() - s / 2, r.center().y() - s / 2)
    p.scale(s / 24.0, s / 24.0)
    pen = QtGui.QPen(col, width * 24.0 / max(s, 1) * 1.15, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                     Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    f = _ICONS.get(name)
    if f:
        f(p, col)
    p.restore()


def icon(name, size=22, col="#c9cfd4", active=None):
    """QIcon with Normal (col) and Active/On (active colour) pixmaps."""
    ic = QtGui.QIcon()
    for mode_col, mode, state in ((col, QtGui.QIcon.Mode.Normal, QtGui.QIcon.State.Off),
                                  (active or "#e3e7ea", QtGui.QIcon.Mode.Normal, QtGui.QIcon.State.On),
                                  (active or "#e3e7ea", QtGui.QIcon.Mode.Active, QtGui.QIcon.State.Off),
                                  ("#5c636a", QtGui.QIcon.Mode.Disabled, QtGui.QIcon.State.Off)):
        pm = QtGui.QPixmap(size * 2, size * 2)
        pm.fill(Qt.GlobalColor.transparent)
        p = QtGui.QPainter(pm)
        paint(p, name, R(0, 0, size * 2, size * 2), QtGui.QColor(mode_col))
        p.end()
        pm.setDevicePixelRatio(2.0)
        ic.addPixmap(pm, mode, state)
    return ic


# --------------------------------------------------------------------------- shapes
def _hand(p, c):
    path = QtGui.QPainterPath()
    # four fingers and thumb of an open hand (pan)
    path.moveTo(7.5, 13.5)
    path.lineTo(7.5, 6.2)
    path.quadTo(7.5, 4.6, 9.0, 4.6)
    path.quadTo(10.5, 4.6, 10.5, 6.2)
    path.lineTo(10.5, 11)
    path.lineTo(10.5, 4.4)
    path.quadTo(10.5, 2.8, 12.0, 2.8)
    path.quadTo(13.5, 2.8, 13.5, 4.4)
    path.lineTo(13.5, 11)
    path.lineTo(13.5, 5.2)
    path.quadTo(13.5, 3.6, 15.0, 3.6)
    path.quadTo(16.5, 3.6, 16.5, 5.2)
    path.lineTo(16.5, 11.5)
    path.lineTo(16.5, 7.4)
    path.quadTo(16.5, 5.9, 18.0, 5.9)
    path.quadTo(19.5, 5.9, 19.5, 7.4)
    path.lineTo(19.5, 14.5)
    path.quadTo(19.5, 21.5, 13.0, 21.5)
    path.quadTo(9.0, 21.5, 7.0, 18.5)
    path.lineTo(3.8, 13.6)
    path.quadTo(3.0, 12.2, 4.3, 11.5)
    path.quadTo(5.6, 10.9, 6.6, 12.1)
    path.lineTo(7.5, 13.5)
    p.drawPath(path)


def _rect(p, c):
    p.setPen(QtGui.QPen(c, p.pen().widthF(), Qt.PenStyle.DashLine))
    p.drawRect(R(4, 6, 16, 12))


def _poly(p, c):
    pts = [P(4, 18), P(6, 6), P(14, 4), P(20, 11), P(13, 19)]
    p.drawPolygon(QtGui.QPolygonF(pts))
    p.setBrush(c)
    for q in pts:
        p.drawEllipse(q, 1.4, 1.4)


def _lasso(p, c):
    path = QtGui.QPainterPath()
    path.moveTo(9, 17)
    path.cubicTo(1, 15, 2, 5, 11, 4)
    path.cubicTo(20, 3, 23, 12, 15, 15)
    path.cubicTo(11, 16.5, 9, 16, 9, 17)
    p.drawPath(path)
    p.drawLine(P(9, 17), P(7.5, 21))


def _brush(p, c):
    p.drawLine(P(19, 3), P(10.5, 13))
    path = QtGui.QPainterPath()
    path.moveTo(10.5, 13)
    path.cubicTo(6, 12, 5, 16, 4, 20.5)
    path.cubicTo(9, 20.5, 12.5, 18, 10.5, 13)
    p.setBrush(c)
    p.drawPath(path)


def _above(p, c):
    p.drawPolyline(QtGui.QPolygonF([P(3, 15), P(9, 12), P(15, 14), P(21, 10)]))
    p.setBrush(c)
    for q in (P(6, 7), P(12, 6), P(18, 4.5)):
        p.drawEllipse(q, 1.3, 1.3)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawLine(P(12, 20), P(12, 16.5))


def _below(p, c):
    p.drawPolyline(QtGui.QPolygonF([P(3, 9), P(9, 12), P(15, 10), P(21, 13)]))
    p.setBrush(c)
    for q in (P(6, 17), P(12, 18), P(18, 19)):
        p.drawEllipse(q, 1.3, 1.3)


def _ruler(p, c):
    p.save()
    p.translate(12, 12)
    p.rotate(-35)
    p.drawRect(R(-10, -3.5, 20, 7))
    for k in range(-7, 9, 3):
        p.drawLine(P(k, -3.5), P(k, -0.5 if (k + 7) % 6 else 0.8))
    p.restore()


def _info(p, c):
    p.drawEllipse(P(12, 12), 9, 9)
    p.setBrush(c)
    p.drawEllipse(P(12, 7.6), 1.1, 1.1)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawLine(P(12, 11), P(12, 17))


def _help(p, c):
    p.drawEllipse(P(12, 12), 9, 9)
    path = QtGui.QPainterPath()
    path.moveTo(9.3, 9.6)
    path.cubicTo(9.3, 6.2, 14.8, 6.2, 14.8, 9.4)
    path.cubicTo(14.8, 11.6, 12, 11.8, 12, 14.2)
    p.drawPath(path)
    p.setBrush(c)
    p.drawEllipse(P(12, 17.2), 1.0, 1.0)


def _undo(p, c):
    path = QtGui.QPainterPath()
    path.moveTo(5, 10)
    path.cubicTo(9, 4.5, 20, 5, 19.5, 13)
    path.cubicTo(19, 18, 14, 19, 11, 19)
    p.drawPath(path)
    p.drawPolyline(QtGui.QPolygonF([P(5, 5), P(5, 10), P(10, 10)]))


def _redo(p, c):
    p.save()
    p.translate(24, 0)
    p.scale(-1, 1)
    _undo(p, c)
    p.restore()


def _eye(p, c):
    path = QtGui.QPainterPath()
    path.moveTo(2.5, 12)
    path.cubicTo(7, 4.5, 17, 4.5, 21.5, 12)
    path.cubicTo(17, 19.5, 7, 19.5, 2.5, 12)
    p.drawPath(path)
    p.drawEllipse(P(12, 12), 3.2, 3.2)


def _eye_off(p, c):
    _eye(p, c)
    p.drawLine(P(4, 20), P(20, 4))


def _reject(p, c):
    p.setPen(QtGui.QPen(QtGui.QColor("#e84855"), p.pen().widthF() * 1.2, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap))
    p.drawLine(P(6, 6), P(18, 18))
    p.drawLine(P(18, 6), P(6, 18))


def _accept(p, c):
    p.setPen(QtGui.QPen(QtGui.QColor("#00e5aa"), p.pen().widthF() * 1.2, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.drawPolyline(QtGui.QPolygonF([P(5, 12.5), P(10, 17.5), P(19, 6.5)]))


def _select(p, c):
    path = QtGui.QPainterPath()
    path.moveTo(6, 3.5)
    path.lineTo(18, 13.5)
    path.lineTo(12.5, 14.3)
    path.lineTo(15.8, 20.5)
    path.lineTo(13.2, 21.5)
    path.lineTo(10.2, 15.4)
    path.lineTo(6, 19)
    path.closeSubpath()
    p.drawPath(path)


def _classify(p, c):
    p.drawPolygon(QtGui.QPolygonF([P(3, 12), P(11, 4), P(20, 4), P(20, 13), P(12, 21)]))
    p.drawEllipse(P(16, 8), 1.5, 1.5)


def _filter(p, c):
    p.drawPolygon(QtGui.QPolygonF([P(3, 4), P(21, 4), P(14, 12.5), P(14, 20), P(10, 18), P(10, 12.5)]))


def _inside(p, c):
    p.setPen(QtGui.QPen(c, p.pen().widthF(), Qt.PenStyle.DashLine))
    p.drawRect(R(4, 4, 16, 16))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(c)
    for x in (9, 15):
        for y in (9, 15):
            p.drawEllipse(P(x, y), 1.6, 1.6)


def _outside(p, c):
    p.setPen(QtGui.QPen(c, p.pen().widthF(), Qt.PenStyle.DashLine))
    p.drawRect(R(7, 7, 10, 10))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(c)
    for q in ((3.5, 3.5), (12, 3), (20.5, 3.5), (3, 12), (21, 12), (3.5, 20.5), (12, 21), (20.5, 20.5)):
        p.drawEllipse(P(*q), 1.4, 1.4)


def _menu(p, c):
    for y in (6.5, 12, 17.5):
        p.drawLine(P(4, y), P(20, y))


def _props(p, c):
    p.drawRect(R(4, 4, 16, 16))
    for y, x in ((9, 8), (13, 8), (17, 8)):
        p.drawLine(P(x, y - 0.5), P(x + 8, y - 0.5))
    p.drawLine(P(4, 6.5 + 0.5), P(20, 6.5 + 0.5))


def _prev(p, c):
    p.drawPolyline(QtGui.QPolygonF([P(15, 5), P(8, 12), P(15, 19)]))


def _next(p, c):
    p.drawPolyline(QtGui.QPolygonF([P(9, 5), P(16, 12), P(9, 19)]))


def _rotate(p, c):
    p.drawArc(R(4, 4, 16, 16), 60 * 16, 290 * 16)
    p.drawPolyline(QtGui.QPolygonF([P(16, 2.5), P(16.5, 6.5), P(12.5, 7.5)]))


def _full(p, c):
    for (x, y, dx, dy) in ((4, 4, 1, 1), (20, 4, -1, 1), (4, 20, 1, -1), (20, 20, -1, -1)):
        p.drawLine(P(x, y), P(x + 5 * dx, y))
        p.drawLine(P(x, y), P(x, y + 5 * dy))


def _zoomall(p, c):
    p.drawEllipse(P(10.5, 10.5), 6, 6)
    p.drawLine(P(15, 15), P(20.5, 20.5))
    p.drawLine(P(8, 10.5), P(13, 10.5))
    p.drawLine(P(10.5, 8), P(10.5, 13))


def _brackets(p):
    for (x, y, dx, dy) in ((3, 3, 1, 1), (21, 3, -1, 1), (3, 21, 1, -1), (21, 21, -1, -1)):
        p.drawLine(P(x, y), P(x + 4.5 * dx, y))
        p.drawLine(P(x, y), P(x, y + 4.5 * dy))


def _zoom_area(p, c):
    """Zoom to the Inspection Area: a turned rectangle framed by corner brackets."""
    _brackets(p)
    col = QtGui.QColor(c)
    col.setAlpha(70)
    p.setBrush(col)
    p.drawPolygon(QtGui.QPolygonF([P(7, 13.5), P(14.5, 6.5), P(17, 10.5), P(9.5, 17.5)]))
    p.setBrush(Qt.BrushStyle.NoBrush)


def _zoom_extents(p, c):
    """Zoom to the whole project: a grid of tiles framed by corner brackets."""
    _brackets(p)
    p.drawRect(R(7, 7, 10, 10))
    p.drawLine(P(12, 7), P(12, 17))
    p.drawLine(P(7, 12), P(17, 12))


def _cube(p, c, face=None):
    a, b, d, e = P(12, 3), P(20, 7.5), P(12, 12), P(4, 7.5)
    p.drawPolygon(QtGui.QPolygonF([a, b, d, e]))
    p.drawPolygon(QtGui.QPolygonF([e, d, P(12, 21), P(4, 16.5)]))
    p.drawPolygon(QtGui.QPolygonF([d, b, P(20, 16.5), P(12, 21)]))
    if face is not None:
        col = QtGui.QColor(c)
        col.setAlpha(150)
        p.setBrush(col)
        p.drawPolygon(QtGui.QPolygonF(face))


def _top(p, c):
    _cube(p, c, [P(12, 3), P(20, 7.5), P(12, 12), P(4, 7.5)])


def _along(p, c):
    _cube(p, c, [P(12, 12), P(20, 7.5), P(20, 16.5), P(12, 21)])


def _across(p, c):
    _cube(p, c, [P(4, 7.5), P(12, 12), P(12, 21), P(4, 16.5)])


def _iso(p, c):
    _cube(p, c)


def _plus(p, c):
    p.drawLine(P(12, 5), P(12, 19))
    p.drawLine(P(5, 12), P(19, 12))


def _minus(p, c):
    p.drawLine(P(5, 12), P(19, 12))


def _copy(p, c):
    p.drawRect(R(8, 8, 12, 12))
    p.drawPolyline(QtGui.QPolygonF([P(16, 5), P(4, 5), P(4, 16)]))


def _up(p, c):
    p.drawPolyline(QtGui.QPolygonF([P(5, 15), P(12, 8), P(19, 15)]))


def _down(p, c):
    p.drawPolyline(QtGui.QPolygonF([P(5, 9), P(12, 16), P(19, 9)]))


def _edl(p, c):
    p.drawEllipse(P(12, 12), 8, 8)
    path = QtGui.QPainterPath()
    path.moveTo(12, 4)
    path.arcTo(R(4, 4, 16, 16), 90, 180)
    path.closeSubpath()
    p.setBrush(c)
    p.drawPath(path)


def _map(p, c):
    p.drawPolygon(QtGui.QPolygonF([P(3, 6), P(9, 4), P(15, 6), P(21, 4), P(21, 18), P(15, 20), P(9, 18), P(3, 20)]))
    p.drawLine(P(9, 4), P(9, 18))
    p.drawLine(P(15, 6), P(15, 20))


def _lines(p, c):
    p.drawPolyline(QtGui.QPolygonF([P(3, 18), P(9, 12), P(14, 15), P(21, 6)]))
    p.drawPolyline(QtGui.QPolygonF([P(3, 13), P(8, 8), P(15, 10), P(21, 3)]))


def _legend(p, c):
    g = QtGui.QLinearGradient(0, 4, 0, 20)
    for t, col in ((0, "#e84855"), (0.35, "#f5d742"), (0.65, "#00e5aa"), (1, "#3ab4f2")):
        g.setColorAt(t, QtGui.QColor(col))
    p.save()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(g)
    p.drawRect(R(4, 4, 6, 16))
    p.restore()
    p.drawRect(R(4, 4, 6, 16))
    for y in (5, 12, 19):
        p.drawLine(P(12, y), P(20, y))


def _link(p, c):
    p.drawRoundedRect(R(2, 8, 11, 8), 4, 4)
    p.drawRoundedRect(R(11, 8, 11, 8), 4, 4)


def _cross(p, c):
    p.drawLine(P(12, 3), P(12, 21))
    p.drawLine(P(3, 12), P(21, 12))
    p.drawEllipse(P(12, 12), 4, 4)


def _float(p, c):
    """Two overlapping windows: float this view in its own window / dock it again."""
    p.drawRect(R(3, 8, 12, 11))
    p.drawLine(P(9, 5), P(21, 5))
    p.drawLine(P(21, 5), P(21, 15))
    p.drawLine(P(9, 5), P(9, 8))
    p.drawLine(P(15, 15), P(21, 15))


def _close(p, c):
    p.drawLine(P(6, 6), P(18, 18))
    p.drawLine(P(18, 6), P(6, 18))


_ICONS = {"float": _float, "close": _close, "hand": _hand, "rect": _rect, "poly": _poly, "lasso": _lasso, "brush": _brush, "above": _above,
          "below": _below, "ruler": _ruler, "info": _info, "help": _help, "undo": _undo, "redo": _redo, "eye": _eye,
          "eye_off": _eye_off, "reject": _reject, "accept": _accept, "select": _select, "classify": _classify,
          "filter": _filter, "inside": _inside, "outside": _outside, "menu": _menu, "props": _props, "prev": _prev,
          "next": _next, "rotate": _rotate, "full": _full, "zoomall": _zoomall, "top": _top, "along": _along,
          "across": _across, "iso": _iso, "plus": _plus, "minus": _minus, "copy": _copy, "up": _up, "down": _down,
          "edl": _edl, "map": _map, "lines": _lines, "cross": _cross,
          "legend": _legend, "link": _link, "zoom_area": _zoom_area, "zoom_extents": _zoom_extents}
