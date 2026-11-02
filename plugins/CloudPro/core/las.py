"""LAS 1.4 writer (point data record format 6) and a minimal reader, pure
numpy, streamed tile by tile so exports of any size use little memory.

Fields written: X, Y, Z (mm), intensity (8-bit Hesai value x 257), return
number / number of returns (strongest = 1 of 2, last = 2 of 2, single = 1 of
1), classification, withheld flag (rejected points, when included), scanner
channel 0, user data = laser channel, point source id = line + 1, scan angle
(0.006 deg) from the azimuth of the return relative to the vehicle is not
known per point and is left 0, GPS time = adjusted standard GPS time.
CRS as an OGC WKT VLR.
"""
import datetime as _dt
import struct

import numpy as np

from .store import REJECTED

PDRF6 = np.dtype([("X", "<i4"), ("Y", "<i4"), ("Z", "<i4"), ("intensity", "<u2"), ("returns", "u1"),
                  ("flags", "u1"), ("classification", "u1"), ("user_data", "u1"), ("scan_angle", "<i2"),
                  ("point_source_id", "<u2"), ("gps_time", "<f8")])
assert PDRF6.itemsize == 30
GPS_UNIX = 315964800.0
LEAP = 18


def utc_to_adjusted_gps(t):
    return t - GPS_UNIX + LEAP - 1e9


class LasWriter:
    def __init__(self, path, offset, scale=0.001, wkt="", system="CloudPro", software="CloudPro (QGIS)"):
        self.f = open(path, "wb")
        self.offset = np.asarray(offset, float)
        self.scale = scale
        self.n = 0
        self.by_return = np.zeros(15, np.int64)
        self.mins = np.full(3, np.inf)
        self.maxs = np.full(3, -np.inf)
        self.wkt = (wkt or "").encode("utf-8") + b"\x00" if wkt else b""
        self.system = system
        self.software = software
        self._write_header()

    def _write_header(self):
        vlr = b""
        if self.wkt:
            vlr = struct.pack("<H16sHH32s", 0, b"LASF_Projection", 2112, len(self.wkt),
                              b"OGC WKT coordinate system") + self.wkt
        self.vlr = vlr
        self.offset_to_points = 375 + len(vlr)
        self.f.write(b"\x00" * self.offset_to_points)

    def write(self, E, N, Z, t, intensity, ret, cls, channel, line, flag):
        n = len(E)
        if n == 0:
            return
        r = np.zeros(n, PDRF6)
        r["X"] = np.round((E - self.offset[0]) / self.scale).astype(np.int32)
        r["Y"] = np.round((N - self.offset[1]) / self.scale).astype(np.int32)
        r["Z"] = np.round((Z - self.offset[2]) / self.scale).astype(np.int32)
        r["intensity"] = intensity.astype(np.uint16) * 257
        rn = np.where(ret == 1, 1, np.where(ret == 0, 2, 1)).astype(np.uint8)
        nr = np.where(ret == 2, 1, 2).astype(np.uint8)
        r["returns"] = rn | (nr << 4)
        withheld = (flag & REJECTED) != 0
        r["flags"] = np.where(withheld, 0x04, 0).astype(np.uint8)
        r["classification"] = cls
        r["user_data"] = channel
        r["point_source_id"] = (line.astype(np.int64) + 1).astype(np.uint16)
        r["gps_time"] = utc_to_adjusted_gps(t)
        r.tofile(self.f)
        self.n += n
        np.add.at(self.by_return, rn - 1, 1)
        for k, v in enumerate((E, N, Z)):
            self.mins[k] = min(self.mins[k], float(v.min()))
            self.maxs[k] = max(self.maxs[k], float(v.max()))

    def close(self):
        today = _dt.date.today()
        h = bytearray(375)
        struct.pack_into("<4sHH", h, 0, b"LASF", 0, 0x0001 | (0x0010 if self.wkt else 0))
        struct.pack_into("<BB32s32sHHHII", h, 24, 1, 4, self.system.encode()[:32], self.software.encode()[:32],
                         today.timetuple().tm_yday, today.year, 375, self.offset_to_points, 1 if self.wkt else 0)
        struct.pack_into("<BHI", h, 104, 6, 30, 0)                      # legacy count 0 for PDRF 6
        struct.pack_into("<5I", h, 111, 0, 0, 0, 0, 0)
        struct.pack_into("<3d3d", h, 131, self.scale, self.scale, self.scale, *self.offset)
        mx = self.maxs if self.n else np.zeros(3)
        mn = self.mins if self.n else np.zeros(3)
        struct.pack_into("<6d", h, 179, mx[0], mn[0], mx[1], mn[1], mx[2], mn[2])
        struct.pack_into("<QQIQ", h, 227, 0, 0, 0, self.n)
        struct.pack_into("<15Q", h, 255, *[int(v) for v in self.by_return])
        self.f.seek(0)
        self.f.write(bytes(h))
        self.f.write(self.vlr)
        self.f.close()


def read_las(path):
    """Minimal reader (PDRF 6 written by CloudPro) -> dict of arrays."""
    with open(path, "rb") as f:
        h = f.read(375)
        off = struct.unpack_from("<I", h, 96)[0]
        fmt, rl = struct.unpack_from("<BH", h, 104)
        n = struct.unpack_from("<Q", h, 247)[0]
        sc = struct.unpack_from("<3d", h, 131)
        of = struct.unpack_from("<3d", h, 155)
        f.seek(off)
        a = np.fromfile(f, PDRF6, n)
    return {"x": a["X"] * sc[0] + of[0], "y": a["Y"] * sc[1] + of[1], "z": a["Z"] * sc[2] + of[2], "raw": a}
