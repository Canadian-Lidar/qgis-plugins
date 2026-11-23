"""Writers of small point-cloud files in every format CloudPro 2.16.0 reads, for the tests (each written from
its format description, independently of core/pcread.py)."""
import math
import os
import struct

import numpy as np

GPS_UNIX = 315964800.0


def cloud(n=5000, seed=1, x0=2_490_000.0, y0=7_440_000.0):
    """A sloping plane with a box on it: x, y, z, intensity (0-255), rgb, class, time (UTC), source id."""
    r = np.random.default_rng(seed)
    x = x0 + r.uniform(0, 40, n)
    y = y0 + r.uniform(0, 30, n)
    z = 20.0 + 0.05 * (x - x0) + r.normal(0, 0.01, n)
    box = (x - x0 > 10) & (x - x0 < 14) & (y - y0 > 10) & (y - y0 < 14)
    z[box] += 2.0
    i = r.integers(0, 256, n).astype(np.uint8)
    rgb = np.column_stack([r.integers(0, 256, n), r.integers(0, 256, n), r.integers(0, 256, n)]).astype(np.uint8)
    cls = np.where(box, 6, 2).astype(np.uint8)
    t = 1_790_000_000.0 + np.sort(r.uniform(0, 60, n))
    src = np.where(x - x0 < 20, 1, 2).astype(np.uint16)
    return {"x": x, "y": y, "z": z, "i": i, "rgb": rgb, "cls": cls, "t": t, "src": src}


def _geokeys(epsg):
    keys = [1, 1, 0, 3, 1024, 0, 1, 1, 1025, 0, 1, 1, 3072, 0, 1, int(epsg)]
    data = struct.pack("<%dH" % len(keys), *keys)
    return struct.pack("<H16sHH32s", 0, b"LASF_Projection", 34735, len(data), b"GeoKeyDirectoryTag") + data


def write_las_legacy(path, c, epsg=2953, pdrf=3):
    """LAS 1.2, point format 3 (time + RGB, 16-bit colour and intensity), GeoKeys EPSG."""
    n = len(c["x"])
    dt = np.dtype([("X", "<i4"), ("Y", "<i4"), ("Z", "<i4"), ("I", "<u2"), ("b14", "u1"), ("b15", "u1"),
                   ("scan", "i1"), ("user", "u1"), ("psid", "<u2"), ("gps", "<f8"), ("R", "<u2"), ("G", "<u2"),
                   ("B", "<u2")])
    off = [math.floor(c["x"].min()), math.floor(c["y"].min()), 0.0]
    a = np.zeros(n, dt)
    a["X"] = np.round((c["x"] - off[0]) / 0.001)
    a["Y"] = np.round((c["y"] - off[1]) / 0.001)
    a["Z"] = np.round((c["z"] - off[2]) / 0.001)
    a["I"] = c["i"].astype(np.uint16) * 257
    a["b14"] = 1 | (1 << 3)
    a["b15"] = c["cls"]
    a["psid"] = c["src"]
    a["gps"] = c["t"] - GPS_UNIX + 18 - 1e9
    for k, ch in enumerate("RGB"):
        a[ch] = c["rgb"][:, k].astype(np.uint16) * 257
    vlr = _geokeys(epsg) if epsg else b""
    h = bytearray(227)
    struct.pack_into("<4sHH", h, 0, b"LASF", 0, 1)
    struct.pack_into("<BB", h, 24, 1, 2)
    struct.pack_into("<HIIBHI", h, 94, 227, 227 + len(vlr), 1 if vlr else 0, pdrf, dt.itemsize, n)
    struct.pack_into("<3d3d", h, 131, 0.001, 0.001, 0.001, *off)
    struct.pack_into("<6d", h, 179, c["x"].max(), c["x"].min(), c["y"].max(), c["y"].min(), c["z"].max(), c["z"].min())
    with open(path, "wb") as f:
        f.write(bytes(h))
        f.write(vlr)
        a.tofile(f)


PDRF7 = np.dtype([("X", "<i4"), ("Y", "<i4"), ("Z", "<i4"), ("I", "<u2"), ("b14", "u1"), ("b15", "u1"),
                  ("cls", "u1"), ("user", "u1"), ("scan", "<i2"), ("psid", "<u2"), ("gps", "<f8"),
                  ("R", "<u2"), ("G", "<u2"), ("B", "<u2")])


