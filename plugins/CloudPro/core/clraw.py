"""Mapper .clraw recordings: attachments, events, Hesai PandarXT-32
point packets and Advanced Navigation ANPP packets, decoded with numpy.

Byte layouts follow the project's ".clraw & Sensor Reference for Plugin
Developers" (mapper 2.9.0, format version 1).
"""
import csv
import io
import json
import os
import re

import numpy as np

from . import mcap

TOPIC_POINTS = "/lidar/points"
TOPIC_STATUS = "/lidar/status"
TOPIC_ANPP = "/ins/anpp"
TOPIC_EVENTS = "/capture/events"
TOPIC_QUALITY = "/capture/quality"

HESAI_LEN = 1080
DEG = np.pi / 180.0

DEFAULT_CALIB = np.array([
    (14.916, 0.506), (13.899, 0.503), (12.891, 0.520), (11.886, 0.519), (10.882, 0.509), (9.882, 0.506),
    (8.882, 0.502), (7.879, 0.495), (6.881, 0.490), (5.881, 0.484), (4.884, 0.482), (3.890, 0.471),
    (2.896, 0.465), (1.900, 0.459), (0.909, 0.454), (-0.089, 0.447), (-1.084, 0.441), (-2.080, 0.437),
    (-3.079, 0.432), (-4.074, 0.427), (-5.071, 0.417), (-6.074, 0.416), (-7.077, 0.416), (-8.079, 0.412),
    (-9.080, 0.402), (-10.081, 0.401), (-11.087, 0.397), (-12.096, 0.391), (-13.100, 0.372), (-14.112, 0.364),
    (-15.132, 0.361), (-16.160, 0.356)])

_FNAME = re.compile(r"^(\d+)_(\d{8})_(\d{6})_(\d{4})\.clraw$")


def parse_calibration(text):
    """Hesai channel calibration CSV -> (32, 2) array of (elevation, azimuth offset) degrees."""
    rows = []
    for r in csv.reader(io.StringIO(text)):
        if len(r) < 3:
            continue
        try:
            rows.append((int(r[0]), float(r[1]), float(r[2])))
        except ValueError:
            continue
    if not rows:
        return DEFAULT_CALIB.copy()
    rows.sort()
    return np.array([(e, a) for _i, e, a in rows], float)


def group_recordings(paths):
    """Group .clraw paths into recordings (survey lines) read in name order.
    Returns {(folder, line): [paths...]}. ``.partial`` files are ignored."""
    groups = {}
    for p in paths:
        b = os.path.basename(p)
        if b.endswith(".partial"):
            continue
        m = _FNAME.match(b)
        key = (os.path.dirname(os.path.abspath(p)), m.group(1) if m else b)
        groups.setdefault(key, []).append(p)
    for k in groups:
        groups[k].sort(key=lambda q: os.path.basename(q))
    return groups


