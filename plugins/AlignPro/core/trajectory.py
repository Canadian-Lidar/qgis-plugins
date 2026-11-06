"""Trajectories: Certus real-time packet 20 or a post-processed SBET (PosPro's trajectory.json, or a file)."""
import os

import numpy as np
from dcs_core import sbet

GPS_EPOCH_UNIX = sbet.GPS_EPOCH_UNIX
LEAP_SECONDS = sbet.LEAP


class Trajectory:
    """Time-sorted samples. Angles and lat/lon in radians, heading unwrapped."""

    def __init__(self, t, lat, lon, h, roll, pitch, heading, source="",
                 sd_pos=None, sd_att=None, fix=None):
        o = np.argsort(t, kind="stable")
        t = np.asarray(t, float)[o]
        keep = np.concatenate([[True], np.diff(t) > 0]) if len(t) else np.zeros(0, bool)
        sel = o[keep]
        self.t = np.asarray(t)[keep]
        self.lat, self.lon, self.h = (np.asarray(x, float)[sel] for x in (lat, lon, h))
        self.roll, self.pitch = np.asarray(roll, float)[sel], np.asarray(pitch, float)[sel]
        self.heading = np.unwrap(np.asarray(heading, float)[sel])
        self.sd_pos = None if sd_pos is None else np.asarray(sd_pos, float)[sel]
        self.sd_att = None if sd_att is None else np.asarray(sd_att, float)[sel]
        self.fix = None if fix is None else np.asarray(fix)[sel]
        self.source = source
        self.segments = None            # [(t0, t1)] when merged from several files: no data between them

    def __len__(self):
        return len(self.t)

    @property
    def span(self):
        return (float(self.t[0]), float(self.t[-1])) if len(self.t) else (0.0, 0.0)

    def covers(self, t0, t1):
        """True when [t0, t1] lies inside the trajectory (inside one piece of a merged trajectory)."""
        if len(self.t) < 2:
            return False
        for a, b in (getattr(self, "segments", None) or [self.span]):
            if a <= t0 and b >= t1:
                return True
        return False

    def inside(self, t):
        """Mask of the times that have trajectory data: never the gap between two merged files, nor a gap
        longer than MAX_GAP_S inside one file (e.g. the recording holes of mapper 2.9.0 captures)."""
        t = np.asarray(t, float)
        m = np.zeros(len(t), bool)
        if len(self.t) < 2:
            return m
        for a, b in (getattr(self, "segments", None) or [self.span]):
            m |= (t >= a) & (t <= b)
        big = np.diff(self.t) > MAX_GAP_S
        if big.any() and m.any():
            k = np.searchsorted(self.t, t, side="right") - 1
            ok = (k >= 0) & (k < len(big))
            hole = np.zeros(len(t), bool)
            hole[ok] = big[k[ok]] & (t[ok] > self.t[k[ok]])
            m &= ~hole
        return m

    def gaps(self):
        """[(t0, t1)] of the gaps longer than MAX_GAP_S inside the data (not bridged by inside())."""
        if len(self.t) < 2:
            return []
        i = np.nonzero(np.diff(self.t) > MAX_GAP_S)[0]
        return [(float(self.t[k]), float(self.t[k + 1])) for k in i]

    def rate(self):
        return 1.0 / float(np.median(np.diff(self.t))) if len(self.t) > 2 else 0.0

    def interp(self, t, latency=0.0):
        """-> lat, lon, h, roll, pitch, heading at times t (linear; heading unwrapped)."""
        tt = np.asarray(t, float) - latency
        f = lambda y: np.interp(tt, self.t, y)  # noqa: E731
        return f(self.lat), f(self.lon), f(self.h), f(self.roll), f(self.pitch), f(self.heading)

    def window(self, t0, t1, pad=1.0):
        m = (self.t >= t0 - pad) & (self.t <= t1 + pad)
        tr = Trajectory.__new__(Trajectory)
        for k in ("t", "lat", "lon", "h", "roll", "pitch", "heading"):
            setattr(tr, k, getattr(self, k)[m])
        for k in ("sd_pos", "sd_att", "fix"):
            v = getattr(self, k)
            setattr(tr, k, None if v is None else v[m])
        tr.source = self.source
        segs = getattr(self, "segments", None)
        tr.segments = None if not segs else [(max(a, t0 - pad), min(b, t1 + pad)) for a, b in segs
                                             if b >= t0 - pad and a <= t1 + pad] or None
        return tr


