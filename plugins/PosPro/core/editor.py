"""Data for the Trajectory editor (plan and profile views of the processed trajectory). No Qt.

Track: the trajectory as processed (trajectory_original.bin) and as edited (trajectory.bin), read at
up to MAX_POINTS samples with numpy, plus per sample the review state of its time (open, rejected,
interpolated, poor, excluded, accepted) and the solution type. Sections split the job where the vehicle
stood still for a while or the data stops, so the views are not flooded.
"""
import math
import os

import numpy as np

from . import review

MAX_POINTS = 250000
STATE_GOOD, STATE_OPEN, STATE_INTERP, STATE_POOR, STATE_EXCL, STATE_ACCEPT = 0, 1, 2, 3, 4, 5
STATE_LABEL = {STATE_GOOD: "Good", STATE_OPEN: "Open / rejected", STATE_INTERP: "Interpolated",
               STATE_POOR: "Marked poor", STATE_EXCL: "Bridged with IMU", STATE_ACCEPT: "Accepted"}
_DT = np.dtype([("t", "<f8"), ("lat", "<f8"), ("lon", "<f8"), ("h", "<f8"), ("vel", "<f4", 3), ("att", "<f4", 3),
                ("sdp", "<f4", 3), ("rest", "<f4", 18), ("sep", "<f4", 3), ("status", "u1"), ("nsat", "u1"),
                ("ratio", "u1"), ("flags", "u1"), ("gap", "<f4")])
assert _DT.itemsize == review.TRJ.size


def _read(path, step=None):
    if not os.path.exists(path):
        return None, 1
    n = (os.path.getsize(path) - review.TRJ_HDR) // _DT.itemsize
    if n <= 0:
        return None, 1
    mm = np.memmap(path, dtype=_DT, mode="r", offset=review.TRJ_HDR, shape=(n,))
    if step is None:
        step = max(1, int(math.ceil(n / float(MAX_POINTS))))
    return np.array(mm[::step]), step


class Track:
    """The trajectory for the editor views. Positions in metres east/north/up of the first sample
    (local tangent plane, `project` can replace it with a map projection for the basemap)."""

    def __init__(self, work_dir):
        d = os.path.join(work_dir, "results")
        orig = os.path.join(d, "trajectory_original.bin")
        final = os.path.join(d, "trajectory.bin")
        self.o, self.step = _read(orig if os.path.exists(orig) else final)
        self.f, _ = _read(final, self.step)
        if self.o is None or self.f is None or len(self.o) != len(self.f):
            self.f = self.o
        self.ok = self.o is not None and len(self.o) > 1
        if not self.ok:
            return
        self.t = self.o["t"]
        self.lat0, self.lon0 = float(self.o["lat"][0]), float(self.o["lon"][0])
        M, N = review._radii(self.lat0)
        self._k = (M, N * math.cos(self.lat0))
        self.on, self.oe = self._local(self.o)
        self.fn, self.fe = self._local(self.f)
        self.oh, self.fh = self.o["h"].astype(float), self.f["h"].astype(float)
        self.status = self.f["status"].astype(int)
        self.state = np.zeros(len(self.t), dtype=np.int8)
        self.speed = np.hypot(self.f["vel"][:, 0], self.f["vel"][:, 1])
        self.course = np.arctan2(self.f["vel"][:, 1], self.f["vel"][:, 0])
        att = self.f["att"].astype(float)
        self.roll, self.pitch, self.heading = att[:, 0], att[:, 1], att[:, 2]
        self.sections = sections(self.t, self.speed)

    def _local(self, a):
        return (a["lat"] - self.lat0) * self._k[0], (a["lon"] - self.lon0) * self._k[1]

    def antenna(self, lever):
        """North, east (m, plan coordinates) of a point at body-frame lever [F, S, D] for every sample."""
        from .antennas import offsets_ne
        dn, de = offsets_ne(self.roll, self.pitch, self.heading, [float(x) for x in lever])
        return self.fn + dn, self.fe + de

    def lat_lon_deg(self, i):
        return math.degrees(self.f["lat"][i]), math.degrees(self.f["lon"][i])

    def changed(self):
        """Samples whose position the edits moved."""
        return (np.abs(self.fn - self.on) > 1e-4) | (np.abs(self.fe - self.oe) > 1e-4) | (np.abs(self.fh - self.oh) > 1e-4)

    def set_states(self, project):
        """State of every sample from the review list and edits."""
        r = review.state(project)
        st = np.zeros(len(self.t), dtype=np.int8)
        code = {"interpolate": STATE_INTERP, "poor": STATE_POOR, "exclude": STATE_EXCL, "accept": STATE_ACCEPT,
                "reject": STATE_OPEN}
        for a in review.listed(project):
            if review.edit_for(project, a["t0"], a["t1"]) is None:
                st[self.span(a["t0"], a["t1"])] = STATE_OPEN
        for e in r["edits"]:
            st[self.span(e["t0"], e["t1"])] = code.get(e["action"], STATE_OPEN)
        self.state = st

    def span(self, t0, t1):
        i0 = int(np.searchsorted(self.t, t0, "left"))
        i1 = int(np.searchsorted(self.t, t1, "right"))
        return slice(i0, max(i0, i1))

    def index_at(self, t):
        i = int(np.searchsorted(self.t, t))
        if i <= 0:
            return 0
        if i >= len(self.t):
            return len(self.t) - 1
        return i if self.t[i] - t < t - self.t[i - 1] else i - 1


