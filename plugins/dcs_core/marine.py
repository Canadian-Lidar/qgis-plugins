"""Marine sonar recordings (.clraw kind "sonar", made by RemixPro): one reader for PosPro, AlignPro and CloudPro.

    rec = SonarRecording(paths)            # the .clraw files of one recording
    rec.heads                              # {device id: {"topic", "nickname", "lever_m", "mount", ...}}
    rec.pings(head)                        # per ping: utc, ping number, sound speed, points (angle, tof, power, type)
    rec.attitude(head)                     # 504: utc, up vector (x, y, z), roll, pitch  (the sonar's own fusion)
    rec.imu(head)                          # 505: utc, accelerometer (g), gyro (deg/s), at ~100 Hz
    rec.vehicle()                          # MAVLink: positions (GLOBAL_POSITION_INT), attitude (ATTITUDE), in UTC
    rec.llh() / rec.ppk()                  # the rover's solution log lines (UTC)
    rec.rinex(role)                        # {name: bytes} of the RINEX attached for "rover" / "base"

Times are UTC seconds (float). MAVLink time_boot_ms is mapped to UTC with a straight line fitted against the
log times the recording gave the messages (the sonar's NTP clock); the residual is reported.

Geometry (verified on the Lumston recording, 13 Nov 2026; see dcs/marine-clraw.md): a sounding is a ray in the
plane perpendicular to the sonar's x axis (forward): ray = range * (0, SIDE * sin(angle), -cos(angle)) in the
frame of the attitude up vector, range = time of flight * sound speed / 2. `level_vectors` turns rays into
forward / left / up components in a level frame (no heading yet) from each ping's up vector.
"""
import json
import math
import re

import numpy as np

from . import clraw, ping

# which side a positive beam angle looks to, in the attitude frame (see level_vectors); settled from reciprocal
# lines (dcs/marine-clraw.md)
SIDE = -1.0
OLD_SV_SOURCE = "SonarView Mount Offset (X forward, Y starboard, Z down)"

_IMU = np.dtype([("t", "<f8"), ("acc", "<f4", 3), ("gyro", "<f4", 3)])
_ATT = np.dtype([("t", "<f8"), ("up", "<f4", 3), ("roll", "<f4"), ("pitch", "<f4")])


def imu_505(payload):
    """Packet 505 (Omniscan 3D raw IMU, ~100 Hz; layout decoded from the Lumston recording): accelerometer (g),
    gyro (deg/s), magnetometer (or zeros), utc_msec, pwr_up_msec, channel. Axes as packet 504."""
    v = ping_struct_505.unpack_from(payload, 0)
    return {"acc": v[0:3], "gyro": v[3:6], "mag": v[6:9], "utc_msec": v[9], "pwr_up_msec": v[10], "channel": v[11]}


import struct as _struct                       # noqa: E402
ping_struct_505 = _struct.Struct("<9fQIBB")


