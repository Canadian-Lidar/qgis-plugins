"""Exports split into sections by distance (chainage) along a line: a survey line's trajectory, a centerline
computed from the drive lines, or a line drawn in QGIS.

Every point gets the chainage of its nearest position on the line and its offset from it; sections are cut
square to the line every `length` metres from `start` (chainage at the line's first vertex). With a width, only
points within width / 2 either side are kept. Points before the line's start or past its end belong to the first
or last section. Files are numbered and named by their chainages: <base>_001_0+000-0+100<ext>.
"""
import math

import numpy as np

DENSIFY = 0.25          # m between the vertices the nearest-position search uses (positions are then exact)


def chainage_text(s):
    """1250 -> '1+250', 1250.5 -> '1+250.5', -40 -> '-0+040'."""
    neg = s < 0
    s = abs(s)
    km = int(s // 1000)
    m = s - km * 1000
    ms = ("%03d" % int(round(m))) if abs(m - round(m)) < 1e-6 else ("%05.1f" % m)
    if ms.startswith("1000"):
        km, ms = km + 1, "000"
    return "%s%d+%s" % ("-" if neg else "", km, ms)


class Alignment:
    """A polyline in local metres (n, 2) with chainage along it."""

    def __init__(self, xy, name=""):
        xy = np.asarray(xy, float)[:, :2]
        keep = np.r_[True, np.hypot(*np.diff(xy, axis=0).T) > 1e-6]
        xy = xy[keep]
        if len(xy) < 2:
            raise ValueError("the line has fewer than two distinct points")
        self.xy = xy
        self.name = name
        seg = np.hypot(*np.diff(xy, axis=0).T)
        self.s = np.r_[0.0, np.cumsum(seg)]
        self.length = float(self.s[-1])
        # dense copy for the nearest-vertex search
        n = np.maximum(1, np.ceil(seg / DENSIFY).astype(int))
        parts = [xy[i] + (xy[i + 1] - xy[i]) * (np.arange(n[i])[:, None] / n[i]) for i in range(len(seg))]
        self.dense = np.vstack(parts + [xy[-1:]])
        self.dense_seg = np.r_[np.repeat(np.arange(len(seg)), n), len(seg) - 1]
        from scipy.spatial import cKDTree
        self.tree = cKDTree(self.dense)

    def reversed(self):
        return Alignment(self.xy[::-1], self.name)

    def station(self, x, y):
        """Chainage from the first vertex (m) and signed offset (m, left of the direction of travel positive)
        of the nearest position on the line, for points (x, y) in local metres."""
        P = np.column_stack([x, y])
        _d, j = self.tree.query(P, workers=-1)
        seg = self.dense_seg[j]
        best_s = np.zeros(len(P))
        best_o = np.zeros(len(P))
        best_d = np.full(len(P), np.inf)
        nseg = len(self.xy) - 1
        for off in (-1, 0, 1):              # the nearest vertex's segment and its neighbours
            k = np.clip(seg + off, 0, nseg - 1)
            a, b = self.xy[k], self.xy[k + 1]
            ab = b - a
            L2 = (ab ** 2).sum(1)
            u = ((P - a) * ab).sum(1) / L2
            first, last = k == 0, k == nseg - 1
            uc = np.where(first & (u < 0), u, np.where(last & (u > 1), u, np.clip(u, 0, 1)))   # ends extend
            q = a + ab * uc[:, None]
            d = np.hypot(*(P - q).T)
            better = d < best_d - 1e-12
            L = np.sqrt(L2)
            best_d = np.where(better, d, best_d)
            best_s = np.where(better, self.s[k] + uc * L, best_s)
            cross = (ab[:, 0] * (P[:, 1] - a[:, 1]) - ab[:, 1] * (P[:, 0] - a[:, 0])) / L
            best_o = np.where(better, cross, best_o)
        return best_s, best_o


class Sections:
    """Section plan: alignment, section length, start chainage, width (0 = all points)."""

    def __init__(self, alignment, length=100.0, start=0.0, width=0.0):
        if not length > 0:
            raise ValueError("the section length must be more than 0")
        self.al = alignment
        self.length = float(length)
        self.start = float(start)
        self.width = float(width or 0.0)
        self.count = max(1, int(math.ceil(alignment.length / self.length - 1e-9)))

    def index(self, x, y):
        """Section index of local points (or -1 when outside the width)."""
        s, o = self.al.station(x, y)
        k = np.clip(np.floor(s / self.length).astype(np.int64), 0, self.count - 1)
        if self.width > 0:
            k = np.where(np.abs(o) <= self.width / 2, k, -1)
        return k

    def range(self, k):
        a = self.start + k * self.length
        b = self.start + min((k + 1) * self.length, self.al.length)
        return a, b

    def name(self, base, ext, k, line=None):
        a, b = self.range(k)
        part = "" if line is None else "_L%03d" % (line + 1)
        return "%s%s_%03d_%s-%s%s" % (base, part, k + 1, chainage_text(a), chainage_text(b), ext)

    def split(self, a, origin):
        """[(k, None, rows)] of store records (x, y integer mm from the project origin) by section."""
        k = self.index(a["x"] * 1e-3, a["y"] * 1e-3)
        out = []
        for c in np.unique(k):
            if c >= 0:
                out.append((int(c), None, a[k == c]))
        return out

    def describe(self):
        return "%d sections of %g m along %s (%.0f m)%s" % (
            self.count, self.length, self.al.name or "the line", self.al.length,
            ", %g m wide" % self.width if self.width else "")


# ------------------------------------------------------------------ the lines to measure along
def track_alignment(project, lid, min_speed=0.5):
    """A survey line's trajectory (sensor track), without the samples while stopped (they would zigzag)."""
    tr = project.track(lid)
    if tr is None:
        raise ValueError("Line %d has no trajectory track; georeference it first" % (lid + 1))
    E, N, t = np.asarray(tr["E"], float), np.asarray(tr["N"], float), np.asarray(tr["t"], float)
    keep = _moving(E, N, t, min_speed)
    name = next((l.get("name", "") for l in project.lines if l["id"] == lid), "") or "Line %d" % (lid + 1)
    return Alignment(np.column_stack([E[keep], N[keep]]), "%s (trajectory)" % name)


def _moving(E, N, t, min_speed):
    """Samples kept: moving, and at least 0.5 m from the previous kept one."""
    keep = np.zeros(len(E), bool)
    if not len(E):
        return keep
    keep[0] = True
    last = 0
    for i in range(1, len(E)):
        if math.hypot(E[i] - E[last], N[i] - N[last]) >= 0.5:
            keep[i] = True
            last = i
    return keep


def centerline_from_lines(project, near=15.0, smooth=20.0, step=1.0):
    """A centerline computed from the drive lines: along the longest line, every `step` m, the mean position of
    every line passing within `near` m (each line's nearest track position), smoothed over `smooth` m. Where the
    road was driven both ways it lies between the passes; where one line covers it, on that line."""
    tracks = {}
    for ln in project.lines:
        try:
            tracks[ln["id"]] = track_alignment(project, ln["id"])
        except ValueError:
            pass
    if not tracks:
        raise ValueError("no line has a trajectory track; georeference the captures first")
    ref = max(tracks.values(), key=lambda a: a.length)
    ss = np.arange(0.0, ref.length, step)
    if ss[-1] < ref.length:
        ss = np.r_[ss, ref.length]
    R = np.column_stack([np.interp(ss, ref.s, ref.xy[:, 0]), np.interp(ss, ref.s, ref.xy[:, 1])])
    acc = R.copy()
    cnt = np.ones(len(R))
    for al in tracks.values():
        if al is ref:
            continue
        d, j = al.tree.query(R)
        ok = d <= near
        acc[ok] += al.dense[j[ok]]
        cnt[ok] += 1
    C = acc / cnt[:, None]
    w = max(1, int(round(smooth / step)))
    if len(C) > 2 * w:
        k = np.ones(2 * w + 1) / (2 * w + 1)
        Cs = C.copy()
        for c in (0, 1):
            pad = np.r_[np.full(w, C[0, c]), C[:, c], np.full(w, C[-1, c])]
            Cs[:, c] = np.convolve(pad, k, mode="valid")
        Cs[0], Cs[-1] = C[0], C[-1]
        C = Cs
    return Alignment(C, "the centerline from %d drive line%s" % (len(tracks), "s" if len(tracks) > 1 else ""))


def layer_alignment(layer, project_crs, origin, feature_id=None):
    """A QGIS line layer: the selected feature, else the only one, else the longest; in the project's CRS and
    local metres. Multi-part features use their longest part."""
    from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject
    feats = list(layer.getFeatures())
    if feature_id is not None:
        feats = [f for f in feats if f.id() == feature_id]
    elif layer.selectedFeatureCount() == 1:
        feats = list(layer.selectedFeatures())
    if not feats:
        raise ValueError("the layer %s has no line" % layer.name())
    xf = QgsCoordinateTransform(layer.crs(), QgsCoordinateReferenceSystem(project_crs), QgsProject.instance())
    best = None
    for f in feats:
        g = f.geometry()
        if g is None or g.isEmpty():
            continue
        g = type(g)(g)
        g.transform(xf)
        parts = g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]
        for pts in parts:
            xy = np.array([[p.x(), p.y()] for p in pts], float)
            if len(xy) >= 2:
                L = np.hypot(*np.diff(xy, axis=0).T).sum()
                if best is None or L > best[0]:
                    best = (L, xy)
    if best is None:
        raise ValueError("the layer %s has no line" % layer.name())
    return Alignment(best[1] - np.asarray(origin[:2], float), layer.name())
