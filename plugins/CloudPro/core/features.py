"""Feature extraction filters (group "Features").

Road-side objects found in mobile Lidar point clouds and given their own
classes, after the published methods below (each filter's ``ref``):

  road markings   Guan et al. 2014   intensity image, Otsu threshold per distance band from the trajectory
  curbs           Yang, Fang, Li 2013 height jump + steep face, continuous along the road
  poles           Cabo et al. 2014    small isolated sections in horizontal voxel layers, stacked vertically
  traffic signs   Riveiro et al. 2016 retro-reflective intensity, planar cluster of sign size and height
  power lines     Guan et al. 2016    high, linear, horizontal points; clusters that fit a catenary
  manhole covers  Guan et al. 2014    blobs of different intensity in the road's intensity image
  guardrails      Gao et al. 2022, Huang et al. 2020   a straight band 0.4-1.2 m above ground along the road

All of them classify by default (the classes are kept in the LAS output);
Action can also reject.  They need ground: when no ground class is present
yet they use a robust minimum surface instead, and say so.  Thresholds are
tuned for the Hesai XT-32 (calibrated reflectivity 0-255, 0.18 deg steps).
"""
import math

import numpy as np

from . import spatial as sp

# classes added by the feature filters (ASPRS 64-255 are user definable)
FEATURE_CLASSES = {14: "Wire - conductor", 65: "Pole", 67: "Traffic sign", 68: "Road marking", 69: "Curb",
                   70: "Manhole cover", 71: "Guardrail"}


def _imports():
    from .filters import ACTION, P, Filter, _bilinear, _interp_grid, _min_grid, _res, _action
    return ACTION, P, Filter, _bilinear, _interp_grid, _min_grid, _res, _action


ACTION, P, Filter, _bilinear, _interp_grid, _min_grid, _res, _action = _imports()


# =============================================================================
# shared helpers
# =============================================================================
def height_above_ground(ctx, idx, cell=0.5):
    """Height of points idx above the ground class (2), or above a robust
    minimum surface of all accepted points when there is no ground yet."""
    key = ("hag", cell)
    cache = ctx.__dict__.setdefault("_feat_cache", {})
    if key not in cache:
        gm = ctx.accepted & (ctx.cls == 2)
        if gm.sum() >= 50:
            gi = np.flatnonzero(gm)
            ix, iy, x0, y0 = sp.grid_index(ctx.x[gi], ctx.y[gi], cell)
            nx, ny = int(ix.max()) + 1, int(iy.max()) + 1
            S = np.zeros(ny * nx)
            C = np.zeros(ny * nx)
            np.add.at(S, iy * nx + ix, ctx.z[gi])
            np.add.at(C, iy * nx + ix, 1)
            with np.errstate(invalid="ignore", divide="ignore"):
                G = (S / C).reshape(ny, nx)
            dem = _interp_grid(G)
        else:
            ai = np.flatnonzero(ctx.accepted)
            G, _ix, _iy, x0, y0 = _min_grid(ctx.x[ai], ctx.y[ai], ctx.z[ai], max(cell, 1.0), pit=0.3, rank=3)
            cell = max(cell, 1.0)
            from scipy import ndimage
            dem = ndimage.grey_opening(sp.fill_nan_nearest(G), size=(5, 5))
            ctx.messages.append("Features: no ground class yet - heights are above a minimum surface "
                                "(run a ground filter first for better results)")
        cache[key] = (dem, x0, y0, cell)
    dem, x0, y0, c = cache[key]
    return ctx.z[idx] - _bilinear(dem, (ctx.x[idx] - x0) / c - 0.5, (ctx.y[idx] - y0) / c - 0.5)


def track_distance(ctx, idx):
    """Horizontal distance of points idx to the nearest drive line (None without tracks)."""
    if not ctx.tracks:
        return None
    E = np.concatenate([np.asarray(t["E"], float) for t in ctx.tracks.values()])
    N = np.concatenate([np.asarray(t["N"], float) for t in ctx.tracks.values()])
    if len(E) == 0:
        return None
    from scipy.spatial import cKDTree
    d, _ = cKDTree(np.column_stack([E, N])).query(np.column_stack([ctx.x[idx], ctx.y[idx]]), workers=-1)
    return d


def otsu(v, bins=128):
    """Otsu threshold of values v (and the separability eta, 0..1)."""
    v = np.asarray(v, float)
    if len(v) < 10 or v.max() <= v.min():
        return (float(v.max()) if len(v) else 0.0), 0.0
    h, e = np.histogram(v, bins=bins)
    c = (e[:-1] + e[1:]) / 2
    w0 = np.cumsum(h)
    w1 = w0[-1] - w0
    m0 = np.cumsum(h * c)
    mt = m0[-1]
    with np.errstate(invalid="ignore", divide="ignore"):
        mu0 = m0 / w0
        mu1 = (mt - m0) / w1
        sb = w0 * w1 * (mu0 - mu1) ** 2
    sb = np.nan_to_num(sb) / (w0[-1] ** 2)
    k = int(np.argmax(sb))
    var = v.var()
    return float(e[k + 1]), (float(sb[k] / var) if var > 0 else 0.0)