class SonarRecording:
    """The .clraw files of one marine recording (kind "sonar"), read in file order."""

    def __init__(self, paths):
        paths = [paths] if isinstance(paths, str) else sorted(paths)
        self.files = [clraw.ClrawFile(p) for p in paths]
        if not self.files or self.files[0].metadata.get("kind") != clraw.SONAR:
            raise clraw.ClrawError("not a marine sonar recording (.clraw kind 'sonar')")
        self.metadata = dict(self.files[0].metadata)
        self.name = self.metadata.get("capture") or self.files[0].name
        self.hardware = self.files[0].hardware
        self.topics = clraw.sonar_topics(self.files[0])
        self.time_basis = self.metadata.get("time_basis", "")
        self._veh = None

    # ---------------------------------------------------------------- what it holds
    @property
    def heads(self):
        out = {}
        hw = {s.get("device_id"): s for s in self.hardware.get("sonars") or []}
        for did, topic in self.topics.items():
            s = hw.get(did, {})
            lever, src, side = s.get("acoustic_centre_m"), s.get("acoustic_centre_source") or "", s.get("side")
            if lever and src.startswith(OLD_SV_SOURCE):
                # RemixPro 1.2.0 read SonarView's Y as starboard; it is port (13 Nov 2026)
                lever = [lever[0], -lever[1], lever[2]]
                src = "SonarView Mount Offset (X forward, Y port, Z down), as recorded (side corrected)"
                side = "starboard" if lever[1] > 0 else "port" if lever[1] < 0 else "centre"
            out[did] = {"topic": topic, "nickname": s.get("nickname") or str(did), "lever_m": lever,
                        "lever_source": src, "mount": s.get("mount_deg_sonarview"), "side": side,
                        "product": s.get("product_id")}
        return out

    def span(self):
        a = [f.span() for f in self.files]
        return min(x[0] for x in a if x[0] is not None), max(x[1] for x in a if x[1] is not None)

    def _iter(self, topic):
        for f in self.files:
            cid = f._m.topic_id(topic)
            if cid is None:
                continue
            for _ch, lt, pt, p in f.iter_messages({cid}):
                yield lt, pt, p

    def _packets(self, head, want):
        for lt, _pt, p in self._iter(self.topics[head]):
            b = bytes(p)
            if len(b) < 10:
                continue
            mid = _struct.unpack_from("<H", b, 4)[0]
            if mid in want:
                yield mid, lt, b[8:-2]

    # ---------------------------------------------------------------- sonar packets
    def pings(self, head, every=1):
        """List of pings of one head: dicts {t (UTC s), ping, sos, thresholds, points: (n, 4) float array of
        angle rad, time of flight s, power, type}. every=k keeps every k-th ping (quick looks)."""
        out = []
        k = 0
        for _mid, lt, pl in self._packets(head, (ping.OS3D_POINT_SET,)):
            k += 1
            if (k - 1) % every:
                continue
            try:
                d = ping.os3d_point_set(pl)
            except (ping.PingError, _struct.error):
                continue
            t = d["utc_msec"] / 1000.0 if d["utc_msec"] else lt * 1e-9
            pts = np.array(d["points"], float).reshape(-1, 4)
            out.append({"t": t, "ping": d["ping_number"], "sos": d["sos_mps"], "thresholds": d["thresholds"],
                        "points": pts})
        return out

    def attitude(self, head):
        """504 as a structured array (t, up[3], roll, pitch), the sonar's own attitude at each ping."""
        rows = []
        for _mid, lt, pl in self._packets(head, (ping.ATTITUDE_REPORT,)):
            try:
                a = ping.attitude_report(pl)
            except _struct.error:
                continue
            rows.append((a["utc_msec"] / 1000.0 if a["utc_msec"] else lt * 1e-9, a["up"], a["roll_deg"],
                         a["pitch_deg"]))
        return np.array(rows, _ATT)

    def imu(self, head):
        """505 as a structured array (t, acc[3] g, gyro[3] deg/s); the ms timestamps are evened out (100 Hz
        packets often share a millisecond stamp)."""
        rows = []
        for _mid, lt, pl in self._packets(head, (505,)):
            if len(pl) < ping_struct_505.size:
                continue
            v = ping_struct_505.unpack_from(pl, 0)
            rows.append((v[9] / 1000.0 if v[9] else lt * 1e-9, v[0:3], v[3:6]))
        a = np.array(rows, _IMU)
        if len(a) > 10:
            a["t"] = even_times(a["t"])
        return a

    # ---------------------------------------------------------------- vehicle and GNSS
    def vehicle(self):
        """MAVLink in UTC: {"pos": structured (t, lat, lon, alt, vn, ve, hdg), "att": (t, roll, pitch, yaw) rad,
        "boot_fit": (slope, offset, rms s), "types": {type: count}}."""
        if self._veh is not None:
            return self._veh
        pos, att, types = [], [], {}
        for lt, _pt, p in self._iter("/vehicle/mavlink"):
            try:
                o = json.loads(bytes(p).rstrip(b"\x00").decode("utf-8"))
            except ValueError:
                continue
            m = o.get("message") or {}
            typ = m.get("type")
            types[typ] = types.get(typ, 0) + 1
            b = m.get("time_boot_ms")
            if b is None:
                continue
            if typ == "GLOBAL_POSITION_INT":
                pos.append((b / 1000.0, lt * 1e-9, m["lat"] * 1e-7, m["lon"] * 1e-7, m["alt"] / 1000.0, m["vx"] / 100.0,
                            m["vy"] / 100.0, (m.get("hdg", 65535) / 100.0) if m.get("hdg", 65535) != 65535 else np.nan))
            elif typ == "ATTITUDE":
                att.append((b / 1000.0, lt * 1e-9, m["roll"], m["pitch"], m["yaw"]))
        fit = (1.0, 0.0, None)
        boot = np.array([r[0] for r in pos + att])
        logt = np.array([r[1] for r in pos + att])
        if len(boot) > 20:
            A = np.polyfit(boot, logt, 1)
            res = logt - np.polyval(A, boot)
            keep = np.abs(res) < 3 * max(np.std(res), 0.01)
            A = np.polyfit(boot[keep], logt[keep], 1)
            fit = (float(A[0]), float(A[1]), float(np.std(logt[keep] - np.polyval(A, boot[keep]))))
        P = np.array([(fit[0] * r[0] + fit[1],) + r[2:] for r in pos],
                     dtype=[("t", "<f8"), ("lat", "<f8"), ("lon", "<f8"), ("alt", "<f8"), ("vn", "<f4"), ("ve", "<f4"),
                            ("hdg", "<f4")])
        T = np.array([(fit[0] * r[0] + fit[1],) + r[2:] for r in att],
                     dtype=[("t", "<f8"), ("roll", "<f4"), ("pitch", "<f4"), ("yaw", "<f4")])
        P.sort(order="t")
        T.sort(order="t")
        self._veh = {"pos": P, "att": T, "boot_fit": fit, "types": types}
        return self._veh

    def _lines(self, topic):
        out = []
        for lt, _pt, p in self._iter(topic):
            out.append((lt * 1e-9, bytes(p).decode("utf-8", "replace")))
        return out

    def llh(self):
        """The rover's .LLH (Emlid) as a structured array (t UTC, lat, lon, h, q, ns, sdn, sde, sdu)."""
        rows = []
        for t, s in self._lines("/gnss/llh"):
            a = s.split()
            try:
                rows.append((t, float(a[2]), float(a[3]), float(a[4]), int(a[5]), int(a[6]), float(a[7]), float(a[8]),
                             float(a[9])))
            except (IndexError, ValueError):
                continue
        return np.array(rows, dtype=[("t", "<f8"), ("lat", "<f8"), ("lon", "<f8"), ("h", "<f8"), ("q", "<i2"),
                                     ("ns", "<i2"), ("sdn", "<f4"), ("sde", "<f4"), ("sdu", "<f4")])

    def ppk(self):
        """The rover's RTKLIB .pos lines (t UTC, lat, lon, h, q, ns, sdn, sde, sdu) (lat/lon/height form)."""
        rows = []
        for t, s in self._lines("/gnss/ppk"):
            a = s.split()
            try:
                rows.append((t, float(a[2]), float(a[3]), float(a[4]), int(a[5]), int(a[6]), float(a[7]), float(a[8]),
                             float(a[9])))
            except (IndexError, ValueError):
                continue
        return np.array(rows, dtype=[("t", "<f8"), ("lat", "<f8"), ("lon", "<f8"), ("h", "<f8"), ("q", "<i2"),
                                     ("ns", "<i2"), ("sdn", "<f4"), ("sde", "<f4"), ("sdu", "<f4")])

    def rinex(self, role):
        """{file name: bytes} of the RINEX files attached for "rover" or "base" (older files: gnss/<name> = rover)."""
        out = {}
        for k, v in self.files[0].attachments.items():
            m = re.match(r"^gnss/(?:(rover|base)/)?(.+)$", k)
            if m and (m.group(1) or "rover") == role:
                out[m.group(2)] = v
        return out

    @property
    def tidalcal(self):
        return self.hardware.get("tidalcal") or {}


