"""Point-cloud files read in blocks (CloudPro 2.16.0, Sean 23 Nov 2026: CloudPro takes finished point clouds
without PosPro / AlignPro): LAS 1.0-1.4 (point formats 0-10), LAZ (bundled lazrs), E57 (pure numpy: XML,
pages with checksums, compressed vectors with the bit-pack codec, poses), PLY (ASCII and binary), Leica PTS,
and text columns (XYZ, TXT, CSV, ASC, PTX-free).

    src = open_source(path)            # header only: format, count, CRS, fields, parts
    for b in src.blocks(2_000_000):    # dicts of numpy arrays
        b["x"], b["y"], b["z"]         # float64, the file's coordinates (pose applied for E57)
        b.get("i")                     # uint8 intensity (scaled to 0-255 for the whole file)
        b.get("t")                     # float64 UTC Unix seconds (LAS adjusted GPS time), or seconds as stored
        b.get("ret")                   # uint8 CloudPro return code: 2 single, 1 first / intermediate, 0 last
        b.get("cls")                   # uint8 class (ASPRS), b.get("withheld") bool
        b.get("rgb")                   # (n, 3) uint8
        b.get("part")                  # int64 part (LAS point source id, E57 scan, PTS block) for the line split

Every reader stops at the end of the file it was given; a damaged file raises PointCloudError with the place.
"""
import math
import os
import re
import struct
import xml.etree.ElementTree as ET

import numpy as np

GPS_UNIX = 315964800.0
LEAP = 18
EXTENSIONS = (".las", ".laz", ".e57", ".ply", ".pts", ".xyz", ".txt", ".csv", ".asc", ".pcd")
FILTER = "Point clouds (*.las *.laz *.e57 *.ply *.pts *.xyz *.txt *.csv *.asc *.pcd)"


class PointCloudError(ValueError):
    pass


def open_source(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".las", ".laz"):
        return LasSource(path)
    if ext == ".e57":
        return E57Source(path)
    if ext == ".ply":
        return PlySource(path)
    if ext == ".pts":
        return PtsSource(path)
    if ext == ".pcd":
        return PcdSource(path)
    if ext in (".xyz", ".txt", ".csv", ".asc"):
        return TextSource(path)
    raise PointCloudError("%s: not a point-cloud file CloudPro reads (%s)" % (os.path.basename(path),
                                                                           ", ".join(EXTENSIONS)))


class Source:
    fmt = "?"

    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        self.count = None           # points in the file when the header says
        self.crs = ""               # "EPSG:n" or WKT, "" when the file does not say
        self.vertical = ""
        self.has_rgb = False
        self.has_time = False
        self.has_intensity = False
        self.has_class = False
        self.time_basis = ""
        self.parts = {}             # part id -> label (E57 scans, PTS blocks); LAS ids are found while reading
        self.part_kind = ""         # "point source" / "scan" / "block"
        self.bbox = None            # (xmin, ymin, zmin, xmax, ymax, zmax) when the header says
        self.notes = []

    def describe(self):
        bits = [self.fmt]
        if self.count is not None:
            bits.append("{:,} points".format(self.count))
        if self.has_rgb:
            bits.append("RGB")
        if self.has_intensity:
            bits.append("intensity")
        if self.has_class:
            bits.append("classes")
        if self.has_time:
            bits.append("time")
        if len(self.parts) > 1:
            bits.append("%d %ss" % (len(self.parts), self.part_kind or "part"))
        return ", ".join(bits)

    def blocks(self, size=2_000_000):
        raise NotImplementedError


def _u8_scale(maxval):
    """Factor bringing a 16-bit field (intensity, colour) to 0-255 for the whole file."""
    return 1.0 / 257.0 if maxval > 255 else 1.0


def _ret_code(rn, nr):
    rn = rn.astype(np.int16)
    nr = np.maximum(nr.astype(np.int16), 1)
    out = np.ones(len(rn), np.uint8)
    out[(rn >= nr) & (nr > 1)] = 0
    out[nr == 1] = 2
    return out


# --------------------------------------------------------------------------------------------- LAS / LAZ
def _las_dtype(pdrf, reclen):
    legacy = pdrf <= 5
    names, fmts, offs = ["X", "Y", "Z", "intensity"], ["<i4", "<i4", "<i4", "<u2"], [0, 4, 8, 12]
    if legacy:
        names += ["b14", "b15", "scan", "user", "psid"]
        fmts += ["u1", "u1", "i1", "u1", "<u2"]
        offs += [14, 15, 16, 17, 18]
        o = 20
        if pdrf in (1, 3, 4, 5):
            names.append("gps")
            fmts.append("<f8")
            offs.append(o)
            o += 8
        if pdrf in (2, 3, 5):
            names += ["R", "G", "B"]
            fmts += ["<u2"] * 3
            offs += [o, o + 2, o + 4]
    else:
        names += ["b14", "b15", "cls", "user", "scan", "psid", "gps"]
        fmts += ["u1", "u1", "u1", "u1", "<i2", "<u2", "<f8"]
        offs += [14, 15, 16, 17, 18, 20, 22]
        if pdrf in (7, 8, 10):
            names += ["R", "G", "B"]
            fmts += ["<u2"] * 3
            offs += [30, 32, 34]
    need = max(o + np.dtype(f).itemsize for o, f in zip(offs, fmts))
    if reclen < need:
        raise PointCloudError("point record of %d bytes is too short for point format %d" % (reclen, pdrf))
    return np.dtype({"names": names, "formats": fmts, "offsets": offs, "itemsize": reclen})