def cluster(xyz, r):
    """Single-linkage clusters of points closer than r (DBSCAN with min_pts 1).
    Returns (label per point, size per label)."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    n = len(xyz)
    if n == 0:
        return np.zeros(0, int), np.zeros(0, int)
    pairs = sp.tree(xyz).query_pairs(r, output_type="ndarray")
    g = coo_matrix((np.ones(len(pairs), np.int8), (pairs[:, 0], pairs[:, 1])), shape=(n, n))
    _, lab = connected_components(g, directed=False)
    return lab, np.bincount(lab)


def pca(P):
    """Eigenvalues (descending) and eigenvectors (columns, same order) of points P."""
    Q = P - P.mean(0)
    w, V = np.linalg.eigh(Q.T @ Q / max(1, len(P)))
    return w[::-1], V[:, ::-1]


def point_features(ctx, sel, k):
    """Per point: linearity, planarity, |z| of the main direction, |z| of the normal (k nearest accepted)."""
    tr, acc = ctx.neighbours()
    k = int(min(k, len(acc)))
    _, i = tr.query(ctx.xyz(sel), k=k, workers=-1)
    xyz = ctx.xyz(acc)
    out = {n: np.empty(len(sel), np.float32) for n in ("lin", "pla", "dz", "nz")}
    for s in range(0, len(sel), 200000):
        C = sp.local_cov(xyz, i[s:s + 200000])
        w, V = np.linalg.eigh(C)
        s1, s2, s3 = (np.sqrt(np.maximum(w[:, j], 1e-12)) for j in (2, 1, 0))
        out["lin"][s:s + 200000] = (s1 - s2) / s1
        out["pla"][s:s + 200000] = (s2 - s3) / s1
        out["dz"][s:s + 200000] = np.abs(V[:, 2, 2])
        out["nz"][s:s + 200000] = np.abs(V[:, 2, 0])
    return out


def _intensity(ctx, idx, norm):
    I = ctx.ps.i[idx].astype(float)
    if norm:
        r = np.maximum(ctx.ps.rng[idx] * 0.01, 0.5)
        I = I * (r / 10.0) ** 2
    return I


def _components(mask, structure=None):
    from scipy import ndimage
    lab, n = ndimage.label(mask, structure=structure if structure is not None else np.ones((3, 3)))
    return lab, n


DIST = lambda lo, hi: [P("dmin", "Distance from the drive line, from", "float", lo, 0, 200, 0.1, " m"),   # noqa: E731
                       P("dmax", "Distance from the drive line, to", "float", hi, 0.1, 200, 0.5, " m")]
NORM = P("norm", "Range-normalise the intensity (I*(r/10 m)^2)", "bool", False,
         help="Leave off for the XT-32, whose reflectivity is already calibrated for range")


def _road(ctx, sel, flt):
    """Road-surface points: near the ground, and of the ground class when there is one."""
    h = height_above_ground(ctx, sel)
    sel = sel[np.abs(h) <= flt.v("hmax")]
    if flt.v("ground_only") and (ctx.cls == 2).sum() >= 50:
        sel = sel[ctx.cls[sel] == 2]
    return sel


def _near_track(ctx, sel, flt):
    d = track_distance(ctx, sel)
    if d is None:
        ctx.messages.append("%s: no drive lines - distance limits not used" % flt.name)
        return sel, None
    m = (d >= flt.v("dmin")) & (d <= flt.v("dmax"))
    return sel[m], d[m]


# =============================================================================
# road markings
# =============================================================================
class RoadMarkings(Filter):
    key, name, group, mod, speed = "f_markings", "Road markings (intensity)", "Features", "C", "+"
    ref = ("Guan, Li, Yu, Wang, Chapman, Yang 2014, 'Using mobile laser scanning data for automated extraction of "
           "road markings', ISPRS J. 87:93-107, doi:10.1016/j.isprsjprs.2013.11.005")
    help = ("Painted lines, arrows and crossings are much brighter than asphalt. The road points (low above "
            "ground, near the drive line) are split into bands by distance from the drive line, because "
            "intensity falls with range and incidence; each band gets its own Otsu threshold. Bright points "
            "are gridded, the gaps closed, and patches smaller than the minimum area are dropped.")
    params = ([P("cell", "Image cell", "float", 0.10, 0.02, 1.0, 0.01, " m"),
               P("band", "Distance band width", "float", 2.5, 0.5, 20, 0.5, " m"),
               P("contrast", "Minimum contrast over the road", "float", 40, 0, 255, 1,
                 help="The threshold is at least the band's median intensity plus this"),
               P("hmax", "Road: up to this height above ground", "float", 0.15, 0.02, 1.0, 0.01, " m"),
               P("ground_only", "Only ground-class points (when there is ground)", "bool", True,
                 help="Keeps the bright bases of poles, rails and walls out; needs a ground filter before"),
               P("area", "Minimum marking area", "float", 0.05, 0.0, 10, 0.01, " m2")] + DIST(0.0, 15.0) +
              [NORM] + ACTION("Classify", 68))

    def run(self, ctx, sel):
        sel = _road(ctx, sel, self)
        sel, d = _near_track(ctx, sel, self)
        if len(sel) < 50:
            return _res()
        I = _intensity(ctx, sel, self.v("norm"))
        if d is None:
            d = np.zeros(len(sel))
        band = np.floor(d / self.v("band")).astype(int)
        cand = np.zeros(len(sel), bool)
        for b in np.unique(band):
            m = band == b
            if m.sum() < 30:
                continue
            thr, _ = otsu(I[m])
            thr = max(thr, float(np.median(I[m])) + self.v("contrast"))
            cand[m] = I[m] >= thr
        if not cand.any():
            return _res()
        c = self.v("cell")
        ix, iy, _, _ = sp.grid_index(ctx.x[sel], ctx.y[sel], c)
        nx, ny = int(ix.max()) + 1, int(iy.max()) + 1
        img = np.zeros((ny, nx), bool)
        img[iy[cand], ix[cand]] = True
        from scipy import ndimage
        img = ndimage.binary_closing(img, structure=np.ones((3, 3)), iterations=1) | img
        lab, n = _components(img)
        area = np.bincount(lab.ravel(), minlength=n + 1) * c * c
        keep = area >= self.v("area")
        keep[0] = False
        ok = cand & keep[lab[iy, ix]]
        ctx.messages.append("Road markings: %d patches, %d points" % (int(keep.sum()), int(ok.sum())))
        return _action(ctx, sel[ok], self.v("action"), self.v("cls"))


# =============================================================================
# curbs
# =============================================================================
class Curbs(Filter):
    key, name, group, mod, speed = "f_curbs", "Curbs (height jump)", "Features", "C", "+"
    ref = ("Yang, Fang, Li 2013, 'Semi-automated extraction and delineation of 3D roads of street scene from "
           "mobile laser scanning point clouds', ISPRS J. 79:80-93, doi:10.1016/j.isprsjprs.2013.01.016")
    help = ("A curb is a small step (typically 8-30 cm) with a steep face between the road and the sidewalk. "
            "A fine minimum-height grid of the points near the ground is searched for cells whose "
            "neighbourhood rises by a curb's height over a short distance with a steep slope. Candidate "
            "cells must form a run at least the minimum length long, which drops cars' wheels, kerb stones "
            "and debris.")
    params = ([P("cell", "Grid cell", "float", 0.10, 0.03, 0.5, 0.01, " m"),
               P("window", "Window", "float", 0.30, 0.1, 2.0, 0.05, " m"),
               P("hmin", "Height jump from", "float", 0.08, 0.01, 1.0, 0.01, " m"),
               P("hmax", "Height jump to", "float", 0.30, 0.02, 2.0, 0.01, " m"),
               P("slope", "Steeper than", "float", 40.0, 5, 89, 1, " deg"),
               P("length", "Minimum length", "float", 2.0, 0.2, 100, 0.1, " m")] + DIST(1.0, 20.0) +
              ACTION("Classify", 69))

    def run(self, ctx, sel):
        h = height_above_ground(ctx, sel)
        sel = sel[(h > -0.3) & (h < self.v("hmax") + 0.3)]
        sel, _ = _near_track(ctx, sel, self)
        if len(sel) < 50:
            return _res()
        c = self.v("cell")
        x, y, z = ctx.x[sel], ctx.y[sel], ctx.z[sel]
        ix, iy, _, _ = sp.grid_index(x, y, c)
        nx, ny = int(ix.max()) + 1, int(iy.max()) + 1
        lo = np.full(ny * nx, np.inf)
        hi = np.full(ny * nx, -np.inf)
        np.minimum.at(lo, iy * nx + ix, z)
        np.maximum.at(hi, iy * nx + ix, z)
        lo, hi = lo.reshape(ny, nx), hi.reshape(ny, nx)
        occ = np.isfinite(lo)
        from scipy import ndimage
        w = max(3, int(round(self.v("window") / c)) | 1)
        L = np.where(occ, lo, np.inf)
        H = np.where(occ, hi, -np.inf)
        mn = ndimage.minimum_filter(L, size=w, mode="nearest")
        mx = ndimage.maximum_filter(H, size=w, mode="nearest")
        jump = mx - mn
        steep = math.tan(math.radians(self.v("slope")))
        # the jump happens over at most the window, so its slope is at least jump / window
        cand = occ & (jump >= self.v("hmin")) & (jump <= self.v("hmax")) & (jump / self.v("window") >= steep * 0.5)
        # the cell itself must lie in the step (between the low and the high level)
        with np.errstate(invalid="ignore"):
            mid = occ & (hi - mn >= 0.25 * jump) & (lo - mn <= 0.75 * jump + 0.02)
        cand &= mid
        lab, n = _components(cand)
        if n == 0:
            return _res()
        keep = np.zeros(n + 1, bool)
        objs = ndimage.find_objects(lab)
        for k, sl in enumerate(objs, start=1):
            yy, xx = np.nonzero(lab[sl] == k)
            if len(xx) < 3:
                continue
            P2 = np.column_stack([xx, yy]) * c
            ev, _ = pca(np.c_[P2, np.zeros(len(P2))])
            length = 4 * math.sqrt(max(ev[0], 0)) + c          # ~ extent of a uniform run
            width = 4 * math.sqrt(max(ev[1], 0)) + c
            keep[k] = length >= self.v("length") and width <= max(1.0, 0.25 * length)
        ok = keep[lab[iy, ix]]
        ctx.messages.append("Curbs: %d runs, %d points" % (int(keep.sum()), int(ok.sum())))
        return _action(ctx, sel[ok], self.v("action"), self.v("cls"))


# =============================================================================
# poles
# =============================================================================
class Poles(Filter):
    key, name, group, mod, speed = "f_poles", "Poles (voxel layers)", "Features", "C", "+"
    ref = ("Cabo, Ordonez, Garcia-Cortes, Martinez 2014, 'An algorithm for automatic detection of pole-like "
           "street furniture objects from mobile laser scanner point clouds', ISPRS J. 87:47-56, "
           "doi:10.1016/j.isprsjprs.2013.10.008; cf. Lehtomaki et al. 2010, Remote Sens. 2(3):641")
    help = ("The points above the ground are put in voxels. In every horizontal layer of voxels a pole is a "
            "small section with empty space around it. Small isolated sections stacked over the minimum "
            "height, starting near the ground, are poles (lamp posts, sign posts, traffic lights, bollards). "
            "Tree trunks look the same; with 'Not under a canopy' a stack that ends in a large crown is left "
            "alone.")
    params = ([P("voxel", "Voxel size", "float", 0.20, 0.05, 1.0, 0.01, " m"),
               P("diam", "Section at most", "float", 0.6, 0.1, 3.0, 0.05, " m"),
               P("height", "Minimum height", "float", 1.5, 0.3, 30, 0.1, " m"),
               P("base", "Starts within this of the ground", "float", 2.0, 0.1, 10, 0.1, " m"),
               P("min_pts", "Points to occupy a voxel", "int", 3, 1, 100,
                 help="Voxels with fewer points (snow, rain, stray returns) do not break a pole's isolation"),
               P("trunks", "Not under a canopy (tree trunks)", "bool", True)] + ACTION("Classify", 65))

    def run(self, ctx, sel):
        h = height_above_ground(ctx, sel)
        sel, h = sel[h > 0.25], h[h > 0.25]
        if len(sel) < 20:
            return _res()
        v = self.v("voxel")
        X = np.floor(np.column_stack([ctx.x[sel], ctx.y[sel], h]) / v).astype(np.int64)
        X -= X.min(0)
        dims = X.max(0) + 3
        X += 1
        key = (X[:, 2] * dims[1] + X[:, 1]) * dims[0] + X[:, 0]
        uk, inv, cnt = np.unique(key, return_inverse=True, return_counts=True)
        inv = inv.ravel()
        dense = cnt >= int(self.v("min_pts"))
        vz = uk // (dims[0] * dims[1])
        vy = (uk // dims[0]) % dims[1]
        vx = uk % dims[0]
        maxc = max(1, int(math.ceil(self.v("diam") / v)))
        small = np.zeros(len(uk), bool)
        from scipy import ndimage
        for z in np.unique(vz):
            m = np.flatnonzero((vz == z) & dense)
            if len(m) == 0:
                continue
            x0, y0 = vx[m].min() - 2, vy[m].min() - 2
            img = np.zeros((vy[m].max() - y0 + 3, vx[m].max() - x0 + 3), bool)
            img[vy[m] - y0, vx[m] - x0] = True
            # isolation: components of the layer dilated by one voxel (a ring of empty space)
            lab, n = ndimage.label(ndimage.binary_dilation(img, np.ones((3, 3))), np.ones((3, 3)))
            if n == 0:
                continue
            sl = ndimage.find_objects(lab)
            ext = np.array([[s[0].stop - s[0].start - 2, s[1].stop - s[1].start - 2] for s in sl])
            ok = np.r_[False, (ext[:, 0] <= maxc) & (ext[:, 1] <= maxc)]
            small[m] = ok[lab[vy[m] - y0, vx[m] - x0]]
        # stacks: 3-D components of the small sections
        cand = np.flatnonzero(small)
        if len(cand) == 0:
            return _res()
        comp, sizes = sp.voxel_components(np.column_stack([vx[cand], vy[cand], vz[cand]]).astype(float), 1.0)
        nz = len(sizes)
        zmin = np.full(nz, 1 << 30)
        zmax = np.full(nz, -1)
        np.minimum.at(zmin, comp, vz[cand])
        np.maximum.at(zmax, comp, vz[cand])
        good = ((zmax - zmin + 1) * v >= self.v("height")) & ((zmin - 1) * v <= self.v("base"))
        if self.v("trunks") and good.any():
            # voxels of large sections (crowns) just above the top of a stack
            big = ~small
            occ_big = set(uk[big].tolist())
            for k in np.flatnonzero(good):
                mk = cand[comp == k]
                top = vz[mk] == zmax[k]
                hit = 0
                for dz in range(1, max(2, int(round(1.0 / v))) + 1):
                    for xx, yy in zip(vx[mk][top], vy[mk][top]):
                        for ddx in (-1, 0, 1):
                            for ddy in (-1, 0, 1):
                                if ((zmax[k] + dz) * dims[1] + yy + ddy) * dims[0] + xx + ddx in occ_big:
                                    hit += 1
                if hit >= 3:
                    lid = np.flatnonzero(big & (vz > zmax[k]) & (vz <= zmax[k] + int(round(2.0 / v))) &
                                         (np.abs(vx - vx[mk].mean()) < 3 / v) & (np.abs(vy - vy[mk].mean()) < 3 / v))
                    if len(lid) * v * v > 1.5 / v * v * 4:      # a crown, not a sign or a lamp head
                        good[k] = False
        vox_ok = np.zeros(len(uk), bool)
        vox_ok[cand[good[comp]]] = True
        ok = vox_ok[inv]
        ctx.messages.append("Poles: %d found, %d points" % (int(good.sum()), int(ok.sum())))
        return _action(ctx, sel[ok], self.v("action"), self.v("cls"))


# =============================================================================
# traffic signs
# =============================================================================
class TrafficSigns(Filter):
    key, name, group, mod, speed = "f_signs", "Traffic signs (retro-reflective)", "Features", "C", "++"
    ref = ("Riveiro, Diaz-Vilarino, Conde-Carnero, Soilan, Arias 2016, 'Automatic segmentation and shape-based "
           "classification of retro-reflective traffic signs from mobile LiDAR data', IEEE JSTARS 9(1):295-303, "
           "doi:10.1109/JSTARS.2015.2461680")
    help = ("Sign faces are retro-reflective, so they return far more intensity than anything else at their "
            "height. Bright points at sign height are clustered; a cluster is a sign when it is planar, "
            "upright and of sign size. The whole plate (including its darker back and edges) is then "
            "classified: the points within a few centimetres of the fitted plane inside its outline.")
    params = ([P("imin", "Intensity at least", "int", 180, 0, 255, help="About the 95-98th percentile for the XT-32"),
               P("h0", "Height above ground from", "float", 1.2, 0, 20, 0.1, " m"),
               P("h1", "Height above ground to", "float", 7.0, 0.5, 30, 0.1, " m"),
               P("eps", "Cluster distance", "float", 0.30, 0.05, 2.0, 0.05, " m"),
               P("min_pts", "Minimum points", "int", 15, 3, 10000),
               P("planarity", "Planarity at least", "float", 0.7, 0, 1, 0.01),
               P("a0", "Area from", "float", 0.1, 0.01, 50, 0.05, " m2"),
               P("a1", "Area to", "float", 4.0, 0.05, 50, 0.1, " m2"),
               P("grow", "Include the plate within", "float", 0.06, 0.0, 0.5, 0.01, " m",
                 help="0 = only the bright points")] + [NORM] + ACTION("Classify", 67))

    def run(self, ctx, sel):
        I = _intensity(ctx, sel, self.v("norm"))
        b = sel[I >= self.v("imin")]
        if len(b) < self.v("min_pts"):
            return _res()
        h = height_above_ground(ctx, b)
        b = b[(h >= self.v("h0")) & (h <= self.v("h1"))]
        if len(b) < self.v("min_pts"):
            return _res()
        lab, sizes = cluster(ctx.xyz(b), self.v("eps"))
        out = []
        nsign = 0
        tr, acc = ctx.neighbours()
        for k in np.flatnonzero(sizes >= self.v("min_pts")):
            m = b[lab == k]
            P3 = ctx.xyz(m)
            ev, V = pca(P3)
            s1, s2, s3 = np.sqrt(np.maximum(ev, 1e-12))
            if (s2 - s3) / s1 < self.v("planarity") or abs(V[2, 2]) > 0.4:      # planar and upright
                continue
            c = P3.mean(0)
            uv = (P3 - c) @ V[:, :2]
            lo, hi = uv.min(0), uv.max(0)
            area = float(np.prod(hi - lo))
            if not self.v("a0") <= area <= self.v("a1"):
                continue
            nsign += 1
            g = self.v("grow")
            if g > 0:
                r = float(np.hypot(*(hi - lo))) / 2 + g
                near = acc[tr.query_ball_point(c, r)]
                near = near[np.isin(near, sel)]
                Q = ctx.xyz(near) - c
                quv = Q @ V[:, :2]
                ok = (np.abs(Q @ V[:, 2]) <= g) & np.all((quv >= lo - g) & (quv <= hi + g), axis=1)
                m = np.union1d(m, near[ok])
            out.append(m)
        idx = np.concatenate(out) if out else np.zeros(0, np.int64)
        ctx.messages.append("Traffic signs: %d found, %d points" % (nsign, len(idx)))
        return _action(ctx, idx, self.v("action"), self.v("cls"))


# =============================================================================
# power lines
# =============================================================================
class PowerLines(Filter):
    key, name, group, mod, speed = "f_wires", "Power lines (catenary)", "Features", "C", "+"
    ref = ("Guan, Yu, Li, Ji, Zhang 2016, 'Extraction of power-transmission lines from vehicle-borne lidar data', "
           "Int. J. Remote Sens. 37(1):229-247, doi:10.1080/01431161.2015.1125549")
    help = ("Wires hang high above the ground with free space above and below them. Points higher than the "
            "limit whose voxel column is empty for the clearance above and below are clustered along their "
            "length; a cluster is a wire span when it is long enough, straight in plan and its height follows "
            "a catenary (z = a + c*cosh((s-b)/c), fitted as a parabola) within the RMS limit. Walls, poles and "
            "trees fail the clearance test; roof edges and branches fail the length, straightness or fit.")
    params = [P("hmin", "Higher above ground than", "float", 4.0, 0.5, 100, 0.5, " m"),
              P("voxel", "Voxel size", "float", 0.25, 0.05, 2, 0.05, " m"),
              P("clear", "Free space above and below", "float", 1.0, 0.2, 10, 0.1, " m"),
              P("min_pts", "Points per voxel at least", "int", 3, 1, 100,
                help="Single returns in the air (snow, rain, birds) are not wire"),
              P("eps", "Cluster distance", "float", 1.5, 0.1, 10, 0.1, " m",
                help="Along a wire; about the distance driven per rotation (1 m at 36 km/h, 600 rpm)"),
              P("length", "Minimum length", "float", 5.0, 0.5, 500, 0.5, " m"),
              P("rms", "Catenary fit RMS at most", "float", 0.15, 0.01, 2, 0.01, " m"),
              P("lateral", "Straight in plan within", "float", 0.5, 0.05, 5, 0.05, " m")] + ACTION("Classify", 14)

    def run(self, ctx, sel):
        h = height_above_ground(ctx, sel)
        c = sel[h > self.v("hmin")]
        if len(c) < 10:
            return _res()
        # occupancy of every accepted point that could block the clearance (dense voxels only: snow
        # flakes and stray returns do not count)
        v = self.v("voxel")
        acc = np.flatnonzero(ctx.accepted)
        ha = height_above_ground(ctx, acc)
        acc = acc[ha > self.v("hmin") - self.v("clear") - v]
        o = np.array([ctx.x[acc].min(), ctx.y[acc].min(), ctx.z[acc].min()]) - 4 * v
        A = np.floor((ctx.xyz(acc) - o) / v).astype(np.int64)
        D = A.max(0) + 8
        ka, cnt = np.unique((A[:, 2] * D[1] + A[:, 1]) * D[0] + A[:, 0], return_counts=True)
        occ = ka[cnt >= 2]
        C = np.floor((ctx.xyz(c) - o) / v).astype(np.int64)
        kc = (C[:, 2] * D[1] + C[:, 1]) * D[0] + C[:, 0]
        uc, inv, ncnt = np.unique(kc, return_inverse=True, return_counts=True)
        blocked = ncnt < int(self.v("min_pts"))
        n = max(1, int(round(self.v("clear") / v)))
        for dz in list(range(2, n + 2)) + list(range(-n - 1, -1)):
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    q = uc + (dz * D[1] + dy) * D[0] + dx
                    p = np.clip(np.searchsorted(occ, q), 0, len(occ) - 1)
                    blocked |= occ[p] == q
        c = c[~blocked[inv.ravel()]]
        if len(c) < 5:
            return _res()
        lab, sizes = cluster(ctx.xyz(c), self.v("eps"))
        out = []
        for k in np.flatnonzero(sizes >= 5):
            m = c[lab == k]
            P3 = ctx.xyz(m)
            xy = P3[:, :2] - P3[:, :2].mean(0)
            ev, V = np.linalg.eigh(xy.T @ xy / len(xy))
            s = xy @ V[:, 1]
            lat = xy @ V[:, 0]
            if s.max() - s.min() < self.v("length") or np.sqrt(np.mean(lat ** 2)) > self.v("lateral"):
                continue
            A2 = np.column_stack([np.ones_like(s), s, s * s])
            coef, *_ = np.linalg.lstsq(A2, P3[:, 2], rcond=None)
            if coef[2] < -1e-4:                       # a wire sags; it does not arch
                continue
            if np.sqrt(np.mean((A2 @ coef - P3[:, 2]) ** 2)) > self.v("rms"):
                continue
            out.append(m)
        idx = np.concatenate(out) if out else np.zeros(0, np.int64)
        ctx.messages.append("Power lines: %d spans, %d points" % (len(out), len(idx)))
        return _action(ctx, idx, self.v("action"), self.v("cls"))


# =============================================================================
# manhole covers
# =============================================================================
class Manholes(Filter):
    key, name, group, mod, speed = "f_manholes", "Manhole covers (intensity blobs)", "Features", "C", "+"
    ref = ("Guan, Yu, Li, Liu 2014, 'Automated detection of urban road manhole covers using mobile laser "
           "scanning data', Remote Sens. Lett. 5(12):1042-1050, doi:10.1080/2150704X.2014.994716; "
           "cf. Yu et al. 2014, IEEE GRSL, doi:10.1109/LGRS.2014.2301195")
    help = ("Metal covers return a different intensity from the asphalt around them. The road surface is "
            "rasterised as an intensity image; cells that differ from the local background by more than the "
            "contrast (brighter, darker or either) are grouped, and round blobs of a cover's size are kept. "
            "Long patches (road markings) fail the roundness test.")
    params = ([P("cell", "Image cell", "float", 0.08, 0.02, 0.5, 0.01, " m"),
               P("contrast", "Contrast to the road", "float", 20, 1, 255, 1),
               P("appear", "Covers appear", "choice", "Either", choices=["Brighter", "Darker", "Either"]),
               P("d0", "Diameter from", "float", 0.4, 0.1, 3, 0.05, " m"),
               P("d1", "Diameter to", "float", 1.2, 0.2, 5, 0.05, " m"),
               P("round", "Roundness at least", "float", 0.55, 0, 1, 0.01,
                 help="Area / area of the bounding circle; a disc is about 0.8 on a grid, a dash under 0.2"),
               P("hmax", "Road: up to this height above ground", "float", 0.10, 0.02, 1.0, 0.01, " m"),
              P("ground_only", "Only ground-class points (when there is ground)", "bool", True,
                help="Keeps the bright bases of poles, rails and walls out; needs a ground filter before")] +
              DIST(0.0, 10.0) + [NORM] + ACTION("Classify", 70))

    def run(self, ctx, sel):
        sel = _road(ctx, sel, self)
        sel, _ = _near_track(ctx, sel, self)
        if len(sel) < 100:
            return _res()
        I = _intensity(ctx, sel, self.v("norm"))
        c = self.v("cell")
        ix, iy, _, _ = sp.grid_index(ctx.x[sel], ctx.y[sel], c)
        nx, ny = int(ix.max()) + 1, int(iy.max()) + 1
        S = np.zeros((ny, nx))
        C = np.zeros((ny, nx))
        np.add.at(S, (iy, ix), I)
        np.add.at(C, (iy, ix), 1)
        from scipy import ndimage
        occ = C > 0
        with np.errstate(invalid="ignore", divide="ignore"):
            M = np.where(occ, S / C, np.nan)
        M = sp.fill_nan_nearest(M)
        M = ndimage.median_filter(M, size=3)                    # fill single empty cells, keep edges
        # background: median over 2.5 cover diameters, computed on a coarser grid (fast) and expanded
        f = max(1, int(round(0.25 / c)))
        Mc = M[: (M.shape[0] // f) * f or M.shape[0], : (M.shape[1] // f) * f or M.shape[1]]
        if f > 1 and Mc.shape[0] >= f and Mc.shape[1] >= f:
            Mc = Mc.reshape(Mc.shape[0] // f, f, Mc.shape[1] // f, f).mean(axis=(1, 3))
        else:
            f = 1
            Mc = M
        bgc = ndimage.median_filter(Mc, size=max(5, int(round(2.5 * self.v("d1") / (c * f))) | 1))
        bg = np.repeat(np.repeat(bgc, f, 0), f, 1)
        bg = np.pad(bg, ((0, M.shape[0] - bg.shape[0]), (0, M.shape[1] - bg.shape[1])), mode="edge")
        diff = M - bg
        a = self.v("appear")
        k = self.v("contrast")
        cand = (diff >= k) if a == "Brighter" else (diff <= -k) if a == "Darker" else (np.abs(diff) >= k)
        cand &= ndimage.binary_dilation(occ, np.ones((3, 3)))
        cand = ndimage.binary_opening(ndimage.binary_closing(cand, np.ones((3, 3))), np.ones((2, 2)))
        lab, n = _components(cand)
        keep = np.zeros(n + 1, bool)
        for j, sl in enumerate(ndimage.find_objects(lab), start=1):
            yy, xx = np.nonzero(lab[sl] == j)
            area = len(xx) * c * c
            deq = 2 * math.sqrt(area / math.pi)
            r = float(np.max(np.hypot(xx - xx.mean(), yy - yy.mean()))) * c + c / 2
            rnd = area / (math.pi * r * r) if r > 0 else 0
            keep[j] = self.v("d0") <= deq <= self.v("d1") and rnd >= self.v("round")
        ok = keep[lab[iy, ix]]
        ctx.messages.append("Manhole covers: %d found, %d points" % (int(keep.sum()), int(ok.sum())))
        return _action(ctx, sel[ok], self.v("action"), self.v("cls"))


# =============================================================================
# guardrails
# =============================================================================
class Guardrails(Filter):
    key, name, group, mod, speed = "f_guardrails", "Guardrails (straight band)", "Features", "C", "+"
    ref = ("Gao, Yang, Wu, Wen, Jiang 2022, 'Automatic extraction of road guardrails from mobile laser scanning "
           "point clouds', IEEE T-ITS 23(2):1572-1577, doi:10.1109/TITS.2020.3025067; Huang et al. 2020, "
           "Sensors and Materials 32(11):4005, doi:10.18494/SAM.2020.3118")
    help = ("A guardrail is a long straight band of points at rail height beside the road with nothing above "
            "it. Points in the height band are clustered; each cluster gets a line fit in plan. It is a "
            "guardrail when it is long enough, the points stay close to the line, most 1.5 m slices along it "
            "are occupied, it runs roughly parallel to the drive line and the space above the rail is empty "
            "(which rejects walls, fences of greater height and vehicles).")
    params = ([P("h0", "Height above ground from", "float", 0.4, 0, 3, 0.05, " m"),
               P("h1", "Height above ground to", "float", 1.2, 0.2, 5, 0.05, " m"),
               P("eps", "Cluster distance", "float", 0.5, 0.05, 3, 0.05, " m"),
               P("length", "Minimum length", "float", 4.0, 0.5, 500, 0.5, " m"),
               P("rms", "Line fit RMS at most", "float", 0.10, 0.01, 1, 0.01, " m"),
               P("occupied", "Occupied 1.5 m slices at least", "float", 60, 0, 100, 1, " %"),
               P("angle", "Parallel to the drive line within", "float", 20, 1, 90, 1, " deg"),
               P("above", "Points above the rail at most", "float", 15, 0, 100, 1, " %",
                 help="Share of points 0.3-2.5 m above the band, within 0.3 m of the rail")] + DIST(0.5, 15.0) +
              ACTION("Classify", 71))

    def run(self, ctx, sel):
        h = height_above_ground(ctx, sel)
        band = sel[(h >= self.v("h0")) & (h <= self.v("h1"))]
        band, _ = _near_track(ctx, band, self)
        if len(band) < 20:
            return _res()
        lab, sizes = cluster(ctx.xyz(band), self.v("eps"))
        tr, acc = ctx.neighbours()
        out = []
        tdir = None
        if ctx.tracks:
            from scipy.spatial import cKDTree
            TE = np.concatenate([t["E"] for t in ctx.tracks.values()])
            TN = np.concatenate([t["N"] for t in ctx.tracks.values()])
            HD = np.concatenate([np.asarray(t["heading"], float) for t in ctx.tracks.values()])
            tdir = (cKDTree(np.column_stack([TE, TN])), HD)
        for k in np.flatnonzero(sizes >= 20):
            m = band[lab == k]
            xy = np.column_stack([ctx.x[m], ctx.y[m]])
            c0 = xy.mean(0)
            ev, V = np.linalg.eigh((xy - c0).T @ (xy - c0) / len(xy))
            d = V[:, 1]
            s = (xy - c0) @ d
            lat = (xy - c0) @ V[:, 0]
            L = s.max() - s.min()
            if L < self.v("length") or np.sqrt(np.mean(lat ** 2)) > self.v("rms"):
                continue
            nsl = max(1, int(math.ceil(L / 1.5)))
            occ = np.unique(np.minimum(((s - s.min()) / 1.5).astype(int), nsl - 1)).size / nsl
            if occ * 100 < self.v("occupied"):
                continue
            if tdir is not None:
                _, j = tdir[0].query(c0)
                hd = tdir[1][j]
                tv = np.array([math.sin(hd), math.cos(hd)])
                ang = math.degrees(math.acos(min(1.0, abs(float(tv @ d)))))
                if ang > self.v("angle"):
                    continue
            # empty above the rail
            near = acc[np.unique(np.concatenate(tr.query_ball_point(ctx.xyz(m[::max(1, len(m) // 200)]), 2.6)))]
            hn = height_above_ground(ctx, near)
            q = np.column_stack([ctx.x[near], ctx.y[near]]) - c0
            on_rail = (np.abs(q @ V[:, 0]) <= 0.3) & ((q @ d) >= s.min()) & ((q @ d) <= s.max())
            up = on_rail & (hn > self.v("h1") + 0.3) & (hn < self.v("h1") + 2.5)
            if up.sum() > self.v("above") / 100.0 * len(m):
                continue
            out.append(m)
        idx = np.concatenate(out) if out else np.zeros(0, np.int64)
        ctx.messages.append("Guardrails: %d found, %d points" % (len(out), len(idx)))
        return _action(ctx, idx, self.v("action"), self.v("cls"))


FEATURE_FILTERS = (RoadMarkings, Curbs, Poles, TrafficSigns, PowerLines, Manholes, Guardrails)
