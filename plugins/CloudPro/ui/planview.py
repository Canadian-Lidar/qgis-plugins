"""Plan view: the whole project as a gridded top view (mean / max / min
height, span, hit count, reject count, intensity, with hill shading) under
the points of the Inspection Area, the drive lines with direction arrows,
the slice position and the Inspection Area itself.

Inspection Area (with the Pan tool): drag inside to move, drag a corner to
resize, drag the round handle to rotate, Shift+drag anywhere draws a new
one along the drag. Points are loaded on release. Right click for the
Inspection Area menu (place here, properties, align with a line ...)."""
import math

import numpy as np

from ..core.pointset import rect_corners, to_box_frame
from ..core.store import GRID_CELL, parse_key
from ..qt import QtCore, QtGui, Qt, Signal, evpos, mods
from . import render, theme
from .view2d import CloudView2D

ATTRS = ["Mean height", "Max height", "Min height", "Span", "Hit count", "Reject count", "Intensity", "None"]


class PlanView(CloudView2D):
    boxChanged = Signal(dict, bool)          # box, final

    def __init__(self, parent=None):
        super().__init__("plan", parent)
        self.transparent = True
        self.store = None
        self.tracks = {}
        self.line_colours = {}
        self.line_enabled = {}
        self.track_visible = {}
        self.attr = "Mean height"
        self.lut = "Rainbow"
        self.shade = True
        self.box = None
        self.slice_lines = None              # callable -> list of ((x0,y0),(x1,y1), colour, dashed)
        self.highlight_line = None           # line id drawn thicker (Project Files selection)
        self._tiles = {}
        self._range = None
        self._bdrag = None
        self._bhover = None
        self.empty_text = "Open a job project, then Georeference"

    # ------------------------------------------------------------------ project
    def set_project(self, store, tracks, line_colours):
        self.store = store
        self.tracks = tracks
        self.line_colours = line_colours
        self._tiles = {}
        self._range = None

    def invalidate(self, keys=None):
        if keys is None:
            self._tiles = {}
            self._range = None
        else:
            for k in keys:
                self._tiles.pop(k, None)
        self.update()

    def has_underlay(self):
        return self.store is not None and bool(self.store.keys()) and self.attr != "None"

    def zoom_project(self):
        if self.store is None or self.store.extent() is None:
            return
        x0, y0, x1, y1 = self.store.extent()
        self.ca, self.cb = (x0 + x1) / 2, (y0 + y1) / 2
        W, H = max(self.width(), 50), max(self.height(), 50)
        self.scale = min((W - 30) / max(x1 - x0, 1), (H - 30) / max(y1 - y0, 1))
        self._dirty = True
        self.viewChanged.emit()
        self.update()

    def zoom_box(self):
        if not self.box:
            return
        b = self.box
        self.ca, self.cb = b["cx"], b["cy"]
        W, H = max(self.width(), 50), max(self.height(), 50)
        r = math.hypot(b["length"], b["width"]) * 0.6
        self.scale = min(W, H) / max(2 * r, 1)
        self._dirty = True
        self.viewChanged.emit()
        self.update()

    def zoom_all(self):
        if len(self.a):
            super().zoom_all()
        else:
            self.zoom_project()

    # ------------------------------------------------------------------ grid underlay
    def _attr_grid(self, g):
        if "count" not in g:
            return None
        a = self.attr
        with np.errstate(invalid="ignore"):
            if a == "Mean height":
                v = g["zmean"].astype(float)
            elif a == "Max height":
                v = np.where(np.isfinite(g["zmax"]), g["zmax"], np.nan).astype(float)
            elif a == "Min height":
                v = np.where(np.isfinite(g["zmin"]), g["zmin"], np.nan).astype(float)
            elif a == "Span":
                v = (g["zmax"] - g["zmin"]).astype(float)
            elif a == "Hit count":
                v = g["count"].astype(float)
            elif a == "Reject count":
                v = g["rej"].astype(float)
                v[(g["count"] + g["rej"]) == 0] = np.nan
                return v
            else:
                v = g["imean"].astype(float)
        v[g["count"] == 0] = np.nan
        return v

    def _compute_range(self):
        vals = []
        for k in self.store.keys()[:400]:
            v = self._attr_grid(self.store.load_grid(k))
            if v is not None:
                f = v[np.isfinite(v)]
                if len(f):
                    vals.append(f[:: max(1, len(f) // 2000)])
        self._range = render.auto_range(np.concatenate(vals), (2, 98)) if vals else (0, 1)

    def _tile_image(self, key):
        im = self._tiles.get(key)
        if im is not None:
            return im
        v = self._attr_grid(self.store.load_grid(key))
        if v is None:
            return None
        if self._range is None:
            self._compute_range()
        lo, hi = self._range
        n = v.shape[0]
        ok = np.isfinite(v)
        rgb = render.scalar_colours(np.nan_to_num(v, nan=lo), lo, hi, self.lut).astype(np.float32)
        if self.shade and "height" in self.attr.lower():
            z = np.where(ok, v, np.nanmean(v) if ok.any() else 0)
            gy, gx = np.gradient(z, GRID_CELL)
            az, alt = math.radians(315), math.radians(45)
            slope = np.arctan(np.hypot(gx, gy))
            aspect = np.arctan2(-gx, gy)
            sh = np.sin(alt) * np.cos(slope) + np.cos(alt) * np.sin(slope) * np.cos(az - aspect)
            rgb *= (0.55 + 0.45 * np.clip(sh, 0, 1))[..., None]
        rgb *= 0.55                      # dimmed: the points of the Inspection Area stand out on top
        argb = render.pack(np.clip(rgb, 0, 255).astype(np.uint8))
        argb = np.where(ok, argb, np.uint32(0))
        argb = np.ascontiguousarray(np.flipud(argb.reshape(n, n)))
        img = QtGui.QImage(argb.data, n, n, 4 * n, QtGui.QImage.Format.Format_ARGB32).copy()
        self._tiles[key] = img
        return img

    def underlay(self, p):
        if self.has_underlay():
            T = self.store.tile
            x0, y1 = self.to_view(0, 0)
            x1, y0 = self.to_view(self.width(), self.height())
            for k in self.store.tiles_in_bbox(x0, y0, x1, y1):
                i, j = parse_key(k)
                img = self._tile_image(k)
                if img is None:
                    continue
                sx0, sy0 = self.to_screen(i * T, (j + 1) * T)
                sx1, sy1 = self.to_screen((i + 1) * T, j * T)
                p.drawImage(QtCore.QRectF(sx0, sy0, sx1 - sx0, sy1 - sy0), img)

    # ------------------------------------------------------------------ overlays
    def paintEvent(self, ev):
        super().paintEvent(ev)
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        self._draw_tracks(p)
        if self.slice_lines is not None:
            for (a0, a1, col, dashed) in self.slice_lines():
                p.setPen(QtGui.QPen(QtGui.QColor(col), 1.5, Qt.PenStyle.DashLine if dashed else Qt.PenStyle.SolidLine))
                p.drawLine(QtCore.QPointF(*self.to_screen(*a0)), QtCore.QPointF(*self.to_screen(*a1)))
        self._draw_box(p)
        if self._range and self.has_underlay():
            self._legend(p)
        p.end()

    def _draw_tracks(self, p):
        for lid, tr in self.tracks.items():
            if not self.line_enabled.get(lid, True) or not self.track_visible.get(lid, True):
                continue
            step = max(1, int(len(tr["E"]) / 3000))
            m = getattr(self, "track_masks", {}).get(lid)
            col = QtGui.QColor(self.line_colours.get(lid, theme.ACCENT))
            p.setPen(QtGui.QPen(col, 3.2 if lid == self.highlight_line else 1.4))
            E, N = tr["E"], tr["N"]
            if m is not None and not m.all():
                # draw each run of visible samples (files whose Track is ticked)
                d = np.diff(np.r_[0, m.astype(np.int8), 0])
                for a0, a1 in zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)):
                    rx, ry = self.to_screen(E[a0:a1:step], N[a0:a1:step])
                    if len(rx) > 1:
                        p.drawPolyline(QtGui.QPolygonF([QtCore.QPointF(a, b) for a, b in zip(rx, ry)]))
                idx = np.flatnonzero(m)[::step]
                sx, sy = self.to_screen(E[idx], N[idx])
            else:
                sx, sy = self.to_screen(E[::step], N[::step])
                p.drawPolyline(QtGui.QPolygonF([QtCore.QPointF(a, b) for a, b in zip(sx, sy)]))
            if len(sx) > 1:
                ax, ay = sx[-1], sy[-1]
                dx, dy = sx[-1] - sx[-2], sy[-1] - sy[-2]
                L = math.hypot(dx, dy) or 1
                ux, uy = dx / L, dy / L
                p.drawLine(QtCore.QPointF(ax, ay), QtCore.QPointF(ax - 9 * ux + 5 * uy, ay - 9 * uy - 5 * ux))
                p.drawLine(QtCore.QPointF(ax, ay), QtCore.QPointF(ax - 9 * ux - 5 * uy, ay - 9 * uy + 5 * ux))
                p.setFont(theme.mono(10))
                p.drawText(QtCore.QPointF(sx[0] + 4, sy[0] - 4), "L%d" % (lid + 1))

    def _legend(self, p):
        lo, hi = self._range
        x, y, w, h = self.width() - 22, 34, 10, 110
        lut = render.LUTS[self.lut]
        for k in range(h):
            c = lut[int(255 * (1 - k / (h - 1)))]
            p.fillRect(QtCore.QRectF(x, y + k, w, 1), QtGui.QColor(int(c[0]), int(c[1]), int(c[2])))
        p.setPen(QtGui.QColor(theme.TEXT))
        p.setFont(theme.mono(10))
        off = self.b_offset_z if "height" in self.attr.lower() and self.attr != "Span" else 0
        p.drawText(QtCore.QRectF(x - 66, y - 6, 62, 14), int(Qt.AlignmentFlag.AlignRight), "%.2f" % (hi + off))
        p.drawText(QtCore.QRectF(x - 66, y + h - 8, 62, 14), int(Qt.AlignmentFlag.AlignRight), "%.2f" % (lo + off))
        p.drawText(QtCore.QRectF(x - 110, y + h + 6, 120, 14), int(Qt.AlignmentFlag.AlignRight), self.attr)

    b_offset_z = 0.0

    def handles(self):
        b = self.box
        cor = rect_corners(b["cx"], b["cy"], b["length"], b["width"], b["bearing"])
        br = math.radians(b["bearing"])
        u = np.array([math.sin(br), math.cos(br)])
        rot = np.array([b["cx"], b["cy"]]) + u * (b["length"] / 2 + 14 / self.scale)
        return cor, rot

    def _draw_box(self, p):
        if not self.box:
            return
        cor, rot = self.handles()
        sc = [QtCore.QPointF(*self.to_screen(*c)) for c in cor]
        hl = self._bhover is not None or self._bdrag is not None
        p.setPen(QtGui.QPen(QtGui.QColor(theme.BRIGHT if hl else theme.ACCENT), 2 if hl else 1.4, Qt.PenStyle.DashLine))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPolygon(QtGui.QPolygonF(sc))
        b = self.box
        br = math.radians(b["bearing"])
        u = np.array([math.sin(br), math.cos(br)])
        c = np.array([b["cx"], b["cy"]])
        tip = c + u * b["length"] / 2
        p.setPen(QtGui.QPen(QtGui.QColor(theme.ACCENT), 1.2))
        p.drawLine(QtCore.QPointF(*self.to_screen(*c)), QtCore.QPointF(*self.to_screen(*tip)))
        if self.tool == "pan" and (hl or self._bdrag):
            p.setPen(QtGui.QPen(QtGui.QColor(theme.BRIGHT), 1))
            p.setBrush(QtGui.QColor(93, 133, 124))
            for q in sc:
                p.drawEllipse(q, 5, 5)
            p.drawEllipse(QtCore.QPointF(*self.to_screen(*rot)), 6, 6)
        if self._bdrag:
            p.setFont(theme.mono(11))
            p.setPen(QtGui.QColor(theme.BRIGHT))
            p.drawText(QtCore.QPointF(*self.to_screen(*c)) + QtCore.QPointF(8, -8),
                       "%.2f  %.2f   %.1f x %.1f m  %.1f deg" % (b["cx"] + self.a_offset, b["cy"] + self.b_offset,
                                                                b["length"], b["width"], b["bearing"]))

    # ------------------------------------------------------------------ box interaction
    def _hit(self, pos):
        if not self.box:
            return None
        cor, rot = self.handles()
        for i, c in enumerate(cor):
            sx, sy = self.to_screen(*c)
            if math.hypot(sx - pos.x(), sy - pos.y()) < 8:
                return ("corner", i)
        sx, sy = self.to_screen(*rot)
        if math.hypot(sx - pos.x(), sy - pos.y()) < 9:
            return ("rotate",)
        x, y = self.to_view(pos.x(), pos.y())
        u, v = to_box_frame(np.array([x]), np.array([y]), self.box["cx"], self.box["cy"], self.box["bearing"])
        L, W = self.box["length"] / 2, self.box["width"] / 2
        edge = 6 / self.scale
        if abs(u[0]) <= L + edge and abs(v[0]) <= W + edge and (abs(abs(u[0]) - L) < edge or abs(abs(v[0]) - W) < edge):
            return ("move",)
        return None

    def mousePressEvent(self, e):
        pos = evpos(e)
        sh, ct, _ = mods(e)
        if e.button() == Qt.MouseButton.LeftButton and self.tool == "pan":
            if sh:
                self._bdrag = ("new", self.to_view(pos.x(), pos.y()))
                return
            h = self._hit(pos)
            if h is not None:
                if h[0] == "move":
                    self._bdrag = ("move", self.to_view(pos.x(), pos.y()), dict(self.box))
                elif h[0] == "corner":
                    self._bdrag = ("corner", h[1], dict(self.box))
                else:
                    self._bdrag = ("rotate", dict(self.box))
                return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        pos = evpos(e)
        if self._bdrag is None:
            if self.tool == "pan" and self.box:
                h = self._hit(pos)
                if (h is None) != (self._bhover is None):
                    self._bhover = h
                    self.update()
                self._bhover = h
                if h is not None and not self._drag:
                    self.setCursor(Qt.CursorShape.SizeAllCursor if h[0] == "move" else Qt.CursorShape.CrossCursor)
                elif not self._drag:
                    self.setCursor(Qt.CursorShape.OpenHandCursor)
            super().mouseMoveEvent(e)
            return
        x, y = self.to_view(pos.x(), pos.y())
        self.cursorMoved.emit(x, y)
        k = self._bdrag[0]
        if k == "move":
            _, (x0, y0), b0 = self._bdrag
            self.box["cx"] = b0["cx"] + x - x0
            self.box["cy"] = b0["cy"] + y - y0
        elif k == "corner":
            _, i, b0 = self._bdrag
            u, v = to_box_frame(np.array([x]), np.array([y]), b0["cx"], b0["cy"], b0["bearing"])
            cor = rect_corners(b0["cx"], b0["cy"], b0["length"], b0["width"], b0["bearing"])
            opp = cor[(i + 2) % 4]
            uo, vo = to_box_frame(np.array([opp[0]]), np.array([opp[1]]), b0["cx"], b0["cy"], b0["bearing"])
            mu, mv = (u[0] + uo[0]) / 2, (v[0] + vo[0]) / 2
            br = math.radians(b0["bearing"])
            self.box.update(cx=b0["cx"] + mu * math.sin(br) + mv * math.cos(br),
                            cy=b0["cy"] + mu * math.cos(br) - mv * math.sin(br),
                            length=max(1.0, abs(u[0] - uo[0])), width=max(0.5, abs(v[0] - vo[0])))
        elif k == "rotate":
            b0 = self._bdrag[1]
            self.box["bearing"] = (math.degrees(math.atan2(x - b0["cx"], y - b0["cy"])) + 360) % 360
        elif k == "new":
            (x0, y0) = self._bdrag[1]
            L = math.hypot(x - x0, y - y0)
            if L > 0.5:
                w = self.box["width"] if self.box else 30.0
                self.box = {"cx": (x + x0) / 2, "cy": (y + y0) / 2, "length": L, "width": w,
                            "bearing": (math.degrees(math.atan2(x - x0, y - y0)) + 360) % 360}
        self.boxChanged.emit(dict(self.box) if self.box else {}, False)
        self.update()

    def mouseReleaseEvent(self, e):
        if self._bdrag is not None:
            self._bdrag = None
            if self.box:
                self.boxChanged.emit(dict(self.box), True)
            self.update()
            return
        super().mouseReleaseEvent(e)

    def align_to_track(self, x, y):
        best = None
        for lid, tr in self.tracks.items():
            d = np.hypot(tr["E"] - x, tr["N"] - y)
            j = int(np.argmin(d))
            if best is None or d[j] < best[0]:
                best = (d[j], lid, j)
        if best is None or not self.box:
            return
        _, lid, j = best
        tr = self.tracks[lid]
        j0, j1 = max(0, j - 10), min(len(tr["E"]) - 1, j + 10)
        brg = math.degrees(math.atan2(tr["E"][j1] - tr["E"][j0], tr["N"][j1] - tr["N"][j0])) % 360
        self.box.update(cx=float(tr["E"][j]), cy=float(tr["N"][j]), bearing=brg)
        self.boxChanged.emit(dict(self.box), True)
