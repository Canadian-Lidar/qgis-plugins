"""LAS 1.4 (point data record format 6) for CloudPro deliverables: DCS Core's writer (dcs_core.las),
with rejected points flagged withheld; LazWriter writes the same points compressed (.laz)."""
import struct

import numpy as np
from dcs_core import las as _las
from dcs_core.las import GPS_UNIX, LEAP, PDRF6, read_las, utc_to_adjusted_gps  # noqa: F401

from .store import REJECTED

__all__ = ["GPS_UNIX", "LEAP", "PDRF6", "PDRF7", "LasWriter", "LazWriter", "read_las", "utc_to_adjusted_gps",
           "writer_for"]

# PDRF 7 = PDRF 6 + red, green, blue (16-bit): written when the points have colours (CloudPro 2.16.0)
PDRF7 = np.dtype(PDRF6.descr + [("red", "<u2"), ("green", "<u2"), ("blue", "<u2")])
assert PDRF7.itemsize == 36


def _records(w, E, N, Z, t, intensity, ret, cls, channel, line, flag, rgb):
    """PDRF 6 / 7 records as dcs_core.las.LasWriter.write builds them (checked byte for byte in the tests)."""
    n = len(E)
    r = np.zeros(n, PDRF7 if w.rgb else PDRF6)
    r["X"] = np.round((E - w.offset[0]) / w.scale).astype(np.int32)
    r["Y"] = np.round((N - w.offset[1]) / w.scale).astype(np.int32)
    r["Z"] = np.round((Z - w.offset[2]) / w.scale).astype(np.int32)
    r["intensity"] = intensity.astype(np.uint16) * 257
    rn = np.where(ret == 1, 1, np.where(ret == 0, 2, 1)).astype(np.uint8)
    nr = np.where(ret == 2, 1, 2).astype(np.uint8)
    r["returns"] = rn | (nr << 4)
    r["flags"] = np.where((flag & REJECTED) != 0, 0x04, 0).astype(np.uint8)
    r["classification"] = cls
    r["user_data"] = channel
    r["point_source_id"] = (line.astype(np.int64) + 1).astype(np.uint16)
    r["gps_time"] = utc_to_adjusted_gps(t)
    if w.rgb:
        c = np.zeros((n, 3), np.uint16) if rgb is None else np.asarray(rgb, np.uint16) * 257
        r["red"], r["green"], r["blue"] = c[:, 0], c[:, 1], c[:, 2]
    return r, rn


def _count(w, r, rn, E, N, Z):
    w.n += len(r)
    np.add.at(w.by_return, rn - 1, 1)
    for k, v in enumerate((E, N, Z)):
        w.mins[k] = min(w.mins[k], float(v.min()))
        w.maxs[k] = max(w.maxs[k], float(v.max()))


class LasWriter(_las.LasWriter):
    def __init__(self, path, offset, scale=0.001, wkt="", system="CloudPro", software="CloudPro (QGIS)", rgb=False):
        self.rgb = bool(rgb)
        super().__init__(path, offset, scale, wkt, system, software)

    def write(self, E, N, Z, t, intensity, ret, cls, channel, line, flag, rgb=None):
        if not self.rgb:
            super().write(E, N, Z, t, intensity, ret, cls, channel, line, withheld=(flag & REJECTED) != 0)
            return
        if len(E) == 0:
            return
        r, rn = _records(self, E, N, Z, t, intensity, ret, cls, channel, line, flag, rgb)
        r.tofile(self.f)
        _count(self, r, rn, E, N, Z)

    def close(self):
        super().close()
        if self.rgb:
            with open(self.path, "r+b") as f:       # header: point format 7, 36-byte records
                f.seek(104)
                f.write(struct.pack("<BH", 7, 36))


class LazWriter(LasWriter):
    """The same LAS 1.4 / PDRF 6 points compressed as LAZ (point data format 6 with the compression bit, a
    'laszip encoded' VLR, chunked so readers can seek). Streamed like LasWriter: blocks are compressed as they
    come (in parallel), so a file of any size uses little memory."""

    def _write_header(self):
        from . import laz
        lz = laz.module()
        if lz is None:
            raise RuntimeError(laz.why())
        self._lzvlr = lz.LazVlr.new_for_compression(7 if self.rgb else 6, 0)
        vlr = b""
        if self.wkt:
            vlr = struct.pack("<H16sHH32s", 0, b"LASF_Projection", 2112, len(self.wkt),
                              b"OGC WKT coordinate system") + self.wkt
        vlr += laz.laszip_vlr(self._lzvlr.record_data())
        self.vlr = vlr
        self.offset_to_points = 375 + len(vlr)
        self.f.write(b"\x00" * self.offset_to_points)
        # the compressor writes the offset to its chunk table here first, so the file must be at the points
        self._comp = lz.ParLasZipCompressor(self.f, self._lzvlr)

    def write(self, E, N, Z, t, intensity, ret, cls, channel, line, flag, rgb=None):
        if len(E) == 0:
            return
        r, rn = _records(self, E, N, Z, t, intensity, ret, cls, channel, line, flag, rgb)
        self._comp.compress_many(np.frombuffer(r.tobytes(), np.uint8))
        _count(self, r, rn, E, N, Z)

    def close(self):
        self._comp.done()
        super().close()
        with open(self.path, "r+b") as f:       # header: compressed point format 6 / 7 (bit 7) and the extra VLR
            f.seek(100)
            f.write(struct.pack("<I", 2 if self.wkt else 1))
            f.seek(104)
            f.write(struct.pack("<BH", (7 if self.rgb else 6) | 0x80, 36 if self.rgb else 30))


def writer_for(path):
    """LazWriter for a .laz path, else LasWriter."""
    return LazWriter if path.lower().endswith(".laz") else LasWriter
