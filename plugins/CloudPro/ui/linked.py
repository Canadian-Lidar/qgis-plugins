"""The linked cursor (CloudPro 2.1.0): the point under the mouse in one view is marked in the other two.

    +  a cross around the same point (it is in that view)
    |  a dashed vertical line at its position when the view does not hold that point (outside the slice, or the
       mouse is over empty ground in the Plan): where it is along that view
"""
from ..qt import QtCore, QtGui, Qt

SIZE = 14                # half the cross, px
COLOUR = QtGui.QColor(255, 255, 255)
HALO = QtGui.QColor(0, 0, 0, 200)


def draw_plus(p, x, y, size=SIZE):
    p.save()
    p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
    for pen in (QtGui.QPen(HALO, 5), QtGui.QPen(COLOUR, 2.2)):
        p.setPen(pen)
        p.drawLine(QtCore.QPointF(x - size, y), QtCore.QPointF(x - 4, y))
        p.drawLine(QtCore.QPointF(x + 4, y), QtCore.QPointF(x + size, y))
        p.drawLine(QtCore.QPointF(x, y - size), QtCore.QPointF(x, y - 4))
        p.drawLine(QtCore.QPointF(x, y + 4), QtCore.QPointF(x, y + size))
    p.restore()


def draw_vline(p, x, y0, y1):
    p.save()
    p.setPen(QtGui.QPen(HALO, 3))
    p.drawLine(QtCore.QPointF(x, y0), QtCore.QPointF(x, y1))
    p.setPen(QtGui.QPen(COLOUR, 1.2, Qt.PenStyle.DashLine))
    p.drawLine(QtCore.QPointF(x, y0), QtCore.QPointF(x, y1))
    p.restore()


def paint_2d(p, view):
    """view.linked = ("plus", a, b) or ("line", a) in the view's coordinates, or None."""
    m = getattr(view, "linked", None)
    if not m:
        return
    if m[0] == "plus":
        x, y = view.to_screen(m[1], m[2])
        draw_plus(p, float(x), float(y))
    else:
        x, _y = view.to_screen(m[1], 0.0)
        draw_vline(p, float(x), 0, view.height())
