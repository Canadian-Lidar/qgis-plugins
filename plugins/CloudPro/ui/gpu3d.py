"""The 3D view's points drawn on the graphics card (CloudPro 1.9.7).

The points (local float32 coordinates) and their colours are uploaded once into vertex buffers; a frame is a single
draw with a depth test into an off-screen framebuffer, read back as an image the view paints like before (axes,
navigation, lasso and scale bar stay QPainter drawing on top). The OpenGL calls are made by the CloudPro engine
(core/engine.py), Qt only gives the context. When there is no usable OpenGL (or CLOUDPRO_NO_GPU=1), available() is
False and the view draws with the engine on the processor, as before.
"""
import math
import os

import numpy as np

from ..core import engine
from ..qt import QtGui

NEAR = 0.05
# software OpenGL: slower than the engine on the processor, so not used (CLOUDPRO_GPU=1 uses it anyway)
SOFTWARE = ("llvmpipe", "softpipe", "swrast", "gdi generic", "software rasterizer", "microsoft basic render")


class GpuRenderer:
    def __init__(self):
        self.ok = None              # None = not tried yet
        self.why = ""
        self.ctx = self.surface = None
        self.ids = np.zeros(2, np.uint32)
        self.fb = np.zeros(5, np.uint32)
        self.renderer = ""
        self.n = 0
        self.need_pts = self.need_col = True

    # ------------------------------------------------------------- context
    def available(self):
        if self.ok is None:
            self.ok = self._start()
        return self.ok

    def _start(self):
        if os.environ.get("CLOUDPRO_NO_GPU"):
            self.why = "turned off (CLOUDPRO_NO_GPU)"
            return False
        if not engine.available() or engine.VERSION < 3:
            self.why = "no engine"
            return False
        try:
            fmt = QtGui.QSurfaceFormat()
            fmt.setVersion(2, 1)
            fmt.setProfile(QtGui.QSurfaceFormat.OpenGLContextProfile.CompatibilityProfile)
            fmt.setRenderableType(QtGui.QSurfaceFormat.RenderableType.OpenGL)
            fmt.setDepthBufferSize(24)
            self.surface = QtGui.QOffscreenSurface()
            self.surface.setFormat(fmt)
            self.surface.create()
            self.ctx = QtGui.QOpenGLContext()
            self.ctx.setFormat(fmt)
            if not self.ctx.create() or self.ctx.isOpenGLES():
                self.why = "no desktop OpenGL context"
                return False
            if not self.ctx.makeCurrent(self.surface):
                self.why = "the OpenGL context could not be made current"
                return False
            ok = engine.gl_init()
            if not ok:
                self.why = "OpenGL functions missing"
                return False
            self.renderer = engine.gl_renderer()
            if any(k in self.renderer.lower() for k in SOFTWARE) and os.environ.get("CLOUDPRO_GPU") != "1":
                self.why = "software OpenGL (%s): the processor is faster" % self.renderer
                self.ctx.doneCurrent()
                return False
            # one test frame: a framebuffer and a point must work
            ids = np.zeros(2, np.uint32)
            engine.gl_upload(ids, np.zeros((1, 3), np.float32), np.array([0xFFFFFFFF], np.uint32), 1)
            fb = np.zeros(5, np.uint32)
            eye = np.eye(4, dtype=np.float32)
            _buf, _d, st = engine.gl_draw(ids, 1, eye, eye, 8, 8, 1.0, 0xFF000000, fb, want_depth=True)
            engine.gl_free(ids, fb)
            self.ctx.doneCurrent()
            if st != 0:
                self.why = "OpenGL test frame failed (%d)" % st
                return False
            fmt2 = self.ctx.format()
            self.why = "OpenGL %d.%d, %s" % (fmt2.majorVersion(), fmt2.minorVersion(), self.renderer)
            return True
        except Exception as e:                  # noqa: BLE001 - any failure means "draw on the processor"
            self.why = "OpenGL unavailable (%s)" % e
            return False

    def _current(self):
        return self.ctx is not None and self.ctx.makeCurrent(self.surface)

    # ------------------------------------------------------------- data
    def set_points(self, L, col):
        """Upload what changed since the last frame (mark it with need_pts / need_col). False when the card
        refused (out of memory, lost context): the view then draws on the processor."""
        if not self._current():
            return False
        n = len(L)
        st = 0
        if self.need_pts or n != self.n:
            st |= engine.gl_upload(self.ids, xyz=L, n=n)
            self.need_pts, self.need_col = False, True
            self.n = n
        if self.need_col:
            st |= engine.gl_upload(self.ids, col=col, n=n)
            self.need_col = False
        if st != 0:
            self.need_pts = self.need_col = True
        return st == 0

    def release(self):
        if self.ctx is not None and self._current():
            engine.gl_free(self.ids, self.fb)
            self.ctx.doneCurrent()
        self.need_pts = self.need_col = True
        self.n = 0

    # ------------------------------------------------------------- frames
    @staticmethod
    def matrices(cam, far, near=NEAR):
        """OpenGL projection and model-view (column-major) from the view's camera: eye x = right, y = up,
        z = -depth, so the picture matches view3d._project_local pixel for pixel."""
        M, t = np.asarray(cam["M"], np.float64), np.asarray(cam["t"], np.float64)
        V = np.eye(4)
        V[:3, :3] = M.T
        V[:3, 3] = -t
        V[2, :] *= -1.0                          # OpenGL looks down -z
        W, H = cam["W"], cam["H"]
        n = max(NEAR, near)
        f = max(far, n * 10)
        P = np.zeros((4, 4))
        if cam["ortho"]:
            s = cam["s"]
            P[0, 0], P[1, 1] = 2 * s / W, 2 * s / H
            P[2, 2], P[2, 3], P[3, 3] = -2 / (f - n), -(f + n) / (f - n), 1.0
        else:
            fo = cam["f"]
            P[0, 0], P[1, 1] = 2 * fo / W, 2 * fo / H
            P[2, 2], P[2, 3], P[3, 2] = -(f + n) / (f - n), -2 * f * n / (f - n), -1.0
        return P.T.astype(np.float32).ravel(), V.T.astype(np.float32).ravel(), (n, f)

    def frame(self, cam, m, size, far, bg, depth=True, near=NEAR):
        """-> (buffer (H, W) uint32, camera depth (H*W,) float32 with inf where empty, or None) or None on failure."""
        if not self._current():
            return None
        proj, view, (n, f) = self.matrices(cam, far, near)
        buf, d, st = engine.gl_draw(self.ids, min(m, self.n), proj, view, cam["W"], cam["H"], size, bg, self.fb,
                                    want_depth=depth)
        if st != 0:
            return None
        z = None
        if d is not None:
            empty = d >= 1.0
            if cam["ortho"]:
                z = n + d * (f - n)
            else:
                zn = 2.0 * d - 1.0
                z = (2.0 * n * f) / (f + n - zn * (f - n))
            z = np.where(empty, np.inf, z).astype(np.float32).ravel()
        return buf, z


def far_plane(L, cam_t, dist):
    """A far clipping distance that holds the whole cloud: camera distance plus the cloud's size (with margin)."""
    if len(L) == 0:
        return 1000.0
    lo, hi = L.min(0), L.max(0)
    return float(dist + 2.0 * math.sqrt(((hi - lo) ** 2).sum()) + 100.0)
