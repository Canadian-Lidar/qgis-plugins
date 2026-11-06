"""AlignPro calibration engine: passes, georeferencing, pairs, planar patches, boresight solve,
combinations, analyze sweeps, grids and slices. Pure numpy; no QGIS imports."""
import math
import warnings

import numpy as np

from . import geo

LINE_COLORS = ["#e5484d", "#a855f7", "#3b82f6", "#10b981", "#f59e0b", "#06b6d4",
               "#ec4899", "#84cc16", "#f97316", "#6366f1", "#14b8a6", "#eab308"]

DEFAULT_SETTINGS = {
    # data
    "returns": "strongest", "range_min": 1.5, "range_max": 60.0, "intensity_min": 0,
    "points_per_line": 3000000, "fine_points_per_box": 3000000,
    # passes
    "split_turns": True, "min_speed": 1.0, "turn_deg": 35.0, "min_pass_s": 4.0,
    # pairing
    "reciprocal_deg": 150.0, "same_dir_deg": 30.0, "min_overlap_pct": 20.0, "speed_margin": 2.0,
    # patches
    "voxel_m": 2.0, "min_pts_line": 20, "max_pts_patch_line": 250, "thickness_m": 0.04,
    "flat_ratio": 1.0, "max_patches": 8000, "use_aoi": False, "max_solve_points": 12000000,
    # solver
    "solve_lever": False, "huber_k": 2.0, "reject_sigma": 3.5, "max_iter": 12,
    # quality screening
    "max_sd_pos_m": 0.10,
    # display
    "grid_cell_m": 0.5,
}


# Point densities (Sean, 6 Nov 2026: more points to align properly, chosen by the user). Before 1.6.1 every job
# used "standard". The solver holds about 130 bytes per patch point.
DENSITY_KEYS = ("points_per_line", "max_pts_patch_line", "max_patches", "fine_points_per_box")
DENSITIES = {
    "standard": {"points_per_line": 1500000, "max_pts_patch_line": 80, "max_patches": 4000,
                 "fine_points_per_box": 1500000},
    "dense": {"points_per_line": 3000000, "max_pts_patch_line": 250, "max_patches": 8000,
              "fine_points_per_box": 3000000},
    "very_dense": {"points_per_line": 6000000, "max_pts_patch_line": 600, "max_patches": 15000,
                   "fine_points_per_box": 6000000},
}
DENSITY_TITLES = {"standard": "Standard (quick look)", "dense": "Dense (recommended)",
                  "very_dense": "Very dense (slow, needs memory)", "custom": "Custom (Settings)"}


def density_of(s):
    """The preset name a settings dict matches, else "custom"."""
    for name, d in DENSITIES.items():
        if all(int(s.get(k, DEFAULT_SETTINGS[k])) == v for k, v in d.items()):
            return name
    return "custom"


def solve_points_estimate(s, lines_per_patch=2.5):
    """Upper bound of the points the solver holds for these settings, and its memory (bytes)."""
    n = int(s.get("max_patches", 8000)) * int(s.get("max_pts_patch_line", 250)) * lines_per_patch
    return int(n), int(n * 130)


# ------------------------------------------------------------------ passes
def split_passes(traj, t0, t1, s):
    """Split [t0, t1] into straight passes (drops stops and turns). -> [(ta, tb)]"""
    m = (traj.t >= t0) & (traj.t <= t1)
    if m.sum() < 10:
        return [(t0, t1)]
    t = traj.t[m]
    frame = geo.LocalFrame(math.degrees(traj.lat[m][0]), math.degrees(traj.lon[m][0]), 0)
    en = frame.from_geodetic(traj.lat[m], traj.lon[m], traj.h[m])
    dt_ = 0.5
    tg = np.arange(t[0], t[-1], dt_)
    if len(tg) < 4:
        return [(t0, t1)]
    e = np.interp(tg, t, en[:, 0])
    n = np.interp(tg, t, en[:, 1])
    ve, vn = np.gradient(e, dt_), np.gradient(n, dt_)
    sp = np.hypot(ve, vn)
    cog = np.unwrap(np.arctan2(ve, vn))
    moving = sp >= s.get("min_speed", 1.0)
    if not s.get("split_turns", True):
        idx = np.nonzero(moving)[0]
        return [(float(tg[idx[0]]), float(tg[idx[-1]]))] if len(idx) else [(t0, t1)]
    # straight runs: heading within turn_deg of the run's start heading
    lim = math.radians(s.get("turn_deg", 35.0))
    out, i = [], 0
    while i < len(tg):
        if not moving[i]:
            i += 1
            continue
        j = i
        ref = cog[i]
        while j + 1 < len(tg) and moving[j + 1] and abs(cog[j + 1] - ref) < lim:
            j += 1
            if j - i == 6:
                ref = np.median(cog[i:j + 1])
        if tg[j] - tg[i] >= s.get("min_pass_s", 4.0):
            out.append((float(tg[i]), float(tg[j])))
        i = j + 1
    return out or [(t0, t1)]