def _pdrf7(c, off):
    n = len(c["x"])
    a = np.zeros(n, PDRF7)
    a["X"] = np.round((c["x"] - off[0]) / 0.001)
    a["Y"] = np.round((c["y"] - off[1]) / 0.001)
    a["Z"] = np.round((c["z"] - off[2]) / 0.001)
    a["I"] = c["i"]                          # 8-bit values left as they are (a writer that does not scale)
    a["b14"] = 1 | (1 << 4)
    a["cls"] = c["cls"]
    a["b15"] = np.where(c["cls"] == 7, 0x04, 0)
    a["psid"] = c["src"]
    a["gps"] = c["t"] - GPS_UNIX + 18 - 1e9
    for k, ch in enumerate("RGB"):
        a[ch] = c["rgb"][:, k]
    return a


def write_las14(path, c, wkt="", laz=False):
    """LAS 1.4, point format 7, WKT VLR; laz=True compresses it with the bundled lazrs."""
    off = [math.floor(c["x"].min()), math.floor(c["y"].min()), 0.0]
    a = _pdrf7(c, off)
    n = len(a)
    vlr = b""
    nv = 0
    if wkt:
        w = wkt.encode() + b"\0"
        vlr += struct.pack("<H16sHH32s", 0, b"LASF_Projection", 2112, len(w), b"WKT") + w
        nv += 1
    lzvlr = None
    if laz:
        from CloudPro.core import laz as L
        lz = L.module()
        lzvlr = lz.LazVlr.new_for_compression(7, 0)
        vlr += L.laszip_vlr(lzvlr.record_data())
        nv += 1
    h = bytearray(375)
    struct.pack_into("<4sHH", h, 0, b"LASF", 0, 0x11)
    struct.pack_into("<BB", h, 24, 1, 4)
    struct.pack_into("<HIIBH", h, 94, 375, 375 + len(vlr), nv, 7 | (0x80 if laz else 0), PDRF7.itemsize)
    struct.pack_into("<3d3d", h, 131, 0.001, 0.001, 0.001, *off)
    struct.pack_into("<6d", h, 179, c["x"].max(), c["x"].min(), c["y"].max(), c["y"].min(), c["z"].max(), c["z"].min())
    struct.pack_into("<Q", h, 247, n)
    with open(path, "wb") as f:
        f.write(bytes(h))
        f.write(vlr)
        if laz:
            comp = lz.LasZipCompressor(f, lzvlr)
            comp.compress_many(np.frombuffer(a.tobytes(), np.uint8))
            comp.done()
        else:
            a.tofile(f)


def write_text(path, c, header=True, sep=" ", cols=("x", "y", "z", "i", "r", "g", "b")):
    vals = {"x": c["x"], "y": c["y"], "z": c["z"], "i": c["i"], "r": c["rgb"][:, 0], "g": c["rgb"][:, 1],
            "b": c["rgb"][:, 2], "class": c["cls"], "time": c["t"]}
    arr = np.column_stack([vals[k] for k in cols])
    fmt = sep.join("%.3f" if k in ("x", "y", "z") else ("%.6f" if k == "time" else "%d") for k in cols)
    with open(path, "w") as f:
        if header:
            f.write(sep.join({"i": "intensity", "r": "red", "g": "green", "b": "blue"}.get(k, k) for k in cols) + "\n")
        np.savetxt(f, arr, fmt=fmt)


def write_pts(path, parts):
    """Leica PTS: blocks of count + 'x y z intensity r g b' (intensity -2048..2047)."""
    with open(path, "w") as f:
        for c in parts:
            f.write("%d\n" % len(c["x"]))
            ii = c["i"].astype(np.int64) * 16 - 2048
            arr = np.column_stack([c["x"], c["y"], c["z"], ii, c["rgb"]])
            np.savetxt(f, arr, fmt="%.3f %.3f %.3f %d %d %d %d")


