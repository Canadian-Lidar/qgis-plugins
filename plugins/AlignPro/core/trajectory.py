"""Trajectories: Certus real-time packet 20 or a post-processed SBET (PosPro's trajectory.json, or a file)."""
import datetime as dt
import os
import re

import numpy as np

GPS_EPOCH_UNIX = 315964800.0
LEAP_SECONDS = 18


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

    def __len__(self):
        return len(self.t)

    @property
    def span(self):
        return (float(self.t[0]), float(self.t[-1])) if len(self.t) else (0.0, 0.0)

    def covers(self, t0, t1):
        a, b = self.span
        return len(self.t) > 1 and a <= t0 and b >= t1

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


def sbet_date_from_name(path):
    m = re.search(r"(20\d{2})(\d{2})(\d{2})", os.path.basename(path))
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def gps_week_start_unix(date):
    days = (date - dt.date(1980, 1, 6)).days
    return GPS_EPOCH_UNIX + (days // 7) * 7 * 86400.0


def read_sbet(path, date=None, leap=LEAP_SECONDS, near_unix=None, week=None):
    """SBET (17 doubles per record). Time is GPS seconds of week; converted to UTC.
    date: any UTC date in the trajectory's GPS week (default from the file name sbet_YYYYMMDD).
    near_unix: a UTC time known to be inside the data (e.g. a .clraw start) - the week is then
    chosen so the trajectory lands nearest to it, which also fixes a wrong or missing date.
    week: the GPS week, when known (PosPro's trajectory.json gives it); takes precedence."""
    a = np.fromfile(path, "<f8")
    a = a[: len(a) // 17 * 17].reshape(-1, 17)
    if not len(a):
        raise ValueError("%s: empty SBET" % os.path.basename(path))
    date = date or sbet_date_from_name(path)
    if week is not None:                       # given by the trajectory handover (PosPro)
        wk = GPS_EPOCH_UNIX + int(week) * 604800.0
    elif near_unix is not None:
        base = GPS_EPOCH_UNIX + np.floor((near_unix + leap - GPS_EPOCH_UNIX) / 604800.0) * 604800.0
        cands = [base - 604800.0, base, base + 604800.0]
        mid = np.median(a[:, 0])
        wk = min(cands, key=lambda b: abs(b + mid - leap - near_unix))
    elif date is not None:
        wk = gps_week_start_unix(date)
    else:
        raise ValueError("SBET date unknown: name the file sbet_YYYYMMDD.out or enter the date")
    t = wk + a[:, 0] - leap
    heading = a[:, 9] - a[:, 10]          # platform heading minus wander angle = true heading
    sd_pos = sd_att = None
    sm = _smrmsg_for(path)
    if sm is not None and len(sm):
        ts = wk + sm[:, 0] - leap
        sd_pos = np.stack([np.interp(t, ts, sm[:, k]) for k in (1, 2, 3)], -1)
        sd_att = np.stack([np.radians(np.interp(t, ts, sm[:, k]) / 60.0) for k in (7, 8, 9)], -1)
    return Trajectory(t, a[:, 1], a[:, 2], a[:, 3], a[:, 7], a[:, 8], heading,
                      "SBET %s" % os.path.basename(path), sd_pos=sd_pos, sd_att=sd_att)


def _smrmsg_for(path):
    d, fn = os.path.split(path)
    cands = [re.sub(r"(?i)sbet", "smrmsg", fn), re.sub(r"(?i)sbet", "smrmsg", fn).replace(".out", ".out")]
    for c in cands:
        p = os.path.join(d, c)
        if p != path and os.path.isfile(p):
            a = np.fromfile(p, "<f8")
            return a[: len(a) // 10 * 10].reshape(-1, 10)
    return None


def write_sbet(path, traj, leap=LEAP_SECONDS):
    """Write a trajectory as SBET (used by the simulator and for round-trip tests)."""
    wk = GPS_EPOCH_UNIX + np.floor((traj.t[0] + leap - GPS_EPOCH_UNIX) / 604800.0) * 604800.0
    n = len(traj.t)
    a = np.zeros((n, 17))
    a[:, 0] = traj.t + leap - wk
    a[:, 1], a[:, 2], a[:, 3] = traj.lat, traj.lon, traj.h
    a[:, 7], a[:, 8] = traj.roll, traj.pitch
    a[:, 9] = (traj.heading + np.pi) % (2 * np.pi) - np.pi
    a.astype("<f8").tofile(path)
