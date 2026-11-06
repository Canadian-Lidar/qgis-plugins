"""Advanced Navigation ANPP packets as stored in .clraw channel /ins/anpp
(5-byte header + payload).

Two forms of the same data:
  NavCollector  per-packet, checks the header LRC; arrays in a dict (AlignPro)
  nav_states    numpy structured array of packet 20 (CloudPro)
"""
import struct
from binascii import crc_hqx

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
    return crc_hqx(bytes(data), 0xFFFF)


def split(msg, check_crc=True):
    """-> (packet id, payload bytes), or None when the header LRC, the length or (1.8.0) the payload CRC is
    wrong."""
    b = bytes(msg)
    if len(b) < 5:
        return None
    if (b[0] + b[1] + b[2] + b[3] + b[4]) & 0xFF != 0:
        return None
    pid, ln = b[1], b[2]
    pl = b[5:5 + ln]
    if len(pl) != ln:
        return None
    if check_crc and crc_hqx(pl, 0xFFFF) != (b[3] | (b[4] << 8)):
        return None
    return pid, pl


class NavCollector:
    """Accumulates packet 20 (system state) and 26 (Euler SD) into arrays."""

    def __init__(self):
        self.s20 = []
        self.s26 = []
        self.rejected = 0       # packets with a bad header LRC, length or payload CRC (1.8.0)

    def add(self, msg, pub_ns):
        sp = split(msg)
        if sp is None:
            self.rejected += 1
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


def nav_states(payloads, stats=None):
    """Every packet 20 in payloads (iterable of message payloads) -> (structured SYS_STATE array,
    t UTC s), time sorted with repeated times dropped; None when there is none. Packets with a bad header LRC,
    length or payload CRC are left out (1.8.0) and counted in stats["rejected"] when a dict is given."""
    buf = bytearray()
    bad = 0
    for p in payloads:
        if len(p) >= 105 and p[1] == 20:
            if (p[0] + p[1] + p[2] + p[3] + p[4]) & 0xFF or p[2] < 100 or len(p) < 5 + p[2] or \
                    crc_hqx(p[5:5 + p[2]], 0xFFFF) != (p[3] | (p[4] << 8)):
                bad += 1
                continue
            buf += p[5:105]
    if stats is not None:
        stats["rejected"] = stats.get("rejected", 0) + bad
    if not buf:
        return None
    a = np.frombuffer(buf, SYS_STATE)
    t = a["sec"].astype(np.float64) + a["usec"] * 1e-6
    order = np.argsort(t, kind="stable")
    a = a[order]
    t = t[order]
    keep = np.concatenate([[True], np.diff(t) > 0])
    return a[keep], t[keep]


# ----------------------------------------------------------------------------- raw stream (1.5.0, API 6)
# The Certus byte stream (.anpp files, or /ins/anpp packets joined) and the packets RemixPro converts:
# 20 System State, 28 Raw Sensors, 60 Raw Satellite Data, 61 Raw Satellite Ephemeris, 3 Device Information.
# Taken unchanged from the mapper (mapper/anpp.py, Certus Evo OEM Reference Manual v2.10 chapter 9), so
# the files made from them stay byte-identical to what the mapper made.
from binascii import crc_hqx as _crc_hqx  # noqa: E402
import math as _math  # noqa: E402

HEADER = 5
DEVICE_INFO = 3
SYSTEM_STATE = 20
UNIX_TIME = 21
RAW_SENSORS = 28
RAW_SAT_DATA = 60
RAW_SAT_EPHEMERIS = 61


def build(pid, payload=b""):
    """One ANPP packet (header + payload)."""
    c = _crc_hqx(payload, 0xFFFF)
    lo, hi = c & 0xFF, c >> 8
    lrc = (-(pid + len(payload) + lo + hi)) & 0xFF
    return bytes((lrc, pid, len(payload), lo, hi)) + payload


def iter_packets(fileobj, block=1 << 20):
    """Yield (packet id, payload) from a binary ANPP byte stream (anything with read(n)). Bytes that are
    not a packet with a good header LRC and payload CRC are skipped one at a time, as the Certus tools do."""
    buf = bytearray()
    crc = _crc_hqx
    while True:
        chunk = fileobj.read(block)
        if not chunk:
            break
        buf += chunk
        n = len(buf)
        pos = 0
        while n - pos >= HEADER:
            ln = buf[pos + 2]
            lo = buf[pos + 3]
            hi = buf[pos + 4]
            if (buf[pos] + buf[pos + 1] + ln + lo + hi) & 0xFF:
                pos += 1
                continue
            end = pos + HEADER + ln
            if end > n:
                break
            payload = bytes(buf[pos + HEADER:end])
            if crc(payload, 0xFFFF) != (lo | (hi << 8)):
                pos += 1
                continue
            yield buf[pos + 1], payload
            pos = end
        del buf[:pos]


