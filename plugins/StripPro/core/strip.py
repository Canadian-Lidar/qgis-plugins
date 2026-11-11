"""Strip adjustment with a time-varying trajectory correction (StripPro 1.0.0).

The trajectory is corrected, not the points: per line, a correction delta(t) = (d, theta) - a shift d of the
INS reference point and a small rotation theta of the vehicle about it, both in the job's ENU frame - is linear
between knots KNOT_S apart. A point fired at t moves from p to

    p' = p + d(t) + theta(t) x (p - s(t))            s(t) = INS reference point at t

The knots are solved from planar patches: in each 1 m voxel the points of one line within one BIN_S time bin get
a plane; two planes of the same voxel and nearly the same normal must coincide along that normal. Pairs come
from the same line at different times (the Lidar sees a wall for many seconds: this carries the motion through a
hole in the INS data) and from different lines (strip alignment). Priors keep the correction near zero where the
trajectory had data (sigma PRIOR_GOOD) and let it move inside recording holes (PRIOR_HOLE); a smoothness prior
on the second difference keeps it physical. Gauss-Newton with re-fitted planes and Huber weights.
"""
import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla

KNOT_S = 0.2                 # s between correction knots inside recording holes
KNOT_OUT_S = 1.0             # s between knots where the trajectory has data
BIN_S = 0.5                  # s: a line's points of one voxel within one bin make one plane
VOXEL_M = 1.0
MIN_POINTS = 8
MAX_THICKNESS = 0.03         # m: RMS distance from the plane
MAX_ANGLE_DEG = 10.0
MIN_DT_SAME_LINE = 0.75      # s: planes of the same line closer in time carry no information
MAX_DT_SAME_LINE = 20.0      # s (a tilted scanner sees a wall ahead and again behind)
MAX_OBS = 1_500_000          # observations per iteration
INTER_PER_LINE = 4           # groups per line and voxel used for pairs between lines
OBS_PER_GROUP = 4            # points of a group observed against a reference plane
SIGMA_OBS = 0.01             # m
HUBER_K = 2.0
PRIOR_GOOD = (0.01, np.radians(0.005))    # m, rad: where the trajectory has data
PRIOR_HOLE = (1.0, np.radians(3.0))       # inside a recording hole (and HOLE_PAD around it)
HOLE_PAD_S = 0.0
SMOOTH_GOOD = (0.01, np.radians(0.01))    # second difference per knot
SMOOTH_HOLE = (0.04, np.radians(0.25))
ITERATIONS = 6


class Correction:
    """Per line: knot times tau (k,) and values X (k, 6) = (dE, dN, dU, thE, thN, thU)."""

    def __init__(self, tau, X=None):
        self.tau = np.asarray(tau, float)
        self.X = np.zeros((len(self.tau), 6)) if X is None else np.asarray(X, float)

    def basis(self, t):
        """(i, w): t lies between knots i and i + 1 with weight w on i + 1 (clamped at the ends)."""
        t = np.asarray(t, float)
        tau = self.tau
        if len(tau) == 1:
            return np.zeros(len(t), int), np.zeros(len(t))
        i = np.clip(np.searchsorted(tau, t, side="right") - 1, 0, len(tau) - 2)
        w = np.clip((t - tau[i]) / (tau[i + 1] - tau[i]), 0.0, 1.0)
        return i, w

    def eval(self, t):
        i, w = self.basis(t)
        if len(self.tau) == 1:
            v = np.repeat(self.X[:1], len(i), 0)
        else:
            v = self.X[i] * (1 - w)[:, None] + self.X[i + 1] * w[:, None]
        return v[:, :3], v[:, 3:]

    def to_json(self):
        return {"tau": self.tau.tolist(), "X": np.round(self.X, 7).tolist()}

    @classmethod
    def from_json(cls, d):
        return cls(d["tau"], d["X"])


def apply(p, s, d, th):
    """p' = s + d + Rot(theta) (p - s): the point moves with its INS reference point and turns about it."""
    from .frames import rodrigues
    R = rodrigues(th)
    return s + d + np.einsum("nij,nj->ni", R, p - s)


