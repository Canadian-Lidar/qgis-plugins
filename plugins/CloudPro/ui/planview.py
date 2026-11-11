"""Plan view: the whole project as a gridded top view (mean / max / min
height, span, hit count, density, coverage against a target density, reject
count, intensity, with hill shading) under
the points of the Inspection Area, the drive lines with direction arrows,
the slice position and the Inspection Area itself.

Inspection Area (with the Pan tool): drag inside to move, drag a corner to
resize, drag the round handle to rotate, Shift+drag anywhere draws a new
one along the drag. Points are loaded on release. Right click for the
Inspection Area menu (place here, properties, align with a line ...)."""
import math

import numpy as np

from ..core.pointset import rect_corners, to_box_frame
from ..core.coverage import DEFAULT_TARGET, coverage_summary
from ..core.store import GRID_CELL, parse_key
from ..qt import QtCore, QtGui, Qt, Signal, evpos, mods
from . import render, theme
from .view2d import CloudView2D

ATTRS = ["Mean height", "Max height", "Min height", "Span", "Hit count", "Density", "Coverage", "Line separation",
         "Reject count", "Intensity", "None"]
SEPARATION = "Line separation"
COVER_OK, COVER_LOW, COVER_POOR = (0, 229, 170), (245, 166, 35), (232, 72, 85)   # theme ACCENT, AMBER, RED


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
        self.target_density = DEFAULT_TARGET
        self._cover = None
        self.separation = None               # core.separation.Result (2.2.0)
        self.separation_current = True
        self.separation_filter = None        # None (every pair), ("pair", a, b) or ("line", l)
        self.steady_off = False              # 2.2.1: show what is left after each pair's steady offset
        self._sep_stats = None
        self.tolerance = 0.03                # m: green within, amber within twice, red beyond
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
        self._cover = None

    def set_target(self, target):
        """Target density (pts/m2) for the Coverage grid."""
        target = max(0.01, float(target))
        if target != self.target_density:
            self.target_density = target
            if self.attr == "Coverage":
                self.invalidate()

    def set_separation(self, res, current=True):
        self.separation = res
        self.separation_current = current
        self.separation_filter = None
        self._sep_stats = None
        if self.attr == SEPARATION:
            self.invalidate()

    def set_separation_filter(self, f):
        if f != self.separation_filter:
            self.separation_filter = f
            self._sep_stats = None
            if self.attr == SEPARATION:
                self.invalidate()

    def set_steady_off(self, on):
        if bool(on) != self.steady_off:
            self.steady_off = bool(on)
            self._sep_stats = None
            if self.attr == SEPARATION:
                self.invalidate()

    def _sep_grid(self, key):
        r, f, so = self.separation, self.separation_filter, self.steady_off
        if r is None:
            return None
        if f is None:
            return r.grid(key, steady_off=so)
        if f[0] == "pair":
            return r.grid(key, pair=(f[1], f[2]), steady_off=so)
        return r.grid(key, line=f[1], steady_off=so)

    def separation_title(self):
        r, f = self.separation, self.separation_filter
        nm = (r.data.get("names", {}) if r is not None else {})
        tail = " · steady off" if self.steady_off else ""
        if f is None:
            return "All line pairs" + tail
        if f[0] == "pair":
            return "%s / %s" % (nm.get(str(f[1]), f[1]), nm.get(str(f[2]), f[2])) + tail
        return "%s against the others" % nm.get(str(f[1]), f[1]) + tail

    def set_tolerance(self, tol):
        tol = max(0.001, float(tol))
        if tol != self.tolerance:
            self.tolerance = tol
            if self.attr == SEPARATION:
                self.invalidate()

    def separation_shares(self):
        """(share of cells within the tolerance, within twice, beyond, median of the cells) of what is shown."""
        if self.separation is None:
            return None
        if self._sep_stats is None:
            vals = []
            for k in self.separation.tiles():
                g = self._sep_grid(k)
                if g is not None:
                    vals.append(g[np.isfinite(g)])
            v = np.concatenate(vals) if vals else np.zeros(0)
            self._sep_stats = v
        v = self._sep_stats
        if not len(v):
            return None
        t = self.tolerance
        a = float((v <= t).mean())
        b = float((v <= 2 * t).mean())
        return a, b - a, 1 - b, float(np.median(v))

    def coverage(self):
        """coverage_summary() for the current cell and target, cached until the grid changes."""
        if self.store is None:
            return None
        if self._cover is None:
            self._cover = coverage_summary(self.store, getattr(self, "cell", GRID_CELL), self.target_density)
        return self._cover

    def invalidate(self, keys=None):
        self._cover = None
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
    CELL_CHOICES = (0.5, 1.0, 2.0, 2.5, 5.0, 10.0, 12.5, 25.0)

    def set_cell(self, cell):
        """Display cell of the project grid: the stored 0.5 m grid aggregated by a whole factor."""
        cell = min(self.CELL_CHOICES, key=lambda c: abs(c - cell))
        if cell != getattr(self, "cell", GRID_CELL):
            self.cell = cell
            self.invalidate()
        return cell

    def _aggregate(self, g):
        f = int(round(getattr(self, "cell", GRID_CELL) / GRID_CELL))
        if f <= 1 or "count" not in g:
            return g
        n = g["count"].shape[0] // f

        def blk(a):
            return a[:n * f, :n * f].reshape(n, f, n, f)
        cnt = blk(g["count"].astype(float)).sum(axis=(1, 3))
        with np.errstate(invalid="ignore", divide="ignore"):
            out = {"count": cnt, "rej": blk(g["rej"].astype(float)).sum(axis=(1, 3)),
                   "zmean": np.nansum(blk(np.nan_to_num(g["zmean"]) * g["count"]), axis=(1, 3)) / cnt,
                   "imean": np.nansum(blk(np.nan_to_num(g["imean"]) * g["count"]), axis=(1, 3)) / cnt,
                   "zmax": blk(g["zmax"]).max(axis=(1, 3)), "zmin": blk(g["zmin"]).min(axis=(1, 3))}
        return out

    def _attr_grid(self, g):
        if "count" not in g:
            return None
        g = self._aggregate(g)
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
            elif a in ("Density", "Coverage"):
                cell = getattr(self, "cell", GRID_CELL)
                v = g["count"].astype(float) / (cell * cell)
            elif a == "Reject count":
                v = g["rej"].astype(float)
                v[(g["count"] + g["rej"]) == 0] = np.nan
                return v
            else:
                v = g["imean"].astype(float)
        v[g["count"] == 0] = np.nan
        return v

    def value_at(self, x, y):
        """The grid attribute's value (aggregated to the display cell) at project coordinates x, y, else None."""
        if self.store is None or self.attr in (SEPARATION, "None"):
            return None
        T = float(self.store.tile)
        i, j = int(math.floor(x / T)), int(math.floor(y / T))
        key = "%d_%d" % (i, j)
        if key not in set(self.store.keys()):
            return None
        g = self._attr_grid(self.store.load_grid(key))
        if g is None:
            return None
        cell = getattr(self, "cell", GRID_CELL)
        n = g.shape[0]
        cx, cy = int((x - i * T) // cell), int((y - j * T) // cell)
        if not (0 <= cx < n and 0 <= cy < n):
            return None
        v = float(g[cy, cx])
        return v if math.isfinite(v) else None

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
        if self.attr == SEPARATION:
            return self._separation_image(key)
        v = self._attr_grid(self.store.load_grid(key))
        if v is None:
            return None
        if self._range is None:
            self._compute_range()
        lo, hi = self._range
        n = v.shape[0]
        ok = np.isfinite(v)
        if self.attr == "Coverage":
            t = self.target_density
            d = np.nan_to_num(v, nan=0.0)
            rgb = np.empty(v.shape + (3,), np.float32)
            rgb[...] = COVER_POOR
            rgb[d >= 0.5 * t] = COVER_LOW
            rgb[d >= t] = COVER_OK
            rgb *= 0.75                               # less dimmed than a scalar grid: this is the answer
        else:
            rgb = render.scalar_colours(np.nan_to_num(v, nan=lo), lo, hi, self.lut).astype(np.float32)
        if self.shade and "height" in self.attr.lower():
            z = np.where(ok, v, np.nanmean(v) if ok.any() else 0)
            gy, gx = np.gradient(z, getattr(self, "cell", GRID_CELL))
            az, alt = math.radians(315), math.radians(45)
            slope = np.arctan(np.hypot(gx, gy))
            aspect = np.arctan2(-gx, gy)
            sh = np.sin(alt) * np.cos(slope) + np.cos(alt) * np.sin(slope) * np.cos(az - aspect)
            rgb *= (0.55 + 0.45 * np.clip(sh, 0, 1))[..., None]
        if self.attr not in ("Coverage", "Span"):
            rgb *= 0.55                  # dimmed: the points of the Inspection Area stand out on top
        argb = render.pack(np.clip(rgb, 0, 255).astype(np.uint8))
        argb = np.where(ok, argb, np.uint32(0))
        argb = np.ascontiguousarray(np.flipud(argb.reshape(n, n)))
        img = QtGui.QImage(argb.data, n, n, 4 * n, QtGui.QImage.Format.Format_ARGB32).copy()
        self._tiles[key] = img
        return img

    def _separation_image(self, key):
        g = self._sep_grid(key)
        if g is None:
            return None
        n = g.shape[0]
        ok = np.isfinite(g)
        d = np.nan_to_num(g, nan=0.0)
        t = self.tolerance
        rgb = np.empty(g.shape + (3,), np.float32)
        rgb[...] = COVER_POOR
        rgb[d <= 2 * t] = COVER_LOW
        rgb[d <= t] = COVER_OK
        rgb *= 0.8 if self.separation_current else 0.45     # dimmed when out of date
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
        from .linked import paint_2d
        paint_2d(p, self)                        # the linked cursor on top of the drive lines and the area
        if self._range and self.has_underlay() and self.attr in ("Coverage", "Span"):
            self._legend(p)                      # 2.0.0: only the coverage key; 2.4.0: and the Span scale
        elif self.has_underlay() and self.attr == SEPARATION and self.separation is not None:
            self._draw_problem_areas(p)
            self._separation_legend(p)
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
        if self.attr == "Coverage":
            return self._cover_legend(p)
        lo, hi = self._range
        x, y, w, h = self.width() - 22, 34, 10, 110
        lut = render.LUTS[self.lut]
        p.setFont(theme.mono(10))
        off = self.b_offset_z if "height" in self.attr.lower() and self.attr != "Span" else 0
        p.fillRect(QtCore.QRectF(x - 118, y - 10, 134, h + 36), QtGui.QColor(38, 41, 45, 215))
        for k in range(h):
            c = lut[int(255 * (1 - k / (h - 1)))]
            p.fillRect(QtCore.QRectF(x, y + k, w, 1), QtGui.QColor(int(c[0]), int(c[1]), int(c[2])))
        p.setPen(QtGui.QColor(theme.TEXT))
        fmt = (lambda v: "%.0f cm" % (100 * v) if v < 1 else "%.2f m" % v) if self.attr == "Span" else \
            (lambda v: "%.2f" % v)
        p.drawText(QtCore.QRectF(x - 66, y - 6, 62, 14), int(Qt.AlignmentFlag.AlignRight), fmt(hi + off))
        p.drawText(QtCore.QRectF(x - 66, y + h - 8, 62, 14), int(Qt.AlignmentFlag.AlignRight), fmt(lo + off))
        cell = getattr(self, "cell", GRID_CELL)
        p.drawText(QtCore.QRectF(x - 114, y + h + 6, 124, 14), int(Qt.AlignmentFlag.AlignRight),
                   "pts/m²" if self.attr == "Density" else
                   ("Span, %g m cell" % cell) if self.attr == "Span" else self.attr)

    def _cover_legend(self, p):
        t = self.target_density
        c = self.coverage() or {}
        rows = [(COVER_OK, "≥ %g pts/m²" % t), (COVER_LOW, "%g – %g" % (0.5 * t, t)), (COVER_POOR, "< %g" % (0.5 * t))]
        p.setFont(theme.mono(10))
        w = 200
        x, y = self.width() - w - 12, 30
        p.fillRect(QtCore.QRectF(x - 8, y - 6, w + 12, 18 * len(rows) + 52), QtGui.QColor(38, 41, 45, 215))
        for i, (col, text) in enumerate(rows):
            p.fillRect(QtCore.QRectF(x, y + 18 * i + 2, 12, 10), QtGui.QColor(*col))
            p.setPen(QtGui.QColor(theme.TEXT))
            p.drawText(QtCore.QRectF(x + 18, y + 18 * i - 1, w - 18, 16), int(Qt.AlignmentFlag.AlignLeft), text)
        y += 18 * len(rows) + 4
        p.setPen(QtGui.QColor(theme.BRIGHT))
        p.drawText(QtCore.QRectF(x, y, w, 16), int(Qt.AlignmentFlag.AlignLeft),
                   "%.0f%% on target" % (100 * c.get("ok_share", 0)))
        p.setPen(QtGui.QColor(theme.DIM))
        p.drawText(QtCore.QRectF(x, y + 16, w, 16), int(Qt.AlignmentFlag.AlignLeft),
                   "%s covered · median %.0f" % (_area(c.get("area_m2", 0)), c.get("median", 0)))

    def problem_areas(self):
        """The result's problem areas at the current tolerance, those of the shown line or pair only."""
        r, f = self.separation, self.separation_filter
        if r is None:
            return []
        areas = r.problem_areas(self.tolerance)
        if f is None:
            return areas
        if f[0] == "pair":
            return [a for a in areas if (f[1], f[2]) in a["pairs"]]
        return [a for a in areas if f[1] in a["lines"]]

    def _draw_problem_areas(self, p):
        p.setFont(theme.mono(10))
        for a in self.problem_areas():
            first = None
            for (x0, y0, x1, y1) in a.get("boxes") or [a["bbox"]]:
                sx0, sy0 = self.to_screen(x0 - 2, y1 + 2)
                sx1, sy1 = self.to_screen(x1 + 2, y0 - 2)
                r = QtCore.QRectF(min(sx0, sx1), min(sy0, sy1), max(8, abs(sx1 - sx0)), max(8, abs(sy1 - sy0)))
                p.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0, 200), 3))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(r)
                p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255), 1.4, Qt.PenStyle.DashLine))
                p.drawRect(r)
                if first is None or r.width() * r.height() > first.width() * first.height():
                    first = r
            r = first
            lab = str(a["id"])
            t = QtCore.QRectF(r.left(), r.top() - 16, 10 + 8 * len(lab), 15)
            p.fillRect(t, QtGui.QColor(0, 0, 0, 200))
            p.setPen(QtGui.QColor(255, 255, 255))
            p.drawText(t, int(Qt.AlignmentFlag.AlignCenter), lab)

    def _separation_legend(self, p):
        t = 100 * self.tolerance
        sh = self.separation_shares() or (0, 0, 0, None)
        rows = [(COVER_OK, "≤ %g cm" % t, sh[0]), (COVER_LOW, "%g – %g cm" % (t, 2 * t), sh[1]),
                (COVER_POOR, "> %g cm" % (2 * t), sh[2])]
        p.setFont(theme.mono(10))
        w = 200
        x, y = self.width() - w - 12, 30
        p.fillRect(QtCore.QRectF(x - 8, y - 6, w + 12, 18 * len(rows) + 52), QtGui.QColor(38, 41, 45, 215))
        for i, (col, text, share) in enumerate(rows):
            p.fillRect(QtCore.QRectF(x, y + 18 * i + 2, 12, 10), QtGui.QColor(*col))
            p.setPen(QtGui.QColor(theme.TEXT))
            p.drawText(QtCore.QRectF(x + 18, y + 18 * i - 1, w - 18, 16), int(Qt.AlignmentFlag.AlignLeft), text)
            p.drawText(QtCore.QRectF(x + 18, y + 18 * i - 1, w - 22, 16), int(Qt.AlignmentFlag.AlignRight),
                       "%.0f%%" % (100 * share))
        y += 18 * len(rows) + 4
        med = sh[3]
        p.setPen(QtGui.QColor(theme.BRIGHT if self.separation_current else theme.AMBER))
        p.drawText(QtCore.QRectF(x, y, w, 16), int(Qt.AlignmentFlag.AlignLeft), self.separation_title())
        p.setPen(QtGui.QColor(theme.DIM if self.separation_current else theme.AMBER))
        n_ar = len(self.problem_areas())
        p.drawText(QtCore.QRectF(x, y + 16, w, 16), int(Qt.AlignmentFlag.AlignLeft),
                   ("median %.1f cm · %s" % (100 * med, "%d problem area%s" % (n_ar, "" if n_ar == 1 else "s")
                                             if n_ar else "share of cells") if med is not None else "")
                   if self.separation_current else "Out of date: check again")

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
        box_col = QtGui.QColor(*render.BOX_RGB)                 # the Inspection Area outline: white
        p.setPen(QtGui.QPen(box_col, 2.4 if hl else 1.4, Qt.PenStyle.DashLine))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPolygon(QtGui.QPolygonF(sc))
        b = self.box
        br = math.radians(b["bearing"])
        u = np.array([math.sin(br), math.cos(br)])
        c = np.array([b["cx"], b["cy"]])
        tip = c + u * b["length"] / 2
        p.setPen(QtGui.QPen(box_col, 1.2))
        p.drawLine(QtCore.QPointF(*self.to_screen(*c)), QtCore.QPointF(*self.to_screen(*tip)))
        if self.tool == "pan" and (hl or self._bdrag):
            p.setPen(QtGui.QPen(QtGui.QColor(theme.BRIGHT), 1))
            p.setBrush(QtGui.QColor(160, 169, 177))
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
            # whole metres (2.0.0): the opposite corner stays put, the size snaps to 1 m steps
            L = float(max(1, int(round(abs(u[0] - uo[0])))))
            W = float(max(1, int(round(abs(v[0] - vo[0])))))
            mu = uo[0] + math.copysign(L / 2, u[0] - uo[0])
            mv = vo[0] + math.copysign(W / 2, v[0] - vo[0])
            br = math.radians(b0["bearing"])
            self.box.update(cx=b0["cx"] + mu * math.sin(br) + mv * math.cos(br),
                            cy=b0["cy"] + mu * math.cos(br) - mv * math.sin(br), length=L, width=W)
        elif k == "rotate":
            b0 = self._bdrag[1]
            self.box["bearing"] = (math.degrees(math.atan2(x - b0["cx"], y - b0["cy"])) + 360) % 360
        elif k == "new":
            (x0, y0) = self._bdrag[1]
            L = math.hypot(x - x0, y - y0)
            if L > 0.5:
                w = self.box["width"] if self.box else 30.0
                L = float(max(1, int(round(L))))
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


def _area(m2):
    return "%.2f km²" % (m2 / 1e6) if m2 >= 1e5 else "{:,.0f} m²".format(m2)