_S20 = struct.Struct("<HHIIdddffffffffffffffff")


def system_state(b):
    """Packet 20 payload -> dict; angles in degrees, heading 0..360."""
    (ss, fs, ut, us, lat, lon, h, vn, ve, vd, ax, ay, az, g, roll, pitch, hdg,
     wx, wy, wz, slat, slon, sh) = _S20.unpack_from(b, 0)
    return {
        "system_status": ss, "filter_status": fs, "unix": ut, "micros": us, "time": ut + us * 1e-6,
        "lat": _math.degrees(lat), "lon": _math.degrees(lon), "height": h,
        "vn": vn, "ve": ve, "vd": vd, "speed": _math.hypot(vn, ve),
        "roll": _math.degrees(roll), "pitch": _math.degrees(pitch), "heading": _math.degrees(hdg) % 360.0,
        "sd_lat": slat, "sd_lon": slon, "sd_height": sh,
        "orientation_init": bool(fs & 0x0001), "navigation_init": bool(fs & 0x0002),
        "heading_init": bool(fs & 0x0004), "time_init": bool(fs & 0x0008), "fix": (fs >> 4) & 0x7,
    }


def state_time(b):
    """Packet 20 payload -> (unix seconds, microseconds, filter status), the fast path."""
    fs, ut, us = struct.unpack_from("<HII", b, 2)
    return ut, us, fs


_S28 = struct.Struct("<12f")


def raw_sensors(b):
    """Packet 28 payload: accel (m/s^2) x y z, gyro (rad/s) x y z, mag x y z, IMU temperature (C), pressure,
    pressure temperature. In the vehicle frame (the Certus applies its alignment)."""
    return _S28.unpack_from(b, 0)


_S60H = struct.Struct("<IIiBBBB")
_S60S = struct.Struct("<BBBHB")
_S60F = struct.Struct("<BBddff")


def raw_sat_data(b):
    """Packet 60 payload (one part of an epoch) -> (unix, nanos, clock offset ns, receiver, part, parts, sats);
    sats: [(system, prn, elevation, azimuth, [(frequency, tracking, carrier cycles, pseudorange m, doppler Hz,
    snr)])]."""
    ut, ns, clk, rx, pn, tot, nsat = _S60H.unpack_from(b, 0)
    o = 16
    sats = []
    n = len(b)
    for _ in range(nsat):
        if o + 6 > n:
            break
        sysid, prn, el, az, nf = _S60S.unpack_from(b, o)
        o += 6
        freqs = []
        for _ in range(nf):
            if o + 26 > n:
                break
            freqs.append(_S60F.unpack_from(b, o))
            o += 26
        sats.append((sysid, prn, el, az, freqs))
    return ut, ns, clk, rx, pn, tot, sats


def gps_ephemeris(b):
    """Packet 61 payload of a Keplerian (GPS-style, 132-byte) ephemeris -> dict."""
    u = struct.unpack_from
    return {
        "unix": u("<I", b, 0)[0], "system": b[4], "prn": b[5],
        "toe": u("<I", b, 6)[0], "iodc": u("<H", b, 10)[0], "iode": u("<H", b, 12)[0],
        "af0": u("<f", b, 14)[0], "af1": u("<f", b, 18)[0], "af2": u("<f", b, 22)[0],
        "crs": u("<f", b, 26)[0], "delta_n": u("<f", b, 30)[0], "m0": u("<d", b, 34)[0],
        "cuc": u("<f", b, 42)[0], "e": u("<d", b, 46)[0], "cus": u("<f", b, 54)[0],
        "sqrt_a": u("<d", b, 58)[0], "cic": u("<f", b, 66)[0], "omega0": u("<d", b, 70)[0],
        "cis": u("<f", b, 78)[0], "i0": u("<d", b, 82)[0], "crc": u("<f", b, 90)[0],
        "omega": u("<d", b, 94)[0], "omega_dot": u("<d", b, 102)[0],
        "idot": u("<d", b, 110)[0], "tgd": u("<f", b, 118)[0],
        "week": u("<H", b, 122)[0], "tx_time": u("<I", b, 124)[0],
        "ura": u("<H", b, 128)[0], "flags": u("<H", b, 130)[0],
    }


def device_info(b):
    """Packet 3 payload -> software/hardware version, device id, serial number."""
    sw, dev, hw = struct.unpack_from("<III", b, 0)
    s1, s2, s3 = struct.unpack_from("<III", b, 12)
    return {"software_version": sw, "device_id": dev, "hardware_version": hw,
            "serial": "%08X%08X%08X" % (s1, s2, s3)}
