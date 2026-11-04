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

import numpy as np
from dcs_core import sbet


GPS_EPOCH = _dt.datetime(1980, 1, 6, tzinfo=_dt.timezone.utc)
LEAP = sbet.LEAP
SBET, SMRMSG = sbet.SBET, sbet.SMRMSG


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
    d = sbet.date_from_name(path)
    return sbet.week_start_for_date(d) if d else None


def week_start_for(unix_t):
    """GPS-week start (as UTC Unix seconds of the GPS-time instant, i.e. the
    value to which SOW is added before removing leap seconds)."""
    return float(sbet.week_start_for(unix_t, LEAP))


def read_sbet(path, week_start=None, leap=LEAP, smrmsg=None):
    """SBET read by dcs_core.sbet (week from week_start, else the file name; week rollover handled),
    with the SMRMSG (given, or beside it) for the horizontal and vertical SD."""
    t, a, ws = sbet.read(path, leap, start=week_start)
    sd_h = sd_v = None
    if smrmsg is None:
        smrmsg = sbet.smrmsg_path(path)
    if smrmsg and os.path.exists(smrmsg):
        tm, m = sbet.read_smrmsg(smrmsg, ws, leap)
        if len(m):
            sd_h = np.interp(t, tm, np.hypot(m["sd_n"], m["sd_e"]))
            sd_v = np.interp(t, tm, m["sd_d"])
    return Trajectory(t, a["lat"], a["lon"], a["h"], a["roll"], a["pitch"], sbet.true_heading(a),
                      sd_h, sd_v, source="SBET " + os.path.basename(path))


def write_sbet(path, traj, week_start=None, leap=LEAP):
    """Write a Trajectory as SBET (used by the simulator and for exports)."""
    sbet.write(path, traj.t, traj.lat, traj.lon, traj.h, traj.roll, traj.pitch, traj.heading, leap, week_start)


def from_nav_states(states, t):
    """Trajectory from decoded ANPP packet 20 records (Certus real time)."""
    sd = np.hypot(states["sd_lat"], states["sd_lon"]).astype(float)
    return Trajectory(t, states["lat"], states["lon"], states["h"], states["roll"].astype(float),
                      states["pitch"].astype(float), states["heading"].astype(float), sd,
                      states["sd_h"].astype(float), source="Certus real-time (packet 20)")