# ---------------------------------------------------------------- the lines
class Line:
    def __init__(self, name, data, window, index):
        self.name = name                     # e.g. "001" or "003b"
        self.data = data                     # from lines.read_line
        self.info = data["info"]
        self.window = window                 # (t0, t1)
        self.index = index
        self.use = True
        self.color = LINE_COLORS[index % len(LINE_COLORS)]
        t = data["t"]
        self.mask = (t >= window[0]) & (t <= window[1])
        self.stats = {}
        self.x = None                        # georeferenced points (local ENU), subset by mask
        self.idx = np.nonzero(self.mask)[0]

    @property
    def n(self):
        return len(self.idx)


def pose(traj, frame, t, latency=0.0, batch=400000):
    pos = np.empty((len(t), 3))
    C = np.empty((len(t), 3, 3))
    for a in range(0, len(t), batch):
        b = min(a + batch, len(t))
        lat, lon, h, r, p, hd = traj.interp(t[a:b], latency)
        pos[a:b] = frame.from_geodetic(lat, lon, h)
        C[a:b] = frame.vehicle_to_local(lat, lon, r, p, hd)
    return pos, C


def pose_fast(traj, frame, t, latency=0.0, dt=0.002):
    """pose() for many points: exact poses on a dt grid (only where there are points), linearly
    interpolated between them. Over 2 ms the interpolation error is far below a millimetre."""
    t = np.asarray(t, float)
    if len(t) < 20000:
        return pose(traj, frame, t, latency)
    f = t / dt
    k = np.floor(f).astype(np.int64)
    ug = np.unique(np.concatenate([k, k + 1]))
    posg, Cg = pose(traj, frame, ug * dt, latency)
    i0 = np.searchsorted(ug, k)
    i1 = np.searchsorted(ug, k + 1)
    w = (f - k)[:, None]
    pos = posg[i0] * (1 - w) + posg[i1] * w
    C = Cg[i0] * (1 - w)[:, :, None] + Cg[i1] * w[:, :, None]
    return pos, C


def georef(pos, C, q, lever, B=None):
    a = q if B is None else q @ np.asarray(B).T
    return pos + np.einsum("nij,nj->ni", C, a + np.asarray(lever))


def line_stats(line, traj, frame, settings):
    t0, t1 = line.window
    m = (traj.t >= t0) & (traj.t <= t1)
    st = {"duration": t1 - t0}
    if m.sum() > 2:
        en = frame.from_geodetic(traj.lat[m], traj.lon[m], traj.h[m])
        d = np.diff(en[:, :2], axis=0)
        st["length"] = float(np.hypot(d[:, 0], d[:, 1]).sum())
        st["sog"] = st["length"] / max(t1 - t0, 1e-6)
        v = en[-1, :2] - en[0, :2]
        st["cog"] = (math.degrees(math.atan2(v[0], v[1])) + 360) % 360
        hd = (np.degrees(traj.heading[m]) + 360) % 360
        st["crab"] = float(((np.median(hd) - st["cog"] + 180) % 360) - 180)
        st["track"] = en
        st["track_t"] = traj.t[m]
        if traj.fix is not None:
            st["fix_rtk_pct"] = float(np.mean(traj.fix[m] == 7) * 100)
        if traj.sd_pos is not None:
            st["sd_pos"] = float(np.median(np.linalg.norm(traj.sd_pos[m], axis=1)))
        if traj.sd_att is not None:
            st["sd_att_deg"] = [float(np.degrees(np.median(traj.sd_att[m][:, k]))) for k in range(3)]
    info = line.info
    tl = line.data["t"]
    dur = max(t1 - t0, 1e-6)
    nav_t = line.data["nav"]["t"]
    st["nav_hz"] = float(np.sum((nav_t >= t0) & (nav_t <= t1)) / dur)
    full = max((info["t_stop"] or t1) - (info["t_start"] or t0), 1e-6)
    st["lidar_pps"] = info["packets"] / full
    st["lost"] = info["lost_packets"]
    s = info["status"]
    st["pps_lock_pct"] = 100.0 * s["pps_locked"] / s["count"] if s["count"] else None
    st["nmea_pct"] = 100.0 * s["nmea_valid"] / s["count"] if s["count"] else None
    st["points"] = int(line.n)
    st["traj_covers"] = bool(traj.covers(t0, t1))
    st["sensor_lag_ms"] = info.get("log_minus_publish_ms")
    if line.x is not None and len(line.x):
        dd = np.linalg.norm(line.data["p"][line.idx], axis=1)
        st["median_range"] = float(np.median(dd))
    line.stats = st
    return st