def even_times(t):
    """Timestamps of a fixed-rate stream with coarse (ms) stamps, evened out: a straight line through them,
    restarted wherever the stream has a gap > 0.5 s."""
    t = np.asarray(t, float)
    out = t.copy()
    cut = np.flatnonzero(np.diff(t) > 0.5) + 1
    for a, b in zip(np.r_[0, cut], np.r_[cut, len(t)]):
        if b - a > 5:
            i = np.arange(b - a)
            out[a:b] = np.polyval(np.polyfit(i, t[a:b], 1), i)
    return out


def level_basis(up):
    """For up vectors (n, 3) in the sonar's attitude frame: the level forward (F) and left (L) directions and the
    unit up (U), each (n, 3) in that frame."""
    u = np.asarray(up, float)
    u = u / np.linalg.norm(u, axis=1)[:, None]
    ex = np.array([1.0, 0.0, 0.0])
    F = ex[None, :] - (u @ ex)[:, None] * u
    F /= np.linalg.norm(F, axis=1)[:, None]
    L = np.cross(u, F)
    return F, L, u


def rays(angle, tof, sos, sos_new=None, roll_bias_deg=0.0):
    """Ray vectors (n, 3) in the sonar's attitude frame for soundings (angle rad, time of flight s) recorded with
    sound speed sos; sos_new re-scales the range and re-bends the angle (Snell at the face); roll_bias_deg turns
    the fan about x (a mounting correction)."""
    angle = np.asarray(angle, float)
    r = np.asarray(tof, float) * sos / 2.0
    if sos_new and sos_new != sos:
        s = np.clip(np.sin(angle) * sos_new / sos, -1, 1)
        angle = np.arcsin(s)
        r = r * sos_new / sos
    a = angle + math.radians(roll_bias_deg) * SIDE
    return np.stack([np.zeros_like(a), SIDE * np.sin(a), -np.cos(a)], 1) * r[:, None]


