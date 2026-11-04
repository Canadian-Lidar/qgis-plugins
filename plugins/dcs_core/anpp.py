"""Advanced Navigation ANPP packets as stored in .clraw channel /ins/anpp
(5-byte header + payload).

Two forms of the same data:
  NavCollector  per-packet, checks the header LRC; arrays in a dict (AlignPro)
  nav_states    numpy structured array of packet 20 (CloudPro)
"""
import struct

import numpy as np

FIX_NAMES = {0: "None", 1: "2D", 2: "3D", 3: "SBAS", 4: "DGNSS", 5: "PPP", 6: "RTK float", 7: "RTK fixed"}

_p20 = struct.Struct("<HHII3d3f3ff3f3f3f")
_p26 = struct.Struct("<3f")

# packet 20, System State (100 bytes)
SYS_STATE = np.dtype([("system_status", "<u2"), ("filter_status", "<u2"), ("sec", "<u4"), ("usec", "<u4"),
                      ("lat", "<f8"), ("lon", "<f8"), ("h", "<f8"), ("vn", "<f4"), ("ve", "<f4"), ("vd", "<f4"),
                      ("ax", "<f4"), ("ay", "<f4"), ("az", "<f4"), ("g", "<f4"),
                      ("roll", "<f4"), ("pitch", "<f4"), ("heading", "<f4"),
                      ("wx", "<f4"), ("wy", "<f4"), ("wz", "<f4"),
                      ("sd_lat", "<f4"), ("sd_lon", "<f4"), ("sd_h", "<f4")])
assert SYS_STATE.itemsize == 100
# packet 26, Euler orientation standard deviation (12 bytes)
EULER_SD = np.dtype([("sd_roll", "<f4"), ("sd_pitch", "<f4"), ("sd_heading", "<f4")])


def crc16(data):
    """CRC16-CCITT of a packet payload, as in the ANPP header."""
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def split(msg):
    """-> (packet id, payload bytes), or None when the header LRC or length is wrong."""
    b = bytes(msg)
    if len(b) < 5:
        return None
    if (b[0] + b[1] + b[2] + b[3] + b[4]) & 0xFF != 0:
        return None
    pid, ln = b[1], b[2]
    pl = b[5:5 + ln]
    if len(pl) != ln:
        return None
    return pid, pl


class NavCollector:
    """Accumulates packet 20 (system state) and 26 (Euler SD) into arrays."""

    def __init__(self):
        self.s20 = []
        self.s26 = []

    def add(self, msg, pub_ns):
        sp = split(msg)
        if sp is None:
            return
        pid, pl = sp
        if pid == 20 and len(pl) >= 100:
            v = _p20.unpack_from(pl, 0)
            t = v[2] + v[3] * 1e-6
            self.s20.append((t, v[4], v[5], v[6], v[7], v[8], v[9], v[14], v[15], v[16],
                             v[20], v[21], v[22], v[0], v[1]))
        elif pid == 26 and len(pl) >= 12:
            self.s26.append((pub_ns * 1e-9,) + _p26.unpack_from(pl, 0))

    def arrays(self):
        a = np.array(self.s20, float).reshape(-1, 15)
        if len(a):
            a = a[np.argsort(a[:, 0], kind="stable")]
            keep = np.concatenate([[True], np.diff(a[:, 0]) > 0])
            a = a[keep]
        fs = a[:, 14].astype(int) if len(a) else np.zeros(0, int)
        out = {
            "t": a[:, 0], "lat": a[:, 1], "lon": a[:, 2], "h": a[:, 3],
            "vn": a[:, 4], "ve": a[:, 5], "vd": a[:, 6],
            "roll": a[:, 7], "pitch": a[:, 8], "heading": a[:, 9],
            "sd_n": a[:, 10], "sd_e": a[:, 11], "sd_h": a[:, 12],
            "system_status": a[:, 13].astype(int), "fix": (fs >> 4) & 7,
            "nav_init": (fs >> 1) & 1, "time_init": (fs >> 3) & 1,
        }
        b = np.array(self.s26, float).reshape(-1, 4)
        out["sd_t"], out["sd_roll"], out["sd_pitch"], out["sd_heading"] = b[:, 0], b[:, 1], b[:, 2], b[:, 3]
        return out


def nav_states(payloads):
    """Every packet 20 in payloads (iterable of message payloads) -> (structured SYS_STATE array,
    t UTC s), time sorted with repeated times dropped; None when there is none."""
    rows = []
    for p in payloads:
        if len(p) >= 105 and p[1] == 20:
            rows.append(bytes(p[5:105]))
    if not rows:
        return None
    a = np.frombuffer(b"".join(rows), SYS_STATE)
    t = a["sec"].astype(np.float64) + a["usec"] * 1e-6
    order = np.argsort(t, kind="stable")
    a = a[order]
    t = t[order]
    keep = np.concatenate([[True], np.diff(t) > 0])
    return a[keep], t[keep]
