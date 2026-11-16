"""Side scan (Omniscan 3D packet 2198) on marine jobs: every ping of every head also carries a side-scan profile
(1000 samples over the slant range, e.g. 5.1-101.5 m at Lumston). CloudPro places each sample on the 3D seabed
measured by the same ping, levels the gain, shows the waterfall, makes mosaics, keeps contacts and writes XTF.

Placement (Sean, 16 Nov 2026: "on the 3D seabed"): the ping's own soundings (packet 3104, same ping number) give
the seabed profile across the fan as slant range -> (left, up) in the head's level frame. A side-scan sample at
slant range r is put where that profile has range r: no flat-bottom assumption, so slopes and features are not
smeared. Samples nearer than the first seabed return are water column (left out); beyond the last sounding the
seabed is carried on flat at the last depth. The level vectors are then placed exactly as the soundings
(sonar.place: vessel heading + the head's heading correction, lever arm, latency, projection), so the side scan
and the point cloud line up.

Per CloudPro sonar line (one head, one pass) the work file work/sidescan/line_<id>.npz holds: t (UTC s, latency
applied), ping, db (float16, pings x samples: the sonar's power in dB, linear in the stored u16 between the ping's
min and max dB), r (sample slant ranges m), knots (sample indices), E / N / Z (float32, pings x knots, local store
frame, NaN = water column), ang (float16, angle from vertical deg at the knots), alt (head height above the
seabed m), side (+1 port, -1 starboard), head.
Gain levelling: the mean dB of every sample (range) over blocks of 300 pings is taken off and the line's mean put
back (gain_table), so the image is even from nadir to the far range and along the pass.
"""
import math
import os
import struct
import time

import numpy as np

KNOT = 10                       # a placed position every KNOT samples (linear between)
BIN_M = 0.5                     # the seabed profile in bins of slant range
BLOCK = 300                     # gain levelling: pings per block
VERSION = 2


class SideScanError(ValueError):
    pass


def _quiet():
    import warnings
    w = warnings.catch_warnings()
    w.__enter__()
    warnings.simplefilter("ignore", RuntimeWarning)

    class _C:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            w.__exit__(*a)
    return _C()


def folder(project):
    d = os.path.join(project.work, "sidescan")
    os.makedirs(d, exist_ok=True)
    return d


def line_file(project, lid):
    return os.path.join(folder(project), "line_%d.npz" % lid)


def sonar_lines(project):
    """CloudPro's sonar lines (one head, one pass): [line dict]."""
    return [ln for ln in project.lines if ln.get("kind") == "sonar"]


def passes(project):
    """The passes with side scan: [(key, label, [line ids])], key = (files, pass); label as the lines are named
    everywhere, e.g. "P4 · L7 Port · L8 Stbd"."""
    from .sonar import side_short
    out = {}
    for ln in sonar_lines(project):
        k = (tuple(sorted(ln.get("files") or [])), ln.get("pass"))
        out.setdefault(k, []).append(ln["id"])
    res = []
    for k, ids in out.items():
        ids = sorted(ids, key=lambda i: (str(project.lines[i].get("looks")) != "port", i))
        ln = project.lines[ids[0]]
        heads = " · ".join("L%d %s" % (i + 1, side_short(project.lines[i].get("looks")) or
                                       "Head %s" % project.lines[i].get("head")) for i in ids)
        res.append((k, "%s · %s" % (ln.get("pass_label") or "P%s" % (k[1] or 1), heads), ids))
    res.sort(key=lambda r: min((project.lines[i].get("window") or [0])[0] or 0 for i in r[2]))
    return res


def processed(project, lid):
    f = line_file(project, lid)
    if not os.path.exists(f):
        return False
    try:
        with np.load(f) as z:
            return int(z["version"]) == VERSION and float(z["georef"]) == _georef_stamp(project)
    except Exception:       # noqa: BLE001
        return False


def _georef_stamp(project):
    """Changes when the points are georeferenced again (new trajectory, calibration...)."""
    g = (project.data.get("inputs") or {}).get("georeferenced") or ""
    import zlib
    return float(zlib.crc32(str(g).encode()))


# ----------------------------------------------------------------------------- processing
def process(project, line_ids=None, progress=None, cancel=None):
    """Make the side-scan work files of the sonar lines (all, or line_ids) -> {lid: summary}."""
    from dcs_core.marine import SonarRecording
    from . import chain
    from .geodesy import Projector
    from . import sonar as SN
    if project.dcs is None:
        raise SideScanError("side scan needs the DCS job (PosPro's trajectory)")
    inp = chain.resolve(project.dcs)
    if inp.traj is None:
        raise SideScanError("PosPro has not finalized the boat's trajectory")
    lines = [ln for ln in sonar_lines(project) if line_ids is None or ln["id"] in line_ids]
    if not lines:
        raise SideScanError("no sonar lines: georeference the job first")
    proj = Projector(project.crs, source=inp.source_crs)
    geoid = None
    if project.data.get("geoid_path"):
        from .export import geoid_sampler
        geoid = geoid_sampler(project.data["geoid_path"])
    recs = {}
    out = {}
    sonar_opts = project.data.get("sonar") or {}
    for k, ln in enumerate(lines):
        if cancel and cancel():
            break
        key = tuple(ln["files"])
        if key not in recs:
            recs.clear()
            recs[key] = SonarRecording(list(ln["files"]))
        rec = recs[key]
        did = int(ln["head"])
        att, _src = SN.head_attitude(inp.traj_doc, did, rec)
        cal = {"heading": 0.0, "pitch": 0.0, "roll": 0.0, "latency_s": 0.0}
        cal.update(ln.get("calibration_deg") or {})
        cal["latency_s"] = float(ln.get("latency_s") or 0.0)
        sub_prog = (lambda f, k=k: progress((k + f) / len(lines), "side scan %d / %d" % (k + 1, len(lines)))) \
            if progress else None
        out[ln["id"]] = process_line(project, rec, ln, att, cal, inp.traj, proj, geoid,
                                     sonar_opts.get("sound_speed"), sub_prog, cancel)
    return out