def knots_for(t0, t1, step=KNOT_S, holes=None, outside=KNOT_OUT_S):
    """Knot times: every `outside` s where the trajectory has data, every `step` s inside recording holes, and
    one knot on each hole edge (the correction is pinned there, so it never leaks into good data)."""
    if holes is None or not len(holes):
        n = max(2, int(np.ceil((t1 - t0) / step)) + 1)
        return t0 + np.arange(n) * step
    k = [np.arange(t0, t1 + outside, outside)]
    for a, b in np.asarray(holes, float).reshape(-1, 2):
        if b < t0 or a > t1:
            continue
        n = max(1, int(np.ceil((b - a) / step)))
        k.append(np.linspace(a, b, n + 1))
    tau = np.unique(np.round(np.concatenate(k), 4))
    # drop regular knots crowding a hole edge
    edge = np.concatenate([np.asarray(holes, float).ravel()])
    near = np.array([np.min(np.abs(edge - x)) < 0.5 * step and not np.isclose(np.min(np.abs(edge - x)), 0, atol=1e-3)
                     and not _in_holes_pad(np.array([x]), holes, 0.0)[0] for x in tau])
    return tau[~near]


# ----------------------------------------------------------------------------------------------- planes
def planes(line, t, p, s, rng=None):
    """Groups of points: one per (voxel, line, time bin) with at least MIN_POINTS. Each has its centroid, mean
    time, INS point and, when its points lie on a plane, the plane ("flat"). Also OBS_PER_GROUP sample points per
    group (indices into the inputs) for point-to-plane observations at the points' own times.
    -> dict of arrays: vox, line, bin, tb, c, n, s, npts, thick, flat, sample (m, OBS_PER_GROUP; -1 = none)."""
    vx = np.floor(p / VOXEL_M).astype(np.int64)
    vkey = (vx[:, 0] + 2 ** 20) * 2 ** 42 + (vx[:, 1] + 2 ** 20) * 2 ** 21 + (vx[:, 2] + 2 ** 20)
    tbin = np.floor(t / BIN_S).astype(np.int64)
    ln = line.astype(np.int64)
    o = np.lexsort((tbin, ln, vkey))               # one sort, no (n, 3) unique: fast and light on memory
    vs, ls_, bs = vkey[o], ln[o], tbin[o]
    new = np.r_[True, (vs[1:] != vs[:-1]) | (ls_[1:] != ls_[:-1]) | (bs[1:] != bs[:-1])]
    gid = np.cumsum(new) - 1
    first = np.flatnonzero(new)
    uk = np.stack([vs[first], ls_[first], bs[first]], 1)
    cnt = np.diff(np.r_[first, len(o)])
    inv = np.empty(len(o), np.int64)
    inv[o] = gid
    del vs, ls_, bs, new, gid
    ok = cnt >= MIN_POINTS
    sel = np.flatnonzero(ok[inv])
    inv2 = np.full(len(uk), -1)
    inv2[ok] = np.arange(ok.sum())
    g = inv2[inv[sel]]
    m = int(ok.sum())
    if m == 0:
        return None
    P = p[sel]
    n = np.bincount(g, minlength=m).astype(float)
    c = np.stack([np.bincount(g, P[:, k], m) for k in range(3)], 1) / n[:, None]
    tb = np.bincount(g, t[sel], m) / n
    sm = np.stack([np.bincount(g, s[sel, k], m) for k in range(3)], 1) / n[:, None]
    Q = P - c[g]
    C = np.zeros((m, 3, 3))
    for a in range(3):
        for b in range(a, 3):
            v = np.bincount(g, Q[:, a] * Q[:, b], m) / n
            C[:, a, b] = v
            C[:, b, a] = v
    w, V = np.linalg.eigh(C)
    thick = np.sqrt(np.maximum(w[:, 0], 0))
    flat = (thick <= MAX_THICKNESS) & (w[:, 1] > 9 * np.maximum(w[:, 0], 1e-8)) & (w[:, 1] > 0.01)
    # sample points per group: a random order, then the first OBS_PER_GROUP of each group
    rng = rng or np.random.default_rng(0)
    r = rng.random(len(sel))
    o = np.lexsort((r, g))
    gs = g[o]
    first = np.r_[0, np.flatnonzero(gs[1:] != gs[:-1]) + 1]
    rank = np.arange(len(gs)) - np.repeat(first, np.diff(np.r_[first, len(gs)]))
    keep = rank < OBS_PER_GROUP
    sample = np.full((m, OBS_PER_GROUP), -1)
    sample[gs[keep], rank[keep]] = sel[o[keep]]
    return {"vox": uk[ok][:, 0], "line": uk[ok][:, 1].astype(int), "bin": uk[ok][:, 2], "tb": tb, "c": c,
            "n": V[:, :, 0], "s": sm, "npts": n, "thick": thick, "flat": flat, "sample": sample}


