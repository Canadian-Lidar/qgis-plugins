"""The selection shape in the Slice (and Plan) stays on the points while the view is zoomed or panned during
editing: a polygon started, the view zoomed in and panned, the polygon finished -> the points selected are the ones
inside it in view coordinates, also those now off screen.

    python3 CloudPro/tests/test_slice_shape.py      (from the plugins folder)
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "guides"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import harness                                                  # noqa: E402
import tempfile                                                 # noqa: E402

app, iface = harness.start(os.path.dirname(os.path.dirname(HERE)), tempfile.mkdtemp())
from CloudPro.ui.view2d import CloudView2D, inside_polygon      # noqa: E402
from CloudPro.qt import QtCore, QtGui, Qt                       # noqa: E402


def click(v, a, b, button=Qt.MouseButton.LeftButton):
    x, y = v.to_screen(a, b)
    pt = QtCore.QPointF(x, y)
    for t in (QtCore.QEvent.Type.MouseButtonPress, QtCore.QEvent.Type.MouseButtonRelease):
        ev = QtGui.QMouseEvent(t, pt, v.mapToGlobal(pt.toPoint()), button,
                               button if t == QtCore.QEvent.Type.MouseButtonPress else Qt.MouseButton.NoButton,
                               Qt.KeyboardModifier.NoModifier)
        (v.mousePressEvent if t == QtCore.QEvent.Type.MouseButtonPress else v.mouseReleaseEvent)(ev)


def test_polygon_survives_zoom_and_pan():
    rng = np.random.default_rng(2)
    a, b = rng.uniform(0, 100, 20000), rng.uniform(0, 20, 20000)
    v = CloudView2D()
    v.resize(800, 400)
    v.set_points(a, b, np.arange(len(a)), np.zeros((len(a), 3), np.uint8), np.arange(len(a)), fit=True)
    got = []
    v.selectionMade.connect(lambda m, t: got.append(m))
    v.set_tool("poly")
    P = [(10, 2), (60, 2), (60, 15), (10, 15)]
    click(v, *P[0])
    click(v, *P[1])
    v.scale *= 4.0                                   # zoom in ...
    v.center_on(55, 10)                              # ... and pan, mid-polygon
    assert np.allclose(v._poly[0], P[0], atol=0.2)   # the vertex is kept where it was put on the points
    click(v, *P[2])
    click(v, *P[3], button=Qt.MouseButton.RightButton)   # right click: last vertex and finish
    assert got, "no selection"
    want = inside_polygon(a, b, np.array(P, float))
    assert np.array_equal(got[0], want), (got[0].sum(), want.sum())
    # the polygon's first corner (10, 2) is now off screen: points there are still selected
    x0, _ = v.to_screen(10, 2)
    assert x0 < 0 and got[0][(a > 11) & (a < 15) & (b > 3) & (b < 14)].all()
    # rectangle drawn, zoomed out before release: same points as the rectangle in view coordinates
    got.clear()
    v.set_tool("rect")
    x, y = v.to_screen(56, 5)
    ev = QtGui.QMouseEvent(QtCore.QEvent.Type.MouseButtonPress, QtCore.QPointF(x, y), QtCore.QPointF(x, y),
                           Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    v.mousePressEvent(ev)
    x, y = v.to_screen(58, 12)
    ev = QtGui.QMouseEvent(QtCore.QEvent.Type.MouseMove, QtCore.QPointF(x, y), QtCore.QPointF(x, y),
                           Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    v.mouseMoveEvent(ev)
    v.scale /= 2.0
    ev = QtGui.QMouseEvent(QtCore.QEvent.Type.MouseButtonRelease, QtCore.QPointF(x, y), QtCore.QPointF(x, y),
                           Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    v.mouseReleaseEvent(ev)
    want = (a >= 56) & (a <= 58) & (b >= 5) & (b <= 12)
    assert got and abs(int(got[0].sum()) - int(want.sum())) <= max(3, want.sum() // 50), (got[0].sum(), want.sum())
    print("slice shape: polygon and rectangle stay on the points through zoom / pan; off-screen points inside count")


if __name__ == "__main__":
    test_polygon_survives_zoom_and_pan()
    print("test_slice_shape: OK")