# ---------------------------------------------------------------- patches
class Patches:
    """Planar patches seen by >= 2 lines, with the points that define them."""

    def __init__(self):
        self.center = np.zeros((0, 3))
        self.normal = np.zeros((0, 3))
        self.kind = np.zeros(0, np.uint8)      # 0 flat, 1 slope, 2 wall
        self.lines = []                        # per patch: set of line indices
        self.pt_line = np.zeros(0, np.int32)
        self.pt_patch = np.zeros(0, np.int32)
        self.pt_idx = np.zeros(0, np.int64)    # index into the line's data arrays

    def __len__(self):
        return len(self.center)


def _group_moments(keys, x):
    uk, inv, cnt = np.unique(keys, return_inverse=True, return_counts=True)
    G = len(uk)
    s = np.zeros((G, 3))
    for k in range(3):
        s[:, k] = np.bincount(inv, x[:, k], G)
    mean = s / cnt[:, None]
    d = x - mean[inv]
    cov = np.zeros((G, 3, 3))
    for i in range(3):
        for j in range(i, 3):
            v = np.bincount(inv, d[:, i] * d[:, j], G) / cnt
            cov[:, i, j] = v
            cov[:, j, i] = v
    return uk, inv, cnt, mean, cov


def _vkey(x, v, origin):
    q = np.floor((x - origin) / v).astype(np.int64) + 50000
    return (q[:, 0] << 34) | (q[:, 1] << 17) | q[:, 2]


def build_patches(lines, s, aoi=None, rng=None):
    """lines: list of Line with .x set (current georeference). aoi: callable(xy)->mask or None."""
    rng = rng or np.random.default_rng(1)
    V = float(s["voxel_m"])
    allx = [ln.x for ln in lines if ln.use and ln.x is not None and len(ln.x)]
    if len(allx) < 2:
        return Patches()
    origin = np.floor(np.min([a.min(0) for a in allx], axis=0)) - V
    per = {}   # voxel key -> list of (line_i, mean, cov, n, normal)
    sel_pts = {}
    for li, ln in enumerate(lines):
        if not ln.use or ln.x is None or not len(ln.x):
            continue
        x = ln.x
        msk = np.ones(len(x), bool)
        if aoi is not None:
            msk = aoi(x[:, :2])
        keys = _vkey(x[msk], V, origin)
        uk, inv, cnt, mean, cov = _group_moments(keys, x[msk])
        ok = cnt >= s["min_pts_line"]
        w, vec = np.linalg.eigh(cov[ok])
        thick = np.sqrt(np.maximum(w[:, 0], 0))
        spread = np.sqrt(np.maximum(w[:, 1], 0))
        good = (thick < s["thickness_m"]) & (spread > 0.12 * V)
        okidx = np.nonzero(ok)[0][good]
        normals = vec[good][:, :, 0]
        for g, nrm in zip(okidx, normals):
            per.setdefault(int(uk[g]), []).append((li, mean[g], cov[g], int(cnt[g]), nrm))
        sel_pts[li] = (np.nonzero(msk)[0], inv, set(int(uk[g]) for g in okidx), uk)
    cand = []
    for k, lst in per.items():
        if len(lst) < 2:
            continue
        n0 = lst[0][4]
        agree = [e for e in lst if abs(np.dot(e[4], n0)) > math.cos(math.radians(12))]
        if len(agree) < 2:
            continue
        ntot = sum(e[3] for e in agree)
        c = sum(e[1] * e[3] for e in agree) / ntot
        cv = sum((e[2] + np.outer(e[1] - c, e[1] - c)) * e[3] for e in agree) / ntot
        w, vec = np.linalg.eigh(cv)
        nrm = vec[:, 0]
        if nrm[2] < 0:
            nrm = -nrm
        cand.append((k, c, nrm, frozenset(e[0] for e in agree)))
    if not cand:
        return Patches()
    up = np.array([abs(c[2][2]) for c in cand])
    kind = np.where(up > 0.95, 0, np.where(up < 0.30, 2, 1)).astype(np.uint8)
    nonflat = np.nonzero(kind != 0)[0]
    flat = np.nonzero(kind == 0)[0]
    nf_keep = max(40, int(len(nonflat) * s["flat_ratio"]))
    if len(flat) > nf_keep:
        flat = rng.choice(flat, nf_keep, replace=False)
    keep = np.sort(np.concatenate([nonflat, flat]).astype(int))
    if len(keep) > s["max_patches"]:
        keep = np.sort(rng.choice(keep, int(s["max_patches"]), replace=False))
    cand = [cand[i] for i in keep]
    kind = kind[keep]
    P = Patches()
    P.center = np.array([c[1] for c in cand])
    P.normal = np.array([c[2] for c in cand])
    P.kind = kind
    P.lines = [c[3] for c in cand]
    pid = {c[0]: i for i, c in enumerate(cand)}
    pl, pp, pi = [], [], []
    mpp = int(s["max_pts_patch_line"])
    for li, (orig_idx, inv, okkeys, uk) in sel_pts.items():
        gk = uk[inv]
        sel_keys = np.array([k for k in okkeys if k in pid and li in cand[pid[k]][3]], np.int64)
        if not len(sel_keys):
            continue
        m = np.isin(gk, sel_keys)
        idx = np.nonzero(m)[0]
        kk = gk[idx]
        order = rng.permutation(len(idx))
        idx, kk = idx[order], kk[order]
        srt = np.argsort(kk, kind="stable")
        idx, kk = idx[srt], kk[srt]
        first = np.searchsorted(kk, kk, side="left")
        rank = np.arange(len(kk)) - first
        take = rank < mpp
        idx, kk = idx[take], kk[take]
        pl.append(np.full(len(idx), li, np.int32))
        pp.append(np.array([pid[int(k)] for k in kk], np.int32))
        pi.append(orig_idx[idx])
    P.pt_line = np.concatenate(pl)
    P.pt_patch = np.concatenate(pp)
    P.pt_idx = np.concatenate(pi)          # index into ln.x (and ln.idx)
    cap = int(s.get("max_solve_points", 0) or 0)
    P.thinned_from = 0
    if cap and len(P.pt_line) > cap:       # a memory guard for very dense custom settings
        P.thinned_from = len(P.pt_line)
        sel = np.sort(rng.choice(len(P.pt_line), cap, replace=False))
        P.pt_line, P.pt_patch, P.pt_idx = P.pt_line[sel], P.pt_patch[sel], P.pt_idx[sel]
    return P


