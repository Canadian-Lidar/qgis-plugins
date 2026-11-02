"""Minimal LAS 1.2 writer (point format 1) with an EPSG GeoKey, for checking corrected points in QGIS."""
import struct

import numpy as np


def write_las(path, x, y, z, intensity, source_id, gps_time, epsg=None, scale=0.001):
    n = len(x)
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
    struct.pack_into("<4sHH16sBB32s32sHHHIIBHI5I", h, 0, b"LASF", 0, 0, b"\0" * 16, 1, 2,
                     b"AlignPro".ljust(32, b"\0"), b"AlignPro Lidar patch test".ljust(32, b"\0"),
                     1, 2026, hdr_size, point_off, nvlr, 1, rec, n, n, 0, 0, 0, 0)
    struct.pack_into("<3d3d6d", h, 131, scale, scale, scale, off[0], off[1], off[2],
                     maxs[0], mins[0], maxs[1], mins[1], maxs[2], mins[2])
    with open(path, "wb") as f:
        f.write(bytes(h))
        f.write(vlrs)
        f.write(pts.tobytes())
