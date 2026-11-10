"""Line-to-line separation (CloudPro 2.2.0): how far overlapping lines sit apart, measured on the stored points.

Nothing is changed. The answer to "do these lines need strip alignment?" before such a tool exists.

Method. The project is cut into cubes of PATCH metres (aligned with the tiles). In each cube, each line's kept
points get a plane fit (centroid + covariance; the normal is the smallest eigenvector). A cube counts as a
**patch** for a line when that line's points there are flat and thin (thickness = sqrt of the smallest eigenvalue
<= MAX_THICKNESS, the in-plane spread >= MIN_SPREAD x PATCH, at least MIN_POINTS points). Where two lines both have
a patch in the same cube and their normals agree within MAX_ANGLE, their separation is the distance between the
two planes along the shared normal: d = n . (c_b - c_a). Flat ground and roofs give the vertical separation, walls
the horizontal one.

Per line pair: the number of patches, the median and 95th percentile of |d|, the constant shift (east, north, up)
of line b against line a that best explains the d's (robust least squares; horizontal only when there are walls
in enough directions), and what is left after that shift: a small rest means one shift per line would fix it, a
large rest means the error changes along the line (the case for a time-varying correction).

The Plan view's "Line separation" grid shows, per PATCH cell, the median |d| of all patches in the column - of
every pair, of one pair, or of the pairs with one line (`Result.grid(key, pair=, line=)`).

    run(project, progress, cancelled, patch=1.0) -> Result   (also saved: work/separation/)
    load(project) -> Result | None
    signature(project)                                        (the result is out of date when it changes)
    report_lines(result, names, tolerance)                    text report
"""
import hashlib
import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from .store import REJECTED, parse_key

VERSION = 3
PATCH = 1.0                       # cube side (m); must divide the tile size
PATCHES = (0.5, 1.0, 2.0)
MIN_POINTS = 12                   # points of one line in a cube
MAX_THICKNESS = 0.03              # m, sqrt(smallest eigenvalue)
MIN_SPREAD = 0.15                 # x PATCH, sqrt(middle eigenvalue)
MAX_ANGLE = 10.0                  # deg between the two lines' normals
MAX_SEP = 0.5                     # m: larger is not the same surface (a car moved, a door opened)
FACE_GAP = 0.04                   # m: patches this close to a cube face across their normal are left out
TILE_POINTS = 2_000_000           # points used per tile at most (evenly thinned above)
PAIR_SAMPLE = 60_000              # patches kept per pair for the statistics and the shift
NOISE_CLASSES = (7, 18)
DEFAULT_TOLERANCE = 0.03          # m
TOLERANCES = (0.01, 0.02, 0.03, 0.05, 0.10)
MIN_WALLS = 30.0                  # horizontal information (sum of nx^2+ny^2 per direction) needed for E/N


from .importer import Cancelled                      # noqa: E402  (the window treats it as cancelled: nothing written)


def folder(project):
    return os.path.join(project.work, "separation")