def level_vectors(v, up, pitch_bias_deg=0.0):
    """Rays v (n, 3) with their ping's up vector (n, 3) -> (forward, left, up) in a level frame (n, 3)."""
    if pitch_bias_deg:
        c, s = math.cos(math.radians(pitch_bias_deg)), math.sin(math.radians(pitch_bias_deg))
        v = np.stack([c * v[:, 0] - s * v[:, 2], v[:, 1], s * v[:, 0] + c * v[:, 2]], 1)
    F, L, U = level_basis(up)
    return np.stack([(v * F).sum(1), (v * L).sum(1), (v * U).sum(1)], 1)


def up_of_frame(up):
    """Cerulean's up vector as given (kept as a function so the frame convention lives in one place)."""
    return np.asarray(up, float)


# ----------------------------------------------------------------------------- soundings in a local frame
def sample_soundings(rec, head, every=2, power="medium", rmin=0.5, rmax=200.0, attitude=None):
    """Soundings of one head for calibration and checks: dict of arrays t (UTC s), angle, tof, sos, power, up
    (n, 3) (the head's attitude at its ping; `attitude` = PosPro's finalized array, else the recording's 504).
    power: keep soundings at or above the ping's high / medium / low threshold, or "all"."""
    A = attitude if attitude is not None else rec.attitude(head)
    out = {k: [] for k in ("t", "angle", "tof", "sos", "power", "up", "ping")}
    thr = {"high": 0, "medium": 1, "low": 2}.get(power)
    for k, p in enumerate(rec.pings(head, every=every)):
        q = p["points"]
        if not len(q):
            continue
        m = np.ones(len(q), bool)
        if thr is not None:
            m &= q[:, 2] >= float(p["thresholds"][thr])
        r = q[:, 1] * p["sos"] / 2.0
        m &= (r >= rmin) & (r <= rmax)
        if not m.any():
            continue
        q = q[m]
        i = int(np.clip(np.searchsorted(A["t"], p["t"]), 0, len(A) - 1))
        if i > 0 and abs(A["t"][i - 1] - p["t"]) < abs(A["t"][i] - p["t"]):
            i -= 1
        n = len(q)
        out["t"].append(np.full(n, p["t"]))
        out["angle"].append(q[:, 0])
        out["tof"].append(q[:, 1])
        out["sos"].append(np.full(n, p["sos"]))
        out["power"].append(q[:, 2])
        out["up"].append(np.repeat(np.asarray(A["up"][i], float)[None, :], n, 0))
        out["ping"].append(np.full(n, k))
    if not out["t"]:
        return None
    return {k: np.concatenate(v) for k, v in out.items()}


class LocalTrajectory:
    """A trajectory (t UTC, lat / lon deg, h, roll / pitch / heading rad) in a local east / north / up frame
    around (lat0, lon0, h0): flat-earth, fine for the few kilometres of a survey and for comparing passes."""

    def __init__(self, t, lat, lon, h, roll, pitch, heading, origin=None):
        self.t = np.asarray(t, float)
        lat0, lon0, h0 = origin if origin else (float(np.mean(lat)), float(np.mean(lon)), float(np.mean(h)))
        self.origin = (lat0, lon0, h0)
        kn = 111132.954 - 559.822 * math.cos(2 * math.radians(lat0))
        ke = 111412.84 * math.cos(math.radians(lat0))
        self.E = (np.asarray(lon, float) - lon0) * ke
        self.N = (np.asarray(lat, float) - lat0) * kn
        self.U = np.asarray(h, float) - h0
        self.roll = np.asarray(roll, float)
        self.pitch = np.asarray(pitch, float)
        self.heading = np.unwrap(np.asarray(heading, float))

    def at(self, t):
        f = lambda v: np.interp(t, self.t, v)
        return f(self.E), f(self.N), f(self.U), f(self.roll), f(self.pitch), f(self.heading)


