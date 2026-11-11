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


# ----------------------------------------------------------------------------- recording holes (API 14)
HOLE_S = 0.1                # s: a step between records longer than this is a hole (records are 5 ms apart at 200 Hz)
HOLES_MATTER_PCT = 2.0      # % of the time missing from which the holes change what the data can say
HOLES_MATTER_LONGEST_S = 1.0


def holes(t, min_s=HOLE_S):
    """Where a trajectory has no records: (n, 2) array of [last time before, first time after] for every step
    between consecutive records longer than min_s (t in any one time base, sorted)."""
    t = np.asarray(t, float)
    if len(t) < 2:
        return np.zeros((0, 2))
    i = np.flatnonzero(np.diff(t) > min_s)
    return np.column_stack([t[i], t[i + 1]])


def holes_summary(t, min_s=HOLE_S, spans=True):
    """{"missing_pct", "count", "longest_s", "spans": [[t0, t1], ...]} for a trajectory's record times t (the spans
    in t's time base, rounded to 1 ms). missing_pct is the share of the time from the first to the last record."""
    h = holes(t, min_s)
    t = np.asarray(t, float)
    total = float(t[-1] - t[0]) if len(t) > 1 else 0.0
    dur = h[:, 1] - h[:, 0] if len(h) else np.zeros(0)
    out = {"missing_pct": round(100.0 * float(dur.sum()) / total, 2) if total > 0 else 0.0,
           "count": int(len(h)), "longest_s": round(float(dur.max()), 3) if len(dur) else 0.0}
    if spans:
        out["spans"] = [[round(float(a), 3), round(float(b), 3)] for a, b in h]
    return out


def holes_matter(summary):
    """True when a trajectory's holes are large enough that offsets measured on its points follow the holes."""
    if not summary:
        return False
    return (float(summary.get("missing_pct") or 0) >= HOLES_MATTER_PCT or
            float(summary.get("longest_s") or 0) >= HOLES_MATTER_LONGEST_S)


def in_holes(tq, spans):
    """Boolean per time in tq: inside one of spans ((n, 2), same time base, sorted by start)."""
    tq = np.asarray(tq, float)
    s = np.asarray(spans, float).reshape(-1, 2)
    if not len(s):
        return np.zeros(tq.shape, bool)
    j = np.searchsorted(s[:, 0], tq, side="right") - 1
    ok = j >= 0
    out = np.zeros(tq.shape, bool)
    out[ok] = tq[ok] < s[j[ok], 1]
    return out


def longest_hole_in(t0, t1, spans):
    """Longest hole (s) overlapping the time span [t0, t1] (same time base as spans), 0 when none."""
    s = np.asarray(spans, float).reshape(-1, 2)
    if not len(s):
        return 0.0
    m = (s[:, 1] > t0) & (s[:, 0] < t1)
    return float((s[m, 1] - s[m, 0]).max()) if m.any() else 0.0