def signature(project):
    """Changes when the points are georeferenced again (lines, their settings and counts)."""
    keep = []
    for l in project.lines:
        keep.append({k: l.get(k) for k in ("id", "files", "trajectory", "mount", "lever", "bore", "latency_s",
                                           "points", "t0", "t1", "returns", "rmin", "rmax", "decimate", "min_speed")})
    s = json.dumps([keep, int(getattr(project.store, "total", 0))], sort_keys=True, default=str)
    return hashlib.sha1(s.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------------------------- one tile
def _tile_patches(a, x0, y0, T, patch, tbase=0.0):
    """Patch pairs in one tile: (la, lb, n[k,3], d[k], col[k], n, ta[k], tb[k]) with col the cell index
    (iy * n + ix) in the tile and ta / tb the mean time (s after tbase) of each line's points in the cube."""
    m = (a["flag"] & REJECTED) == 0
    for c in NOISE_CLASSES:
        m &= a["cls"] != c
    idx = np.flatnonzero(m)
    if len(idx) > TILE_POINTS:
        idx = idx[:: int(math.ceil(len(idx) / TILE_POINTS))]
    if len(idx) < 2 * MIN_POINTS:
        return None
    b = a[idx]
    x = b["x"] * 0.001 - x0
    y = b["y"] * 0.001 - y0
    z = b["z"] * 0.001
    ln = b["line"].astype(np.int64)
    n = int(round(T / patch))
    ix = np.clip((x // patch).astype(np.int64), 0, n - 1)
    iy = np.clip((y // patch).astype(np.int64), 0, n - 1)
    iz = np.floor(z / patch).astype(np.int64)
    iz -= iz.min()
    nl = int(ln.max()) + 1
    nz = int(iz.max()) + 1
    key = ((iy * n + ix) * nz + iz) * nl + ln
    uk, inv, cnt = np.unique(key, return_inverse=True, return_counts=True)
    good = cnt >= MIN_POINTS
    if good.sum() < 2:
        return None
    # coordinates about each cube's centre keep the sums precise
    xc = x - (ix + 0.5) * patch
    yc = y - (iy + 0.5) * patch
    zc = z - (np.floor(z / patch) + 0.5) * patch
    M = len(uk)

    def s(w):
        return np.bincount(inv, weights=w, minlength=M)
    sx, sy, sz = s(xc), s(yc), s(zc)
    sxx, syy, szz, sxy, sxz, syz = s(xc * xc), s(yc * yc), s(zc * zc), s(xc * yc), s(xc * zc), s(yc * zc)
    stt = s(b["t"] - tbase)
    g = np.flatnonzero(good)
    c = cnt[g].astype(float)
    mx, my, mz = sx[g] / c, sy[g] / c, sz[g] / c
    tm = stt[g] / c
    C = np.empty((len(g), 3, 3))
    C[:, 0, 0] = sxx[g] / c - mx * mx
    C[:, 1, 1] = syy[g] / c - my * my
    C[:, 2, 2] = szz[g] / c - mz * mz
    C[:, 0, 1] = C[:, 1, 0] = sxy[g] / c - mx * my
    C[:, 0, 2] = C[:, 2, 0] = sxz[g] / c - mx * mz
    C[:, 1, 2] = C[:, 2, 1] = syz[g] / c - my * mz
    ev, vec = np.linalg.eigh(C)
    ev = np.clip(ev, 0, None)
    flat = (np.sqrt(ev[:, 0]) <= MAX_THICKNESS) & (np.sqrt(ev[:, 1]) >= MIN_SPREAD * patch)
    # a surface lying along a cube face is cut by it (half its points in the next cube): its centroid is biased,
    # so a patch whose centroid is within FACE_GAP of the face across its normal is left out
    ax = np.argmax(np.abs(vec[:, :, 0]), axis=1)
    off = np.choose(ax, [mx, my, mz])
    flat &= (0.5 * patch - np.abs(off)) >= FACE_GAP
    if flat.sum() < 2:
        return None
    g = g[flat]
    tm = tm[flat]
    nrm = vec[flat, :, 0]
    # one orientation per normal: up for flat surfaces, else east (then north) - the sign only has to agree
    sgn = np.where(np.abs(nrm[:, 2]) > 0.2, np.sign(nrm[:, 2]),
                   np.where(np.abs(nrm[:, 0]) > 1e-6, np.sign(nrm[:, 0]), np.sign(nrm[:, 1] + 1e-12)))
    nrm *= sgn[:, None]
    k = uk[g]
    line = k % nl
    cube = k // nl
    # absolute centroid (tile frame, z absolute)
    czc = cube % nz
    col = cube // nz
    cx = (col % n + 0.5) * patch + mx[flat]
    cy = (col // n + 0.5) * patch + my[flat]
    zbase = np.floor(z.min() / patch)
    cz = (czc + zbase + 0.5) * patch + mz[flat]
    cen = np.stack([cx, cy, cz], 1)
    # pairs: entries are sorted by cube, then line (key order); pair i with i+s inside the same cube
    out_la, out_lb, out_n, out_d, out_col, out_ta, out_tb = [], [], [], [], [], [], []
    cos_max = math.cos(math.radians(MAX_ANGLE))
    for s_ in range(1, nl):
        if s_ >= len(cube):
            break
        same = cube[:-s_] == cube[s_:]
        if not same.any():
            break
        i = np.flatnonzero(same)
        j = i + s_
        dot = np.einsum("ij,ij->i", nrm[i], nrm[j])
        ok = dot >= cos_max
        i, j = i[ok], j[ok]
        if not len(i):
            continue
        nn = nrm[i] + nrm[j]
        nn /= np.linalg.norm(nn, axis=1)[:, None]
        d = np.einsum("ij,ij->i", nn, cen[j] - cen[i])
        ok = np.abs(d) <= MAX_SEP
        out_la.append(line[i][ok])
        out_lb.append(line[j][ok])
        out_n.append(nn[ok])
        out_d.append(d[ok])
        out_col.append(col[i][ok])
        out_ta.append(tm[i][ok])
        out_tb.append(tm[j][ok])
    if not out_la:
        return None
    return (np.concatenate(out_la), np.concatenate(out_lb), np.concatenate(out_n), np.concatenate(out_d),
            np.concatenate(out_col), n, np.concatenate(out_ta), np.concatenate(out_tb))


def _column_median(col, absd, n):
    """n x n grid (row = y) of the median value (|d|, a time ...) per cell; NaN where no patch."""
    grid = np.full(n * n, np.nan, np.float32)
    if len(col):
        o = np.lexsort((absd, col))
        cs, ds = col[o], absd[o]
        starts = np.flatnonzero(np.r_[True, cs[1:] != cs[:-1]])
        ends = np.r_[starts[1:], len(cs)]
        mid = (starts + ends - 1) / 2.0
        lo, hi = np.floor(mid).astype(int), np.ceil(mid).astype(int)
        grid[cs[starts]] = 0.5 * (ds[lo] + ds[hi])
    return grid.reshape(n, n)


# --------------------------------------------------------------------------------------------- pairs
def solve_shift(n, d):
    """Constant shift t (E, N, Up) of line b against line a with n . t ~ d (robust). Returns
    (t, horizontal_ok, rest) - rest = |d - n.t| median after the fit; E/N are NaN when the walls do not fix them."""
    n = np.asarray(n, float)
    d = np.asarray(d, float)
    keep = np.ones(len(d), bool)
    H = n[:, :2].T @ n[:, :2]
    hor = len(d) >= 3 and np.linalg.eigvalsh(H).min() >= MIN_WALLS
    t = np.zeros(3)
    for _ in range(4):
        nk, dk = n[keep], d[keep]
        if len(dk) < 3:
            break
        if hor:
            A = nk.T @ nk + 1e-9 * np.eye(3)
            t = np.linalg.solve(A, nk.T @ dk)
        else:
            up = np.abs(nk[:, 2]) > 0.9
            t = np.array([0.0, 0.0, float(np.median(dk[up] / nk[up, 2])) if up.any() else 0.0])
        r = d - n @ t
        mad = 1.4826 * np.median(np.abs(r[keep] - np.median(r[keep])))
        keep = np.abs(r) <= max(3 * mad, 0.01)
    r = d - n @ t
    if not hor:
        up = np.abs(n[:, 2]) > 0.9
        rest = float(np.median(np.abs(r[up]))) if up.any() else float("nan")
        t = np.array([np.nan, np.nan, t[2]])
    else:
        rest = float(np.median(np.abs(r)))
    return t, bool(hor), rest


class Result:
    def __init__(self, data, grids, pair_grids=None, time_grids=None):
        self.data = data                  # json-able dict
        self.grids = grids                # tile key -> n x n float32 median |d| (m), every pair
        self.pair_grids = pair_grids or {}    # (tile key, a, b) -> the same for one pair
        self.time_grids = time_grids or {}    # (tile key, a, b) -> (2, n, n) float32: median time of a's and b's
        #                                       points per cell, s after data["t_base"]
        self._areas = {}
        self._by_tile = {}
        for (k, a, b) in self.pair_grids:
            self._by_tile.setdefault(k, []).append((a, b))

    @property
    def patch(self):
        return float(self.data.get("patch", PATCH))

    @property
    def pairs(self):
        return self.data.get("pairs", [])

    @property
    def lines(self):
        return self.data.get("lines", [])

    def grid(self, key, pair=None, line=None):
        """Median |d| per cell of a tile: every pair, one pair (a, b), or the pairs with one line (the median of
        those pairs' cells)."""
        if pair is not None:
            return self.pair_grids.get((key, int(pair[0]), int(pair[1])))
        if line is not None:
            gs = [self.pair_grids[(key, a, b)] for a, b in self._by_tile.get(key, ()) if line in (a, b)]
            if not gs:
                return None
            if len(gs) == 1:
                return gs[0]
            with np.errstate(all="ignore"):
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)
                    return np.nanmedian(np.stack(gs), axis=0).astype(np.float32)
        return self.grids.get(key)

    def tiles(self):
        return list(self.grids)

    def problem_areas(self, tol=DEFAULT_TOLERANCE):
        """problem_areas(self, tol), cached per tolerance."""
        k = round(float(tol), 4)
        if k not in self._areas:
            self._areas[k] = problem_areas(self, tol)
        return self._areas[k]

    def is_current(self, project):
        return self.data.get("signature") == signature(project)

    def share_within(self, tol):
        h = self.data.get("hist") or {}
        edges, cnt = np.asarray(h.get("edges", [])), np.asarray(h.get("counts", []), float)
        if not len(cnt) or cnt.sum() == 0:
            return float("nan")
        return float(cnt[edges[:-1] < tol - 1e-9].sum() / cnt.sum())


def run(project, progress=None, cancelled=None, patch=PATCH, workers=None):
    """Measure the separation over the whole project; saves and returns a Result. Raises Cancelled."""
    st = project.store
    T = float(st.tile)
    if abs(T / patch - round(T / patch)) > 1e-9:
        raise ValueError("the patch size must divide the tile size (%g m)" % T)
    keys = [k for k in st.keys() if int(st.counts.get(k, 0)) > 0]
    t0 = time.time()
    t_starts = [float(l["t0"]) for l in project.lines if l.get("t0")]
    tbase = math.floor(min(t_starts)) if t_starts else 0.0
    grids = {}
    pair_grids = {}
    time_grids = {}
    pair_rows = {}                          # (a, b) -> list of (n, d)
    pair_count = {}
    hist_edges = np.r_[np.arange(0, 0.20001, 0.0005), 10.0]
    hist = np.zeros(len(hist_edges) - 1)
    rng = np.random.default_rng(1)
    done = [0]

    def one(k):
        if cancelled and cancelled():
            return k, None
        i, j = parse_key(k)
        a = st.tile_array(k)
        return k, _tile_patches(a, i * T, j * T, T, patch, tbase)

    workers = workers or max(1, min(8, (os.cpu_count() or 2) - 1))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for k, res in ex.map(one, keys):
            if cancelled and cancelled():
                raise Cancelled()
            done[0] += 1
            if progress and (done[0] % 4 == 0 or done[0] == len(keys)):
                progress(done[0] / max(1, len(keys)), "Line separation: tile %d of %d" % (done[0], len(keys)))
            if res is None:
                continue
            la, lb, nn, d, col, n, ta, tb = res
            ad = np.abs(d)
            grids[k] = _column_median(col, ad, n)
            hist += np.histogram(ad, hist_edges)[0]
            pk = la * 65536 + lb
            for p in np.unique(pk):
                sel = np.flatnonzero(pk == p)
                ab = (int(p // 65536), int(p % 65536))
                pair_grids[(k,) + ab] = _column_median(col[sel], ad[sel], n)
                time_grids[(k,) + ab] = np.stack([_column_median(col[sel], ta[sel], n),
                                                  _column_median(col[sel], tb[sel], n)])
                pair_count[ab] = pair_count.get(ab, 0) + len(sel)
                if len(sel) > PAIR_SAMPLE // 4:
                    sel = rng.choice(sel, PAIR_SAMPLE // 4, replace=False)
                pair_rows.setdefault(ab, []).append((nn[sel].astype(np.float32), d[sel].astype(np.float32)))
    names = {int(l["id"]): l.get("name", "Line %s" % l["id"]) for l in project.lines}
    pairs = []
    per_line = {}
    for (a, b), parts in sorted(pair_rows.items()):
        nn = np.concatenate([p[0] for p in parts]).astype(float)
        d = np.concatenate([p[1] for p in parts]).astype(float)
        if len(d) > PAIR_SAMPLE:
            s = rng.choice(len(d), PAIR_SAMPLE, replace=False)
            nn, d = nn[s], d[s]
        ad = np.abs(d)
        t, hor, rest = solve_shift(nn, d)
        up = np.abs(nn[:, 2]) > 0.9
        walls = np.abs(nn[:, 2]) < 0.3
        rec = {"a": a, "b": b, "patches": int(pair_count[(a, b)]), "median": float(np.median(ad)),
               "p95": float(np.percentile(ad, 95)), "flat": int(up.sum()), "walls": int(walls.sum()),
               "shift": [None if not np.isfinite(v) else float(v) for v in t], "horizontal": hor, "rest": rest,
               "vertical_median": float(np.median(np.abs(d[up]))) if up.any() else None,
               "horizontal_median": float(np.median(np.abs(d[walls]))) if walls.any() else None}
        pairs.append(rec)
        for l in (a, b):
            per_line.setdefault(l, []).append((rec["median"], rec["patches"]))
    lines = []
    for l, v in sorted(per_line.items()):
        w = np.array([x[1] for x in v], float)
        m = np.array([x[0] for x in v], float)
        lines.append({"id": l, "name": names.get(l, "Line %d" % l), "pairs": len(v), "patches": int(w.sum()),
                      "median": float(np.sum(m * w) / w.sum())})
    org = getattr(st, "origin", None)
    data = {"version": VERSION, "patch": patch, "tile": T, "t_base": tbase,
            "origin": [float(v) for v in org[:2]] if org is not None else [0.0, 0.0],
            "signature": signature(project),
            "utc": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()), "seconds": round(time.time() - t0, 1),
            "tiles": len(keys), "tiles_with_overlap": len(grids), "patches": int(hist.sum()),
            "median": _hist_median(hist_edges, hist), "pairs": sorted(pairs, key=lambda p: -p["median"]),
            "lines": lines, "names": {str(k): v for k, v in names.items()},
            "hist": {"edges": hist_edges.tolist(), "counts": hist.astype(int).tolist()},
            "settings": {"min_points": MIN_POINTS, "max_thickness": MAX_THICKNESS, "max_angle": MAX_ANGLE,
                         "max_sep": MAX_SEP}}
    res = Result(data, grids, pair_grids, time_grids)
    save(project, res)
    return res


# --------------------------------------------------------------------------------------------- problem areas
AREA_MIN_M2 = 4.0                 # smallest problem area (m2 of cells beyond twice the tolerance)
MERGE_GAP_S = 1.0                 # s: problem areas of the same line this close in time are one
AREA_MAX_SHARE = 0.5              # an area covering more of a pair's overlap is the whole pair, not a local area


def _clusters(gx, gy):
    """Labels of 8-connected clusters of integer cells."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    n = len(gx)
    if n == 0:
        return np.zeros(0, int)
    key = gx.astype(np.int64) * 4_000_000 + gy.astype(np.int64)
    o = np.argsort(key)
    ks = key[o]
    rows, cols = [], []
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            q = key + dx * 4_000_000 + dy
            pos = np.searchsorted(ks, q)
            pos = np.clip(pos, 0, n - 1)
            hit = ks[pos] == q
            rows.append(np.flatnonzero(hit))
            cols.append(o[pos[hit]])
    r = np.concatenate(rows)
    c = np.concatenate(cols)
    g = coo_matrix((np.ones(len(r)), (r, c)), shape=(n, n))
    return connected_components(g, directed=False)[1]


def problem_areas(res, tol=DEFAULT_TOLERANCE):
    """Local areas where two lines disagree by more than twice the tolerance: per pair, 8-connected clusters of
    such cells of at least AREA_MIN_M2 that cover less than AREA_MAX_SHARE of the pair's overlap (a whole pair
    off is shown by the pair's median). Clusters of different pairs that share cells are one area; the line common
    to all its pairs is the one at fault ("line"), else None. Largest first:
    [{"id", "pairs": [(a, b)], "line", "lines", "x", "y" (project frame, m), "e", "n" (world), "bbox"
      (x0, y0, x1, y1), "m2", "median", "max", "t0", "t1" (UTC s of the line at fault's points, or of every
      line in the area)}]"""
    T = float(res.data.get("tile", 50.0))
    pt = res.patch
    n = int(round(T / pt))
    tb = float(res.data.get("t_base", 0.0))
    lim = 2 * tol
    cell_min = max(1, int(math.ceil(AREA_MIN_M2 / (pt * pt))))
    per_pair = {}
    for (k, a, b), g in res.pair_grids.items():
        i, j = parse_key(k)
        ok = np.isfinite(g)
        cnt = int(ok.sum())
        bad = ok & (g > lim)
        rec = per_pair.setdefault((a, b), {"cells": 0, "gx": [], "gy": [], "v": [], "ta": [], "tb": []})
        rec["cells"] += cnt
        if not bad.any():
            continue
        iy, ix = np.nonzero(bad)
        rec["gx"].append(i * n + ix)
        rec["gy"].append(j * n + iy)
        rec["v"].append(g[iy, ix])
        tg = res.time_grids.get((k, a, b))
        if tg is not None:
            rec["ta"].append(tg[0][iy, ix])
            rec["tb"].append(tg[1][iy, ix])
        else:
            rec["ta"].append(np.full(len(ix), np.nan, np.float32))
            rec["tb"].append(np.full(len(ix), np.nan, np.float32))
    raw = []
    for (a, b), rec in per_pair.items():
        if not rec["gx"]:
            continue
        gx, gy = np.concatenate(rec["gx"]), np.concatenate(rec["gy"])
        v, ta, tbb = np.concatenate(rec["v"]), np.concatenate(rec["ta"]), np.concatenate(rec["tb"])
        lab = _clusters(gx, gy)
        for L in range(int(lab.max()) + 1 if len(lab) else 0):
            m = lab == L
            c = int(m.sum())
            if c < cell_min or c > AREA_MAX_SHARE * max(1, rec["cells"]):
                continue
            raw.append({"pair": (a, b), "cells": set(zip(gx[m].tolist(), gy[m].tolist())), "v": v[m],
                        "t": {a: ta[m], b: tbb[m]}})
    # merge clusters of different pairs that share cells
    owner = {}
    parent = list(range(len(raw)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for i, r in enumerate(raw):
        for c in r["cells"]:
            j = owner.setdefault(c, i)
            if j != i:
                parent[find(i)] = find(j)
    groups = {}
    for i in range(len(raw)):
        groups.setdefault(find(i), []).append(raw[i])
    org = res.data.get("origin", [0.0, 0.0])
    blobs = []
    for parts in groups.values():
        cells = set().union(*[p["cells"] for p in parts])
        pairs = sorted({p["pair"] for p in parts})
        common = set(pairs[0])
        for pr in pairs[1:]:
            common &= set(pr)
        line = next(iter(common)) if len(common) == 1 and len(pairs) > 1 else None
        spans = {}
        for p in parts:
            for l, tv in p["t"].items():
                if line is not None and l != line:
                    continue
                tv = tv[np.isfinite(tv)].astype(np.float64)
                if len(tv):
                    lo, hi = spans.get(l, (np.inf, -np.inf))
                    spans[l] = (min(lo, float(tv.min()) + tb), max(hi, float(tv.max()) + tb))
        blobs.append({"pairs": set(pairs), "line": line, "cells": cells, "v": [p["v"] for p in parts],
                      "spans": spans})
    # one area per stretch of a line: blobs with the same line at fault whose times overlap or nearly touch
    # (the line's footprint can cross several separate places in the same seconds)
    blobs.sort(key=lambda r: (r["line"] is None, r["line"] if r["line"] is not None else 0,
                              min((s[0] for s in r["spans"].values()), default=0)))
    merged = []
    for bl in blobs:
        m = merged[-1] if merged else None
        if (m is not None and bl["line"] is not None and m["line"] == bl["line"] and bl["spans"] and m["spans"]
                and bl["spans"][bl["line"]][0] <= m["spans"][m["line"]][1] + MERGE_GAP_S):
            m["pairs"] |= bl["pairs"]
            m["boxes"].append(bl["cells"])
            m["cells"] = m["cells"] | bl["cells"]
            m["v"] += bl["v"]
            a0, a1 = m["spans"][m["line"]]
            b0, b1 = bl["spans"][bl["line"]]
            m["spans"][m["line"]] = (min(a0, b0), max(a1, b1))
            continue
        bl["boxes"] = [bl["cells"]]
        merged.append(bl)
    out = []
    for bl in merged:
        cells = bl["cells"]
        pairs = sorted(bl["pairs"])
        lines = sorted({l for pr in pairs for l in pr})
        cx = np.array([c[0] for c in cells], float)
        cy = np.array([c[1] for c in cells], float)
        v = np.concatenate(bl["v"])
        boxes = []
        for cs in bl["boxes"]:
            bx = np.array([c[0] for c in cs], float)
            by = np.array([c[1] for c in cs], float)
            boxes.append((float(bx.min() * pt), float(by.min() * pt), float((bx.max() + 1) * pt),
                          float((by.max() + 1) * pt)))
        big = max(bl["boxes"], key=len)
        bx = np.array([c[0] for c in big], float)
        by = np.array([c[1] for c in big], float)
        x, y = float((bx.mean() + 0.5) * pt), float((by.mean() + 0.5) * pt)
        spans = {int(l): sp for l, sp in sorted(bl["spans"].items(), key=lambda kv: kv[1][0])}
        first = next(iter(spans.values()), (None, None))
        out.append({"pairs": pairs, "line": bl["line"], "lines": lines, "x": x, "y": y,
                    "e": x + float(org[0]), "n": y + float(org[1]),
                    "bbox": (float(cx.min() * pt), float(cy.min() * pt), float((cx.max() + 1) * pt),
                             float((cy.max() + 1) * pt)), "boxes": boxes,
                    "m2": len(cells) * pt * pt, "median": float(np.median(v)), "max": float(v.max()),
                    "spans": spans, "t0": first[0], "t1": first[1]})
    out.sort(key=lambda r: -r["m2"] * r["median"])
    for i, r in enumerate(out):
        r["id"] = i + 1
    return out


def area_times_text(area, names, gps=False):
    """'15:02:00.2-15:02:03.3' for the line at fault, else each line's: 'Line 3 15:02:00.2-..; Line 6 ...'."""
    sp = area.get("spans") or {}
    if not sp:
        return "-"
    f = (lambda a, b: gps_text(a, b).replace("GPS ", "").replace(" s of week", "")) if gps else \
        (lambda a, b: "%s-%s" % (_hms(a), _hms(b)))
    if area["line"] is not None or len(sp) == 1:
        a, b = next(iter(sp.values()))
        return f(a, b)
    return "; ".join("%s %s" % (names.get(str(l), "Line %d" % (l + 1)), f(a, b)) for l, (a, b) in sp.items())


def area_lines_text(area, names):
    """'Line 3' when one line is at fault, else 'Line 1 / Line 3'."""
    nm = lambda l: names.get(str(l), "Line %d" % (l + 1))      # noqa: E731
    if area["line"] is not None:
        return nm(area["line"])
    return " / ".join(nm(l) for l in area["lines"])


GPS_EPOCH = 315964800.0
LEAP_SECONDS = 18                 # GPS - UTC since 2017


def _hms(t):
    return time.strftime("%H:%M:%S", time.gmtime(t)) + (".%d" % int(10 * (t % 1)))


def time_text(t0, t1):
    """'2026-10-20 15:00:02.0-15:00:02.9 UTC' (the date once)."""
    if t0 is None:
        return "-"
    return "%s %s-%s UTC" % (time.strftime("%Y-%m-%d", time.gmtime(t0)), _hms(t0), _hms(t1))


def gps_text(t0, t1):
    """The same span in GPS seconds of the week (as in an SBET or PosPro)."""
    if t0 is None:
        return "-"
    g = lambda t: (t - GPS_EPOCH + LEAP_SECONDS) % 604800.0      # noqa: E731
    return "GPS %.1f-%.1f s of week" % (g(t0), g(t1))


# --------------------------------------------------------------------------------------------- for PosPro
FEEDBACK_SCHEMA = "dcs.problem_areas/1"
FEEDBACK_NAME = "problem_areas.json"


def gps_week_sow(t):
    g = t - GPS_EPOCH + LEAP_SECONDS
    return int(g // 604800), float(g % 604800)


def feedback(project, res, tol=DEFAULT_TOLERANCE):
    """The problem areas as PosPro reads them (dcs.problem_areas/1): where and when each line's points disagree
    with the other lines, and the trajectory the points were georeferenced with."""
    try:
        from ..guide import installed_version
        ver = installed_version()
    except Exception:                           # noqa: BLE001
        ver = ""
    inp = project.data.get("inputs", {}) or {}
    tr = inp.get("trajectory", {}) or {}
    nm = res.data.get("names", {})
    areas = []
    for a in res.problem_areas(tol):
        rec = {"id": a["id"], "line": nm.get(str(a["line"])) if a["line"] is not None else None,
               "lines": [nm.get(str(l), str(l)) for l in a["lines"]],
               "pairs": [[nm.get(str(x), str(x)) for x in pr] for pr in a["pairs"]],
               "e": round(a["e"], 2), "n": round(a["n"], 2), "area_m2": round(a["m2"], 1),
               "median_m": round(a["median"], 4), "max_m": round(a["max"], 4)}
        times = []
        for l, (t0, t1) in (a.get("spans") or {}).items():
            w0, s0 = gps_week_sow(t0)
            w1, s1 = gps_week_sow(t1)
            times.append({"line": nm.get(str(l), str(l)), "utc": [round(t0, 2), round(t1, 2)], "gps_week": w0,
                          "gps_sow": [round(s0, 2), round(s1 + (w1 - w0) * 604800, 2)]})
        rec["times"] = times                # one entry: the line at fault; else one per line of the pair
        if times:                           # the first, also at the top level
            rec.update({k: times[0][k] for k in ("utc", "gps_week", "gps_sow")})
        areas.append(rec)
    return {"schema": FEEDBACK_SCHEMA, "written_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "by": "CloudPro %s" % ver, "measured_utc": res.data.get("utc"), "tolerance_m": tol,
            "patch_m": res.patch, "crs": project.crs, "leap_seconds": LEAP_SECONDS,
            "trajectory": {"source": tr.get("source"), "handover_id": tr.get("handover_id"),
                           "description": tr.get("description")},
            "current": res.is_current(project), "summary": verdict(res, tol), "areas": areas}


def write_feedback(project, res, tol=DEFAULT_TOLERANCE):
    """03_CloudPro/out/problem_areas.json (atomic). Not a handover: it does not finalize CloudPro or make any
    stage stale; PosPro shows it in Review and the Trajectory editor. Returns the path or None."""
    if project is None or res is None:
        return None
    try:
        d = project.out_dir
        path = os.path.join(d, FEEDBACK_NAME)
        tmp = path + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(feedback(project, res, tol), f, indent=1)
        os.replace(tmp, path)
        return path
    except OSError:
        return None


def _hist_median(edges, cnt):
    if cnt.sum() == 0:
        return None
    c = np.cumsum(cnt)
    i = int(np.searchsorted(c, c[-1] / 2.0))
    lo, hi = edges[i], min(edges[i + 1], edges[i] + 0.0005)
    prev = c[i - 1] if i else 0
    return float(lo + (hi - lo) * ((c[-1] / 2.0 - prev) / max(1, cnt[i])))


def save(project, res):
    d = folder(project)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, "grids.npz.part")
    with open(tmp, "wb") as f:
        arrs = {("t" + k): v for k, v in res.grids.items()}
        arrs.update({("p%s_%d_%d" % kab): v for kab, v in res.pair_grids.items()})
        arrs.update({("q%s_%d_%d" % kab): v for kab, v in res.time_grids.items()})
        np.savez_compressed(f, **arrs)
    os.replace(tmp, os.path.join(d, "grids.npz"))
    tmp = os.path.join(d, "separation.json.part")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(res.data, f, indent=1)
    os.replace(tmp, os.path.join(d, "separation.json"))


def load(project):
    d = folder(project)
    try:
        with open(os.path.join(d, "separation.json"), encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version") != VERSION:
            return None
        with np.load(os.path.join(d, "grids.npz")) as z:
            grids, pg, tg = {}, {}, {}
            for k in z.files:
                if k[0] == "t":
                    grids[k[1:]] = z[k]
                else:
                    i, j, a, b = k[1:].split("_")
                    (pg if k[0] == "p" else tg)[("%s_%s" % (i, j), int(a), int(b))] = z[k]
        return Result(data, grids, pg, tg)
    except (OSError, ValueError, KeyError):
        return None


# --------------------------------------------------------------------------------------------- report
def _cm(v, signed=False):
    if v is None or not np.isfinite(v):
        return "-"
    v = 100 * v
    if abs(v) < 0.05:
        return "0.0"
    return ("%+.1f" if signed else "%.1f") % v


def verdict(res, tol=DEFAULT_TOLERANCE):
    """One line: what the numbers say."""
    if not res.pairs:
        return "No overlapping flat surfaces between lines were found."
    nm = res.data.get("names", {})

    def who(p):
        return "%s / %s" % (nm.get(str(p["a"]), p["a"]), nm.get(str(p["b"]), p["b"]))
    worst = max(res.pairs, key=lambda p: p["median"])
    areas = res.problem_areas(tol)
    if areas:
        at = sorted({area_lines_text(a, nm) for a in areas})
        local = " %d local problem area%s (%s)." % (len(areas), "" if len(areas) == 1 else "s", ", ".join(at[:4])
                                                    + (" ..." if len(at) > 4 else ""))
    else:
        local = ""
    if worst["median"] <= tol:
        if areas:
            return ("Lines agree within %.0f cm (largest median %.1f cm, %s), except%s"
                    % (100 * tol, 100 * worst["median"], who(worst), local))
        return "Lines agree within %.0f cm (largest median %.1f cm, %s)." % (100 * tol, 100 * worst["median"], who(worst))
    varying = [p for p in res.pairs if p["median"] > tol and p["rest"] > tol]
    if varying:
        v = max(varying, key=lambda p: p["rest"])
        return ("Largest median %.1f cm (%s); %.1f cm remains after a constant shift (%s): the error changes along "
                "the line.%s" % (100 * worst["median"], who(worst), 100 * v["rest"], who(v), local))
    return ("Largest median %.1f cm (%s); a constant shift per line would bring every pair within %.0f cm.%s"
            % (100 * worst["median"], who(worst), 100 * tol, local))


def report_lines(res, tol=DEFAULT_TOLERANCE, project_name=""):
    d = res.data
    L = ["CloudPro line separation%s" % (" - " + project_name if project_name else ""),
         "Measured %s in %.0f s; patches %.1f m; %s patches in %d of %d tiles."
         % (d.get("utc", ""), d.get("seconds", 0), res.patch, "{:,}".format(d.get("patches", 0)),
            d.get("tiles_with_overlap", 0), d.get("tiles", 0)),
         "", verdict(res, tol),
         "Overall median %s cm; %.0f %% of patches within %.0f cm." % (_cm(d.get("median")),
                                                                     100 * res.share_within(tol), 100 * tol),
         "", "Line pairs (shift = line b against line a; E/N need walls; rest = left after that shift), cm:",
         "%-24s %9s %7s %7s %7s %7s %7s %7s %7s" % ("a / b", "patches", "median", "95 %", "east", "north", "up",
                                                    "rest", "walls")]
    nm = d.get("names", {})
    for p in res.pairs:
        s = p["shift"]
        L.append("%-24s %9s %7s %7s %7s %7s %7s %7s %7d" % (
            ("%s / %s" % (nm.get(str(p["a"]), p["a"]), nm.get(str(p["b"]), p["b"])))[:24],
            "{:,}".format(p["patches"]), _cm(p["median"]), _cm(p["p95"]), _cm(s[0], True), _cm(s[1], True),
            _cm(s[2], True), _cm(p["rest"]), p["walls"]))
    areas = res.problem_areas(tol)
    L += ["", "Problem areas (local, beyond %.0f cm; the line at fault when the area shows in several pairs):"
          % (200 * tol)]
    if not areas:
        L.append("    none")
    for a in areas:
        day = time.strftime("%Y-%m-%d", time.gmtime(a["t0"])) + " " if a["t0"] else ""
        L.append("  #%-3d %-22s E %.1f N %.1f  %5.0f m2  median %s cm, max %s cm  %s%s UTC  (GPS %s s of week)" % (
            a["id"], area_lines_text(a, nm)[:22], a["e"], a["n"], a["m2"], _cm(a["median"]), _cm(a["max"]),
            day, area_times_text(a, nm), area_times_text(a, nm, gps=True)))
    L += ["", "Per line (median against the lines it overlaps, weighted by patches), cm:"]
    for l in res.lines:
        L.append("%-24s %7s  (%d pair%s)" % (l["name"][:24], _cm(l["median"]), l["pairs"], "" if l["pairs"] == 1 else "s"))
    s = d.get("settings", {})
    L += ["", "Method: per %.1f m cube and line, a plane through the kept points (noise classes left out) when it is "
          "flat (thickness <= %.0f mm, at least %d points); two lines' planes with normals within %.0f deg give the "
          "separation along the normal (|d| <= %.0f cm)." % (res.patch, 1000 * s.get("max_thickness", MAX_THICKNESS),
                                                             s.get("min_points", MIN_POINTS),
                                                             s.get("max_angle", MAX_ANGLE),
                                                             100 * s.get("max_sep", MAX_SEP))]
    return L