def plane_pairs(pl, max_angle=MAX_ANGLE_DEG):
    """(observed group, reference plane) pairs of the same voxel: the reference is flat; the same line MIN_DT..
    MAX_DT apart, or another line (up to INTER_PER_LINE groups per line and voxel). When both are flat their
    normals must agree within max_angle, and both directions are used."""
    o = np.lexsort((pl["tb"], pl["line"], pl["vox"]))
    vox, line, tb = pl["vox"][o], pl["line"][o], pl["tb"][o]
    pairs = []
    for k in range(1, 1 + int(MAX_DT_SAME_LINE / BIN_S)):
        a, b = np.arange(len(o) - k), np.arange(k, len(o))
        m = (vox[a] == vox[b]) & (line[a] == line[b])
        dt = tb[b] - tb[a]
        m &= (dt >= MIN_DT_SAME_LINE) & (dt <= MAX_DT_SAME_LINE)
        pairs.append(np.stack([o[a[m]], o[b[m]]], 1))
    starts = np.flatnonzero(np.r_[True, (vox[1:] != vox[:-1]) | (line[1:] != line[:-1])])
    ends = np.r_[starts[1:], len(o)]
    pick = []
    for s0, e0 in zip(starts, ends):
        idx = np.arange(s0, e0)
        if len(idx) > INTER_PER_LINE:
            idx = idx[np.linspace(0, len(idx) - 1, INTER_PER_LINE).round().astype(int)]
        pick.append(idx)
    pick = np.concatenate(pick) if pick else np.zeros(0, int)
    pv, pli = vox[pick], line[pick]
    vs = np.flatnonzero(np.r_[True, pv[1:] != pv[:-1]])
    ve = np.r_[vs[1:], len(pick)]
    for a0, b0 in zip(vs, ve):
        if b0 - a0 < 2 or pli[a0] == pli[b0 - 1]:
            continue
        ii = pick[a0:b0]
        ll = line[ii]
        A, B = np.meshgrid(np.arange(len(ii)), np.arange(len(ii)), indexing="ij")
        m = (A < B) & (ll[A] != ll[B])
        pairs.append(np.stack([o[ii[A[m]]], o[ii[B[m]]]], 1))
    P = np.concatenate(pairs) if pairs else np.zeros((0, 2), int)
    if not len(P):
        return P
    fa, fb = pl["flat"][P[:, 0]], pl["flat"][P[:, 1]]
    both = fa & fb
    cosang = np.abs(np.einsum("ij,ij->i", pl["n"][P[:, 0]], pl["n"][P[:, 1]]))
    good = ~both | (cosang >= np.cos(np.radians(max_angle)))
    out = [P[good & fb], P[good & fa][:, ::-1]]           # (observed, reference)
    return np.concatenate(out)


# ----------------------------------------------------------------------------------------------- the solve
def _in_holes_pad(tq, holes, pad):
    """Strictly inside a hole (widened by pad): the knots on the edges count as data."""
    tq = np.asarray(tq, float)
    if holes is None or not len(holes):
        return np.zeros(len(tq), bool)
    h = np.asarray(holes, float).reshape(-1, 2)
    j = np.searchsorted(h[:, 0] - pad, tq, side="right") - 1
    ok = j >= 0
    out = np.zeros(len(tq), bool)
    out[ok] = (tq[ok] < h[j[ok], 1] + pad - 1e-6) & (tq[ok] > h[j[ok], 0] - pad + 1e-6)
    return out