def _geokeys_epsg(data):
    """EPSG code from a GeoKeyDirectoryTag (projected first, then geographic), or None."""
    try:
        v = struct.unpack("<%dH" % (len(data) // 2), data)
    except struct.error:
        return None, None
    n = v[3]
    proj = geog = vert = None
    for k in range(n):
        key, loc, _cnt, val = v[4 + 4 * k: 8 + 4 * k]
        if loc != 0:
            continue
        if key == 3072 and val not in (0, 32767):
            proj = val
        elif key == 2048 and val not in (0, 32767):
            geog = val
        elif key == 4096 and val not in (0, 32767):
            vert = val
    return proj or geog, vert


class LasSource(Source):
    def __init__(self, path):
        super().__init__(path)
        with open(path, "rb") as f:
            h = f.read(375)
            if len(h) < 227 or h[:4] != b"LASF":
                raise PointCloudError("%s: not a LAS file" % self.name)
            self.global_enc = struct.unpack_from("<H", h, 6)[0]
            self.minor = h[25]
            hsize = struct.unpack_from("<H", h, 94)[0]
            self.point_off = struct.unpack_from("<I", h, 96)[0]
            nvlr = struct.unpack_from("<I", h, 100)[0]
            raw_fmt = h[104]
            self.compressed = bool(raw_fmt & 0xC0)
            self.pdrf = raw_fmt & 0x3F
            self.reclen = struct.unpack_from("<H", h, 105)[0]
            legacy_n = struct.unpack_from("<I", h, 107)[0]
            self.scale = np.array(struct.unpack_from("<3d", h, 131))
            self.offset = np.array(struct.unpack_from("<3d", h, 155))
            mx = struct.unpack_from("<6d", h, 179)
            self.bbox = (mx[1], mx[3], mx[5], mx[0], mx[2], mx[4])
            n14 = struct.unpack_from("<Q", h, 247)[0] if self.minor >= 4 and len(h) >= 255 else 0
            self.count = int(n14 or legacy_n)
            f.seek(hsize)
            self.laszip = None
            wkt = epsg = None
            vert = None
            for _ in range(nvlr):
                vh = f.read(54)
                if len(vh) < 54:
                    break
                uid = vh[2:18].split(b"\0")[0]
                rid = struct.unpack_from("<H", vh, 18)[0]
                ln = struct.unpack_from("<H", vh, 20)[0]
                data = f.read(ln)
                if uid == b"laszip encoded" and rid == 22204:
                    self.laszip = data
                elif uid == b"LASF_Projection" and rid == 2112:
                    wkt = data.split(b"\0")[0].decode("utf-8", "replace").strip()
                elif uid == b"LASF_Projection" and rid == 34735:
                    epsg, vert = _geokeys_epsg(data)
        if self.pdrf > 10:
            raise PointCloudError("%s: point format %d is not known" % (self.name, self.pdrf))
        if self.compressed and self.laszip is None:
            raise PointCloudError("%s: compressed (LAZ) without its 'laszip encoded' record" % self.name)
        self.dtype = _las_dtype(self.pdrf, self.reclen)
        self.fmt = "LAZ" if self.compressed else "LAS 1.%d" % self.minor
        self.fmt += " (point format %d)" % self.pdrf
        self.crs = ("EPSG:%d" % epsg) if epsg else (wkt or "")
        self.vertical = ("EPSG:%d" % vert) if vert else ""
        self.has_rgb = self.pdrf in (2, 3, 5, 7, 8, 10)
        self.has_time = self.pdrf not in (0, 2)
        self.time_basis = ("UTC (from adjusted standard GPS time)" if self.global_enc & 1 else
                           "GPS seconds of the week (the week is not in the file)") if self.has_time else ""
        self.has_intensity = True
        self.has_class = True
        self.part_kind = "point source"
        self._iscale = self._cscale = None

    def _records(self, size):
        if not self.compressed:
            with open(self.path, "rb") as f:
                f.seek(self.point_off)
                left = self.count
                while left > 0:
                    k = min(size, left)
                    a = np.fromfile(f, self.dtype, k)
                    if len(a) == 0:
                        break
                    left -= len(a)
                    yield a
                    if len(a) < k:
                        self.notes.append("the file ends %d points early" % left)
                        break
            return
        from . import laz
        lz = laz.module()
        if lz is None:
            raise PointCloudError("%s: LAZ needs the bundled lazrs library (%s)" % (self.name, laz.why()))
        with open(self.path, "rb") as f:
            f.seek(self.point_off)
            dec = lz.LasZipDecompressor(f, self.laszip)
            left = self.count
            while left > 0:
                k = min(size, left)
                buf = bytearray(k * self.reclen)
                dec.decompress_many(buf)
                left -= k
                yield np.frombuffer(bytes(buf), self.dtype)

    def blocks(self, size=2_000_000):
        legacy = self.pdrf <= 5
        for a in self._records(size):
            b = {"x": a["X"] * self.scale[0] + self.offset[0], "y": a["Y"] * self.scale[1] + self.offset[1],
                 "z": a["Z"] * self.scale[2] + self.offset[2]}
            it = a["intensity"]
            if self._iscale is None:
                self._iscale = _u8_scale(int(it.max()) if len(it) else 0)
            b["i"] = np.clip(np.round(it * self._iscale), 0, 255).astype(np.uint8)
            if legacy:
                rn, nr = a["b14"] & 7, (a["b14"] >> 3) & 7
                b["cls"] = (a["b15"] & 31).astype(np.uint8)
                b["withheld"] = (a["b15"] & 0x80) != 0
            else:
                rn, nr = a["b14"] & 15, (a["b14"] >> 4) & 15
                b["cls"] = a["cls"].astype(np.uint8)
                b["withheld"] = (a["b15"] & 0x04) != 0
            b["ret"] = _ret_code(rn, nr)
            if self.has_time:
                g = a["gps"].astype(np.float64)
                b["t"] = g + 1e9 + GPS_UNIX - LEAP if self.global_enc & 1 else g
            if self.has_rgb:
                if self._cscale is None:
                    m = max(int(a["R"].max()), int(a["G"].max()), int(a["B"].max())) if len(a) else 0
                    self._cscale = _u8_scale(m)
                b["rgb"] = np.clip(np.round(np.column_stack([a["R"], a["G"], a["B"]]) * self._cscale),
                                   0, 255).astype(np.uint8)
            b["part"] = a["psid"].astype(np.int64)
            yield b


# --------------------------------------------------------------------------------------------- E57
_NS = "{http://www.astm.org/COMMIT/E57/2010-e57-v1.0}"


def _tag(el):
    return el.tag.split("}")[-1]


def _child(el, name):
    for c in el:
        if _tag(c) == name:
            return c
    return None


def _num(el, default=0.0):
    if el is None or el.text is None or not el.text.strip():
        return default
    return float(el.text)


class _E57File:
    """Logical reading of an E57 file: each 1024-byte physical page ends with a 4-byte CRC-32C."""

    def __init__(self, path):
        self.f = open(path, "rb")
        h = self.f.read(48)
        if len(h) < 48 or h[:8] != b"ASTM-E57":
            raise PointCloudError("%s: not an E57 file" % os.path.basename(path))
        (self.major, self.minor, self.length, self.xml_off, self.xml_len,
         self.page) = struct.unpack_from("<IIQQQQ", h, 8)
        self.page = self.page or 1024
        self.data = self.page - 4

    def phys_to_logical(self, p):
        return (p // self.page) * self.data + (p % self.page)

    def read(self, logical, n):
        out = bytearray()
        while n > 0:
            pg, off = divmod(logical, self.data)
            self.f.seek(pg * self.page + off)
            k = min(n, self.data - off)
            chunk = self.f.read(k)
            if len(chunk) < k:
                raise PointCloudError("E57 file ends early (logical offset %d)" % logical)
            out += chunk
            logical += k
            n -= k
        return bytes(out)

    def read_phys(self, phys, n):
        return self.read(self.phys_to_logical(phys), n)

    def close(self):
        self.f.close()


class _Stream:
    """One field's byte stream of a compressed vector, decoded as values arrive (bit-pack codec)."""

    def __init__(self, proto):
        self.name = _tag(proto)
        t = proto.get("type")
        self.kind = t
        self.buf = bytearray()
        self.bitpos = 0
        self.out = []
        self.count = 0
        if t == "Float":
            self.nbytes = 4 if proto.get("precision") == "single" else 8
            self.bits = 8 * self.nbytes
            self.minv, self.scale, self.offset = 0, 1.0, 0.0
        else:
            lo = int(float(proto.get("minimum", "0")))
            hi = int(float(proto.get("maximum", "0")))
            self.minv = lo
            self.bits = max(0, int(math.ceil(math.log2(hi - lo + 1)))) if hi > lo else 0
            self.scale = float(proto.get("scale", "1")) if t == "ScaledInteger" else 1.0
            self.offset = float(proto.get("offset", "0")) if t == "ScaledInteger" else 0.0

    def feed(self, data):
        self.buf += data
        if self.kind == "Float":
            k = len(self.buf) // self.nbytes
            if k:
                a = np.frombuffer(bytes(self.buf[:k * self.nbytes]), "<f4" if self.nbytes == 4 else "<f8")
                self.out.append(a.astype(np.float64))
                del self.buf[:k * self.nbytes]
                self.count += k
            return
        if self.bits == 0:
            return
        total = len(self.buf) * 8 - self.bitpos
        k = total // self.bits
        if k == 0:
            return
        nb = (self.bitpos + k * self.bits + 7) // 8
        bits = np.unpackbits(np.frombuffer(bytes(self.buf[:nb]), np.uint8), bitorder="little")
        bits = bits[self.bitpos: self.bitpos + k * self.bits].reshape(k, self.bits).astype(np.int64)
        vals = (bits << np.arange(self.bits, dtype=np.int64)).sum(1) + self.minv
        self.out.append(vals * self.scale + self.offset if self.kind == "ScaledInteger" else vals.astype(np.float64))
        used = self.bitpos + k * self.bits
        del self.buf[:used // 8]
        self.bitpos = used % 8
        self.count += k

    def take(self, n):
        if self.kind != "Float" and self.bits == 0:          # a constant field
            v = self.minv * self.scale + self.offset
            return np.full(n, v, np.float64)
        a = np.concatenate(self.out) if len(self.out) > 1 else (self.out[0] if self.out else np.zeros(0))
        self.out = [a[n:]] if len(a) > n else []
        return a[:n]

    def available(self):
        if self.kind != "Float" and self.bits == 0:
            return None
        return sum(len(a) for a in self.out)


def _quat_matrix(w, x, y, z):
    n = math.sqrt(w * w + x * x + y * y + z * z) or 1.0
    w, x, y, z = w / n, x / n, y / n, z / n
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
                     [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
                     [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]])


class E57Source(Source):
    fmt = "E57"

    def __init__(self, path):
        super().__init__(path)
        ef = _E57File(path)
        try:
            xml = ef.read_phys(ef.xml_off, ef.xml_len)
        finally:
            ef.close()
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as e:
            raise PointCloudError("%s: the E57 XML section cannot be read (%s)" % (self.name, e))
        cm = _child(root, "coordinateMetadata")
        if cm is not None and cm.text and cm.text.strip():
            self.crs = cm.text.strip()
        d3 = _child(root, "data3D")
        self.scans = []
        if d3 is None:
            raise PointCloudError("%s: no point data (data3D) in the file" % self.name)
        total = 0
        for k, sc in enumerate(d3):
            pts = _child(sc, "points")
            if pts is None:
                continue
            proto = _child(pts, "prototype")
            fields = [_tag(c) for c in proto]
            name = _child(sc, "name")
            label = name.text.strip() if name is not None and name.text else "Scan %d" % (k + 1)
            pose = _child(sc, "pose")
            R, T = np.eye(3), np.zeros(3)
            if pose is not None:
                q = _child(pose, "rotation")
                t = _child(pose, "translation")
                if q is not None:
                    R = _quat_matrix(_num(_child(q, "w"), 1.0), _num(_child(q, "x")), _num(_child(q, "y")),
                                     _num(_child(q, "z")))
                if t is not None:
                    T = np.array([_num(_child(t, "x")), _num(_child(t, "y")), _num(_child(t, "z"))])
            lim = {}
            for grp, keys in (("intensityLimits", ("intensityMinimum", "intensityMaximum")),
                              ("colorLimits", ("colorRedMinimum", "colorRedMaximum", "colorGreenMinimum",
                                               "colorGreenMaximum", "colorBlueMinimum", "colorBlueMaximum"))):
                g = _child(sc, grp)
                if g is not None:
                    for kk in keys:
                        c = _child(g, kk)
                        if c is not None:
                            lim[kk] = _num(c)
            n = int(pts.get("recordCount", "0"))
            codecs = _child(pts, "codecs")
            if codecs is not None and len(codecs):
                for c in codecs:
                    if _tag(c) != "bitPackCodec" and len(c):
                        raise PointCloudError("%s: compressed with a codec CloudPro does not read" % self.name)
            self.scans.append({"label": label, "offset": int(pts.get("fileOffset", "0")), "count": n,
                               "proto": list(proto), "fields": fields, "R": R, "T": T, "limits": lim, "id": k})
            self.parts[k] = label
            total += n
        if not self.scans:
            raise PointCloudError("%s: no point data in the file" % self.name)
        allf = set(f for s in self.scans for f in s["fields"])
        if not ({"cartesianX", "cartesianY", "cartesianZ"} <= allf or
                {"sphericalRange", "sphericalAzimuth", "sphericalElevation"} <= allf):
            raise PointCloudError("%s: no cartesian or spherical coordinates" % self.name)
        self.count = total
        self.has_rgb = {"colorRed", "colorGreen", "colorBlue"} <= allf
        self.has_intensity = "intensity" in allf
        self.has_time = "timeStamp" in allf
        self.time_basis = "as stored in the scan (seconds)" if self.has_time else ""
        self.part_kind = "scan"

    def _scan_records(self, ef, sc, size):
        hdr = ef.read_phys(sc["offset"], 32)
        sid, _r, _sec_len, data_off, _idx = struct.unpack("<B7sQQQ", hdr)
        if sid != 1:
            raise PointCloudError("%s: %s: not a compressed vector section" % (self.name, sc["label"]))
        streams = [_Stream(p) for p in sc["proto"]]
        pos = ef.phys_to_logical(data_off)
        got = 0
        n = sc["count"]
        while got < n:
            ph = ef.read(pos, 6)
            ptype, _flags, lm1, nstreams = struct.unpack("<BBHH", ph)
            plen = lm1 + 1
            if ptype == 1:
                lens = struct.unpack("<%dH" % nstreams, ef.read(pos + 6, 2 * nstreams))
                body = ef.read(pos + 6 + 2 * nstreams, sum(lens))
                o = 0
                for s, ln in zip(streams, lens):
                    s.feed(body[o:o + ln])
                    o += ln
                av = [s.available() for s in streams]
                av = [a for a in av if a is not None]
                k = min(av) if av else 0
                while k >= size or (k > 0 and got + k >= n):
                    m = min(k, size, n - got)
                    yield {s.name: s.take(m) for s in streams}, m
                    got += m
                    k -= m
                    if got >= n:
                        break
            elif ptype not in (0, 2):
                raise PointCloudError("%s: %s: damaged packet at logical offset %d" % (self.name, sc["label"], pos))
            pos += plen
            if ptype == 0 and got < n and plen <= 0:
                break

    def blocks(self, size=2_000_000):
        ef = _E57File(self.path)
        try:
            for sc in self.scans:
                lim = sc["limits"]
                for rec, m in self._scan_records(ef, sc, size):
                    if "cartesianX" in rec:
                        P = np.column_stack([rec["cartesianX"], rec["cartesianY"], rec["cartesianZ"]])
                        bad = rec["cartesianInvalidState"] != 0 if "cartesianInvalidState" in rec else None
                    else:
                        r, az, el = rec["sphericalRange"], rec["sphericalAzimuth"], rec["sphericalElevation"]
                        P = np.column_stack([r * np.cos(el) * np.cos(az), r * np.cos(el) * np.sin(az), r * np.sin(el)])
                        bad = rec["sphericalInvalidState"] != 0 if "sphericalInvalidState" in rec else None
                    P = P @ sc["R"].T + sc["T"]
                    b = {"x": P[:, 0], "y": P[:, 1], "z": P[:, 2]}
                    if "intensity" in rec:
                        lo = lim.get("intensityMinimum", float(np.min(rec["intensity"])) if m else 0)
                        hi = lim.get("intensityMaximum", float(np.max(rec["intensity"])) if m else 1)
                        b["i"] = np.clip(np.round((rec["intensity"] - lo) / max(hi - lo, 1e-12) * 255), 0, 255
                                         ).astype(np.uint8)
                    if {"colorRed", "colorGreen", "colorBlue"} <= set(rec):
                        cs = []
                        for c in ("Red", "Green", "Blue"):
                            lo = lim.get("color%sMinimum" % c, 0.0)
                            hi = lim.get("color%sMaximum" % c, 255.0)
                            cs.append(np.clip(np.round((rec["color" + c] - lo) / max(hi - lo, 1e-12) * 255), 0, 255))
                        b["rgb"] = np.column_stack(cs).astype(np.uint8)
                    if "timeStamp" in rec:
                        b["t"] = rec["timeStamp"].astype(np.float64)
                    b["part"] = np.full(m, sc["id"], np.int64)
                    if bad is not None and bad.any():
                        keep = ~bad
                        b = {k: v[keep] for k, v in b.items()}
                    yield b
        finally:
            ef.close()


# --------------------------------------------------------------------------------------------- PLY
_PLY_TYPES = {"char": "i1", "int8": "i1", "uchar": "u1", "uint8": "u1", "short": "i2", "int16": "i2",
              "ushort": "u2", "uint16": "u2", "int": "i4", "int32": "i4", "uint": "u4", "uint32": "u4",
              "float": "f4", "float32": "f4", "double": "f8", "float64": "f8"}


def _pick(names, *cands):
    low = {n.lower(): n for n in names}
    for c in cands:
        if c in low:
            return low[c]
    return None


class PlySource(Source):
    fmt = "PLY"

    def __init__(self, path):
        super().__init__(path)
        with open(path, "rb") as f:
            if f.readline().strip() != b"ply":
                raise PointCloudError("%s: not a PLY file" % self.name)
            self.encoding = "ascii"
            elems = []
            while True:
                ln = f.readline()
                if not ln:
                    raise PointCloudError("%s: the PLY header has no end_header" % self.name)
                w = ln.decode("ascii", "replace").split()
                if not w:
                    continue
                if w[0] == "format":
                    self.encoding = w[1]
                elif w[0] == "element":
                    elems.append([w[1], int(w[2]), []])
                elif w[0] == "property" and elems:
                    if w[1] == "list":
                        elems[-1][2].append((w[-1], None))
                    else:
                        elems[-1][2].append((w[2], _PLY_TYPES.get(w[1], "f4")))
                elif w[0] == "comment" and len(w) > 2 and w[1].lower() == "crs":
                    self.crs = " ".join(w[2:])
                elif w[0] == "end_header":
                    break
            self.data_off = f.tell()
        if not elems or elems[0][0] != "vertex":
            raise PointCloudError("%s: the PLY file does not start with vertices" % self.name)
        _n, self.count, props = elems[0]
        if any(t is None for _p, t in props):
            raise PointCloudError("%s: list properties on vertices are not supported" % self.name)
        end = "<" if self.encoding == "binary_little_endian" else ">"
        self.dtype = np.dtype([(p, end + t) for p, t in props])
        names = [p for p, _t in props]
        self.fx, self.fy, self.fz = (_pick(names, c) for c in ("x", "y", "z"))
        if None in (self.fx, self.fy, self.fz):
            raise PointCloudError("%s: no x, y, z on the vertices" % self.name)
        self.fr = _pick(names, "red", "r", "diffuse_red")
        self.fg = _pick(names, "green", "g", "diffuse_green")
        self.fb = _pick(names, "blue", "b", "diffuse_blue")
        self.fi = _pick(names, "intensity", "scalar_intensity", "reflectance", "scalar_reflectance")
        self.fc = _pick(names, "classification", "class", "scalar_classification")
        self.ft = _pick(names, "time", "gps_time", "gpstime", "scalar_gpstime", "timestamp")
        self.has_rgb = None not in (self.fr, self.fg, self.fb)
        self.has_intensity = self.fi is not None
        self.has_class = self.fc is not None
        self.has_time = self.ft is not None
        self.time_basis = "as stored" if self.has_time else ""
        self._iscale = self._cscale = None

    def _arrays(self, size):
        with open(self.path, "rb") as f:
            f.seek(self.data_off)
            left = self.count
            while left > 0:
                k = min(size, left)
                if self.encoding == "ascii":
                    a = np.loadtxt(f, dtype=self.dtype, max_rows=k, ndmin=1)
                else:
                    a = np.fromfile(f, self.dtype, k)
                if len(a) == 0:
                    break
                left -= len(a)
                yield a

    def blocks(self, size=2_000_000):
        for a in self._arrays(size):
            b = {"x": a[self.fx].astype(np.float64), "y": a[self.fy].astype(np.float64),
                 "z": a[self.fz].astype(np.float64)}
            if self.has_rgb:
                c = np.column_stack([a[self.fr], a[self.fg], a[self.fb]]).astype(np.float64)
                if self._cscale is None:
                    self._cscale = 255.0 if c.size and c.max() <= 1.0 and a.dtype[self.fr].kind == "f" else \
                        _u8_scale(float(c.max()) if c.size else 0)
                b["rgb"] = np.clip(np.round(c * self._cscale), 0, 255).astype(np.uint8)
            if self.has_intensity:
                v = a[self.fi].astype(np.float64)
                if self._iscale is None:
                    mx = float(v.max()) if len(v) else 0
                    self._iscale = 255.0 if mx <= 1.0 else _u8_scale(mx)
                b["i"] = np.clip(np.round(v * self._iscale), 0, 255).astype(np.uint8)
            if self.has_class:
                b["cls"] = np.clip(a[self.fc], 0, 255).astype(np.uint8)
            if self.has_time:
                b["t"] = a[self.ft].astype(np.float64)
            yield b


# --------------------------------------------------------------------------------------------- PCD
class PcdSource(Source):
    fmt = "PCD"

    def __init__(self, path):
        super().__init__(path)
        hdr = {}
        with open(path, "rb") as f:
            while True:
                ln = f.readline()
                if not ln:
                    raise PointCloudError("%s: the PCD header has no DATA line" % self.name)
                w = ln.decode("ascii", "replace").split()
                if not w or w[0].startswith("#"):
                    continue
                hdr[w[0].upper()] = w[1:]
                if w[0].upper() == "DATA":
                    break
            self.data_off = f.tell()
        self.encoding = hdr["DATA"][0].lower()
        if self.encoding not in ("ascii", "binary"):
            raise PointCloudError("%s: PCD '%s' data is not supported (ascii or binary)" % (self.name, self.encoding))
        names = hdr.get("FIELDS", [])
        sizes = [int(s) for s in hdr.get("SIZE", [])]
        types = hdr.get("TYPE", [])
        counts = [int(c) for c in hdr.get("COUNT", ["1"] * len(names))]
        dt = []
        for n, s, t, c in zip(names, sizes, types, counts):
            code = {"F": "f", "U": "u", "I": "i"}[t] + str(s)
            dt.append((n, "<" + code) if c == 1 else (n, "<" + code, (c,)))
        self.dtype = np.dtype(dt)
        self.count = int(hdr.get("POINTS", ["0"])[0])
        self.names = names
        self.has_rgb = "rgb" in names or "rgba" in names
        self.has_intensity = "intensity" in names
        self._iscale = None

    def blocks(self, size=2_000_000):
        with open(self.path, "rb") as f:
            f.seek(self.data_off)
            left = self.count
            while left > 0:
                k = min(size, left)
                a = np.loadtxt(f, dtype=self.dtype, max_rows=k, ndmin=1) if self.encoding == "ascii" else \
                    np.fromfile(f, self.dtype, k)
                if len(a) == 0:
                    break
                left -= len(a)
                ok = np.isfinite(a["x"]) & np.isfinite(a["y"]) & np.isfinite(a["z"])
                a = a[ok]
                b = {"x": a["x"].astype(np.float64), "y": a["y"].astype(np.float64), "z": a["z"].astype(np.float64)}
                if self.has_rgb:
                    f_ = "rgb" if "rgb" in self.names else "rgba"
                    v = np.ascontiguousarray(a[f_]).view(np.uint32) if a.dtype[f_].kind == "f" else a[f_].astype(np.uint32)
                    b["rgb"] = np.column_stack([(v >> 16) & 255, (v >> 8) & 255, v & 255]).astype(np.uint8)
                if self.has_intensity:
                    v = a["intensity"].astype(np.float64)
                    if self._iscale is None:
                        mx = float(v.max()) if len(v) else 0
                        self._iscale = 255.0 if mx <= 1.0 else _u8_scale(mx)
                    b["i"] = np.clip(np.round(v * self._iscale), 0, 255).astype(np.uint8)
                yield b


# --------------------------------------------------------------------------------------------- text
_SPLIT = re.compile(r"[\s,;]+")
_HEAD = {"x": "x", "easting": "x", "east": "x", "e": "x", "//x": "x", "y": "y", "northing": "y", "north": "y",
         "n": "y", "z": "z", "elevation": "z", "height": "z", "h": "z", "intensity": "i", "i": "i",
         "reflectance": "i", "r": "r", "red": "r", "g": "g", "green": "g", "b": "b", "blue": "b",
         "class": "cls", "classification": "cls", "time": "t", "gps_time": "t", "gpstime": "t", "t": "t",
         "point_source_id": "part", "source": "part", "return_number": "rn", "number_of_returns": "nr"}


def _guess_columns(n, sample):
    """Column roles for headerless text from the column count and value ranges."""
    if n < 3:
        return None
    cols = ["x", "y", "z"]
    rest = n - 3

    def is_colour(j):
        v = sample[:, j]
        return np.all((v >= 0) & (v <= 255) & (np.round(v) == v))
    if rest >= 4 and all(is_colour(j) for j in (4, 5, 6)):
        cols += ["i", "r", "g", "b"] + ["-"] * (rest - 4)
    elif rest >= 3 and all(is_colour(j) for j in (3, 4, 5)):
        cols += ["r", "g", "b"] + ["-"] * (rest - 3)
    elif rest >= 1:
        cols += ["i"] + ["-"] * (rest - 1)
    return cols


class TextSource(Source):
    """Text columns: one point per line, separated by spaces, tabs, commas or semicolons. A first line with
    names (x y z intensity red green blue class time ...) sets the columns; otherwise x y z [intensity]
    [r g b] by count and range. Leica PTS (a count line first) has its own reader."""

    def __init__(self, path):
        super().__init__(path)
        ext = os.path.splitext(path)[1].lower()
        self.fmt = {".xyz": "XYZ text", ".txt": "Text", ".csv": "CSV", ".asc": "ASCII"}.get(ext, "Text")
        with open(path, "r", errors="replace") as f:
            head = [f.readline() for _ in range(30)]
        lines = [ln.strip() for ln in head if ln.strip() and not ln.lstrip().startswith(("#", "//"))]
        self.skip = 0
        self.cols = None
        first = head[0].strip() if head else ""
        names = [w.lower() for w in _SPLIT.split(first.lstrip("/#").strip()) if w]
        if names and any(re.match(r"^[a-z_/]", w) for w in names) and sum(w in _HEAD for w in names) >= 3:
            self.cols = [_HEAD.get(w, "-") for w in names]
            self.skip = 1
            lines = lines[1:]
        try:
            sample = np.array([[float(v) for v in _SPLIT.split(ln) if v] for ln in lines[:20]])
        except ValueError:
            raise PointCloudError("%s: the lines are not numbers in columns" % self.name)
        if sample.ndim != 2 or sample.shape[1] < 3:
            raise PointCloudError("%s: fewer than three columns (x, y, z)" % self.name)
        self.ncol = sample.shape[1]
        if self.cols is None:
            self.cols = _guess_columns(self.ncol, sample)
        self.cols = (self.cols + ["-"] * self.ncol)[:self.ncol]
        if not {"x", "y", "z"} <= set(self.cols):
            raise PointCloudError("%s: no x, y and z columns" % self.name)
        self.has_rgb = {"r", "g", "b"} <= set(self.cols)
        self.has_intensity = "i" in self.cols
        self.has_class = "cls" in self.cols
        self.has_time = "t" in self.cols
        self.time_basis = "as stored" if self.has_time else ""
        self.part_kind = "point source" if "part" in self.cols else ""
        self.delim = "," if "," in lines[0] else (";" if ";" in lines[0] else None)
        self.columns_text = " ".join(c if c != "-" else "(unused)" for c in self.cols)
        self._iscale = self._cscale = None

    def _read(self, size):
        with open(self.path, "r", errors="replace") as f:
            for _ in range(self.skip):
                f.readline()
            while True:
                try:
                    a = np.loadtxt(f, delimiter=self.delim, max_rows=size, ndmin=2, comments=("#", "//"),
                                   usecols=range(self.ncol))
                except ValueError as e:
                    raise PointCloudError("%s: %s" % (self.name, e))
                if a.size == 0:
                    break
                yield a
                if len(a) < size:
                    break

    def blocks(self, size=2_000_000):
        ix = {c: j for j, c in enumerate(self.cols) if c != "-"}
        for a in self._read(size):
            b = {"x": a[:, ix["x"]], "y": a[:, ix["y"]], "z": a[:, ix["z"]]}
            if self.has_intensity:
                v = a[:, ix["i"]]
                if self._iscale is None:
                    mx, mn = (float(v.max()), float(v.min())) if len(v) else (0, 0)
                    self._iscale = ("pts", mn) if mn < 0 else (255.0 if mx <= 1.0 else _u8_scale(mx))
                if isinstance(self._iscale, tuple):          # signed (Leica -2048..2047)
                    b["i"] = np.clip(np.round((v + 2048) / 16.0), 0, 255).astype(np.uint8)
                else:
                    b["i"] = np.clip(np.round(v * self._iscale), 0, 255).astype(np.uint8)
            if self.has_rgb:
                c = np.column_stack([a[:, ix["r"]], a[:, ix["g"]], a[:, ix["b"]]])
                if self._cscale is None:
                    mx = float(c.max()) if c.size else 0
                    self._cscale = 255.0 if mx <= 1.0 and not np.all(np.round(c) == c) else _u8_scale(mx)
                b["rgb"] = np.clip(np.round(c * self._cscale), 0, 255).astype(np.uint8)
            if self.has_class:
                b["cls"] = np.clip(a[:, ix["cls"]], 0, 255).astype(np.uint8)
            if self.has_time:
                b["t"] = a[:, ix["t"]].astype(np.float64)
            if "part" in ix:
                b["part"] = a[:, ix["part"]].astype(np.int64)
            if "rn" in ix and "nr" in ix:
                b["ret"] = _ret_code(a[:, ix["rn"]], a[:, ix["nr"]])
            yield b


class PtsSource(TextSource):
    """Leica PTS: blocks, each a count line then x y z [intensity -2048..2047] [r g b]; a block = a scan."""

    def __init__(self, path):
        Source.__init__(self, path)
        self.fmt = "PTS"
        with open(path, "r", errors="replace") as f:
            first = f.readline().strip()
            rows = [f.readline().strip() for _ in range(20)]
        if not re.match(r"^\d+$", first):
            raise PointCloudError("%s: a PTS file starts with the point count" % self.name)
        sample = np.array([[float(v) for v in _SPLIT.split(r) if v] for r in rows if r and (" " in r or "," in r)])
        self.ncol = sample.shape[1] if sample.ndim == 2 else 3
        cols = ["x", "y", "z"]
        if self.ncol == 4:
            cols += ["i"]
        elif self.ncol == 6:
            cols += ["r", "g", "b"]
        elif self.ncol >= 7:
            cols += ["i", "r", "g", "b"] + ["-"] * (self.ncol - 7)
        self.cols = cols
        self.has_rgb = {"r", "g", "b"} <= set(cols)
        self.has_intensity = "i" in cols
        self.part_kind = "scan"
        self.delim = "," if rows and "," in rows[0] else None
        self._iscale = ("pts", -2048) if self.has_intensity else None
        self._cscale = 1.0
        self.columns_text = " ".join(cols)
        n, k = 0, 0
        # count the blocks (a line with a single integer)
        with open(path, "r", errors="replace") as f:
            for ln in f:
                s = ln.strip()
                if s.isdigit():
                    self.parts[k] = "Scan %d" % (k + 1)
                    n += int(s)
                    k += 1
        self.count = n

    def blocks(self, size=2_000_000):
        ix = {c: j for j, c in enumerate(self.cols) if c != "-"}
        with open(self.path, "r", errors="replace") as f:
            part = -1
            left = 0
            while True:
                if left == 0:
                    ln = f.readline()
                    while ln and not ln.strip():
                        ln = f.readline()
                    if not ln:
                        return
                    left = int(ln.strip())
                    part += 1
                    continue
                k = min(size, left)
                a = np.loadtxt(f, delimiter=self.delim, max_rows=k, ndmin=2, usecols=range(self.ncol))
                if a.size == 0:
                    return
                left -= len(a)
                b = {"x": a[:, 0], "y": a[:, 1], "z": a[:, 2], "part": np.full(len(a), part, np.int64)}
                if self.has_intensity:
                    b["i"] = np.clip(np.round((a[:, ix["i"]] + 2048) / 16.0), 0, 255).astype(np.uint8)
                if self.has_rgb:
                    b["rgb"] = np.clip(np.column_stack([a[:, ix["r"]], a[:, ix["g"]], a[:, ix["b"]]]), 0, 255
                                       ).astype(np.uint8)
                yield b
