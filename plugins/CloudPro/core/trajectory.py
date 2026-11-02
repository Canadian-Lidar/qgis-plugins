"""Trajectories: PosPro SBET (+ optional SMRMSG) and the
Certus real-time packet 20 track from the .clraw itself.

SBET record: 17 little-endian doubles - time (GPS seconds of week), latitude,
longitude (rad), height (m, ellipsoidal), x/y/z velocity, roll, pitch,
platform heading, wander angle (rad), x/y/z acceleration, x/y/z angular rate.
True heading = platform heading - wander angle.  UTC = GPS week start + SOW
- leap seconds (18 since 2017).
"""
import datetime as _dt
import os
import re

import numpy as np

SBET = np.dtype([("t", "<f8"), ("lat", "<f8"), ("lon", "<f8"), ("h", "<f8"),
                 ("vx", "<f8"), ("vy", "<f8"), ("vz", "<f8"),
                 ("roll", "<f8"), ("pitch", "<f8"), ("heading", "<f8"), ("wander", "<f8"),
                 ("ax", "<f8"), ("ay", "<f8"), ("az", "<f8"), ("wx", "<f8"), ("wy", "<f8"), ("wz", "<f8")])
SMRMSG = np.dtype([("t", "<f8"), ("sd_n", "<f8"), ("sd_e", "<f8"), ("sd_d", "<f8"),
                   ("sd_vn", "<f8"), ("sd_ve", "<f8"), ("sd_vd", "<f8"),
                   ("sd_roll", "<f8"), ("sd_pitch", "<f8"), ("sd_heading", "<f8")])
GPS_EPOCH = _dt.datetime(1980, 1, 6, tzinfo=_dt.timezone.utc)
LEAP = 18


class Trajectory:
    """Time-ordered navigation solution at the INS reference point.

    t (UTC Unix s), lat, lon (rad), h (m ellipsoidal), roll, pitch, heading
    (rad, vehicle frame: roll + starboard down, pitch + bow up, heading +
    clockwise from north), sd_h/sd_v (m, optional), source name.
    """

    def __init__(self, t, lat, lon, h, roll, pitch, heading, sd_h=None, sd_v=None, source=""):
        o = np.argsort(t, kind="stable")
        t = np.asarray(t, float)[o]
        keep = np.concatenate([[True], np.diff(t) > 0])
        sel = o[keep]
        self.t = np.asarray(t)[keep]
        self.lat = np.asarray(lat, float)[sel]
        self.lon = np.asarray(lon, float)[sel]
        self.h = np.asarray(h, float)[sel]
        self.roll = np.asarray(roll, float)[sel]
        self.pitch = np.asarray(pitch, float)[sel]
        self.heading = np.unwrap(np.asarray(heading, float)[sel])
        self.sd_h = None if sd_h is None else np.asarray(sd_h, float)[sel]
        self.sd_v = None if sd_v is None else np.asarray(sd_v, float)[sel]
        self.source = source

    def __len__(self):
        return len(self.t)

    @property
    def span(self):
        return (float(self.t[0]), float(self.t[-1])) if len(self.t) else (0.0, 0.0)

    def covers(self, t0, t1):
        a, b = self.span
        return a <= t0 and b >= t1

    def interp(self, t):
        """Interpolate (lat, lon, h, roll, pitch, heading) at times t."""
        tt = self.t
        f = lambda v: np.interp(t, tt, v)
        return f(self.lat), f(self.lon), f(self.h), f(self.roll), f(self.pitch), f(self.heading)

    def subset(self, t0, t1, pad=1.0):
        i0 = max(0, np.searchsorted(self.t, t0 - pad) - 1)
        i1 = min(len(self.t), np.searchsorted(self.t, t1 + pad) + 1)
        s = slice(i0, i1)
        return Trajectory(self.t[s], self.lat[s], self.lon[s], self.h[s], self.roll[s], self.pitch[s],
                          self.heading[s], None if self.sd_h is None else self.sd_h[s],
                          None if self.sd_v is None else self.sd_v[s], self.source)


def gps_week_start_from_name(path):
    """sbet_YYYYMMDD.out -> UTC Unix time of that GPS week's start (Sunday 00:00 GPS)."""
    m = re.search(r"(\d{4})(\d{2})(\d{2})", os.path.basename(path))
    if not m:
        return None
    d = _dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=_dt.timezone.utc)
    return week_start_for(d.timestamp())


def week_start_for(unix_t):
    """GPS-week start (as UTC Unix seconds of the GPS-time instant, i.e. the
    value to which SOW is added before removing leap seconds)."""
    gps = unix_t - GPS_EPOCH.timestamp() + LEAP
    week = int(gps // 604800)
    return GPS_EPOCH.timestamp() + week * 604800


def read_sbet(path, week_start=None, leap=LEAP, smrmsg=None):
    a = np.fromfile(path, SBET)
    if len(a) == 0:
        raise ValueError("%s: empty SBET" % path)
    if week_start is None:
        week_start = gps_week_start_from_name(path)
    if week_start is None:
        raise ValueError("SBET start date unknown: name it sbet_YYYYMMDD.out or give the date")
    t = week_start + a["t"] - leap
    # week rollover inside the file
    jumps = np.nonzero(np.diff(a["t"]) < -302400)[0]
    for j in jumps:
        t[j + 1:] += 604800
    sd_h = sd_v = None
    if smrmsg is None:
        cand = re.sub(r"sbet", "smrmsg", path, flags=re.I)
        if cand != path and os.path.exists(cand):
            smrmsg = cand
    if smrmsg and os.path.exists(smrmsg):
        m = np.fromfile(smrmsg, SMRMSG)
        if len(m):
            tm = week_start + m["t"] - leap
            sd_h = np.interp(t, tm, np.hypot(m["sd_n"], m["sd_e"]))
            sd_v = np.interp(t, tm, m["sd_d"])
    return Trajectory(t, a["lat"], a["lon"], a["h"], a["roll"], a["pitch"], a["heading"] - a["wander"],
                      sd_h, sd_v, source="SBET " + os.path.basename(path))


def write_sbet(path, traj, week_start=None, leap=LEAP):
    """Write a Trajectory as SBET (used by the simulator and for exports)."""
    if week_start is None:
        week_start = week_start_for(traj.t[0])
    a = np.zeros(len(traj), SBET)
    a["t"] = traj.t - week_start + leap
    a["lat"], a["lon"], a["h"] = traj.lat, traj.lon, traj.h
    a["roll"], a["pitch"] = traj.roll, traj.pitch
    a["heading"] = np.mod(traj.heading + np.pi, 2 * np.pi) - np.pi
    a.tofile(path)


def from_nav_states(states, t):
    """Trajectory from decoded ANPP packet 20 records (Certus real time)."""
    sd = np.hypot(states["sd_lat"], states["sd_lon"]).astype(float)
    return Trajectory(t, states["lat"], states["lon"], states["h"], states["roll"].astype(float),
                      states["pitch"].astype(float), states["heading"].astype(float), sd,
                      states["sd_h"].astype(float), source="Certus real-time (packet 20)")