def georef_local(S, traj, lever, cal=None, side=None):
    """Soundings S (sample_soundings) of one head -> (n, 3) east / north / up in traj's local frame.
    cal: {"roll", "pitch", "heading" (deg), "latency_s"} corrections for this head; lever [F, S, D] m."""
    cal = dict({"roll": 0.0, "pitch": 0.0, "heading": 0.0, "latency_s": 0.0}, **(cal or {}))
    sd = SIDE if side is None else side
    t = S["t"] + float(cal["latency_s"])
    a = S["angle"] + math.radians(cal["roll"]) * sd
    r = S["tof"] * S["sos"] / 2.0
    v = np.stack([np.zeros_like(a), sd * np.sin(a), -np.cos(a)], 1) * r[:, None]
    flu = level_vectors(v, S["up"], cal["pitch"])
    E0, N0, U0, rr, pp, yy = traj.at(t)
    hd = yy + math.radians(cal["heading"])
    f, l_, u = flu[:, 0], flu[:, 1], flu[:, 2]
    e = f * np.sin(hd) - l_ * np.cos(hd)
    n = f * np.cos(hd) + l_ * np.sin(hd)
    # the lever arm turns with the vessel (roll, pitch, heading; vessel frame F, S, D)
    cr, sr, cp, sp, cy, sy = np.cos(rr), np.sin(rr), np.cos(pp), np.sin(pp), np.cos(yy), np.sin(yy)
    lx, ly, lz = (float(x) for x in (lever or (0.0, 0.0, 0.0)))
    ln = cy * cp * lx + (cy * sp * sr - sy * cr) * ly + (cy * sp * cr + sy * sr) * lz
    le = sy * cp * lx + (sy * sp * sr + cy * cr) * ly + (sy * sp * cr - cy * sr) * lz
    ld = -sp * lx + cp * sr * ly + cp * cr * lz
    return np.column_stack([E0 + le + e, N0 + ln + n, U0 - ld + u])


# ----------------------------------------------------------------------------- passes (survey lines)
PASS_TURN_DEG = 45.0        # straight runs whose headings differ by less are one pass
PASS_MIN_S = 20.0           # a straight run shorter than this is part of a turn
PASS_TURN_RATE = 2.0        # deg/s: turning faster than this is a turn


def split_passes(t, heading, t0=None, t1=None):
    """The straight runs of the boat (its survey lines) between t0 and t1, from the trajectory's time t (s) and
    heading (rad): [(start, end)], contiguous, covering t0..t1. A turn is split at its middle; straight runs with
    about the same heading on either side of a short wiggle stay one pass."""
    t = np.asarray(t, float)
    t0 = float(t[0]) if t0 is None else float(t0)
    t1 = float(t[-1]) if t1 is None else float(t1)
    if t1 - t0 < 2 * PASS_MIN_S:
        return [(t0, t1)]
    ts = np.arange(t0, t1, 1.0)
    hd = np.degrees(np.interp(ts, t, np.unwrap(np.asarray(heading, float))))
    k = 7
    hs = np.convolve(np.r_[np.full(k // 2, hd[0]), hd, np.full(k // 2, hd[-1])], np.ones(k) / k, "valid")
    straight = np.abs(np.gradient(hs)) < PASS_TURN_RATE
    cores = []                                       # [i0, i1) straight runs
    i = 0
    while i < len(ts):
        if straight[i]:
            j = i
            while j < len(ts) and straight[j]:
                j += 1
            if ts[j - 1] - ts[i] >= PASS_MIN_S:
                cores.append([i, j])
            i = j
        else:
            i += 1
    if not cores:
        return [(t0, t1)]
    merged = [cores[0]]
    for c in cores[1:]:
        p = merged[-1]
        if abs(np.median(hs[c[0]:c[1]]) - np.median(hs[p[0]:p[1]])) < PASS_TURN_DEG:
            p[1] = c[1]
        else:
            merged.append(c)
    cuts = [0.5 * (ts[a[1] - 1] + ts[b[0]]) for a, b in zip(merged[:-1], merged[1:])]
    edges = [t0] + cuts + [t1]
    return list(zip(edges[:-1], edges[1:]))


def pass_of(spans, t):
    """Index of the pass (split_passes) each time t falls in."""
    starts = np.array([s for s, _e in spans])
    return np.clip(np.searchsorted(starts, np.asarray(t, float), "right") - 1, 0, len(spans) - 1)
