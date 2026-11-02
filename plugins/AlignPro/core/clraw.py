"""Reader for the mapper's .clraw recordings (MCAP container, profile 'clraw').

Reads front to back, so files without a summary (unfinished) still open.
A chunk whose CRC does not match is skipped and counted, never fatal.
"""
import json
import os
import re
import struct
import zlib

from . import zstd

MAGIC = b"\x89MCAP0\r\n"
CH_POINTS, CH_STATUS, CH_ANPP, CH_EVENTS, CH_QUALITY = 1, 2, 3, 4, 5
TOPICS = {"/lidar/points": CH_POINTS, "/lidar/status": CH_STATUS, "/ins/anpp": CH_ANPP,
          "/capture/events": CH_EVENTS, "/capture/quality": CH_QUALITY}

_u16 = struct.Struct("<H").unpack_from
_u32 = struct.Struct("<I").unpack_from
_u64 = struct.Struct("<Q").unpack_from
_msg_hdr = struct.Struct("<HIQQ").unpack_from  # channel, sequence, log_time, publish_time
_rec_hdr = struct.Struct("<BQ").unpack_from


class ClrawError(Exception):
    pass


def _str(b, o):
    n = _u32(b, o)[0]
    return b[o + 4:o + 4 + n].decode("utf-8", "replace"), o + 4 + n


def _map(b, o):
    n = _u32(b, o)[0]
    end = o + 4 + n
    o += 4
    out = {}
    while o < end:
        k, o = _str(b, o)
        v, o = _str(b, o)
        out[k] = v
    return out, end