# ------------------------------------------------------------------ solver
class SolveData:
    """Points of the patches with their poses (fixed for one latency)."""

    def __init__(self, lines, P, traj, frame, M, latency):
        self.P = P
        n = len(P.pt_line)
        self.t = np.empty(n)
        self.q = np.empty((n, 3))
        for li in np.unique(P.pt_line):
            m = P.pt_line == li
            ln = lines[li]
            di = ln.idx[P.pt_idx[m]]
            self.t[m] = ln.data["t"][di]
            self.q[m] = ln.data["p"][di].astype(float) @ M.T
        self.pos, self.C = pose(traj, frame, self.t, latency)
        self.line = P.pt_line
        self.patch = P.pt_patch


def _cross(a, b):
    return np.cross(a, b)


def _perp_basis(n):
    a = np.where(np.abs(n[:, 0:1]) < 0.9, np.array([[1.0, 0, 0]]), np.array([[0, 1.0, 0]]))
    a1 = np.cross(n, a)
    a1 /= np.linalg.norm(a1, axis=1, keepdims=True)
    a2 = np.cross(n, a1)
    return a1, a2


def fit_planes(x, patch, G, w=None):
    """Weighted PCA plane per patch -> centroid (G,3), normal (G,3)."""
    w = np.ones(len(x)) if w is None else w
    sw = np.bincount(patch, w, G) + 1e-12
    c = np.stack([np.bincount(patch, w * x[:, k], G) for k in range(3)], -1) / sw[:, None]
    d = x - c[patch]
    cov = np.zeros((G, 3, 3))
    for i in range(3):
        for j in range(i, 3):
            v = np.bincount(patch, w * d[:, i] * d[:, j], G) / sw
            cov[:, i, j] = cov[:, j, i] = v
    _, vec = np.linalg.eigh(cov)
    n = vec[:, :, 0]
    n *= np.where(n[:, 2:3] < 0, -1, 1)
    return c, n


def residuals(sd, B, lever, line_mask=None):
    """Point-to-plane residuals with planes re-fitted (closed form) at these parameters."""
    x = georef(sd.pos, sd.C, sd.q, lever, B)
    m = np.ones(len(x), bool) if line_mask is None else line_mask[sd.line]
    G = len(sd.P)
    c, n = fit_planes(x[m], sd.patch[m], G)
    e = np.einsum("ij,ij->i", x - c[sd.patch], n[sd.patch])
    return e, m


