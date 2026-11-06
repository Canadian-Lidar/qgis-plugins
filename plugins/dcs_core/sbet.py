"""SBET trajectories and SMRMSG accuracy files, as PosPro publishes them.

SBET record: 17 little-endian doubles - time (GPS seconds of week), latitude,
longitude (rad), height (m, ellipsoidal), x/y/z velocity, roll, pitch,
platform heading, wander angle (rad), x/y/z acceleration, x/y/z angular rate.
True heading = platform heading - wander angle.
SMRMSG record: 10 doubles - time, north/east/down position SD (m), north/east/down
velocity SD (m/s), roll/pitch/heading SD (arc minutes).
UTC = GPS week start + seconds of week - leap seconds (18 since 2017).

Every function works in UTC Unix seconds; each plugin builds its own
trajectory object from what read() returns.
"""
import datetime as dt
import os
import re

import numpy as np

from .timeutil import LEAP

SBET = np.dtype([("t", "<f8"), ("lat", "<f8"), ("lon", "<f8"), ("h", "<f8"),
                 ("vx", "<f8"), ("vy", "<f8"), ("vz", "<f8"),
                 ("roll", "<f8"), ("pitch", "<f8"), ("heading", "<f8"), ("wander", "<f8"),
                 ("ax", "<f8"), ("ay", "<f8"), ("az", "<f8"), ("wx", "<f8"), ("wy", "<f8"), ("wz", "<f8")])
SMRMSG = np.dtype([("t", "<f8"), ("sd_n", "<f8"), ("sd_e", "<f8"), ("sd_d", "<f8"),
                   ("sd_vn", "<f8"), ("sd_ve", "<f8"), ("sd_vd", "<f8"),
                   ("sd_roll", "<f8"), ("sd_pitch", "<f8"), ("sd_heading", "<f8")])
GPS_EPOCH_UNIX = 315964800.0          # 1980-01-06 00:00 UTC
WEEK = 604800.0


# ----------------------------------------------------------------------------- GPS weeks
def date_from_name(path):
    """sbet_YYYYMMDD.out -> date, or None."""
    m = re.search(r"(20\d{2})(\d{2})(\d{2})", os.path.basename(path))
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def week_start(week):
    """Start of GPS week number `week`, as the value seconds of week are added to."""
    return GPS_EPOCH_UNIX + int(week) * WEEK


def week_start_for_date(date):
    """Start of the GPS week holding this calendar date."""
    days = (date - dt.date(1980, 1, 6)).days
    return GPS_EPOCH_UNIX + (days // 7) * 7 * 86400.0


def week_start_for(unix_t, leap=LEAP):
    """Start of the GPS week holding the UTC instant unix_t."""
    return GPS_EPOCH_UNIX + np.floor((unix_t + leap - GPS_EPOCH_UNIX) / WEEK) * WEEK


def _unroll(sow):
    """Seconds of week -> seconds since the first week's start, across week rollovers."""
    s = np.array(sow, float)
    for j in np.nonzero(np.diff(s) < -WEEK / 2)[0]:
        s[j + 1:] += WEEK
    return s


def resolve_week_start(sow, leap=LEAP, week=None, start=None, near_unix=None, date=None, path=None):
    """The week start for these seconds of week, from the first of: start, week (PosPro's handover
    gives it), near_unix (a UTC time inside the data, such as a .clraw start: the nearest week wins,
    which also fixes a wrong or missing date), date, or the date in the file name."""
    if start is not None:
        return float(start)
    if week is not None:
        return week_start(week)
    if near_unix is not None:
        base = week_start_for(near_unix, leap)
        mid = float(np.median(sow)) if len(sow) else 0.0
        return min((base - WEEK, base, base + WEEK), key=lambda b: abs(b + mid - leap - near_unix))
    date = date or (date_from_name(path) if path else None)
    if date is not None:
        return week_start_for_date(date)
    raise ValueError("SBET date unknown: name the file sbet_YYYYMMDD.out or enter the date")


# ----------------------------------------------------------------------------- reading
def read(path, leap=LEAP, week=None, start=None, near_unix=None, date=None):
    """SBET -> (t UTC s, records as a structured SBET array, week start used).
    The week comes from resolve_week_start(); a week rollover inside the file is handled."""
    a = np.fromfile(path, SBET, count=os.path.getsize(path) // SBET.itemsize)     # one copy in memory
    if not len(a):
        raise ValueError("%s: empty SBET" % os.path.basename(path))
    sow = _unroll(a["t"])
    ws = resolve_week_start(sow, leap, week, start, near_unix, date, path)
    return ws + sow - leap, a, ws


def true_heading(a):
    """Platform heading minus wander angle (rad)."""
    return a["heading"] - a["wander"]


def smrmsg_path(sbet_path):
    """The SMRMSG next to an SBET (sbet -> smrmsg in the file name), or None."""
    d, fn = os.path.split(sbet_path)
    c = re.sub(r"(?i)sbet", "smrmsg", fn)
    p = os.path.join(d, c)
    return p if c != fn and os.path.isfile(p) else None


def read_smrmsg(path, week_start_unix, leap=LEAP):
    """SMRMSG -> (t UTC s, records as a structured SMRMSG array); empty arrays for an empty file."""
    a = np.fromfile(path, SMRMSG, count=os.path.getsize(path) // SMRMSG.itemsize)
    return week_start_unix + _unroll(a["t"]) - leap, a


# ----------------------------------------------------------------------------- writing
def write(path, t, lat, lon, h, roll, pitch, heading, leap=LEAP, start=None):
    """Write a trajectory (UTC s, rad, m) as SBET; the week is the first sample's unless start is given."""
    t = np.asarray(t, float)
    ws = week_start_for(t[0], leap) if start is None else start
    a = np.zeros(len(t), SBET)
    a["t"] = t + leap - ws
    a["lat"], a["lon"], a["h"] = lat, lon, h
    a["roll"], a["pitch"] = roll, pitch
    a["heading"] = np.mod(np.asarray(heading, float) + np.pi, 2 * np.pi) - np.pi
    a.tofile(path)
    return ws