def write_ply(path, c, binary=True):
    n = len(c["x"])
    dt = np.dtype([("x", "<f8"), ("y", "<f8"), ("z", "<f4"), ("red", "u1"), ("green", "u1"), ("blue", "u1"),
                   ("intensity", "<u2")])
    a = np.zeros(n, dt)
    a["x"], a["y"], a["z"] = c["x"], c["y"], c["z"]
    a["red"], a["green"], a["blue"] = c["rgb"].T
    a["intensity"] = c["i"].astype(np.uint16) * 257
    head = ("ply\nformat %s 1.0\ncomment made by the CloudPro tests\nelement vertex %d\nproperty double x\n"
            "property double y\nproperty float z\nproperty uchar red\nproperty uchar green\nproperty uchar blue\n"
            "property ushort intensity\nend_header\n" % ("binary_little_endian" if binary else "ascii", n))
    with open(path, "wb") as f:
        f.write(head.encode())
        if binary:
            a.tofile(f)
        else:
            for r in a:
                f.write(("%.3f %.3f %.3f %d %d %d %d\n" % tuple(r)).encode())


def write_pcd(path, c):
    n = len(c["x"])
    rgb = (c["rgb"][:, 0].astype(np.uint32) << 16) | (c["rgb"][:, 1].astype(np.uint32) << 8) | c["rgb"][:, 2]
    dt = np.dtype([("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("rgb", "<f4")])
    a = np.zeros(n, dt)
    a["x"], a["y"], a["z"] = c["x"] - 2_490_000, c["y"] - 7_440_000, c["z"]
    a["rgb"] = rgb.view(np.float32)
    head = ("# .PCD v0.7\nVERSION 0.7\nFIELDS x y z rgb\nSIZE 4 4 4 4\nTYPE F F F F\nCOUNT 1 1 1 1\nWIDTH %d\n"
            "HEIGHT 1\nVIEWPOINT 0 0 0 1 0 0 0\nPOINTS %d\nDATA binary\n" % (n, n))
    with open(path, "wb") as f:
        f.write(head.encode())
        a.tofile(f)


# ---------------------------------------------------------------------------------------------- E57
def _crc32c(data):
    crc = 0xFFFFFFFF
    tbl = _CRC_TABLE
    for b in data:
        crc = tbl[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


def _table():
    t = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0x82F63B78 if c & 1 else c >> 1
        t.append(c)
    return t


_CRC_TABLE = _table()


def _bitpack(vals, lo, hi):
    bits = int(math.ceil(math.log2(hi - lo + 1)))
    v = (np.asarray(vals, np.int64) - lo)
    b = ((v[:, None] >> np.arange(bits)) & 1).astype(np.uint8).ravel()
    return np.packbits(b, bitorder="little").tobytes()


def write_e57(path, scans, crc=True):
    """scans: [{"name", "x", "y", "z" (scanner frame), "i" (0-1000), "rgb", "invalid" (bool) or None,
    "rotation" (w, x, y, z), "translation" (x, y, z)}]. cartesianX ScaledInteger (0.1 mm), cartesianY double,
    cartesianZ single, intensity Integer 0..1000, colours Integer 0..255, cartesianInvalidState 0..2; streams
    split into data packets of at most 4 kB so values cross packet and page boundaries."""
    PAGE, DATA = 1024, 1020
    logical = bytearray(48)                                     # header filled in at the end
    secs = []
    for sc in scans:
        while len(logical) % 4:
            logical += b"\0"
        sec_start = len(logical)
        n = len(sc["x"])
        lo_x, hi_x = -2_000_000, 2_000_000
        rx = np.round(np.asarray(sc["x"]) / 1e-4).astype(np.int64)
        inv = np.zeros(n, np.int64) if sc.get("invalid") is None else np.where(sc["invalid"], 2, 0)
        streams = [_bitpack(rx, lo_x, hi_x),
                   np.asarray(sc["y"], "<f8").tobytes(),
                   np.asarray(sc["z"], "<f4").tobytes(),
                   _bitpack(sc["i"], 0, 1000),
                   _bitpack(sc["rgb"][:, 0], 0, 255), _bitpack(sc["rgb"][:, 1], 0, 255),
                   _bitpack(sc["rgb"][:, 2], 0, 255),
                   _bitpack(inv, 0, 2)]
        body = bytearray()
        pos = [0] * len(streams)
        step = 4096 // len(streams) - 8
        while any(p < len(s) for p, s in zip(pos, streams)):
            parts = []
            for k, s in enumerate(streams):
                parts.append(s[pos[k]:pos[k] + step])
                pos[k] += len(parts[-1])
            pk = struct.pack("<BBHH", 1, 0, 0, len(streams)) + struct.pack("<%dH" % len(parts), *[len(p) for p in parts])
            pk += b"".join(parts)
            while len(pk) % 4:
                pk += b"\0"
            pk = pk[:2] + struct.pack("<H", len(pk) - 1) + pk[4:]
            body += pk
        data_logical = sec_start + 32
        logical += b"\0" * 32 + body
        secs.append((sec_start, data_logical, len(logical) - sec_start, n))

    def phys(lg):
        return (lg // DATA) * PAGE + lg % DATA
    for sc, (s0, d0, ln, n) in zip(scans, secs):
        logical[s0:s0 + 32] = struct.pack("<B7sQQQ", 1, b"\0" * 7, ln, phys(d0), 0)
    while len(logical) % 4:
        logical += b"\0"
    xml_start = len(logical)
    items = []
    for sc, (s0, d0, ln, n) in zip(scans, secs):
        w, x, y, z = sc.get("rotation", (1, 0, 0, 0))
        tx, ty, tz = sc.get("translation", (0, 0, 0))
        items.append(
            '<vectorChild type="Structure"><name type="String"><![CDATA[%s]]></name>'
            '<pose type="Structure"><rotation type="Structure"><w type="Float">%r</w><x type="Float">%r</x>'
            '<y type="Float">%r</y><z type="Float">%r</z></rotation><translation type="Structure">'
            '<x type="Float">%r</x><y type="Float">%r</y><z type="Float">%r</z></translation></pose>'
            '<intensityLimits type="Structure"><intensityMinimum type="Integer">0</intensityMinimum>'
            '<intensityMaximum type="Integer">1000</intensityMaximum></intensityLimits>'
            '<colorLimits type="Structure"><colorRedMinimum type="Integer">0</colorRedMinimum><colorRedMaximum '
            'type="Integer">255</colorRedMaximum><colorGreenMinimum type="Integer">0</colorGreenMinimum>'
            '<colorGreenMaximum type="Integer">255</colorGreenMaximum><colorBlueMinimum type="Integer">0'
            '</colorBlueMinimum><colorBlueMaximum type="Integer">255</colorBlueMaximum></colorLimits>'
            '<points type="CompressedVector" fileOffset="%d" recordCount="%d"><prototype type="Structure">'
            '<cartesianX type="ScaledInteger" minimum="-2000000" maximum="2000000" scale="0.0001" offset="0"/>'
            '<cartesianY type="Float" precision="double"/><cartesianZ type="Float" precision="single"/>'
            '<intensity type="Integer" minimum="0" maximum="1000"/>'
            '<colorRed type="Integer" minimum="0" maximum="255"/><colorGreen type="Integer" minimum="0" maximum="255"/>'
            '<colorBlue type="Integer" minimum="0" maximum="255"/>'
            '<cartesianInvalidState type="Integer" minimum="0" maximum="2"/></prototype>'
            '<codecs type="Vector" allowHeterogeneousChildren="1"/></points></vectorChild>'
            % (sc["name"], w, x, y, z, tx, ty, tz, phys(s0), n))
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<e57Root type="Structure" '
           'xmlns="http://www.astm.org/COMMIT/E57/2010-e57-v1.0"><formatName type="String"><![CDATA[ASTM E57 3D '
           'Imaging Data File]]></formatName><versionMajor type="Integer">1</versionMajor><coordinateMetadata '
           'type="String"><![CDATA[EPSG:2953]]></coordinateMetadata><data3D type="Vector" '
           'allowHeterogeneousChildren="1">%s</data3D><images2D type="Vector" allowHeterogeneousChildren="1"/>'
           '</e57Root>' % "".join(items)).encode()
    logical += xml
    while len(logical) % DATA:
        logical += b"\0"
    npages = len(logical) // DATA
    struct.pack_into("<8sIIQQQQ", logical, 0, b"ASTM-E57", 1, 0, npages * PAGE, phys(xml_start), len(xml), PAGE)
    out = bytearray()
    for k in range(npages):
        pg = bytes(logical[k * DATA:(k + 1) * DATA])
        out += pg + (struct.pack(">I", _crc32c(pg)) if crc else b"\0\0\0\0")
    with open(path, "wb") as f:
        f.write(out)