def patch_weights(patch, m, G):
    cnt = np.bincount(patch[m], minlength=G).astype(float)
    w = np.zeros(len(patch))
    w[m] = 1.0 / np.maximum(cnt[patch[m]], 1)
    w *= m.sum() / max(w.sum(), 1e-12)
    return w


def error_metric(e, w):
    """Weighted mean absolute point-to-plane distance (m)."""
    return float(np.sum(w * np.abs(e)) / max(np.sum(w), 1e-12))


def solve(sd, lever0, s, line_mask=None, B0=None, progress=None):
    """Point-to-plane Gauss-Newton for the boresight rotation B (and optionally the lever),
    plane parameters eliminated by Schur complement. Returns dict."""
    G = len(sd.P)
    m = np.ones(len(sd.line), bool) if line_mask is None else line_mask[sd.line].copy()
    # patches must still be seen by >= 2 of the used lines
    if line_mask is not None:
        seen = np.zeros(G, int)
        for li in np.nonzero(line_mask)[0]:
            seen += np.bincount(sd.patch[(sd.line == li)], minlength=G) > 0
        m &= seen[sd.patch] >= 2
    if m.sum() < 50:
        return None
    B = np.eye(3) if B0 is None else np.array(B0, float)
    lever = np.array(lever0, float)
    solve_lever = bool(s.get("solve_lever"))
    NP = 6 if solve_lever else 3
    wp = patch_weights(sd.patch, m, G)
    x = georef(sd.pos, sd.C, sd.q, lever, B)
    c, n = fit_planes(x[m], sd.patch[m], G)
    d = np.zeros(G)
    e0 = np.einsum("ij,ij->i", x - c[sd.patch], n[sd.patch])
    before = error_metric(e0[m], wp[m])
    rej = np.zeros(len(m), bool)
    hist = []
    for it in range(int(s.get("max_iter", 12))):
        a = sd.q @ B.T
        x = sd.pos + np.einsum("nij,nj->ni", sd.C, a + lever)
        npt = n[sd.patch]
        dx = x - c[sd.patch]
        e = np.einsum("ij,ij->i", dx, npt) - d[sd.patch]
        act = m & ~rej
        sig_g = 1.4826 * np.median(np.abs(e[act])) + 1e-6
        # per-patch noise (range noise and incidence differ per surface), floored at half the global
        cnt = np.bincount(sd.patch[act], minlength=G) + 1e-9
        sig_p = 1.2533 * np.bincount(sd.patch[act], np.abs(e[act]), G) / cnt
        sig = np.maximum(sig_p, 0.5 * sig_g)[sd.patch]
        k = s.get("huber_k", 2.0) * sig
        wh = np.minimum(1.0, k / np.maximum(np.abs(e), 1e-12))
        W = wp * wh * act
        u = np.einsum("nji,nj->ni", sd.C, npt)          # C^T n
        g = np.empty((len(e), NP))
        g[:, :3] = -np.cross(u, a)
        if solve_lever:
            g[:, 3:] = u
        a1, a2 = _perp_basis(n)
        h = np.stack([np.einsum("ij,ij->i", dx, a1[sd.patch]),
                      np.einsum("ij,ij->i", dx, a2[sd.patch]), -np.ones(len(e))], -1)
        A = (g * W[:, None]).T @ g
        bg = (g * (W * e)[:, None]).sum(0)
        D = np.zeros((G, 3, 3))
        Bj = np.zeros((G, NP, 3))
        bh = np.zeros((G, 3))
        for i in range(3):
            bh[:, i] = np.bincount(sd.patch, W * h[:, i] * e, G)
            for j in range(3):
                D[:, i, j] = np.bincount(sd.patch, W * h[:, i] * h[:, j], G)
            for p in range(NP):
                Bj[:, p, i] = np.bincount(sd.patch, W * g[:, p] * h[:, i], G)
        D += np.eye(3)[None] * 1e-9
        Di = np.linalg.inv(D)
        BDi = np.einsum("gpi,gij->gpj", Bj, Di)
        S = A - np.einsum("gpj,gqj->pq", BDi, Bj)
        r = bg - np.einsum("gpj,gj->p", BDi, bh)
        try:
            delta = -np.linalg.solve(S, r)
        except np.linalg.LinAlgError:
            return None
        tj = -np.einsum("gij,gj->gi", Di, bh + np.einsum("gpi,p->gi", Bj, delta))
        B = geo.small_rotation(*delta[:3]) @ B
        if solve_lever:
            lever = lever + delta[3:]
        n = n + tj[:, 0:1] * a1 + tj[:, 1:2] * a2
        n /= np.linalg.norm(n, axis=1, keepdims=True)
        d = d + tj[:, 2]
        hist.append(float(np.degrees(np.linalg.norm(delta[:3]))))
        if progress:
            progress(min(0.95, (it + 1) / 6.0))
        if np.linalg.norm(delta[:3]) < 2e-7 and (not solve_lever or np.linalg.norm(delta[3:]) < 1e-6):
            if not rej.any() and s.get("reject_sigma"):
                rej = m & (np.abs(e) > s["reject_sigma"] * sig)
                continue
            break
    a = sd.q @ B.T
    x = sd.pos + np.einsum("nij,nj->ni", sd.C, a + lever)
    act = m & ~rej
    c2, n2 = fit_planes(x[act], sd.patch[act], G)
    e1 = np.einsum("ij,ij->i", x - c2[sd.patch], n2[sd.patch])
    W = wp * act
    sig0 = math.sqrt(float(np.sum(W * e1 ** 2) / max(np.sum(W) - NP - 3 * G, 1)))
    try:
        cov = np.linalg.inv(S) * sig0 ** 2
        sd_par = np.sqrt(np.maximum(np.diag(cov), 0))
    except np.linalg.LinAlgError:
        sd_par = np.full(NP, np.nan)
    return {"B": B, "lever": lever, "before": before, "after": error_metric(e1[act], wp[act]),
            "rms_before": float(np.sqrt(np.average(e0[m] ** 2, weights=wp[m]))),
            "rms_after": float(np.sqrt(np.average(e1[act] ** 2, weights=wp[act]))),
            "formal_sd_deg": [float(np.degrees(v)) for v in sd_par[:3]],
            "formal_sd_lever": [float(v) for v in sd_par[3:]] if solve_lever else None,
            "iterations": len(hist), "rejected": int(rej.sum()), "points": int(act.sum()),
            "patches": int(len(np.unique(sd.patch[act]))), "e_before": e0, "e_after": e1,
            "active": act, "weights": wp}


