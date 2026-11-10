"""3D view on the graphics card (CloudPro 1.9.7): with OpenGL the card draws every point and the picture matches the
engine's drawing on the processor (odd point sizes pixel for pixel; even sizes may differ by the half-pixel
convention); without OpenGL, or with CLOUDPRO_NO_GPU=1, the view draws on the processor as before. Also checks the
compiled eye-dome lighting against numpy.

    xvfb-run -a env CLOUDPRO_GPU=1 python3 CloudPro/tests/test_gpu3d.py     (card path, software OpenGL allowed)
    QT_QPA_PLATFORM=offscreen python3 CloudPro/tests/test_gpu3d.py         (fallback path)
"""
import os
import sys

import numpy as np

PLUGINS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PLUGINS)
from CloudPro.qt import QtWidgets                                   # noqa: E402
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
from CloudPro.core import engine                                    # noqa: E402
from CloudPro.ui import render, view3d                              # noqa: E402

assert engine.available() and engine.VERSION == 3

# --- compiled eye-dome == numpy ---------------------------------------------------------------------------------
rng = np.random.default_rng(0)
h, w = 300, 500
buf = (0xFF000000 | rng.integers(0, 0xFFFFFF, h * w)).astype(np.uint32).reshape(h, w)
d = (rng.random(h * w) * 50 + 5).astype(np.float32)
d[::3] = np.inf
e = render._engine
render._engine = lambda: None
ref = render.eye_dome(buf, d, 1.0)
render._engine = e
got = render.eye_dome(buf, d, 1.0)
assert (ref == got).all(), (ref != got).mean()
print("eye-dome: compiled == numpy")

# --- the view ---------------------------------------------------------------------------------------------------
n = 300_000
P = np.column_stack([rng.random(n) * 100, rng.random(n) * 40, rng.random(n) ** 3 * 15]) + [5e5, 5e6, 80]
col = (0xFF000000 | rng.integers(0, 0xFFFFFF, n)).astype(np.uint32)
told = []


def _visible_diff(a, b):
    """Share of pixels differing by more than 3 levels in a channel (depth read back from the card has 24 bits, so
    the eye-dome shading can differ by a level or two)."""
    ch = [np.abs(((a >> s) & 255).astype(int) - ((b >> s) & 255).astype(int)) for s in (0, 8, 16)]
    return float((np.maximum(np.maximum(ch[0], ch[1]), ch[2]) > 3).mean())


def view(gpu, size):
    v = view3d.CloudView3D()
    v.resize(640, 420)
    v.use_gpu, v.async_still, v.point_size = gpu, False, size
    v.rest_max = v.lod_max = 10 ** 9
    v.gpuStatus.connect(told.append)
    v.set_points(P, col, np.arange(n), fit=True)
    return v


g, c = view(True, 3), view(False, 3)
g._render(quick=False)
if not g.gpu_active():
    print("no usable OpenGL here (%s): the view draws on the processor" % g.gpu_why)
    assert told and told[0].startswith("processor")
    c._render(quick=False)
    assert (g._buf == c._buf).all() and g.shown == c.shown
    print("test_gpu3d: OK (fallback path)")
    sys.exit(0)
print("drawn by %s" % g.gpu_why)
for setup in (lambda v: None, lambda v: setattr(v, "ortho", True), lambda v: setattr(v, "zscale", 3.0),
              lambda v: v.preset("top"), lambda v: setattr(v, "edl", False)):
    for v in (g, c):
        setup(v)
        v._render(quick=False)
    assert g.shown == n
    same = (g._buf == c._buf).mean()
    big = _visible_diff(g._buf, c._buf)
    print("  identical %.2f%%, visibly different (> 3 levels) %.2f%%" % (100 * same, 100 * big))
    assert big < 0.005, big
col2 = (col ^ 0x00FFFFFF).astype(np.uint32)
g.set_colours(col2)
c.set_colours(col2)
g._render(quick=False)
c._render(quick=False)
assert _visible_diff(g._buf, c._buf) < 0.005
# new points (edits / a new area) are uploaded again
g.set_points(P[: n // 2], col[: n // 2], np.arange(n // 2))
c.set_points(P[: n // 2], col[: n // 2], np.arange(n // 2))
g._render(quick=False)
c._render(quick=False)
assert g.shown == n // 2 and _visible_diff(g._buf, c._buf) < 0.005
# moving frames: every point, no shading
g._interacting = True
g._render()
assert g.shown == n // 2
g.release_gpu()
print("test_gpu3d: OK (graphics card path: pictures match the processor drawing, colours and points re-uploaded)")
