"""LAS for every DCS plugin, pure numpy.

  LasWriter   LAS 1.4, point data record format 6, streamed in blocks so exports of
              any size use little memory; CRS as an OGC WKT VLR (deliverables)
  read_las    minimal reader for the files LasWriter writes
  write_las   LAS 1.2, point format 1, in one call with an EPSG GeoKey
              (quick files for checking points in QGIS)

LasWriter fields: X, Y, Z (scale 1 mm), intensity (8-bit Hesai value x 257),
return number / number of returns (strongest = 1 of 2, last = 2 of 2,
single = 1 of 1), classification, withheld flag, user data = laser channel,
point source id = line + 1, scan angle 0 (not known per point), GPS time =
adjusted standard GPS time.
"""
import datetime as _dt
import os
import struct

import numpy as np

from .timeutil import LEAP

PDRF6 = np.dtype([("X", "<i4"), ("Y", "<i4"), ("Z", "<i4"), ("intensity", "<u2"), ("returns", "u1"),
                  ("flags", "u1"), ("classification", "u1"), ("user_data", "u1"), ("scan_angle", "<i2"),
                  ("point_source_id", "<u2"), ("gps_time", "<f8")])
assert PDRF6.itemsize == 30
GPS_UNIX = 315964800.0


def utc_to_adjusted_gps(t, leap=LEAP):
    """UTC Unix seconds -> adjusted standard GPS time (GPS seconds - 1e9)."""
    return t - GPS_UNIX + leap - 1e9


class LasWriter:
    def __init__(self, path, offset, scale=0.001, wkt="", system="DCS", software="DCS Core (QGIS)"):
        self.path = path
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

    def write(self, E, N, Z, t, intensity, ret, cls, channel, line, withheld=None):
        """Append points. ret: 0 last, 1 strongest, 2 single (as dcs_core.hesai);
        withheld: bool array (or None) of points flagged withheld."""
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
        if withheld is not None:
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
        # global encoding: bit 0 = adjusted standard GPS time, bit 4 = CRS as WKT (required for PDRF 6-10)
        struct.pack_into("<4sHH", h, 0, b"LASF", 0, 0x0001 | 0x0010)
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

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        if et is not None:
            # an export that failed part way: never leave a LAS file that looks complete
            try:
                self.f.close()
            except OSError:
                pass
            try:
                os.remove(self.path)
            except OSError:
                pass
            return False
        if not self.f.closed:
            self.close()
        return False


def read_las(path):
    """Minimal reader for PDRF 6 files written by LasWriter -> dict of x, y, z and the raw records."""
    with open(path, "rb") as f:
        h = f.read(375)
        off = struct.unpack_from("<I", h, 96)[0]
        n = struct.unpack_from("<Q", h, 247)[0]
        sc = struct.unpack_from("<3d", h, 131)
        of = struct.unpack_from("<3d", h, 155)
        f.seek(off)
        a = np.fromfile(f, PDRF6, n)
    return {"x": a["X"] * sc[0] + of[0], "y": a["Y"] * sc[1] + of[1], "z": a["Z"] * sc[2] + of[2], "raw": a}


def write_las(path, x, y, z, intensity, source_id, gps_time, epsg=None, scale=0.001,
              system="AlignPro", software="AlignPro Lidar patch test", date=None):
    """LAS 1.2 point format 1 in one call, with an EPSG GeoKey when given. gps_time: seconds of the GPS week,
    or adjusted standard GPS time (negative or above one week; the global encoding then says so). The header
    date is `date`, else the date of the data when the times are adjusted standard GPS time, else today."""
    n = len(x)
    gt = np.asarray(gps_time, float)
    adjusted = bool(n) and (float(np.min(gt)) < 0 or float(np.min(gt)) > 604800.0)
    if date is None:
        date = (_dt.datetime.fromtimestamp(float(np.min(gt)) + 1e9 + GPS_UNIX - LEAP, _dt.timezone.utc).date()
                if adjusted else _dt.date.today())
    off = [float(np.floor(np.min(v))) if n else 0.0 for v in (x, y, z)]
    vlrs = b""
    nvlr = 0
    if epsg:
        keys = [1, 1, 0, 3,            # KeyDirectoryVersion, KeyRevision, MinorRevision, NumberOfKeys
                1024, 0, 1, 1,         # GTModelTypeGeoKey = projected
                1025, 0, 1, 1,         # GTRasterTypeGeoKey = PixelIsArea
                3072, 0, 1, int(epsg)]  # ProjectedCSTypeGeoKey
        data = struct.pack("<%dH" % len(keys), *keys)
        vlrs += struct.pack("<H16sHH32s", 0, b"LASF_Projection", 34735, len(data), b"GeoKeyDirectoryTag") + data
        nvlr = 1
    hdr_size = 227
    point_off = hdr_size + len(vlrs)
    rec = 28
    xi = np.round((np.asarray(x) - off[0]) / scale).astype("<i4")
    yi = np.round((np.asarray(y) - off[1]) / scale).astype("<i4")
    zi = np.round((np.asarray(z) - off[2]) / scale).astype("<i4")
    dt = np.dtype([("x", "<i4"), ("y", "<i4"), ("z", "<i4"), ("i", "<u2"), ("f", "u1"), ("c", "u1"),
                   ("a", "i1"), ("u", "u1"), ("s", "<u2"), ("t", "<f8")])
    pts = np.zeros(n, dt)
    pts["x"], pts["y"], pts["z"] = xi, yi, zi
    pts["i"] = np.asarray(intensity, np.uint16)
    pts["f"] = 0b00001001          # return 1 of 1
    pts["c"] = 1
    pts["s"] = np.asarray(source_id, np.uint16)
    pts["t"] = np.asarray(gps_time, float)
    mins = [float(np.min(v)) if n else 0.0 for v in (x, y, z)]
    maxs = [float(np.max(v)) if n else 0.0 for v in (x, y, z)]
    h = bytearray(hdr_size)
    struct.pack_into("<4sHH16sBB32s32sHHHIIBHI5I", h, 0, b"LASF", 0, 1 if adjusted else 0, b"\0" * 16, 1, 2,
                     system.encode()[:32].ljust(32, b"\0"), software.encode()[:32].ljust(32, b"\0"),
                     date.timetuple().tm_yday, date.year, hdr_size, point_off, nvlr, 1, rec, n, n, 0, 0, 0, 0)
    struct.pack_into("<3d3d6d", h, 131, scale, scale, scale, off[0], off[1], off[2],
                     maxs[0], mins[0], maxs[1], mins[1], maxs[2], mins[2])
    with open(path, "wb") as f:
        f.write(bytes(h))
        f.write(vlrs)
        f.write(pts.tobytes())