def bore_angles(B):
    """Small rotation B as (roll, pitch, heading) about the vehicle axes, degrees."""
    p = math.asin(max(-1, min(1, -B[2, 0])))
    return (math.degrees(math.atan2(B[2, 1], B[2, 2])), math.degrees(p),
            math.degrees(math.atan2(B[1, 0], B[0, 0])))


# -------------------------------------------------------------- pairs
def pairs(lines, P, s, cell=2.0):
    out = []
    used = [i for i, l in enumerate(lines) if l.use]
    occ = {}
    for i in used:
        x = lines[i].x
        if x is None or not len(x):
            occ[i] = set()
            continue
        k = np.unique(np.floor(x[:, :2] / cell).astype(np.int64) @ np.array([1, 1 << 24]))
        occ[i] = set(k.tolist())
    for ai in range(len(used)):
        for bi in range(ai + 1, len(used)):
            i, j = used[ai], used[bi]
            A, Bs = lines[i].stats, lines[j].stats
            dh = abs(((A.get("cog", 0) - Bs.get("cog", 0)) + 180) % 360 - 180)
            dsp = abs(A.get("sog", 0) - Bs.get("sog", 0))
            inter = len(occ[i] & occ[j])
            ov = 100.0 * inter / max(1, min(len(occ[i]), len(occ[j])))
            shared = sum(1 for ls in P.lines if i in ls and j in ls) if len(P) else 0
            sep = _track_sep(A, Bs)
            if ov < s["min_overlap_pct"] or shared < 3:
                typ = "Additional"
            elif dh >= s["reciprocal_deg"]:
                typ = "Reciprocal"
            elif dh <= s["same_dir_deg"]:
                typ = "Latency" if dsp >= s["speed_margin"] and sep < 2.0 else "Same direction"
            else:
                typ = "Crossing"
            out.append({"a": i, "b": j, "dhdg": dh, "dspeed": dsp, "overlap": ov, "shared": shared,
                        "separation": sep, "type": typ})
    return out


