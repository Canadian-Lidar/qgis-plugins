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

from . import pairmodel
from .store import REJECTED, parse_key

VERSION = 7                     # 6: recording holes (2.4.0); 7: marine settings, adaptive holes (2.6.0)
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
WINDOW_GAP_S = 5.0                # s: overlap times of a line closer than this are one window
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
    out_la, out_lb, out_n, out_d, out_col, out_ta, out_tb, out_xy = [], [], [], [], [], [], [], []
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
        out_xy.append((0.5 * (cen[i, :2] + cen[j, :2]))[ok] + np.array([x0, y0]))
    if not out_la:
        return None
    return (np.concatenate(out_la), np.concatenate(out_lb), np.concatenate(out_n), np.concatenate(out_d),
            np.concatenate(out_col), n, np.concatenate(out_ta), np.concatenate(out_tb), np.concatenate(out_xy))


CELLS = np.dtype([("c", "<i4"), ("d", "<f4"), ("ta", "<f4"), ("tb", "<f4"), ("ds", "<f4"), ("nx", "<f4"),
                  ("ny", "<f4"), ("nz", "<f4")])


def _cell_medians(col, val):
    """(occupied cells, median value per cell), cells sorted."""
    o = np.lexsort((val, col))
    cs, vs = col[o], val[o]
    starts = np.flatnonzero(np.r_[True, cs[1:] != cs[:-1]])
    ends = np.r_[starts[1:], len(cs)]
    mid = (starts + ends - 1) / 2.0
    lo, hi = np.floor(mid).astype(int), np.ceil(mid).astype(int)
    return cs[starts], (0.5 * (vs[lo] + vs[hi])).astype(np.float32)


def _pair_cells(col, d, nn, ta, tb):
    """One pair in one tile, sparse (CELLS): cell, median |d|, median time of each line's points, median signed d
    and the mean normal (to take the pair's steady relation off, 2.2.1)."""
    c, ad = _cell_medians(col, np.abs(d))
    out = np.empty(len(c), CELLS)
    out["c"], out["d"] = c, ad
    out["ta"] = _cell_medians(col, ta)[1]
    out["tb"] = _cell_medians(col, tb)[1]
    out["ds"] = _cell_medians(col, d)[1]
    idx = np.searchsorted(c, col)
    cnt = np.bincount(idx, minlength=len(c)).astype(float)
    nm = np.stack([np.bincount(idx, weights=nn[:, k], minlength=len(c)) / cnt for k in range(3)], 1)
    nm /= np.maximum(np.linalg.norm(nm, axis=1), 1e-9)[:, None]
    out["nx"], out["ny"], out["nz"] = nm[:, 0], nm[:, 1], nm[:, 2]
    return out


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
    n = np.asarray(n, float).copy()
    d = np.asarray(d, float)
    n[np.abs(n[:, 2]) >= pairmodel.STEEP, :2] = 0.0      # E / N from walls only (2.2.2)
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


def motions_of(project):
    """Line id -> pairmodel.Motion from CloudPro's sensor tracks (missing tracks are left out)."""
    out = {}
    try:
        for lid, tr in project.tracks().items():
            if tr is not None and len(tr.get("t", ())) > 2:
                out[int(lid)] = pairmodel.Motion(tr)
    except Exception:                           # noqa: BLE001
        pass
    return out


def _runs(t, gap=WINDOW_GAP_S):
    t = np.sort(np.asarray(t, float))
    t = t[np.isfinite(t)]
    if not len(t):
        return []
    br = np.flatnonzero(np.diff(t) > gap)
    starts = np.r_[0, br + 1]
    ends = np.r_[br, len(t) - 1]
    return [[round(float(t[i]), 2), round(float(t[j]), 2)] for i, j in zip(starts, ends)]


def _windows(ta, tb):
    """When each line of a pair was over the shared surfaces: {"a": [[t0, t1], ...], "b": [...]} (UTC s)."""
    return {"a": _runs(ta), "b": _runs(tb)}