class ClrawFile:
    """One .clraw file. Opening reads the header records only (fast)."""

    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        self.size = os.path.getsize(path)
        self.metadata = {}
        self.attachments = {}
        self.channels = {}          # id -> topic
        self.bad_chunks = 0
        self.chunks = 0
        self._data_start = None
        self._scan_head()

    # ------------------------------------------------------------------ head
    def _scan_head(self):
        with open(self.path, "rb") as f:
            if f.read(8) != MAGIC:
                raise ClrawError("%s: not an MCAP/.clraw file" % self.name)
            pos = 8
            while True:
                hdr = f.read(9)
                if len(hdr) < 9:
                    break
                op, ln = _rec_hdr(hdr, 0)
                if op == 0x06:  # first chunk: header part is over
                    self._data_start = pos
                    break
                body = f.read(ln)
                if len(body) < ln:
                    break
                self._head_record(op, body)
                pos += 9 + ln
        if self._data_start is None:
            self._data_start = pos
        if self.metadata.get("format_version", "1") not in ("1",):
            raise ClrawError("%s: .clraw format_version %s is newer than this plugin understands"
                             % (self.name, self.metadata.get("format_version")))

    def _head_record(self, op, b):
        if op == 0x01:
            prof, o = _str(b, 0)
            self.profile = prof
        elif op == 0x0C:
            name, o = _str(b, 0)
            m, _ = _map(b, o)
            if name == "clraw":
                self.metadata = m
        elif op == 0x09:
            o = 16
            name, o = _str(b, o)
            _media, o = _str(b, o)
            n = _u64(b, o)[0]
            data = b[o + 8:o + 8 + n]
            crc = _u32(b, o + 8 + n)[0] if len(b) >= o + 12 + n else 0
            if crc and zlib.crc32(b[:o + 8 + n]) & 0xFFFFFFFF != crc:
                return
            self.attachments[name] = bytes(data)
        elif op == 0x04:
            cid = _u16(b, 0)[0]
            topic, _ = _str(b, 4)
            self.channels[cid] = topic

    # ------------------------------------------------------------ properties
    def attachment_json(self, name):
        d = self.attachments.get(name)
        return json.loads(d.decode("utf-8")) if d else {}

    @property
    def hardware(self):
        return self.attachment_json("hardware.json")

    @property
    def session(self):
        return self.attachment_json("session.json")

    @property
    def line(self):
        v = self.metadata.get("line")
        if v not in (None, ""):
            try:
                return int(v)
            except ValueError:
                pass
        m = re.match(r"(\d+)_", self.name)
        return int(m.group(1)) if m else 0

    @property
    def file_number(self):
        v = self.metadata.get("file_number")
        try:
            return int(v)
        except (TypeError, ValueError):
            m = re.search(r"_(\d+)\.clraw$", self.name)
            return int(m.group(1)) if m else 1

    def scan_late_attachments(self):
        """lidar_calibration.csv arrives a moment after Start, so in a line's first file it sits
        between chunks. Walk the record headers (chunk bodies are skipped) to collect it."""
        if getattr(self, "_late_done", False):
            return
        self._late_done = True
        with open(self.path, "rb") as f:
            f.seek(self._data_start)
            while True:
                hdr = f.read(9)
                if len(hdr) < 9:
                    break
                op, ln = _rec_hdr(hdr, 0)
                if op == 0x09:
                    body = f.read(ln)
                    if len(body) < ln:
                        break
                    self._head_record(op, body)
                elif op in (0x0F, 0x02):
                    break
                else:
                    f.seek(ln, 1)

    def calibration(self):
        """Hesai channel calibration [(elev_deg, az_deg)] * 32 from this file, or None."""
        if "lidar_calibration.csv" not in self.attachments:
            self.scan_late_attachments()
        d = self.attachments.get("lidar_calibration.csv")
        if not d:
            return None
        rows = []
        for ln in d.decode("utf-8", "replace").splitlines():
            parts = [p.strip() for p in ln.split(",")]
            if len(parts) >= 3:
                try:
                    rows.append((int(parts[0]), float(parts[1]), float(parts[2])))
                except ValueError:
                    continue
        rows.sort()
        return [(e, a) for _, e, a in rows] if rows else None

    # --------------------------------------------------------------- reading
    def iter_chunks(self, channels=None, progress=None):
        """Yield one list per chunk: [(channel, log_ns, publish_ns, payload memoryview)]."""
        want = set(channels) if channels else None
        with open(self.path, "rb") as f:
            f.seek(self._data_start)
            pos = self._data_start
            while True:
                hdr = f.read(9)
                if len(hdr) < 9:
                    return
                op, ln = _rec_hdr(hdr, 0)
                if op == 0x02:          # footer
                    return
                if op != 0x06:
                    if op in (0x0F,):   # data end: the summary follows
                        return
                    f.seek(ln, 1)
                    pos += 9 + ln
                    continue
                body = f.read(ln)
                pos += 9 + ln
                if len(body) < ln:
                    return
                self.chunks += 1
                if progress:
                    progress(pos / max(self.size, 1))
                try:
                    out = self._chunk_messages(body, want)
                except Exception:
                    self.bad_chunks += 1
                    continue
                if out:
                    yield out

    def _chunk_messages(self, b, want):
        usize = _u64(b, 16)[0]
        ucrc = _u32(b, 24)[0]
        comp, o = _str(b, 28)
        rlen = _u64(b, o)[0]
        recs = b[o + 8:o + 8 + rlen]
        if comp == "zstd":
            data = zstd.decompress(recs, usize)
        elif comp == "":
            data = bytes(recs)
        else:
            raise ClrawError("unsupported compression %r" % comp)
        if ucrc and zlib.crc32(data) & 0xFFFFFFFF != ucrc:
            raise ClrawError("chunk CRC")
        mv = memoryview(data)
        out = []
        o, n = 0, len(data)
        while o + 9 <= n:
            op, ln = _rec_hdr(data, o)
            if op == 0x05:
                ch, _seq, lt, pt = _msg_hdr(data, o + 9)
                if want is None or ch in want:
                    out.append((ch, lt, pt, mv[o + 31:o + 9 + ln]))
            o += 9 + ln
        return out

    def iter_messages(self, channels=None):
        for chunk in self.iter_chunks(channels):
            for m in chunk:
                yield m

    def events(self):
        ev = []
        for ch, lt, pt, d in self.iter_messages({CH_EVENTS}):
            try:
                ev.append(json.loads(bytes(d).decode("utf-8")))
            except ValueError:
                pass
        return ev


def find_clraw(paths):
    """Expand files/folders (capture folders, clraw/ folders, export dirs) to .clraw files.
    .partial files are ignored."""
    out = []
    for p in paths:
        if os.path.isdir(p):
            for root, _dirs, files in os.walk(p):
                for fn in files:
                    if fn.lower().endswith(".clraw"):
                        out.append(os.path.join(root, fn))
        elif p.lower().endswith(".clraw") and os.path.isfile(p):
            out.append(p)
    return sorted(set(out), key=lambda x: os.path.basename(x))


def group_lines(files):
    """Group .clraw files into survey lines {key: [ClrawFile...]} in name order.
    The key is capture id + line number so lines from different capture folders never merge."""
    lines = {}
    for p in files:
        cf = ClrawFile(p)
        key = "%s:%03d" % (cf.metadata.get("capture", os.path.dirname(p)), cf.line)
        lines.setdefault(key, []).append(cf)
    for k in lines:
        lines[k].sort(key=lambda c: (c.file_number, c.name))
    return dict(sorted(lines.items(), key=lambda kv: kv[1][0].name))