class Solver:
    """lines: {line id: Correction}; holes: (n, 2) recording holes (same time base as t)."""

    def __init__(self, corrections, holes=None):
        self.cor = corrections
        self.holes = None if holes is None else np.asarray(holes, float).reshape(-1, 2)
        self.ids = sorted(corrections)
        self.off = {}
        k = 0
        for l in self.ids:
            self.off[l] = k
            k += 6 * len(corrections[l].tau)
        self.nvar = k
        self.history = []

    def corrected(self, line, t, p, s):
        """Points and INS points with the current corrections."""
        p2, s2 = p.copy(), s.copy()
        for l in self.ids:
            m = line == l
            if m.any():
                d, th = self.cor[l].eval(t[m])
                p2[m] = apply(p[m], s[m], d, th)
                s2[m] = s[m] + d
        return p2, s2

    def _obs(self, pl, P, t, p2, s2):
        """Point-to-plane observations: OBS_PER_GROUP points of each observed group against its reference plane.
        -> (point index, reference plane index, residual)."""
        smp = pl["sample"][P[:, 0]]                       # (m, k)
        ref = np.repeat(P[:, 1], smp.shape[1])
        q = smp.ravel()
        ok = q >= 0
        q, ref = q[ok], ref[ok]
        r = np.einsum("ij,ij->i", pl["n"][ref], p2[q] - pl["c"][ref])
        return q, ref, r

    def _rows_obs(self, pl, q, ref, r, w, line, t, p2, s2):
        """Sparse rows: r = n_ref . (p_q - c_ref) linearised in the increments (the point at its own time, the
        reference plane at its mean time)."""
        n = pl["n"][ref]
        m = len(q)
        rows, cols, vals = [], [], []
        sides = ((line[q], t[q], np.concatenate([n, np.cross(p2[q] - s2[q], n)], 1)),
                 (pl["line"][ref], pl["tb"][ref], -np.concatenate([n, np.cross(pl["c"][ref] - pl["s"][ref], n)], 1)))
        for ln, tt, coef in sides:
            for l in self.ids:
                ml = np.flatnonzero(ln == l)
                if not len(ml):
                    continue
                ki, kw = self.cor[l].basis(tt[ml])
                for kk, ww in ((ki, 1 - kw), (ki + 1, kw)):
                    base = self.off[l] + 6 * kk
                    for c6 in range(6):
                        rows.append(ml)
                        cols.append(base + c6)
                        vals.append(coef[ml, c6] * ww * w[ml])
        A = sp.csr_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))),
                          shape=(m, self.nvar))
        return A, -r * w

    def _rows_prior(self):
        rows, cols, vals, rhs = [], [], [], []
        r0 = 0
        for l in self.ids:
            cor = self.cor[l]
            k = len(cor.tau)
            hole = _in_holes_pad(cor.tau, self.holes, HOLE_PAD_S)
            sig =np.where(hole[:, None], [PRIOR_HOLE[0]] * 3 + [PRIOR_HOLE[1]] * 3,
                           [PRIOR_GOOD[0]] * 3 + [PRIOR_GOOD[1]] * 3)
            idx = self.off[l] + np.arange(6 * k)
            wv = (SIGMA_OBS / sig).ravel()
            rows.append(r0 + np.arange(6 * k))
            cols.append(idx)
            vals.append(wv)
            rhs.append(-wv * cor.X.ravel())
            r0 += 6 * k
            if k >= 3:                              # smoothness: X[k-1] - 2 X[k] + X[k+1]
                hs = hole[1:-1] | hole[:-2] | hole[2:]
                sg = np.where(hs[:, None], [SMOOTH_HOLE[0]] * 3 + [SMOOTH_HOLE[1]] * 3,
                              [SMOOTH_GOOD[0]] * 3 + [SMOOTH_GOOD[1]] * 3)
                ws = (SIGMA_OBS / sg)
                for c6 in range(6):
                    kk = np.arange(1, k - 1)
                    rr = r0 + np.arange(k - 2)
                    for dk, cf in ((-1, 1.0), (0, -2.0), (1, 1.0)):
                        rows.append(rr)
                        cols.append(self.off[l] + 6 * (kk + dk) + c6)
                        vals.append(cf * ws[:, c6])
                    x = cor.X[:, c6]
                    rhs.append(-ws[:, c6] * (x[:-2] - 2 * x[1:-1] + x[2:]))
                    r0 += k - 2
        A = sp.csr_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))),
                          shape=(r0, self.nvar))
        return A, np.concatenate(rhs)

    def solve(self, line, t, p, s, iterations=ITERATIONS, progress=None, cancelled=None):
        """Iterate: correct the points, fit planes, pair them, solve the increments. Returns a summary."""
        max_r = 0.5
        for it in range(iterations):
            if cancelled and cancelled():
                raise RuntimeError("cancelled")
            p2, s2 = self.corrected(line, t, p, s)
            pl = planes(line, t, p2, s2)
            if pl is None:
                raise ValueError("no planar surfaces found")
            P = plane_pairs(pl)
            if not len(P):
                raise ValueError("no planar surfaces seen twice")
            q, ref, r = self._obs(pl, P, t, p2, s2)
            if len(q) > MAX_OBS:                  # bounded memory: an even sample, every hole kept
                rng = np.random.default_rng(it)
                hole = _in_holes_pad(t[q], self.holes, 0.0) | _in_holes_pad(pl["tb"][ref], self.holes, 0.0)
                frac = (MAX_OBS - hole.sum()) / max(1, (~hole).sum())
                sel = hole | (rng.random(len(q)) < frac)
                if sel.sum() > MAX_OBS:
                    sel &= rng.random(len(q)) < MAX_OBS / sel.sum()
                q, ref, r = q[sel], ref[sel], r[sel]
            keep = np.abs(r) <= max_r
            q, ref, r = q[keep], ref[keep], r[keep]
            sig = np.hypot(SIGMA_OBS, pl["thick"][ref])
            e = np.abs(r) / sig
            hub = np.where(e <= HUBER_K, 1.0, HUBER_K / np.maximum(e, 1e-9))
            w = (SIGMA_OBS / sig) * np.sqrt(hub)
            Ao, bo = self._rows_obs(pl, q, ref, r, w, line, t, p2, s2)
            Ap, bp = self._rows_prior()
            A = sp.vstack([Ao, Ap]).tocsr()
            b = np.concatenate([bo, bp])
            N = (A.T @ A).tocsc()
            dx = spla.spsolve(N, A.T @ b)
            for l in self.ids:
                k = len(self.cor[l].tau)
                self.cor[l].X += dx[self.off[l]:self.off[l] + 6 * k].reshape(k, 6)
            inter = line[q] != pl["line"][ref]
            self.history.append({"iteration": it + 1, "pairs": int(len(q)), "inter": int(inter.sum()),
                                 "median_before_cm": round(100 * float(np.median(np.abs(r))), 2),
                                 "step_max_cm": round(100 * float(np.abs(dx).max()), 2)})
            if progress:
                progress((it + 1) / iterations, self.history[-1])
            max_r = max(0.15, max_r * 0.6)
            del Ao, Ap, A, N, b, pl, P, q, ref, r, p2, s2       # the next iteration builds its own (memory)
            import gc
            gc.collect()
            if np.abs(dx).max() < 1e-4:
                break
        return self.history


def residuals(line, t, p, s, solver=None):
    """Point-to-plane separations of sample points against reference planes, as the points are (or corrected by
    solver): {"same": |r| for the same line at another time, "inter": |r| against another line}."""
    if solver is not None:
        p, s = solver.corrected(line, t, p, s)
    pl = planes(line, t, p, s)
    if pl is None:
        return {"same": np.zeros(0), "inter": np.zeros(0)}
    P = plane_pairs(pl)
    tmp = Solver({0: Correction([0.0, 1.0])})
    q, ref, r = tmp._obs(pl, P, t, p, s)
    r = np.abs(r)
    inter = line[q] != pl["line"][ref]
    return {"same": r[~inter], "inter": r[inter]}