# ----------------------------------------------------------------------------
# Hesai PandarXT-32
# ----------------------------------------------------------------------------
def _days_from_civil(y, m, d):
    y = y - (m <= 2)
    era = np.floor_divide(y, 400)
    yoe = y - era * 400
    mp = (m + 9) % 12
    doy = (153 * mp + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def hesai_packet_times(P):
    """UTC Unix seconds of each packet (N,) from the packet tails."""
    t = P[:, 1065:1071].astype(np.int64)
    days = _days_from_civil(t[:, 0] + 1900, t[:, 1], t[:, 2])
    us = P[:, 1071:1075].copy().view("<u4")[:, 0].astype(np.float64)
    return days * 86400.0 + t[:, 3] * 3600.0 + t[:, 4] * 60.0 + t[:, 5] + us * 1e-6


def decode_hesai(P, calib, rmin=0.5, rmax=200.0, returns="both", drop_duplicates=True):
    """Decode an (N, 1080) uint8 array of XT-32 point packets.

    Returns a dict of flat arrays: t (UTC s), x, y, z (sensor frame, m),
    range (m), intensity, channel (0-31), ret (0 = last/first block of a pair,
    1 = strongest/second block, 2 = single-return mode), azimuth (deg),
    packet (index into P).
    """
    N = P.shape[0]
    if N == 0:
        return None
    unit = P[:, 9].astype(np.float64) * 1e-3              # mm -> m (4 on the mapper)
    mode = P[:, 1062]
    dual = np.isin(mode, (0x39, 0x3B, 0x3C))
    tp = hesai_packet_times(P)
    blocks = np.arange(8)
    az = P[:, 12 + 130 * blocks[:, None] + np.array([0, 1])[None, :]]          # (N,8,2)
    az = (az[..., 0].astype(np.uint16) | (az[..., 1].astype(np.uint16) << 8)).astype(np.float64) * 0.01
    k = np.arange(32)
    base = 14 + 130 * blocks[:, None] + 4 * k[None, :]                          # (8,32)
    dist = P[:, base].astype(np.uint16) | (P[:, base + 1].astype(np.uint16) << 8)  # (N,8,32)
    inten = P[:, base + 2]
    rng = dist.astype(np.float64) * unit[:, None, None]

    # firing index s (1..S) and return type per block
    S = np.where(dual, 4, 8)[:, None]
    s_dual = (blocks // 2) + 1
    s_single = blocks + 1
    s = np.where(dual[:, None], s_dual[None, :], s_single[None, :])
    ret = np.where(dual[:, None], (blocks % 2)[None, :], 2)
    # point time (Hesai XT32 manual formula, as in the mapper kit)
    tb = tp[:, None] + 3.28e-6 - 50e-6 * (S - s) + 0.28e-6                    # (N,8)
    t = tb[:, :, None] + 1.512e-6 * k[None, None, :]

    keep = (dist > 0) & (rng >= rmin) & (rng <= rmax)
    if returns == "last":
        keep &= (ret != 1)[:, :, None]
    elif returns == "strongest":
        keep &= (ret != 0)[:, :, None] | (~dual[:, None, None])
    if drop_duplicates and returns == "both":
        # second block repeats the first when last == strongest (equal range)
        d0 = dist[:, 0::2, :]
        d1 = dist[:, 1::2, :]
        dup = (np.abs(d0.astype(np.int32) - d1.astype(np.int32)) <= 1) & dual[:, None, None]
        keep[:, 1::2, :] &= ~dup
    ii, bb, kk = np.nonzero(keep)
    elev = calib[kk, 0] * DEG
    azi = (az[ii, bb] + calib[kk, 1]) * DEG
    r = rng[ii, bb, kk]
    ce = np.cos(elev)
    out = {
        "t": t[ii, bb, kk],
        "x": r * ce * np.sin(azi),
        "y": r * ce * np.cos(azi),
        "z": r * np.sin(elev),
        "range": r,
        "intensity": inten[ii, bb, kk],
        "channel": kk.astype(np.uint8),
        "ret": ret[ii, bb].astype(np.uint8),
        "azimuth": np.mod(az[ii, bb] + calib[kk, 1], 360.0),
        "packet": ii,
    }
    return out


# ----------------------------------------------------------------------------
# ANPP (Advanced Navigation)
# ----------------------------------------------------------------------------
SYS_STATE = np.dtype([("system_status", "<u2"), ("filter_status", "<u2"), ("sec", "<u4"), ("usec", "<u4"),
                      ("lat", "<f8"), ("lon", "<f8"), ("h", "<f8"), ("vn", "<f4"), ("ve", "<f4"), ("vd", "<f4"),
                      ("ax", "<f4"), ("ay", "<f4"), ("az", "<f4"), ("g", "<f4"),
                      ("roll", "<f4"), ("pitch", "<f4"), ("heading", "<f4"),
                      ("wx", "<f4"), ("wy", "<f4"), ("wz", "<f4"),
                      ("sd_lat", "<f4"), ("sd_lon", "<f4"), ("sd_h", "<f4")])
assert SYS_STATE.itemsize == 100
EULER_SD = np.dtype([("sd_roll", "<f4"), ("sd_pitch", "<f4"), ("sd_heading", "<f4")])


# ----------------------------------------------------------------------------
# Recording (one survey line, possibly several files)
# ----------------------------------------------------------------------------
class Recording:
    def __init__(self, paths):
        if isinstance(paths, str):
            paths = [paths]
        self.paths = sorted(paths, key=lambda q: os.path.basename(q))
        self.files = [mcap.McapFile(p).scan() for p in self.paths]
        f0 = self.files[0]
        if f0.profile and f0.profile != "clraw":
            raise ValueError("%s: MCAP profile %r is not clraw" % (self.paths[0], f0.profile))
        self.meta = dict(f0.metadata.get("clraw", {}))
        fv = self.meta.get("format_version", "1")
        if fv not in ("1", ""):
            raise ValueError("unsupported .clraw format_version %s (CloudPro reads version 1)" % fv)
        self.hardware = self._json("hardware.json")
        self.session = self._json("session.json")
        cal = None
        for f in self.files:
            if "lidar_calibration.csv" in f.attachments:
                cal = f.attachments["lidar_calibration.csv"].decode("utf-8", "replace")
                break
        self.calib = parse_calibration(cal) if cal else DEFAULT_CALIB.copy()
        self.calib_source = "file" if cal else "default table"
        self.line = self.meta.get("line") or os.path.basename(self.paths[0]).split("_")[0]
        self._events = None

    def _json(self, name):
        for f in self.files:
            if name in f.attachments:
                try:
                    return json.loads(f.attachments[name].decode("utf-8"))
                except ValueError:
                    return {}
        return {}

    @property
    def n_chunks(self):
        return sum(len(f.chunk_pos) for f in self.files)

    def lever_arm(self):
        v = self.hardware.get("ins_to_lidar_m") or [0.0, 0.0, 0.0]
        return [float(a) for a in v]

    def mount(self):
        m = self.hardware.get("lidar_mount_navaq_deg") or {}
        if isinstance(m, (list, tuple)):
            return [float(a) for a in m[:3]]
        return [float(m.get("roll", 0.0)), float(m.get("pitch", 0.0)), float(m.get("heading", 0.0))]

    def crs(self):
        cs = self.hardware.get("coordinate_system") or {}
        d = cs.get("definition") if isinstance(cs, dict) else None
        return d or self.meta.get("coordinate_system") or "EPSG:2953"

    # -- iteration -------------------------------------------------------
    def iter_chunks(self, topics):
        """Yield (file index, chunk index, {channel id: list of (log, pub, payload)})."""
        for fi, f in enumerate(self.files):
            want = {f.topic_id(t) for t in topics} - {None}
            if not want:
                continue
            with open(f.path, "rb") as fh:
                for ci in range(len(f.chunk_pos)):
                    data = f.read_chunk(fh, ci)
                    if data is None:
                        continue
                    yield fi, ci, data, want

    def chunk_list(self):
        return [(fi, ci) for fi, f in enumerate(self.files) for ci in range(len(f.chunk_pos))]

    def read_lidar_chunk(self, fh_cache, fi, ci):
        """Return an (N, 1080) uint8 array of the point packets in one chunk."""
        f = self.files[fi]
        cid = f.topic_id(TOPIC_POINTS)
        fh = fh_cache.get(fi)
        if fh is None:
            fh = fh_cache[fi] = open(f.path, "rb")
        data = f.read_chunk(fh, ci)
        if data is None or cid is None:
            return np.zeros((0, HESAI_LEN), np.uint8)
        bufs = [bytes(p) for ch, _lt, _pt, p in mcap.iter_messages(data, {cid}) if len(p) == HESAI_LEN]
        if not bufs:
            return np.zeros((0, HESAI_LEN), np.uint8)
        return np.frombuffer(b"".join(bufs), np.uint8).reshape(-1, HESAI_LEN)

    def events(self):
        if self._events is not None:
            return self._events
        ev = []
        for fi, ci, data, want in self.iter_chunks([TOPIC_EVENTS]):
            for ch, lt, pt, p in mcap.iter_messages(data, want):
                try:
                    ev.append(json.loads(bytes(p).decode("utf-8")))
                except ValueError:
                    pass
        self._events = ev
        return ev

    def recording_window(self):
        t0 = t1 = None
        for e in self.events():
            if e.get("event") == "clraw_start":
                t0 = float(e["t"])
            elif e.get("event") == "clraw_stop":
                t1 = float(e["t"])
        return t0, t1

    def marks(self):
        return [e for e in self.events() if e.get("event") == "mark"]

    def nav_states(self):
        """Decode every ANPP packet 20 (System State) -> structured array
        with an extra 't' (UTC s)."""
        rows = []
        sds = []
        for fi, ci, data, want in self.iter_chunks([TOPIC_ANPP]):
            for ch, lt, pt, p in mcap.iter_messages(data, want):
                if len(p) < 5:
                    continue
                pid = p[1]
                if pid == 20 and len(p) >= 105:
                    rows.append(bytes(p[5:105]))
                elif pid == 26 and len(p) >= 17:
                    sds.append((pt, bytes(p[5:17])))
        if not rows:
            return None
        a = np.frombuffer(b"".join(rows), SYS_STATE)
        t = a["sec"].astype(np.float64) + a["usec"] * 1e-6
        order = np.argsort(t, kind="stable")
        a = a[order]
        t = t[order]
        keep = np.concatenate([[True], np.diff(t) > 0])
        return a[keep], t[keep]