def _track_sep(A, B):
    ta, tb = A.get("track"), B.get("track")
    if ta is None or tb is None or not len(ta) or not len(tb):
        return float("nan")
    pa = ta[::max(1, len(ta) // 50), :2]
    pb = tb[::max(1, len(tb) // 400), :2]
    d = np.sqrt(((pa[:, None, :] - pb[None, :, :]) ** 2).sum(-1)).min(1)
    return float(np.median(d))


def pair_errors(sd, res, pair_list, e_key="e_after"):
    """Mean |residual| of the two lines on patches they share."""
    e = res[e_key]
    w = res["weights"]
    for pr in pair_list:
        i, j = pr["a"], pr["b"]
        G = len(sd.P)
        ci = np.bincount(sd.patch[sd.line == i], minlength=G) > 0
        cj = np.bincount(sd.patch[sd.line == j], minlength=G) > 0
        sh = ci & cj
        m = ((sd.line == i) | (sd.line == j)) & sh[sd.patch] & res["active"]
        if m.sum():
            pr[e_key] = float(np.sum(w[m] * np.abs(e[m])) / np.sum(w[m]))
    return pair_list


def line_errors(sd, res, n_lines, e_key):
    out = []
    e, w, act = res[e_key], res["weights"], res["active"]
    for i in range(n_lines):
        m = (sd.line == i) & act
        out.append(float(np.sum(w[m] * np.abs(e[m])) / np.sum(w[m])) if m.sum() else None)
    return out


# ------------------------------------------------------------- display aids
def grid_stats(lines, cell, max_pts=300000, rng=None):
    rng = rng or np.random.default_rng(3)
    xs, ls = [], []
    for i, ln in enumerate(lines):
        if not ln.use or ln.x is None or not len(ln.x):
            continue
        x = ln.x
        if len(x) > max_pts:
            x = x[rng.choice(len(x), max_pts, replace=False)]
        xs.append(x)
        ls.append(np.full(len(x), i))
    if not xs:
        return None
    x = np.concatenate(xs)
    li = np.concatenate(ls)
    x0, y0 = np.floor(x[:, 0].min() / cell) * cell, np.floor(x[:, 1].min() / cell) * cell
    nx = int((x[:, 0].max() - x0) / cell) + 1
    ny = int((x[:, 1].max() - y0) / cell) + 1
    if nx * ny > 25e6:
        return None
    ix = ((x[:, 0] - x0) / cell).astype(int)
    iy = ((x[:, 1] - y0) / cell).astype(int)
    c = iy * nx + ix
    N = nx * ny
    zmin = np.full(N, np.inf)
    np.minimum.at(zmin, c, x[:, 2])
    surf = x[:, 2] < zmin[c] + 0.5         # lowest surface in the cell (ground / roof), not walls
    cnt = np.bincount(c, minlength=N).astype(float)
    cs, zs = c[surf], x[surf, 2]
    ns = np.bincount(cs, minlength=N).astype(float)
    mean = np.bincount(cs, zs, N) / np.maximum(ns, 1)
    var = np.bincount(cs, zs ** 2, N) / np.maximum(ns, 1) - mean ** 2
    sdev = np.sqrt(np.maximum(var, 0))
    nl = int(li.max()) + 1
    lm = np.full((nl, N), np.nan)
    for i in np.unique(li):
        mm = surf & (li == i)
        k = np.bincount(c[mm], minlength=N)
        s = np.bincount(c[mm], x[mm, 2], N)
        lm[i] = np.where(k >= 3, s / np.maximum(k, 1), np.nan)
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)       # cells seen by no line are all-NaN: expected
        sep = np.nanmax(lm, 0) - np.nanmin(lm, 0)
        nlines = np.sum(np.isfinite(lm), 0)
    sep[nlines < 2] = np.nan
    shp = (ny, nx)
    nan = lambda a, m: np.where(m, a, np.nan).reshape(shp)  # noqa: E731
    return {"x0": x0, "y0": y0, "cell": cell, "nx": nx, "ny": ny,
            "mean": nan(mean, ns > 0), "count": cnt.reshape(shp), "sdev": nan(sdev, ns >= 3),
            "separation": sep.reshape(shp), "lines": nlines.reshape(shp).astype(float)}


def grid_summary(g):
    if g is None:
        return {}
    sd = g["sdev"][np.isfinite(g["sdev"])]
    sp = g["separation"][np.isfinite(g["separation"])]
    return {"avg_sdev_cm": float(np.mean(sd) * 100) if len(sd) else None,
            "avg_sep_cm": float(np.mean(sp) * 100) if len(sp) else None}


def slice_points(lines, box, max_per_line=60000, rng=None, B=None, sd_lines=None):
    """box: dict(p0=(e,n), p1=(e,n), width, view 'profile'|'plan', zmin, zmax)."""
    rng = rng or np.random.default_rng(5)
    p0, p1 = np.array(box["p0"], float), np.array(box["p1"], float)
    L = np.linalg.norm(p1 - p0)
    if L < 1e-3:
        return []
    u = (p1 - p0) / L
    v = np.array([-u[1], u[0]])
    out = []
    for i, ln in enumerate(lines):
        if not ln.use or ln.x is None:
            continue
        x = ln.x if sd_lines is None else sd_lines[i]
        rel = x[:, :2] - p0
        al = rel @ u
        ac = rel @ v
        m = (al >= 0) & (al <= L) & (np.abs(ac) <= box["width"] / 2)
        if box.get("zmin") is not None:
            m &= (x[:, 2] >= box["zmin"]) & (x[:, 2] <= box["zmax"])
        idx = np.nonzero(m)[0]
        if len(idx) > max_per_line:
            idx = rng.choice(idx, max_per_line, replace=False)
        out.append((i, al[idx], ac[idx], x[idx, 2]))
    return out


def _wall_clusters(P, mask):
    """Group wall patches into planes (same horizontal normal, same offset). -> list of index arrays."""
    idx = np.nonzero(mask)[0]
    out = []
    used = np.zeros(len(idx), bool)
    for k in range(len(idx)):
        if used[k]:
            continue
        n0 = P.normal[idx[k], :2] / (np.linalg.norm(P.normal[idx[k], :2]) + 1e-9)
        nn = P.normal[idx, :2] / (np.linalg.norm(P.normal[idx, :2], axis=1, keepdims=True) + 1e-9)
        same = (~used) & (np.abs(nn @ n0) > 0.97) & (np.abs((P.center[idx, :2] - P.center[idx[k], :2]) @ n0) < 0.4)
        used |= same
        out.append(idx[same])
    return out


def auto_boxes(lines, P):
    """Choose roll / pitch / heading inspection boxes from the patches .

    Every box cuts its wall edge-on (the box runs along the wall's normal):
      roll     facade parallel to the road, profile from the road to the wall (ground tilt + wall lean)
      pitch    wall across the road, narrow profile: pitch leans it differently on reciprocal passes
      heading  same wall, plan view along its whole length: heading shifts it along track, more with range
    """
    used = [l for l in lines if l.use and l.stats.get("track") is not None]
    if not used or not len(P):
        return {}
    cogs = np.radians([l.stats.get("cog", 0) for l in used])
    ang = np.arctan2(np.mean(np.sin(2 * cogs)), np.mean(np.cos(2 * cogs))) / 2
    u = np.array([math.sin(ang), math.cos(ang)])          # along track (E, N)
    v = np.array([-u[1], u[0]])
    tr = np.concatenate([l.stats["track"][:, :2] for l in used])
    tc = tr.mean(0)
    nh = P.normal[:, :2]
    nhn = np.linalg.norm(nh, axis=1) + 1e-9
    walls = P.kind == 2
    par = walls & (np.abs(nh @ v) / nhn > 0.8)            # facades along the road
    perp = walls & (np.abs(nh @ u) / nhn > 0.7)           # walls across the road
    boxes = {}

    def best_cluster(mask):
        cl = _wall_clusters(P, mask)
        if not cl:
            return None
        return max(cl, key=lambda ix: (sum(len(P.lines[i]) for i in ix), -np.min(np.abs((P.center[ix, :2] - tc) @ v))))

    def unit_normal(ix, toward):
        n = P.normal[ix, :2].mean(0)
        n /= np.linalg.norm(n) + 1e-12
        return n if n @ toward >= 0 else -n

    ix = best_cluster(par)
    if ix is not None:
        c = P.center[ix, :2].mean(0)
        n = unit_normal(ix, c - tc)                         # points away from the road
        dist = abs((c - tc) @ n)
        z = P.center[ix, 2]
        boxes["roll"] = {"p0": tuple(c - n * (dist + 4.0)), "p1": tuple(c + n * 2.0), "width": 1.0,
                         "view": "profile", "zmin": None, "zmax": None}
    ix = best_cluster(perp)
    if ix is not None:
        c = P.center[ix, :2].mean(0)
        n = unit_normal(ix, u)
        t = np.array([-n[1], n[0]])                          # along the wall
        along = (P.center[ix, :2] - c) @ t
        lo, hi = float(along.min()) - 1.5, float(along.max()) + 1.5
        mid = c + t * (lo + hi) / 2
        z = P.center[ix, 2]
        boxes["pitch"] = {"p0": tuple(mid - n * 3.0), "p1": tuple(mid + n * 3.0), "width": 1.0,
                          "view": "profile", "zmin": None, "zmax": None}
        boxes["heading"] = {"p0": tuple(mid - n * 3.0), "p1": tuple(mid + n * 3.0), "width": max(4.0, hi - lo),
                            "view": "plan", "zmin": float(z.min()) - 0.5, "zmax": float(z.max()) + 0.5}
    return {k: {kk: (list(map(float, vv)) if isinstance(vv, tuple) else vv) for kk, vv in b.items()}
            for k, b in boxes.items()}