class Result:
    def __init__(self, data, grids, pair_cells=None):
        self.data = data                  # json-able dict
        self.grids = grids                # tile key -> n x n float32 median |d| (m), every pair
        self.pair_cells = pair_cells or {}    # (tile key, a, b) -> CELLS: only the occupied cells of one pair
        #                                       (median |d|, times, signed median d, mean normal)
        self.motions = {}                 # line id -> pairmodel.Motion (to take each pair's steady relation off)
        self._areas = {}
        self._resid = {}
        self._by_tile = {}
        for (k, a, b) in self.pair_cells:
            self._by_tile.setdefault(k, []).append((a, b))
        self._pair = {(p["a"], p["b"]): p for p in data.get("pairs", [])}
        for p in data.get("pairs", []):                 # pair_findings needs the trajectory's holes
            p["recording"] = data.get("recording")
        tb = float(data.get("t_base", 0.0))
        self.hole_spans = np.asarray(data.get("holes") or [], float).reshape(-1, 2) + tb   # UTC s

    @property
    def n(self):
        return int(round(float(self.data.get("tile", 50.0)) / self.patch))

    def _dense(self, cells, values=None):
        g = np.full(self.n * self.n, np.nan, np.float32)
        g[cells["c"]] = cells["d"] if values is None else values
        return g.reshape(self.n, self.n)

    @property
    def patch(self):
        return float(self.data.get("patch", PATCH))

    @property
    def pairs(self):
        return self.data.get("pairs", [])

    @property
    def lines(self):
        return self.data.get("lines", [])

    def residual(self, key, a, b):
        """|separation| per occupied cell of one pair in one tile once the pair's steady relation (the vehicle-frame
        model, else the world shift) is taken off."""
        k = (key, a, b)
        if k not in self._resid:
            cells = self.pair_cells[k]
            rec = self._pair.get((a, b), {})
            i, j = parse_key(key)
            T, pt, n = float(self.data.get("tile", 50.0)), self.patch, self.n
            xy = np.stack([i * T + (cells["c"] % n + 0.5) * pt, j * T + (cells["c"] // n + 0.5) * pt], 1)
            nrm = np.stack([cells["nx"], cells["ny"], cells["nz"]], 1).astype(float)
            tb0 = float(self.data.get("t_base", 0.0))
            pred = pairmodel.predict(rec.get("model"), nrm, xy, cells["ta"].astype(float) + tb0,
                                     cells["tb"].astype(float) + tb0, self.motions.get(a), self.motions.get(b),
                                     rec.get("shift"))
            self._resid[k] = np.abs(cells["ds"] - pred).astype(np.float32)
        return self._resid[k]

    def grid(self, key, pair=None, line=None, steady_off=False):
        """Median |d| per cell of a tile: every pair, one pair (a, b), or the pairs with one line (the median of
        those pairs' cells); steady_off: after each pair's steady relation is taken off."""
        def one(a, b):
            c = self.pair_cells[(key, a, b)]
            return self._dense(c, self.residual(key, a, b) if steady_off else None)
        if pair is not None:
            if (key, int(pair[0]), int(pair[1])) not in self.pair_cells:
                return None
            return one(int(pair[0]), int(pair[1]))
        if line is not None or steady_off:
            gs = [one(a, b) for a, b in self._by_tile.get(key, ()) if line is None or line in (a, b)]
            if not gs:
                return None
            if len(gs) == 1:
                return gs[0]
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


def recording_holes(project):
    """Where PosPro's trajectory has no records (the recording lost INS data): {"missing_pct", "count",
    "longest_s", "spans": [[t0, t1] UTC s, ...]}, or None (no DCS project / no PosPro trajectory). From
    trajectory.json's recording_holes (PosPro 2.3.0), else from the SBET itself."""
    dcs = getattr(project, "dcs", None)
    if dcs is None:
        return None
    try:
        from dcs_core import handover
        from dcs_core import sbet as dsbet
        src = ((project.data.get("inputs") or {}).get("trajectory") or {}).get("source")
        doc = handover.read(dcs, "strippro" if src == "strippro" else "pospro", required=False)
    except Exception:       # noqa: BLE001
        return None
    if not doc:
        return None
    tm = doc.get("time") or {}
    week = int(tm.get("week") or 0)
    leap = float(tm.get("leap_seconds", 18))
    rh = doc.get("recording_holes")
    if rh and "spans" in rh and week:
        ws = dsbet.week_start(week)
        out = dict(rh)
        out["spans"] = [[ws + a - leap, ws + b - leap] for a, b in rh["spans"]]
        return out
    try:
        t, _a, _ws = dsbet.read(doc["paths"]["sbet"], leap, week=week or None)
    except (OSError, ValueError, KeyError):
        return None
    return dsbet.holes_summary(t)


# a marine job (sonar soundings): a seabed is rougher and coarser than the surfaces a Lidar sees (2.6.0)
MARINE = {"PATCH": 2.0, "MIN_POINTS": 20, "MAX_THICKNESS": 0.15, "MAX_ANGLE": 15.0, "MAX_SEP": 2.0, "FACE_GAP": 0.10}
MARINE_TOLERANCE = 0.10


def is_marine(project):
    return bool((project.data.get("inputs") or {}).get("marine"))


def run(project, progress=None, cancelled=None, patch=None, workers=None):
    """Line separation; a marine job uses the MARINE settings (put back afterwards)."""
    g = globals()
    keep = {k: g[k] for k in MARINE}
    if is_marine(project):
        g.update(MARINE)
    try:
        return _run(project, progress, cancelled, patch if patch is not None else g["PATCH"], workers)
    finally:
        g.update(keep)


def _run(project, progress=None, cancelled=None, patch=PATCH, workers=None):
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
    pair_cells = {}
    pair_rows = {}                          # (a, b) -> [normals, d]: a sample of at most 2 x PAIR_SAMPLE
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

    if not workers:
        # threads while their memory fits in half the free memory (~150 B per point read, TILE_POINTS a tile)
        workers = max(1, min(8, (os.cpu_count() or 2) - 1))
        try:
            from .perflog import _ram_gb
            free = _ram_gb()[1]
            if free:
                workers = max(1, min(workers, int(free * 2 ** 30 / 2 / (150 * TILE_POINTS))))
        except Exception:                       # noqa: BLE001
            pass
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for k, res in ex.map(one, keys):
            if cancelled and cancelled():
                raise Cancelled()
            done[0] += 1
            if progress and (done[0] % 4 == 0 or done[0] == len(keys)):
                progress(done[0] / max(1, len(keys)), "Line separation: tile %d of %d" % (done[0], len(keys)))
            if res is None:
                continue
            la, lb, nn, d, col, n, ta, tb, pxy = res
            ad = np.abs(d)
            grids[k] = _column_median(col, ad, n)
            hist += np.histogram(ad, hist_edges)[0]
            pk = la * 65536 + lb
            for p in np.unique(pk):
                sel = np.flatnonzero(pk == p)
                ab = (int(p // 65536), int(p % 65536))
                pair_cells[(k,) + ab] = _pair_cells(col[sel], d[sel], nn[sel], ta[sel], tb[sel])
                pair_count[ab] = pair_count.get(ab, 0) + len(sel)
                # an even sample over the whole project, bounded in memory: each tile adds its share, and the
                # pair's sample is thinned back to PAIR_SAMPLE whenever it passes twice that
                if len(sel) > PAIR_SAMPLE // 4:
                    sel = rng.choice(sel, PAIR_SAMPLE // 4, replace=False)
                cur = pair_rows.get(ab)
                new = {"n": nn[sel].astype(np.float32), "d": d[sel].astype(np.float32), "xy": pxy[sel],
                       "ta": ta[sel].astype(np.float64), "tb": tb[sel].astype(np.float64)}
                if cur is None:
                    pair_rows[ab] = new
                else:
                    for kk in cur:
                        cur[kk] = np.concatenate([cur[kk], new[kk]])
                    if len(cur["d"]) > 2 * PAIR_SAMPLE:
                        keep = rng.choice(len(cur["d"]), PAIR_SAMPLE, replace=False)
                        for kk in cur:
                            cur[kk] = cur[kk][keep]
    names = {int(l["id"]): l.get("name", "Line %s" % l["id"]) for l in project.lines}
    motions = motions_of(project)
    holes = _holes_in_lines(recording_holes(project), project)
    hs = np.asarray(holes["spans"], float).reshape(-1, 2) if holes else np.zeros((0, 2))
    pairs = []
    per_line = {}
    for (a, b), parts in sorted(pair_rows.items()):
        if len(parts["d"]) > PAIR_SAMPLE:
            s = rng.choice(len(parts["d"]), PAIR_SAMPLE, replace=False)
            parts = {kk: v[s] for kk, v in parts.items()}
        nn = parts["n"].astype(float)
        d = parts["d"].astype(float)
        ad = np.abs(d)
        t, hor, rest = solve_shift(nn, d)
        model = pairmodel.fit(nn, d, parts["xy"], parts["ta"] + tbase, parts["tb"] + tbase,
                              motions.get(a), motions.get(b))
        up = np.abs(nn[:, 2]) > 0.9
        walls = np.abs(nn[:, 2]) < 0.3
        rec = {"a": a, "b": b, "patches": int(pair_count[(a, b)]), "median": float(np.median(ad)),
               "p95": float(np.percentile(ad, 95)), "flat": int(up.sum()), "walls": int(walls.sum()),
               "shift": [None if not np.isfinite(v) else float(v) for v in t], "horizontal": hor,
               "rest": model["rest"] if model else rest, "rest_world": rest, "model": model,
               "windows": _windows(parts["ta"] + tbase, parts["tb"] + tbase),
               "in_holes": _share_in_holes(parts["ta"] + tbase, parts["tb"] + tbase, hs),
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
            "recording": {k: v for k, v in holes.items() if k != "spans"} if holes else None,
            "holes": [[round(a - tbase, 3), round(b - tbase, 3)] for a, b in hs] if len(hs) else [],
            "hist": {"edges": hist_edges.tolist(), "counts": hist.astype(int).tolist()},
            "settings": {"min_points": MIN_POINTS, "max_thickness": MAX_THICKNESS, "max_angle": MAX_ANGLE,
                         "max_sep": MAX_SEP}}
    res = Result(data, grids, pair_cells)
    res.motions = motions
    save(project, res)
    res.stamp = stamp(project)
    return res


def _holes_in_lines(rh, project):
    """The recording holes while the lines were recorded (from their sensor tracks): the summary counts only those,
    as a share of the lines' time."""
    if not rh or not rh.get("spans"):
        return rh
    spans = np.asarray(rh["spans"], float).reshape(-1, 2)
    ranges = []
    for tr in project.tracks().values():
        t = np.asarray(tr.get("t", []), float)
        if len(t) > 1:
            ranges.append((float(t[0]), float(t[-1])))
    if not ranges:
        return rh
    keep, missing, total = [], 0.0, sum(b - a for a, b in ranges)
    for a, b in spans:
        cut = sum(max(0.0, min(b, r1) - max(a, r0)) for r0, r1 in ranges)
        if cut > 0:
            keep.append([float(a), float(b)])
            missing += cut
    dur = [min(b, max(r1 for _r0, r1 in ranges)) - a for a, b in keep]
    return {"missing_pct": round(100.0 * missing / total, 2) if total > 0 else 0.0, "count": len(keep),
            "longest_s": round(max(dur), 3) if dur else 0.0, "spans": keep}


def _share_in_holes(ta, tb, hs):
    """Share of a pair's patches measured while either line's trajectory had no record (a recording hole)."""
    if not len(hs) or not len(ta):
        return 0.0
    from dcs_core import sbet as dsbet
    return float(np.mean(dsbet.in_holes(ta, hs) | dsbet.in_holes(tb, hs)))


# --------------------------------------------------------------------------------------------- problem areas
AREA_MIN_M2 = 10.0                # smallest problem area (m2 of cells beyond twice the tolerance)
MERGE_GAP_S = 3.0                 # s: problem areas of the same line(s) this close in time are one
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
    """Local areas where two lines disagree by more than twice the tolerance once the pair's steady relation is
    taken off (2.2.1: a steady offset is reported per pair, as calibration or trajectory): per pair, 8-connected clusters of
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
    for (k, a, b), cells in res.pair_cells.items():
        i, j = parse_key(k)
        rec = per_pair.setdefault((a, b), {"cells": 0, "gx": [], "gy": [], "v": [], "ta": [], "tb": []})
        rec["cells"] += len(cells)
        r = res.residual(k, a, b)
        sel = r > lim
        if not sel.any():
            continue
        bad = cells[sel]
        iy, ix = bad["c"] // n, bad["c"] % n
        rec["gx"].append(i * n + ix)
        rec["gy"].append(j * n + iy)
        rec["v"].append(r[sel])
        rec["ta"].append(bad["ta"])
        rec["tb"].append(bad["tb"])
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
    # one area per stretch: blobs of the same line at fault (or of the same pair when the line is not known) whose
    # times are within MERGE_GAP_S on every line (a footprint crosses several places in the same seconds, and a
    # long stretch breaks into pieces where a few cells stay under the limit)
    def group(r):
        return ("line", r["line"]) if r["line"] is not None else ("pair", tuple(sorted(r["pairs"])))

    def near(m, bl):
        if not bl["spans"] or set(bl["spans"]) != set(m["spans"]):
            return False
        for l, (b0, b1) in bl["spans"].items():
            a0, a1 = m["spans"][l]
            if max(0.0, b0 - a1, a0 - b1) > MERGE_GAP_S:
                return False
        return True
    blobs.sort(key=lambda r: (str(group(r)), min((s[0] for s in r["spans"].values()), default=0)))
    merged = []
    for bl in blobs:
        bl["boxes"] = [bl["cells"]]
        for m in merged:
            if group(m) == group(bl) and near(m, bl):
                m["pairs"] |= bl["pairs"]
                m["boxes"].append(bl["cells"])
                m["cells"] = m["cells"] | bl["cells"]
                m["v"] += bl["v"]
                for l, (b0, b1) in bl["spans"].items():
                    a0, a1 = m["spans"][l]
                    m["spans"][l] = (min(a0, b0), max(a1, b1))
                break
        else:
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
                    "spans": spans, "t0": first[0], "t1": first[1],
                    "hole_s": _area_hole(spans, getattr(res, "hole_spans", None))})
    out.sort(key=lambda r: -r["m2"] * r["median"])
    for i, r in enumerate(out):
        r["id"] = i + 1
    return out


def _area_hole(spans, hs):
    """Longest recording hole (s) inside any of an area's line time spans (widened 0.5 s), 0 when none."""
    if hs is None or not len(hs):
        return 0.0
    from dcs_core import sbet as dsbet
    return round(max((dsbet.longest_hole_in(a - 0.5, b + 0.5, hs) for a, b in spans.values()), default=0.0), 2)


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


def _leap(t):
    """GPS - UTC (s) at Unix time t, from DCS Core's table (the single source)."""
    try:
        import datetime
        from dcs_core import timeutil
        return int(timeutil.leap_seconds(datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=float(t))))
    except Exception:                           # noqa: BLE001
        return 18


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
    g = lambda t: (t - GPS_EPOCH + _leap(t)) % 604800.0      # noqa: E731
    return "GPS %.1f-%.1f s of week" % (g(t0), g(t1))


# --------------------------------------------------------------------------------------------- for PosPro
FEEDBACK_SCHEMA = "dcs.problem_areas/1"
FEEDBACK_NAME = "problem_areas.json"


def gps_week_sow(t):
    g = t - GPS_EPOCH + _leap(t)
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
               "median_m": round(a["median"], 4), "max_m": round(a["max"], 4),
               "recording_hole_s": a.get("hole_s", 0.0)}
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
            "patch_m": res.patch, "crs": project.crs, "leap_seconds": _leap(time.time()),
            # PosPro judges "current" by its own trajectory's id: with StripPro's trajectory, the PosPro one it
            # was made from (StripPro's id beside it)
            "trajectory": {"source": tr.get("source"),
                           "handover_id": tr.get("pospro_id") if tr.get("source") == "strippro" else tr.get("handover_id"),
                           "strippro_id": tr.get("handover_id") if tr.get("source") == "strippro" else None,
                           "description": tr.get("description")},
            "current": res.is_current(project), "summary": verdict(res, tol),
            "recording_holes": res.data.get("recording"), "areas": areas}


CALIBRATION_SCHEMA = "dcs.calibration_check/1"
CALIBRATION_NAME = "calibration_check.json"


def calibration_check(project, res, tol=DEFAULT_TOLERANCE):
    """What the line pairs say about the calibration, for AlignPro (dcs.calibration_check/1): per pair the steady
    relation in the vehicle frame, the findings, the lines' recordings and when each line was over the shared
    surfaces (AlignPro uses those times as patch-test material), and the calibration the points were made with."""
    try:
        from ..guide import installed_version
        ver = installed_version()
    except Exception:                           # noqa: BLE001
        ver = ""
    inp = project.data.get("inputs", {}) or {}
    al = inp.get("alignment", {}) or {}
    tr = inp.get("trajectory", {}) or {}
    nm = res.data.get("names", {})
    rec_of = {int(l["id"]): l for l in project.lines}
    pairs = []
    for p in res.pairs:
        m = p.get("model")
        fi = pair_findings(p, tol)
        la, lb = rec_of.get(p["a"], {}), rec_of.get(p["b"], {})

        def tw(ws):
            out = []
            for t0, t1 in ws:
                w, s0 = gps_week_sow(t0)
                out.append({"utc": [t0, t1], "gps_week": w, "gps_sow": [round(s0, 2), round(s0 + (t1 - t0), 2)]})
            return out
        win = p.get("windows") or {}
        pairs.append({
            "lines": [nm.get(str(p["a"]), str(p["a"])), nm.get(str(p["b"]), str(p["b"]))],
            "line_numbers": [la.get("line"), lb.get("line")],
            "files": [[os.path.basename(f) for f in la.get("files", [])], [os.path.basename(f) for f in lb.get("files", [])]],
            "patches": p["patches"], "walls": p["walls"], "median_m": round(p["median"], 4),
            "relation": m["relation"] if m else None,
            "steady_m": {k: (round(m[k], 4) if m and m.get(k) is not None else None) for k in ("along", "cross", "up")},
            "roll_m_per_m": (round(m["roll"], 6) if m and m.get("roll") is not None else None),
            "rest_m": round(p["rest"], 4), "shift_enu_m": p["shift"],
            "findings": [{"kind": k, "text": t} for k, t in fi],
            "calibration": any(k == "calibration" for k, _ in fi),
            "recording_holes_share": round(float(p.get("in_holes") or 0.0), 3),
            "windows": [tw(win.get("a", [])), tw(win.get("b", []))]})
    return {"schema": CALIBRATION_SCHEMA, "written_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "by": "CloudPro %s" % ver, "measured_utc": res.data.get("utc"), "tolerance_m": tol,
            "current": res.is_current(project), "summary": verdict(res, tol),
            "recording_holes": res.data.get("recording"),
            "alignment": {"source": al.get("source"), "handover_id": al.get("handover_id"),
                          "calibration": al.get("calibration")},
            "trajectory": {"source": tr.get("source"), "handover_id": tr.get("handover_id")},
            "pairs": pairs}


def _atomic_json(path, obj):
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def write_feedback(project, res, tol=DEFAULT_TOLERANCE):
    """03_CloudPro/out/problem_areas.json (for PosPro) and calibration_check.json (for AlignPro), atomic. Not
    handovers: they finalize nothing and make no stage stale. Returns the problem-areas path or None."""
    if project is None or res is None:
        return None
    try:
        d = project.out_dir
        path = os.path.join(d, FEEDBACK_NAME)
        _atomic_json(path, feedback(project, res, tol))
        _atomic_json(os.path.join(d, CALIBRATION_NAME), calibration_check(project, res, tol))
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
        arrs.update({("p%s_%d_%d" % kab): v for kab, v in res.pair_cells.items()})
        np.savez_compressed(f, **arrs)
    os.replace(tmp, os.path.join(d, "grids.npz"))
    tmp = os.path.join(d, "separation.json.part")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(res.data, f, indent=1)
    os.replace(tmp, os.path.join(d, "separation.json"))


def stamp(project):
    """(size, mtime) of the saved result, None when there is none."""
    try:
        st = os.stat(os.path.join(folder(project), "grids.npz"))
        st2 = os.stat(os.path.join(folder(project), "separation.json"))
        return (st.st_size, st.st_mtime_ns, st2.st_mtime_ns)
    except OSError:
        return None


def load(project):
    d = folder(project)
    try:
        with open(os.path.join(d, "separation.json"), encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version") != VERSION:
            return None
        with np.load(os.path.join(d, "grids.npz")) as z:
            grids, pc = {}, {}
            for k in z.files:
                if k[0] == "t":
                    grids[k[1:]] = z[k]
                elif k[0] == "p":
                    i, j, a, b = k[1:].split("_")
                    pc[("%s_%s" % (i, j), int(a), int(b))] = z[k]
        res = Result(data, grids, pc)
        res.motions = motions_of(project)
        res.stamp = stamp(project)
        return res
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


HOLES_PAIR_SHARE = 0.05         # a pair with this share of its patches measured in recording holes


def pair_findings(rec, tol=DEFAULT_TOLERANCE):
    """[(kind, sentence)] for one pair (from its steady relation). When the trajectory has recording holes that
    touch the pair (CloudPro 2.4.0), the first finding is kind "recording" and the calibration / trajectory
    findings become notes: offsets measured across holes follow the holes."""
    f = pairmodel.findings(rec.get("model"), tol)
    rh = rec.get("recording")
    share = float(rec.get("in_holes") or 0.0)
    if not rh:
        return f
    from dcs_core import sbet as dsbet
    if not (dsbet.holes_matter(rh) and (share >= HOLES_PAIR_SHARE or "in_holes" not in rec)):
        return f
    head = ("recording", "The recording lost INS data: the trajectory has no data for %.0f %% of its time (%d holes, "
                         "longest %.1f s)%s. Offsets measured here follow the holes, so they are not calibration or "
                         "trajectory findings." % (
                             rh["missing_pct"], rh["count"], rh["longest_s"],
                             "; %.0f %% of this pair's patches were measured in a hole" % (100 * share)
                             if "in_holes" in rec else ""))
    return [head] + [("note" if k in ("calibration", "trajectory", "either") else k, t) for k, t in f]


def calibration_pairs(res, tol=DEFAULT_TOLERANCE):
    """Pairs whose steady relation points at the calibration (AlignPro)."""
    return [p for p in res.pairs if any(k == "calibration" for k, _ in pair_findings(p, tol))]


def verdict(res, tol=DEFAULT_TOLERANCE):
    """One or two sentences: what the numbers say."""
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
        local = " No local problem areas."
    rec = res.data.get("recording")
    from dcs_core import sbet as dsbet
    if rec and dsbet.holes_matter(rec):
        local += (" Recording holes: the trajectory has no data for %.0f %% of the lines' time (%d holes, longest "
                  "%.1f s); offsets follow the holes, so there is no calibration or trajectory finding."
                  % (rec["missing_pct"], rec["count"], rec["longest_s"]))
    cal = calibration_pairs(res, tol)
    if cal:
        c = cal[0]
        first = next(t for k, t in pair_findings(c, tol) if k == "calibration")
        calib = " Calibration (AlignPro), %s: %s" % (who(c), first[0].lower() + first[1:])
    else:
        calib = ""
    if worst["median"] <= tol:
        return ("Lines agree within %.0f cm (largest median %.1f cm, %s).%s%s"
                % (100 * tol, 100 * worst["median"], who(worst), calib, local))
    return ("Largest median %.1f cm (%s).%s%s" % (100 * worst["median"], who(worst), calib, local))


def report_lines(res, tol=DEFAULT_TOLERANCE, project_name=""):
    d = res.data
    L = ["CloudPro line separation%s" % (" - " + project_name if project_name else ""),
         "Measured %s in %.0f s; patches %.1f m; %s patches in %d of %d tiles."
         % (d.get("utc", ""), d.get("seconds", 0), res.patch, "{:,}".format(d.get("patches", 0)),
            d.get("tiles_with_overlap", 0), d.get("tiles", 0)),
         "", verdict(res, tol),
         ("Overall median %s cm; %.0f %% of patches within %.0f cm." % (_cm(d.get("median")),
                                                                      100 * res.share_within(tol), 100 * tol)
          if d.get("patches") else "No patches: no flat surfaces seen by two lines."),
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
    for p in res.pairs:
        m = p.get("model")
        if not m:
            continue
        L += ["", "%s / %s - driven %s; in %s's frame (cm): along %s, sideways %s, up %s; roll %s; rest %s" % (
            nm.get(str(p["a"]), p["a"]), nm.get(str(p["b"]), p["b"]),
            {"opposite": "in opposite directions", "same": "the same way", "crossing": "crossing"}[m["relation"]],
            nm.get(str(p["a"]), p["a"]), _cm(m.get("along"), True), _cm(m.get("cross"), True), _cm(m.get("up"), True),
            ("%.1f cm per 10 m" % (100 * 10 * m["roll"])) if m.get("roll") is not None else "not seen",
            _cm(m.get("rest")))]
        for kind, text in pair_findings(p, tol):
            L.append("  - %s: %s" % ({"calibration": "Calibration (AlignPro)", "trajectory": "Trajectory (PosPro)",
                                      "either": "Calibration or trajectory", "note": "Note", "recording": "Recording holes"}[kind], text))
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
        if a.get("hole_s"):
            L.append("        in a %.1f s recording hole" % a["hole_s"])
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