def sections(t, speed, stop_s=30.0, stop_speed=0.5, gap_s=5.0, min_s=20.0):
    """Split the drive into sections at stops of stop_s or longer and at data gaps: [(t0, t1)]."""
    if len(t) < 2:
        return [(float(t[0]), float(t[-1]))] if len(t) else []
    still = speed < stop_speed
    cuts = []
    i, n = 0, len(t)
    while i < n:
        if still[i]:
            j = i
            while j + 1 < n and still[j + 1]:
                j += 1
            if t[j] - t[i] >= stop_s:
                cuts.append((t[i], t[j]))
            i = j + 1
        else:
            i += 1
    gaps = np.nonzero(np.diff(t) > gap_s)[0]
    cuts += [(t[g], t[g + 1]) for g in gaps]
    cuts.sort()
    out, start = [], float(t[0])
    for a, b in cuts:
        if a - start >= min_s:
            out.append((start, float(a)))
        start = max(start, float(b))
    if t[-1] - start >= min_s or not out:
        out.append((start, float(t[-1])))
    return out


def windows_from_mask(t, mask, join_s=3.0, pad_s=None):
    """Selected samples -> time windows [(t0, t1)], pieces closer than join_s joined, each widened by half a
    sample so the window covers every selected sample."""
    idx = np.nonzero(mask)[0]
    if not len(idx):
        return []
    dt = float(np.median(np.diff(t))) if len(t) > 1 else 0.05
    pad = 0.5 * dt if pad_s is None else pad_s
    out = []
    a = b = idx[0]
    for k in idx[1:]:
        if t[k] - t[b] <= join_s:
            b = k
        else:
            out.append((float(t[a]) - pad, float(t[b]) + pad))
            a = b = k
    out.append((float(t[a]) - pad, float(t[b]) + pad))
    return out


def inside_polygon(x, y, poly):
    """Points (x, y arrays) inside the polygon [(x, y)...] (even-odd rule)."""
    inside = np.zeros(len(x), dtype=bool)
    m = len(poly)
    if m < 3:
        return inside
    px = np.array([p[0] for p in poly])
    py = np.array([p[1] for p in poly])
    j = m - 1
    for i in range(m):
        xi, yi, xj, yj = px[i], py[i], px[j], py[j]
        cond = (yi > y) != (yj > y)
        with np.errstate(divide="ignore", invalid="ignore"):
            xc = (xj - xi) * (y - yi) / (yj - yi) + xi
        inside ^= cond & (x < xc)
        j = i
    return inside