def process_line(project, rec, ln, A, cal, traj, proj, geoid, sound_speed=None, progress=None, cancel=None):
    """One sonar line (head + pass) -> its side-scan work file; returns a summary."""
    from dcs_core import marine, ping
    from .georef import GeorefContext
    from . import sonar as SN
    t_start = time.time()
    did = int(ln["head"])
    win = ln.get("window") or [-1e18, 1e18]
    p3 = {}
    for p in rec.pings(did):
        if win[0] <= p["t"] < win[1]:
            p3[p["ping"]] = p
    rows = []
    for _mid, _lt, pl in rec._packets(did, (ping.OS_MONO_PROFILE,)):
        try:
            d = ping.os_mono_profile(pl)
        except struct.error:
            continue
        p = p3.get(d["ping_number"])
        if p is None or not d["samples"]:
            continue
        rows.append((p, d))
    if not rows:
        raise SideScanError("%s: no side-scan profiles" % ln.get("name"))
    ns = max(len(d["samples"]) for _p, d in rows)
    rows = [(p, d) for p, d in rows if len(d["samples"]) == ns]
    n = len(rows)
    lat = float(cal.get("latency_s") or 0.0)
    t = np.array([p["t"] for p, _d in rows]) + lat
    raw = np.array([d["samples"] for _p, d in rows], np.uint16)
    lo = np.array([d["min_pwr_db"] for _p, d in rows], np.float32)
    hi = np.array([d["max_pwr_db"] for _p, d in rows], np.float32)
    db = (lo[:, None] + raw.astype(np.float32) / 65535.0 * (hi - lo)[:, None]).astype(np.float16)
    d0 = rows[0][1]
    r = (d0["start_mm"] + (np.arange(ns) + 0.5) * d0["length_mm"] / ns) / 1000.0
    if sound_speed:
        r = r * float(sound_speed) / (d0["sos_dmps"] / 10.0)
    knots = np.unique(np.r_[np.arange(0, ns, KNOT), ns - 1])
    rk = r[knots]
    K = len(knots)
    LEFT = np.full((n, K), np.nan)
    UP = np.full((n, K), np.nan)
    alt = np.full(n, np.nan)
    side = 0.0
    sub = traj.subset(float(t[0]), float(t[-1]), pad=2.0)
    for i, (p, _d) in enumerate(rows):
        if progress and i % 2000 == 0:
            progress(0.8 * i / n)
        if cancel and cancel():
            raise SideScanError("cancelled")
        pts = p["points"]
        if len(pts) < 5:
            continue
        m = pts[:, 2] >= float(p["thresholds"][1])            # medium power and up: the seabed
        if m.sum() < 5:
            m = np.ones(len(pts), bool)
        q = pts[m]
        tp = p["t"]
        j = int(np.clip(np.searchsorted(A["t"], tp), 0, len(A) - 1))
        v = SN._rays_fast(marine, q[:, 0], q[:, 1], np.full(len(q), p["sos"]), cal.get("roll", 0.0), sound_speed)
        flu = marine.level_vectors(v, np.repeat(np.asarray(A["up"][j], float)[None], len(q), 0),
                                   cal.get("pitch", 0.0))
        rr = np.linalg.norm(v, axis=1)
        b = np.floor(rr / BIN_M).astype(np.int64)
        nb = b.max() + 1
        cnt = np.bincount(b, minlength=nb)
        ok = cnt > 0
        lb = np.bincount(b, flu[:, 1], minlength=nb)[ok] / cnt[ok]
        ub = np.bincount(b, flu[:, 2], minlength=nb)[ok] / cnt[ok]
        rb = np.bincount(b, rr, minlength=nb)[ok] / cnt[ok]
        if len(rb) < 2:
            continue
        sgn = 1.0 if np.median(lb) >= 0 else -1.0
        side += sgn
        L = np.interp(rk, rb, lb)
        U = np.interp(rk, rb, ub)
        far = rk > rb[-1]                                     # past the soundings: flat at the last depth
        if far.any():
            dl = -ub[-1]
            L[far] = sgn * np.sqrt(np.maximum(rk[far] ** 2 - dl * dl, 0.0))
            U[far] = ub[-1]
        L[rk < rb[0]] = np.nan                                # water column
        U[rk < rb[0]] = np.nan
        LEFT[i], UP[i] = L, U
        near = np.abs(lb) < max(2.0, 0.15 * np.abs(ub).max())
        alt[i] = float(np.median(-ub[near])) if near.any() else float(-ub[0])
    ctx = GeorefContext(sub, proj, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), geoid=geoid, origin=project.store.origin)
    E = np.full((n, K), np.nan, np.float32)
    N = np.full((n, K), np.nan, np.float32)
    Z = np.full((n, K), np.nan, np.float32)
    ok = np.isfinite(LEFT) & (t[:, None] >= sub.t[0]) & (t[:, None] <= sub.t[-1])
    ii, kk = np.nonzero(ok)
    if len(ii):
        flu = np.column_stack([np.zeros(len(ii)), LEFT[ii, kk], UP[ii, kk]])
        e, nn, z = SN.place(ctx, t[ii], flu, cal.get("heading", 0.0), ln.get("lever") or [0.0, 0.0, 0.0])
        E[ii, kk], N[ii, kk], Z[ii, kk] = e, nn, z
    ang = np.degrees(np.arctan2(np.abs(LEFT), np.maximum(-UP, 1e-3))).astype(np.float16)
    # the head's own track: local position (waterfall <-> map), latitude / longitude and attitude (XTF)
    tt = np.clip(t, sub.t[0], sub.t[-1])
    hE, hN, hZ = SN.place(ctx, tt, np.zeros((n, 3)), cal.get("heading", 0.0), ln.get("lever") or [0.0, 0.0, 0.0])
    f_ = lambda v: np.interp(tt, sub.t, v)                                         # noqa: E731
    lat, lon = f_(sub.lat), f_(sub.lon)
    E0, N0 = ctx._nav(tt)[:2]
    R = ctx.R
    hlat = np.degrees(lat + (hN - N0) / R)
    hlon = np.degrees(lon + (hE - E0) / (R * np.cos(lat)))
    nav = np.column_stack([hlat, hlon, np.degrees(f_(sub.heading) + math.radians(cal.get("heading", 0.0))) % 360.0,
                           np.degrees(f_(sub.roll)), np.degrees(f_(sub.pitch))])
    np.savez(line_file(project, ln["id"]) + ".tmp.npz", version=VERSION, georef=_georef_stamp(project),
             t=t, ping=np.array([p["ping"] for p, _d in rows]), db=db, r=r.astype(np.float32), knots=knots,
             E=E, N=N, Z=Z, ang=ang, alt=alt.astype(np.float32), side=np.sign(side) or 1.0, head=did,
             raw_lo=lo, raw_hi=hi, hE=hE.astype(np.float32), hN=hN.astype(np.float32),
             hZ=hZ.astype(np.float32), nav=nav, sos=float(d0["sos_dmps"]) / 10.0)
    os.replace(line_file(project, ln["id"]) + ".tmp.npz", line_file(project, ln["id"]))
    if progress:
        progress(1.0)
    return {"pings": n, "samples": ns, "placed_pct": round(100.0 * float(np.isfinite(E).mean()), 1),
            "range_m": [round(float(r[0]), 2), round(float(r[-1]), 2)], "seconds": round(time.time() - t_start, 1),
            "side": "port" if side >= 0 else "starboard"}