def from_nav(nav, source="Certus real-time (packet 20)"):
    ok = nav["time_init"].astype(bool) & nav["nav_init"].astype(bool) if len(nav["t"]) else []
    if len(nav["t"]) and not np.any(ok):
        ok = np.ones(len(nav["t"]), bool)
    sd = np.stack([nav["sd_n"], nav["sd_e"], nav["sd_h"]], -1)[ok]
    sd_att = None
    if len(nav["sd_t"]):
        sd_att = np.stack([np.interp(nav["t"][ok], nav["sd_t"], nav[k]) for k in
                           ("sd_roll", "sd_pitch", "sd_heading")], -1)
    return Trajectory(nav["t"][ok], nav["lat"][ok], nav["lon"][ok], nav["h"][ok],
                      nav["roll"][ok], nav["pitch"][ok], nav["heading"][ok], source,
                      sd_pos=sd, sd_att=sd_att, fix=nav["fix"][ok])


MAX_GAP_S = 1.0     # a gap longer than this inside one file is not bridged either


def merge(trajs, source=""):
    """One trajectory from several (one SBET per capture, as the MapPro format recorded them).

    Pieces are kept in time order; where two overlap, the later one starts where the earlier ends.
    The time between pieces has no data: segments lists the pieces so the georeferencing never
    interpolates across a gap (inside(), covers())."""
    trajs = sorted([t for t in trajs if len(t) > 1], key=lambda t: t.span[0])
    if not trajs:
        raise ValueError("No trajectory samples in the SBET files")
    if len(trajs) == 1:
        return trajs[0]
    parts, segs, end = [], [], -np.inf
    for tr in trajs:
        m = tr.t > end
        if m.sum() < 2:
            continue
        idx = np.nonzero(m)[0]
        parts.append((tr, idx))
        segs.append((float(tr.t[idx[0]]), float(tr.t[idx[-1]])))
        end = segs[-1][1]
    cat = lambda k: np.concatenate([getattr(tr, k)[ix] for tr, ix in parts])  # noqa: E731
    opt = lambda k: None if any(getattr(tr, k) is None for tr, _ in parts) else cat(k)  # noqa: E731
    out = Trajectory(cat("t"), cat("lat"), cat("lon"), cat("h"), cat("roll"), cat("pitch"), cat("heading"),
                     source or " + ".join(tr.source for tr, _ in parts),
                     sd_pos=opt("sd_pos"), sd_att=opt("sd_att"), fix=opt("fix"))
    out.segments = segs
    return out


def sbet_date_from_name(path):
    return sbet.date_from_name(path)


def gps_week_start_unix(date):
    return sbet.week_start_for_date(date)


def read_sbet(path, date=None, leap=LEAP_SECONDS, near_unix=None, week=None):
    """SBET (17 doubles per record), read by dcs_core.sbet. Time is GPS seconds of week; converted to UTC.
    date: any UTC date in the trajectory's GPS week (default from the file name sbet_YYYYMMDD).
    near_unix: a UTC time known to be inside the data (e.g. a .clraw start) - the week is then
    chosen so the trajectory lands nearest to it, which also fixes a wrong or missing date.
    week: the GPS week, when known (PosPro's trajectory.json gives it); takes precedence.
    The SMRMSG beside it (sbet -> smrmsg in the name) gives the position and attitude SD."""
    t, a, wk = sbet.read(path, leap, week=week, near_unix=near_unix, date=date)
    sd_pos = sd_att = None
    sp = sbet.smrmsg_path(path)
    if sp is not None:
        ts, sm = sbet.read_smrmsg(sp, wk, leap)
        if len(sm):
            sd_pos = np.stack([np.interp(t, ts, sm[k]) for k in ("sd_n", "sd_e", "sd_d")], -1)
            sd_att = np.stack([np.radians(np.interp(t, ts, sm[k]) / 60.0)
                               for k in ("sd_roll", "sd_pitch", "sd_heading")], -1)
    return Trajectory(t, a["lat"], a["lon"], a["h"], a["roll"], a["pitch"], sbet.true_heading(a),
                      "SBET %s" % os.path.basename(path), sd_pos=sd_pos, sd_att=sd_att)


def write_sbet(path, traj, leap=LEAP_SECONDS):
    """Write a trajectory as SBET (used by the simulator and for round-trip tests)."""
    sbet.write(path, traj.t, traj.lat, traj.lon, traj.h, traj.roll, traj.pitch, traj.heading, leap)