# ----------------------------------------------------------------------------- reading back
class LineSS:
    """A processed side-scan line: dB, gain levelling, sample positions."""

    def __init__(self, project, lid):
        self.lid = lid
        with np.load(line_file(project, lid)) as z:
            for k in z.files:
                setattr(self, k, z[k])
        self.db = self.db.astype(np.float32)
        self.side = float(self.side)
        self._avg = None

    @property
    def n(self):
        return len(self.t)

    def sample_angle(self):
        """Angle from vertical of every sample (deg), linear between the knots."""
        k = self.knots
        a = self.ang.astype(np.float32)
        out = np.empty_like(self.db)
        for i in range(self.n):
            out[i] = np.interp(np.arange(self.db.shape[1]), k, a[i], left=np.nan, right=np.nan)
        return out

    def gain_table(self):
        """Gain levelling: the mean dB of every sample (range) over blocks of BLOCK pings, the blocks' tables
        joined linearly from ping to ping. Taking it off removes the sonar's beam pattern, spreading loss and
        the change with angle from nadir to the far range, following slow changes of height above the seabed
        and of the bottom type along the pass. -> (table pings x samples, mean dB of the line)."""
        if self._avg is None:
            ang = self.sample_angle()
            ok = np.isfinite(ang)
            D = np.where(ok, self.db, np.nan)
            n, ns = D.shape
            nb = max(1, int(round(n / BLOCK)))
            edges = np.linspace(0, n, nb + 1).astype(int)
            T = np.full((nb, ns), np.nan, np.float32)
            with np.errstate(invalid="ignore"), _quiet():
                for k in range(nb):
                    T[k] = np.nanmean(D[edges[k]:edges[k + 1]], 0)
            mean = float(np.nanmean(D)) if ok.any() else 0.0
            idx = np.arange(ns)
            for k in range(nb):                      # samples with no data in a block: from their neighbours
                g = np.isfinite(T[k])
                T[k] = np.interp(idx, idx[g], T[k][g]) if g.any() else mean
            c = (edges[:-1] + edges[1:]) / 2.0
            Tp = np.empty((n, ns), np.float32)
            if nb == 1:
                Tp[:] = T[0]
            else:
                pos = np.clip(np.interp(np.arange(n), c, np.arange(nb)), 0, nb - 1)
                k0 = np.minimum(pos.astype(int), nb - 2)
                w = (pos - k0)[:, None].astype(np.float32)
                Tp = T[k0] * (1 - w) + T[k0 + 1] * w
            self._avg = (Tp, mean, ok)
        return self._avg

    def levelled(self):
        """dB with the gain levelled (NaN in the water column)."""
        tab, mean, ok = self.gain_table()
        out = np.full(self.db.shape, np.nan, np.float32)
        out[ok] = self.db[ok] - tab[ok] + mean
        return out

    def coverage(self, block=400, window=50, thr=0.08):
        """The data limit of every ping (sample index; the number of samples when the echoes reach the end):
        past it the sonar records only its own noise (the static at far range, worst on the deeper side).
        Seabed echoes have texture that carries on from ping to ping (ripples, lines, patches); noise does not.
        So in blocks of `block` pings and windows of `window` samples, the raw dB (window mean taken off,
        smoothed over 5 samples along the range) of each ping is correlated with the ping two later: seabed
        0.1-0.8, noise 0 +- 0.02. The limit is the far end of the farthest window above thr, plus one window
        (it never cuts data that has any texture), joined from block to block and smoothed along the pass.
        (2.11.0 compared the level with the far-range level instead and cut real seabed when the sonar's own
        gain held the level flat to the end.)"""
        if getattr(self, "_cov", None) is None:
            n, ns = self.db.shape
            D = self.db.astype(np.float32)
            k = np.ones(5, np.float32) / 5
            S = np.apply_along_axis(lambda r: np.convolve(r, k, mode="same"), 1, D) if n else D
            nw = max(1, ns // window)
            step = max(1, block // 2)
            starts = list(range(0, max(1, n - block + 1), step)) or [0]
            cen, lim = [], []
            for b0 in starts:
                X = S[b0:b0 + block, :nw * window].reshape(-1, nw, window)
                if X.shape[0] < 4:
                    continue
                X = X - X.mean(0, keepdims=True)
                a, c = X[:-2], X[2:]
                num = (a * c).sum((0, 2))
                den = np.sqrt((a * a).sum((0, 2)) * (c * c).sum((0, 2))) + 1e-12
                r = num / den
                good = np.flatnonzero(r >= thr)
                if not len(good):
                    end = ns
                else:
                    end = ns if good[-1] >= nw - 2 else min(ns, (good[-1] + 2) * window)
                cen.append(b0 + min(block, n - b0) / 2.0)
                lim.append(end)
            if not lim:
                self._cov = np.full(n, ns, np.int32)
            else:
                v = np.interp(np.arange(n), cen, lim)
                kk = min(n, 301)
                if kk > 2:
                    from numpy.lib.stride_tricks import sliding_window_view
                    pad = np.r_[np.full(kk // 2, v[0]), v, np.full(kk // 2, v[-1])]
                    v = np.max(sliding_window_view(pad, kk), 1)[:n]      # keep the farther limit nearby
                self._cov = np.clip(np.round(v), 1, ns).astype(np.int32)
        return self._cov

    def coverage_m(self):
        """The median data limit as slant range (m), or None when the echoes reach the last sample."""
        c = self.coverage()
        ns = self.db.shape[1]
        m = int(np.median(c))
        return None if m >= ns - 1 else float(self.r[min(m, ns - 1)])

    def trimmed(self, data):
        """data (pings x samples) with the samples past each ping's data limit set to NaN."""
        c = self.coverage()
        out = data.copy()
        out[np.arange(data.shape[1])[None, :] >= c[:, None]] = np.nan
        return out

    def ground_ranges(self, i):
        """Horizontal distance (m) from the head to every sample of ping i (NaN in the water column)."""
        if getattr(self, "_gk", None) is None:
            self._gk = np.hypot(self.E - self.hE[:, None], self.N - self.hN[:, None]).astype(np.float32)
        g = self._gk[i]
        ok = np.isfinite(g)
        out = np.full(self.db.shape[1], np.nan, np.float32)
        if ok.sum() >= 2:
            k = self.knots[ok]
            s = np.arange(k[0], k[-1] + 1)
            out[s] = np.interp(s, k, g[ok])
        return out

    def positions(self, rows=None):
        """E, N, Z of every sample (rows: ping indices) by linear interpolation between the knots."""
        rows = np.arange(self.n) if rows is None else np.asarray(rows)
        ns = self.db.shape[1]
        idx = np.arange(ns)
        out = []
        for A in (self.E, self.N, self.Z):
            M = np.empty((len(rows), ns), np.float32)
            for j, i in enumerate(rows):
                M[j] = np.interp(idx, self.knots, A[i], left=np.nan, right=np.nan)
            out.append(M)
        return out

    def at(self, i, s):
        """Position (E, N, Z local) of sample s of ping i."""
        k = self.knots
        return tuple(float(np.interp(s, k, A[i])) for A in (self.E, self.N, self.Z))


# ----------------------------------------------------------------------------- colour palettes
# The side-scan palettes in common use (Klein / EdgeTech bronze, SonarWiz gold, sepia, ocean blue, phosphor green,
# thermal): (position 0-1, (r, g, b)) stops, weak returns first.
PALETTES = [
    ("grey", "Grey", [(0, (0, 0, 0)), (1, (255, 255, 255))]),
    ("grey_inv", "Grey inverted", [(0, (255, 255, 255)), (1, (0, 0, 0))]),
    ("bronze", "Bronze", [(0, (0, 0, 0)), (0.3, (110, 55, 15)), (0.6, (205, 127, 50)), (0.85, (255, 205, 145)),
                          (1, (255, 250, 235))]),
    ("gold", "Gold", [(0, (0, 0, 0)), (0.3, (95, 62, 0)), (0.6, (205, 150, 20)), (0.85, (255, 222, 95)),
                      (1, (255, 255, 225))]),
    ("sepia", "Sepia", [(0, (22, 13, 5)), (0.35, (112, 66, 20)), (0.7, (192, 152, 100)), (1, (245, 232, 210))]),
    ("blue", "Ocean blue", [(0, (0, 0, 12)), (0.3, (0, 42, 112)), (0.6, (40, 130, 210)), (0.85, (170, 220, 250)),
                            (1, (255, 255, 255))]),
    ("green", "Green", [(0, (0, 0, 0)), (0.35, (0, 92, 32)), (0.7, (60, 200, 92)), (1, (210, 255, 210))]),
    ("thermal", "Thermal", [(0, (0, 0, 0)), (0.25, (80, 0, 120)), (0.5, (200, 30, 40)), (0.75, (255, 150, 0)),
                            (1, (255, 255, 210))]),
]


def palette_lut(key):
    """256 x 3 uint8 colours of a palette (grey when the key is unknown)."""
    stops = dict((k, st) for k, _n, st in PALETTES).get(key) or PALETTES[0][2]
    x = np.array([p for p, _c in stops], float)
    c = np.array([c for _p, c in stops], float)
    t = np.linspace(0, 1, 256)
    return np.stack([np.interp(t, x, c[:, k]) for k in range(3)], 1).round().astype(np.uint8)


# ----------------------------------------------------------------------------- mosaic
def mosaic(project, line_ids, cell=0.25, priority="nearest", levelled=True, fill=2, progress=None, cancel=None,
           trim=True):
    """Grid the placed samples of the lines -> (grid float32 dB with NaN, x0, y0 local of the upper-left corner,
    cell). priority "nearest": in each cell the sample nearest its own track wins (the best-resolved data where
    passes or heads overlap); "average": the mean dB."""
    parts = []
    for k, lid in enumerate(line_ids):
        if cancel and cancel():
            raise SideScanError("cancelled")
        L = LineSS(project, lid)
        db = L.levelled() if levelled else L.db
        if trim:
            db = L.trimmed(db)
        E, N, _Z = L.positions()
        rng = np.broadcast_to(L.r[None, :], db.shape)
        ok = np.isfinite(E) & np.isfinite(db)
        parts.append((E[ok], N[ok], db[ok], rng[ok]))
        if progress:
            progress(0.6 * (k + 1) / len(line_ids))
    if not parts or not sum(len(p[0]) for p in parts):
        raise SideScanError("nothing to grid: process the side scan first")
    E = np.concatenate([p[0] for p in parts])
    N = np.concatenate([p[1] for p in parts])
    V = np.concatenate([p[2] for p in parts])
    R = np.concatenate([p[3] for p in parts])
    x0, y1 = math.floor(E.min() / cell) * cell, math.ceil(N.max() / cell) * cell
    nx = int(math.ceil((E.max() - x0) / cell)) + 1
    ny = int(math.ceil((y1 - N.min()) / cell)) + 1
    if nx * ny > 400_000_000:
        raise SideScanError("the mosaic would be %d x %d cells: use a larger cell" % (nx, ny))
    ix = ((E - x0) / cell).astype(np.int64)
    iy = ((y1 - N) / cell).astype(np.int64)
    cellid = iy * nx + ix
    G = np.full(nx * ny, np.nan, np.float32)
    if priority == "average":
        s = np.bincount(cellid, V, minlength=nx * ny)
        c = np.bincount(cellid, minlength=nx * ny)
        with np.errstate(invalid="ignore", divide="ignore"):
            G = (s / c).astype(np.float32)
        G[c == 0] = np.nan
    else:
        o = np.lexsort((R, cellid))                 # per cell, the smallest slant range first
        first = np.r_[True, cellid[o][1:] != cellid[o][:-1]]
        G[cellid[o][first]] = V[o][first]
    G = G.reshape(ny, nx)
    if fill:
        G = fill_gaps(G, int(fill))
    if progress:
        progress(1.0)
    return G, x0, y1, cell


def fill_gaps(G, steps):
    """Empty cells with at least 3 of their 8 neighbours filled take the neighbours' mean, steps times (so gaps
    up to 2 x steps cells wide close; open water outside the swath stays empty)."""
    G = G.copy()
    for _ in range(steps):
        v = np.isfinite(G)
        if v.all():
            break
        Z = np.where(v, G, 0.0)
        P = np.pad(Z, 1)
        C = np.pad(v.astype(np.float32), 1)
        s = np.zeros_like(Z)
        c = np.zeros_like(Z)
        h, w = G.shape
        for dy in (0, 1, 2):
            for dx in (0, 1, 2):
                if dy == 1 and dx == 1:
                    continue
                s += P[dy:dy + h, dx:dx + w]
                c += C[dy:dy + h, dx:dx + w]
        m = ~v & (c >= 3)
        if not m.any():
            break
        G[m] = s[m] / c[m]
    return G


def write_geotiff(path, G, x0, y1, cell, origin, crs, byte=False, palette="grey"):
    """The mosaic as a GeoTIFF in the project's CRS (world coordinates = local + the store's origin). byte: an
    8-bit image (2-98 % stretch, 0 = no data) with the palette as its colour table, for viewing; else float32
    dB, no data -9999 (as CloudPro's grids)."""
    from osgeo import gdal
    from .export import crs_wkt
    ny, nx = G.shape
    drv = gdal.GetDriverByName("GTiff")
    opts = ["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER"]
    tmp = path + ".tmp.tif"
    if byte:
        ok = np.isfinite(G)
        lo, hi = (np.percentile(G[ok], [2, 98]) if ok.any() else (0.0, 1.0))
        B = np.zeros(G.shape, np.uint8)
        B[ok] = np.clip(1 + (G[ok] - lo) / max(hi - lo, 1e-6) * 254, 1, 255).astype(np.uint8)
        ds = drv.Create(tmp, nx, ny, 1, gdal.GDT_Byte, opts)
        b = ds.GetRasterBand(1)
        b.SetNoDataValue(0)
        b.WriteArray(B)
        if palette and palette != "grey":
            lut = palette_lut(palette)
            ct = gdal.ColorTable()
            ct.SetColorEntry(0, (0, 0, 0, 0))
            for k in range(1, 256):
                ct.SetColorEntry(k, tuple(int(v) for v in lut[k]) + (255,))
            b.SetRasterColorTable(ct)
            b.SetRasterColorInterpretation(gdal.GCI_PaletteIndex)
    else:
        ds = drv.Create(tmp, nx, ny, 1, gdal.GDT_Float32, opts)
        ds.GetRasterBand(1).SetNoDataValue(-9999.0)
        ds.GetRasterBand(1).WriteArray(np.where(np.isfinite(G), G, -9999.0).astype(np.float32))
    ds.SetGeoTransform((x0 + origin[0], cell, 0.0, y1 + origin[1], 0.0, -cell))
    wkt = crs_wkt(crs)
    if wkt:
        ds.SetProjection(wkt)
    ds.FlushCache()
    ds = None
    os.replace(tmp, path)
    return path


# ----------------------------------------------------------------------------- contacts
def target_height(alt, r_target, shadow_len):
    """Height of a target from its acoustic shadow (flat seabed, Fish 1990): H = alt * Ls / (Rt + Ls), alt = the
    head's height above the seabed, Rt = slant range to the target's far edge, Ls = the shadow's slant length."""
    if shadow_len <= 0 or r_target + shadow_len <= 0:
        return 0.0
    return float(alt) * float(shadow_len) / (float(r_target) + float(shadow_len))


def find_shadow(L, i0, i1, s1, max_m=30.0):
    """The sample where a target's acoustic shadow ends: beyond the target's far edge s1, the levelled dB of
    pings i0..i1 (mean, smoothed over 5 samples) falls 6 dB or more under the line's median within 1 m of the
    edge; the shadow ends where it comes back. None when there is no shadow (flat target or a hole)."""
    lv = L.levelled()[max(0, i0):i1 + 1]
    with np.errstate(invalid="ignore"), _quiet():
        prof = np.nanmean(lv, 0)
        med = float(np.nanmedian(lv))
    ns = len(prof)
    dr = float(L.r[1] - L.r[0])
    end = min(ns, s1 + 1 + int(max_m / dr))
    seg = prof[s1 + 1:end]
    if len(seg) < 3:
        return None
    seg = np.convolve(np.nan_to_num(seg, nan=med), np.ones(5) / 5, mode="same")
    low = seg < med - 6.0
    near = max(3, int(1.0 / dr))                      # the shadow starts within 1 m of the target's far edge
    if not low[:near].any():
        return None
    k0 = int(np.argmax(low))
    rest = ~low[k0:]
    k = k0 + (int(np.argmax(rest)) if rest.any() else len(rest))
    return s1 + 1 + k if k - k0 > 1 else None


def contacts(project):
    return project.data.setdefault("contacts", [])


def add_contact(project, lid, i0, i1, s0, s1, shadow_s=None, name=None, remarks=""):
    """A contact boxed in the waterfall of line lid: pings i0..i1, samples s0..s1 (s1 = far edge); shadow_s = the
    sample where the shadow ends (for the height; found by find_shadow when None). -> the contact dict (also stored in project.data)."""
    L = LineSS(project, lid)
    i0, i1 = sorted((int(i0), int(i1)))
    s0, s1 = sorted((int(s0), int(s1)))
    ic, sc = (i0 + i1) // 2, (s0 + s1) // 2
    e, n, z = L.at(ic, sc)
    a0 = np.array(L.at(i0, sc)[:2])
    a1 = np.array(L.at(i1, sc)[:2])
    w0 = np.array(L.at(ic, s0)[:2])
    w1 = np.array(L.at(ic, s1)[:2])
    length = float(np.hypot(*(a1 - a0)))
    width = float(np.hypot(*(w1 - w0)))
    h = 0.0
    if shadow_s is None:
        shadow_s = find_shadow(L, i0, i1, s1)
    if shadow_s is not None and int(shadow_s) > s1:
        h = target_height(L.alt[ic], float(L.r[s1]), float(L.r[int(shadow_s)] - L.r[s1]))
    o = project.store.origin
    cs = contacts(project)
    num = 1 + max([c.get("number", 0) for c in cs] or [0])
    ln = project.lines[lid]
    c = {"number": num, "name": name or "C%03d" % num, "line": ln.get("name"), "line_id": int(lid),
         "head": int(L.head), "ping": int(L.ping[ic]), "time_utc": float(L.t[ic]),
         "e": float(e + o[0]), "n": float(n + o[1]), "z": float(z + o[2]), "slant_range_m": float(L.r[sc]),
         "length_m": round(length, 2), "width_m": round(width, 2), "height_m": round(h, 2),
         "altitude_m": round(float(L.alt[ic]), 2), "box": [i0, i1, s0, s1],
         "shadow_sample": int(shadow_s) if shadow_s is not None else None, "remarks": remarks,
         "crs": project.crs}
    cs.append(c)
    return c


def export_contacts_csv(project, path):
    cols = ("name", "e", "n", "z", "length_m", "width_m", "height_m", "slant_range_m", "altitude_m", "time_utc",
            "line", "head", "ping", "remarks")
    import csv
    import datetime as _dt
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for c in contacts(project):
            row = []
            for k in cols:
                v = c.get(k)
                if k == "time_utc" and v is not None:
                    v = _dt.datetime.fromtimestamp(v, _dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                elif isinstance(v, float):
                    v = "%.3f" % v
                row.append(v)
            w.writerow(row)
    return path


def export_contacts_gpkg(project, path):
    """Contacts as a point GeoPackage in the project CRS (OGR)."""
    from osgeo import ogr, osr
    if os.path.exists(path):
        os.remove(path)
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(path)
    from .export import crs_wkt
    wkt = crs_wkt(project.crs)
    sr = osr.SpatialReference()
    if wkt:
        sr.ImportFromWkt(wkt)
    lyr = ds.CreateLayer("contacts", sr, ogr.wkbPoint25D)
    fields = [("name", ogr.OFTString), ("length_m", ogr.OFTReal), ("width_m", ogr.OFTReal),
              ("height_m", ogr.OFTReal), ("depth_z", ogr.OFTReal), ("slant_rng", ogr.OFTReal),
              ("altitude", ogr.OFTReal), ("time_utc", ogr.OFTString), ("line", ogr.OFTString),
              ("head", ogr.OFTInteger), ("ping", ogr.OFTInteger), ("remarks", ogr.OFTString)]
    for nm, tp in fields:
        lyr.CreateField(ogr.FieldDefn(nm, tp))
    import datetime as _dt
    for c in contacts(project):
        f = ogr.Feature(lyr.GetLayerDefn())
        f.SetField("name", c["name"])
        for a, b in (("length_m", "length_m"), ("width_m", "width_m"), ("height_m", "height_m"),
                     ("depth_z", "z"), ("slant_rng", "slant_range_m"), ("altitude", "altitude_m")):
            f.SetField(a, float(c.get(b) or 0.0))
        f.SetField("time_utc", _dt.datetime.fromtimestamp(c["time_utc"], _dt.timezone.utc).isoformat())
        f.SetField("line", c.get("line") or "")
        f.SetField("head", int(c.get("head") or 0))
        f.SetField("ping", int(c.get("ping") or 0))
        f.SetField("remarks", c.get("remarks") or "")
        g = ogr.Geometry(ogr.wkbPoint25D)
        g.AddPoint(c["e"], c["n"], c["z"])
        f.SetGeometry(g)
        lyr.CreateFeature(f)
    ds = None
    return path


# ----------------------------------------------------------------------------- XTF
XTF_DB0 = -50.0                 # XTF samples: 16 bit, linear in dB: dB = XTF_DB0 + value / XTF_DB_SCALE
XTF_DB_SCALE = 500.0


def _pad_name(s, n):
    b = str(s).encode("ascii", "replace")[:n - 1]
    return b + b"\0" * (n - len(b))


def write_xtf(project, line_ids, path, progress=None, cancel=None):
    """The side scan of one pass (its port and starboard lines) as an XTF file (Triton eXtended Triton Format,
    the side-scan exchange format): channel 1 port, channel 2 starboard, one record per ping of the port head
    with the starboard ping nearest in time. Samples 16 bit linear in dB (dB = -50 + value / 500), sample 0
    at the transducer (the gap to the sonar's first range filled with 0), raw dB (not gain levelled), so the
    reading program applies its own gains. Navigation latitude / longitude (WGS84 degrees) of the head,
    heading, roll, pitch, altitude above the seabed (from the 3D soundings) and the sound speed. -> pings
    written."""
    import datetime as _dt
    Ls = [LineSS(project, lid) for lid in line_ids]
    port = [L for L in Ls if L.side > 0]
    stbd = [L for L in Ls if L.side < 0]
    if not port and not stbd:
        raise SideScanError("no side-scan lines")
    A = port[0] if port else stbd[0]
    B = stbd[0] if (port and stbd) else None
    chans = [A, B] if B is not None else [A]
    dr = float(A.r[1] - A.r[0])
    pad = int(round(float(A.r[0] - dr / 2) / dr))
    ns = pad + A.db.shape[1]
    # file header
    H = bytearray(1024)
    H[0] = 0x7B
    H[1] = 1
    H[2:10] = _pad_name("CloudPro", 8)
    H[10:18] = _pad_name("2", 8)
    H[18:34] = _pad_name("Omniscan 3D", 16)
    struct.pack_into("<H", H, 34, 0)
    H[36:100] = _pad_name("Cerulean Omniscan 3D side scan, dB = -50 + value/500", 64)
    H[100:164] = _pad_name(os.path.basename(path), 64)
    struct.pack_into("<HHH", H, 164, 3, len(chans), 0)                  # nav units: lat / long
    for k, L in enumerate(chans):
        o = 256 + 128 * k
        struct.pack_into("<BBHHH", H, o, 1 if L.side > 0 else 2, k, 0, 1, 2)   # type 1 port, 2 starboard; unipolar
        H[o + 12:o + 28] = _pad_name("Port" if L.side > 0 else "Starboard", 16)
        struct.pack_into("<f", H, o + 28, 1.0)
        struct.pack_into("<f", H, o + 32, 0.0)
        struct.pack_into("<f", H, o + 44, 90.0)
        struct.pack_into("<H", H, o + 72, ns)
        H[o + 74] = 0                                                    # sample format: legacy (bytes / sample)
    tb = B.t if B is not None else None
    with open(path + ".tmp", "wb") as f:
        f.write(H)
        written = 0
        for i in range(A.n):
            if cancel and cancel() and i % 500 == 0:
                raise SideScanError("cancelled: nothing was written")
            if progress and i % 2000 == 0:
                progress(i / A.n)
            rows = [(A, i)]
            if B is not None:
                j = int(np.clip(np.searchsorted(tb, A.t[i]), 1, B.n - 1))
                j = j - 1 if abs(tb[j - 1] - A.t[i]) < abs(tb[j] - A.t[i]) else j
                rows.append((B, j if abs(tb[j] - A.t[i]) < 0.5 else None))
            body = bytearray()
            for k, (L, j) in enumerate(rows):
                C = bytearray(64)
                v = np.zeros(ns, np.uint16)
                if j is not None:
                    v[pad:pad + L.db.shape[1]] = np.clip((L.db[j] - XTF_DB0) * XTF_DB_SCALE, 0, 65535).astype(
                        np.uint16)
                rng = ns * dr
                sos = float(L.sos)
                struct.pack_into("<HHffff", C, 0, k, 0, rng, float(np.sqrt(max(rng * rng - L.alt[j or 0] ** 2, 0)))
                                 if np.isfinite(L.alt[j or 0]) else rng, 0.0, 2 * rng / sos)
                struct.pack_into("<I", C, 42, ns)
                body += C + v.tobytes()
            P = bytearray(256)
            tot = 256 + len(body)
            tot64 = (tot + 63) // 64 * 64
            struct.pack_into("<HBBHHHI", P, 0, 0xFACE, 0, 0, len(rows), 0, 0, tot64)
            d = _dt.datetime.fromtimestamp(float(A.t[i]), _dt.timezone.utc)
            struct.pack_into("<HBBBBBBH", P, 14, d.year, d.month, d.day, d.hour, d.minute, d.second,
                             d.microsecond // 10000, d.timetuple().tm_yday)
            struct.pack_into("<II", P, 24, i, int(A.ping[i]))
            struct.pack_into("<f", P, 32, float(A.sos) / 2.0)          # XTF keeps half the sound speed
            lat, lon, hd, rl, pt = (float(x) for x in A.nav[i])
            struct.pack_into("<f", P, 124, hd)
            struct.pack_into("<dd", P, 128, lat, lon)
            struct.pack_into("<BBBB", P, 148, d.hour, d.minute, d.second, d.microsecond // 10000)
            struct.pack_into("<dd", P, 160, lat, lon)
            alt = float(A.alt[i]) if np.isfinite(A.alt[i]) else 0.0
            struct.pack_into("<fffff", P, 192, 0.0, alt, 0.0, pt, rl)
            struct.pack_into("<f", P, 212, hd)
            struct.pack_into("<I", P, 232, d.microsecond // 1000)
            f.write(P + body + b"\0" * (tot64 - tot))
            written += 1
    os.replace(path + ".tmp", path)
    if progress:
        progress(1.0)
    return written
