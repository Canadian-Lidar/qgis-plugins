""".CLRAW: the mapper's raw recording format (version 2.8). TESTS ONLY: the mapper's own writer (2.10.0),
used to make test recordings exactly as the mapper makes them, and its old to_bwxraw for comparison.

A .clraw file is an MCAP file (https://mcap.dev, an open, documented
container used widely for sensor logging) with the profile "clraw". Any MCAP
library (C++, Python, C#, Go, Rust, TypeScript) or viewer can open it; the
mapper writes it with its own small writer so it costs little on the Atom.

Why this layout
  * Lossless: every sensor packet is stored exactly as received.
  * Two times per packet: log_time = when it arrived (the mapper's clock,
    kept on GNSS time), publish_time = the sensor's own time stamp (Hesai
    packet time, Certus packet time). Both in nanoseconds since 1970 (UTC).
  * Chunks of about a second, each compressed (Zstandard) and with its own
    CRC-32: damage costs one chunk, never the rest of the file, and a power
    loss costs at most the chunk being written.
  * Indexed: a summary at the end lists every chunk with its time span, so a
    time or a mark is found without reading the file.
  * Self-describing: the hardware configuration (offsets and angles in the one
    convention), the BeamworX survey configuration, the LiDAR's angle file and
    the capture details travel inside every file as attachments and metadata.
  * One continuous series: files split by size or time with nothing repeated;
    navigation from 3 s before Start to 3 s after Stop is included.

Channels
  /lidar/points     Hesai PandarXT-32 point packets (1080-byte UDP payloads)
  /lidar/status     Hesai GPS/PPS status packets (512-byte UDP payloads)
  /ins/anpp         every Certus packet (complete ANPP packets incl. header),
                    raw GNSS observations and ephemerides included
  /capture/events   start, stop, marks, notes, statics (JSON, one per message)
  /capture/quality  1 Hz positioning quality (JSON)

File names: NNN_YYYYMMDD_HHMMSS_0001.clraw (survey line, UTC start, file
number within the recording). BeamworX .bwxraw files are made from them
(to_bwxraw) when copying, downloading, or after the capture.
"""
from __future__ import annotations

import json
import os
import struct
import threading
import time
import zlib

try:                                    # Ubuntu 20.04 has no package; shipped in mapper/vendor
    import zstandard as _zstd
except ImportError:                     # pragma: no cover - depends on the machine
    try:
        import sys as _sys
        import zstandard as _zstd
    except ImportError:
        _zstd = None

FORMAT_VERSION = "1"
MAGIC = b"\x89MCAP0\r\n"
PROFILE = "clraw"
EXT = ".clraw"

OP_HEADER, OP_FOOTER, OP_SCHEMA, OP_CHANNEL, OP_MESSAGE = 0x01, 0x02, 0x03, 0x04, 0x05
OP_CHUNK, OP_MSG_INDEX, OP_CHUNK_INDEX, OP_ATTACHMENT, OP_ATT_INDEX = 0x06, 0x07, 0x08, 0x09, 0x0A
OP_STATISTICS, OP_METADATA, OP_META_INDEX, OP_SUMMARY_OFFSET, OP_DATA_END = 0x0B, 0x0C, 0x0D, 0x0E, 0x0F

CH_POINTS, CH_STATUS, CH_ANPP, CH_EVENTS, CH_QUALITY = 1, 2, 3, 4, 5

SCHEMAS = [
    (1, "clraw.HesaiPandarXT32.Point", "x-clraw-doc",
     "Hesai PandarXT-32 point cloud packet, protocol 6.1, the UDP payload exactly as received "
     "(1080 bytes; little-endian; header 12, 8 blocks of azimuth + 32 x (distance, intensity, "
     "reserved), tail with return mode, rpm, UTC date-time, microseconds, UDP sequence)."),
    (2, "clraw.HesaiPandarXT32.GpsStatus", "x-clraw-doc",
     "Hesai GPS/PPS status packet, the UDP payload exactly as received (512 bytes): date-time, "
     "the NMEA sentence the LiDAR received, positioning status and PPS lock."),
    (3, "clraw.AdvancedNavigation.ANPP", "x-clraw-doc",
     "One complete Advanced Navigation Packet Protocol packet from the Certus: 5-byte header "
     "(LRC, packet id, length, CRC-16) and payload, exactly as received."),
    (4, "clraw.Json", "jsonschema", json.dumps({"type": "object"})),
]
CHANNELS = [
    (CH_POINTS, 1, "/lidar/points", "x-hesai-pandar-udp"),
    (CH_STATUS, 2, "/lidar/status", "x-hesai-pandar-udp"),
    (CH_ANPP, 3, "/ins/anpp", "x-anpp"),
    (CH_EVENTS, 4, "/capture/events", "json"),
    (CH_QUALITY, 4, "/capture/quality", "json"),
]
TOPICS = {c[0]: c[2] for c in CHANNELS}

CHUNK_BYTES = 4 << 20          # about 0.7 s at the full LiDAR rate
CHUNK_SECONDS = 1.0            # and never longer than this (so little is at risk)
CHUNK_NS = int(CHUNK_SECONDS * 1e9)
ZSTD_LEVEL = 1                 # 3.5x smaller than raw on Hesai data, cheap on the Atom
RUN_SECONDS = 3.0
NS = 1000000000

_MSG = struct.Struct("<BQHIQQ")         # opcode, record length, channel, sequence, log, publish
assert _MSG.size == 31


def _str(s):
    b = s.encode("utf-8")
    return struct.pack("<I", len(b)) + b


def _map(d):
    body = b"".join(_str(k) + _str(v) for k, v in d.items())
    return struct.pack("<I", len(body)) + body


def _rec(op, body):
    return struct.pack("<BQ", op, len(body)) + body


def file_name(line, start_unix, number, digits=3, prefix=None):
    """NNN_YYYYMMDD_HHMMSS_0001.clraw; 2.10: <prefix>_0001.clraw, the prefix
    naming the project, line and start (Deck_Test_L001_20261104_124512) or the
    project's navigation recording (Deck_Test_NAV_20261104_123000)."""
    if prefix:
        return "%s_%04d%s" % (prefix, number, EXT)
    return "%0*d_%s_%04d%s" % (digits, line, time.strftime("%Y%m%d_%H%M%S", time.gmtime(start_unix)),
                               number, EXT)


_HESAI_DATE_OFF = 1052 + 13           # yy mo dd hh mi ss, then u32 microseconds
_date_cache = {}


def sensor_time_ns(channel, data, log_ns):
    """The sensor's own time stamp of a packet (publish_time): the Hesai UTC
    time in every LiDAR packet, the Certus Unix time in System State (20) and
    Unix Time (21) packets; otherwise the arrival time."""
    try:
        if channel == CH_POINTS and len(data) >= 1080:
            key = bytes(data[_HESAI_DATE_OFF:_HESAI_DATE_OFF + 6])
            base = _date_cache.get(key)
            if base is None:
                yy, mo, dd, hh, mi, ss = key
                if not (1 <= mo <= 12 and 1 <= dd <= 31):
                    return log_ns
                import calendar
                base = calendar.timegm((1900 + yy, mo, dd, hh, mi, ss)) * NS
                if len(_date_cache) > 4096:
                    _date_cache.clear()
                _date_cache[key] = base
            us, = struct.unpack_from("<I", data, _HESAI_DATE_OFF + 6)
            return base + us * 1000
        if channel == CH_ANPP and len(data) >= 5:
            pid = data[1]
            if pid == 20 and len(data) >= 5 + 12:
                s, us = struct.unpack_from("<II", data, 5 + 4)
                return s * NS + us * 1000
            if pid == 21 and len(data) >= 5 + 8:
                s, us = struct.unpack_from("<II", data, 5)
                return s * NS + us * 1000
    except (struct.error, ValueError, OverflowError):
        pass
    return log_ns


def compressor():
    return _zstd.ZstdCompressor(level=ZSTD_LEVEL) if _zstd is not None else None


class Writer:
    """One .clraw file. Not thread-safe; the Recorder serialises."""

    def __init__(self, path, metadata, attachments=(), library="mapper", compress=True):
        self.path = path
        self.f = open(path + ".partial", "wb")
        self.pos = 0
        self.crc = 0                         # CRC-32 of the data section
        self.cz = compressor() if compress else None
        self.chunk = bytearray()
        self.chunk_idx = {}                  # channel -> [(log_time, offset), ...]
        self.chunk_t0 = self.chunk_t1 = None
        self.chunk_opened = time.monotonic()
        self.chunk_indexes = []
        self.att_indexes = []
        self.meta_indexes = []
        self.seq = {}
        self.counts = {}
        self.msg_count = 0
        self.t_first = self.t_last = None
        self.closed = False
        self._write(MAGIC)
        self._write(_rec(OP_HEADER, _str(PROFILE) + _str(library)))
        self.add_metadata("clraw", metadata)
        for name, media, data in attachments:
            self.add_attachment(name, media, data)
        for sid, name, enc, data in SCHEMAS:
            d = data.encode("utf-8")
            self._write(_rec(OP_SCHEMA, struct.pack("<H", sid) + _str(name) + _str(enc) + struct.pack("<I", len(d)) + d))
        for cid, sid, topic, enc in CHANNELS:
            self._write(_rec(OP_CHANNEL, struct.pack("<HH", cid, sid) + _str(topic) + _str(enc) + _map({})))
        self.sync()

    # ---- low level -------------------------------------------------------------
    def _write(self, b):
        self.f.write(b)
        self.pos += len(b)
        self.crc = zlib.crc32(b, self.crc)

    def sync(self):
        self.f.flush()
        try:
            os.fdatasync(self.f.fileno())
        except (AttributeError, OSError):
            os.fsync(self.f.fileno())

    # ---- content -------------------------------------------------------------------
    def add_metadata(self, name, data):
        self.flush_chunk()
        off = self.pos
        rec = _rec(OP_METADATA, _str(name) + _map({k: str(v) for k, v in data.items()}))
        self._write(rec)
        self.meta_indexes.append((off, len(rec), name))

    def add_attachment(self, name, media_type, data, log_ns=None):
        self.flush_chunk()
        t = int(log_ns if log_ns is not None else time.time() * NS)
        body = struct.pack("<QQ", t, t) + _str(name) + _str(media_type) + struct.pack("<Q", len(data)) + data
        body += struct.pack("<I", zlib.crc32(body))
        off = self.pos
        rec = _rec(OP_ATTACHMENT, body)
        self._write(rec)
        self.att_indexes.append((off, len(rec), t, t, len(data), name, media_type))

    def add(self, channel, log_ns, publish_ns, data):
        s = self.seq.get(channel, 0) + 1
        self.seq[channel] = s
        off = len(self.chunk)
        self.chunk += _MSG.pack(OP_MESSAGE, 22 + len(data), channel, s & 0xFFFFFFFF, log_ns, publish_ns)
        self.chunk += data
        self.chunk_idx.setdefault(channel, []).append((log_ns, off))
        if self.chunk_t0 is None or log_ns < self.chunk_t0:
            self.chunk_t0 = log_ns
        if self.chunk_t1 is None or log_ns > self.chunk_t1:
            self.chunk_t1 = log_ns
        self.counts[channel] = self.counts.get(channel, 0) + 1
        self.msg_count += 1
        if self.t_first is None or log_ns < self.t_first:
            self.t_first = log_ns
        if self.t_last is None or log_ns > self.t_last:
            self.t_last = log_ns
        if len(self.chunk) >= CHUNK_BYTES or log_ns - self.chunk_t0 >= CHUNK_NS:
            self.flush_chunk()

    def idle_flush(self, seconds=2.0):
        """Write the chunk in progress if it has waited this long (data stopped)."""
        if self.chunk and time.monotonic() - self.chunk_opened >= seconds:
            self.flush_chunk()

    def flush_chunk(self, sync=True):
        """Write the chunk in progress (compressed, with its CRC and message
        indexes) and make it durable."""
        if not self.chunk:
            self.chunk_opened = time.monotonic()
            return
        raw = bytes(self.chunk)
        if self.cz is not None:
            data, comp = self.cz.compress(raw), "zstd"
        else:
            data, comp = raw, ""
        start = self.pos
        body = struct.pack("<QQQI", self.chunk_t0, self.chunk_t1, len(raw), zlib.crc32(raw)) + _str(comp) + \
            struct.pack("<Q", len(data)) + data
        self._write(_rec(OP_CHUNK, body))
        chunk_len = self.pos - start
        idx_start = self.pos
        offsets = {}
        for ch in sorted(self.chunk_idx):
            recs = self.chunk_idx[ch]
            offsets[ch] = self.pos
            ib = struct.pack("<HI", ch, 16 * len(recs)) + b"".join(struct.pack("<QQ", t, o) for t, o in recs)
            self._write(_rec(OP_MSG_INDEX, ib))
        self.chunk_indexes.append((self.chunk_t0, self.chunk_t1, start, chunk_len, offsets,
                                   self.pos - idx_start, comp, len(data), len(raw)))
        self.chunk = bytearray()
        self.chunk_idx = {}
        self.chunk_t0 = self.chunk_t1 = None
        self.chunk_opened = time.monotonic()
        if sync:
            self.sync()

    @property
    def size(self):
        return self.pos + len(self.chunk)

    def close(self):
        if self.closed:
            return
        self.flush_chunk(sync=False)
        _finish(self.f, self.pos, self.crc, self.chunk_indexes, self.att_indexes, self.meta_indexes,
                self.counts, self.t_first, self.t_last)
        self.f.flush()
        os.fsync(self.f.fileno())
        self.f.close()
        os.replace(self.path + ".partial", self.path)
        self.closed = True


def _finish(f, pos, data_crc, chunk_indexes, att_indexes, meta_indexes, counts, t_first, t_last):
    """Data End, summary (schemas, channels, statistics, chunk / attachment /
    metadata indexes, summary offsets), footer and closing magic."""
    f.write(_rec(OP_DATA_END, struct.pack("<I", data_crc)))
    pos += 13
    summary_start = pos
    groups = []
    out = bytearray()

    def group(op, recs):
        if not recs:
            return
        start = len(out)
        for r in recs:
            out.extend(r)
        groups.append((op, summary_start + start, len(out) - start))

    group(OP_SCHEMA, [_rec(OP_SCHEMA, struct.pack("<H", sid) + _str(n) + _str(e) +
                           struct.pack("<I", len(d.encode())) + d.encode()) for sid, n, e, d in SCHEMAS])
    group(OP_CHANNEL, [_rec(OP_CHANNEL, struct.pack("<HH", cid, sid) + _str(t) + _str(e) + _map({}))
                       for cid, sid, t, e in CHANNELS])
    cmc = b"".join(struct.pack("<HQ", ch, n) for ch, n in sorted(counts.items()))
    stats = struct.pack("<QHIIII", sum(counts.values()), len(SCHEMAS), len(CHANNELS), len(att_indexes),
                        len(meta_indexes), len(chunk_indexes)) + \
        struct.pack("<QQ", t_first or 0, t_last or 0) + struct.pack("<I", len(cmc)) + cmc
    group(OP_STATISTICS, [_rec(OP_STATISTICS, stats)])
    ci = []
    for t0, t1, start, length, offsets, mi_len, comp, csize, usize in chunk_indexes:
        ob = b"".join(struct.pack("<HQ", ch, o) for ch, o in sorted(offsets.items()))
        ci.append(_rec(OP_CHUNK_INDEX, struct.pack("<QQQQ", t0, t1, start, length) + struct.pack("<I", len(ob)) + ob +
                       struct.pack("<Q", mi_len) + _str(comp) + struct.pack("<QQ", csize, usize)))
    group(OP_CHUNK_INDEX, ci)
    group(OP_ATT_INDEX, [_rec(OP_ATT_INDEX, struct.pack("<QQQQQ", off, ln, lt, ct, ds) + _str(nm) + _str(mt))
                         for off, ln, lt, ct, ds, nm, mt in att_indexes])
    group(OP_META_INDEX, [_rec(OP_META_INDEX, struct.pack("<QQ", off, ln) + _str(nm)) for off, ln, nm in meta_indexes])
    offset_start = summary_start + len(out)
    for op, gs, gl in groups:
        out.extend(_rec(OP_SUMMARY_OFFSET, struct.pack("<BQQ", op, gs, gl)))
    footer_head = struct.pack("<BQQQ", OP_FOOTER, 20, summary_start, offset_start)
    crc = zlib.crc32(footer_head, zlib.crc32(bytes(out)))
    f.write(bytes(out))
    f.write(footer_head + struct.pack("<I", crc))
    f.write(MAGIC)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
class FormatError(ValueError):
    pass


def _rstr(b, o):
    n, = struct.unpack_from("<I", b, o)
    return b[o + 4:o + 4 + n].decode("utf-8", "replace"), o + 4 + n


def _rmap(b, o):
    n, = struct.unpack_from("<I", b, o)
    end, o, d = o + 4 + n, o + 4, {}
    while o < end:
        k, o = _rstr(b, o)
        v, o = _rstr(b, o)
        d[k] = v
    return d, end


def _decompress(comp, data, usize):
    if comp == "":
        return data
    if comp == "zstd":
        if _zstd is None:
            raise FormatError("this computer cannot read Zstandard-compressed chunks (zstandard missing)")
        return _zstd.ZstdDecompressor().decompress(data, max_output_size=usize)
    if comp == "lz4":
        import lz4.frame
        return lz4.frame.decompress(data)
    raise FormatError("unknown chunk compression %r" % comp)


def scan(path, on_message=None, on_record=None, verify=True):
    """Walk the file's top-level records in order (works on unfinished files).
    on_message(channel, sequence, log_ns, publish_ns, data); on_record(op,
    offset, body). Returns a summary dict; problems are in 'problem'."""
    out = {"chunks": 0, "messages": 0, "counts": {}, "attachments": [], "metadata": {}, "finished": False,
           "problem": None, "bad_chunks": 0, "end": 0, "t_first": None, "t_last": None}
    with open(path, "rb") as f:
        if f.read(8) != MAGIC:
            out["problem"] = "not a .clraw (MCAP) file"
            return out
        pos = 8
        size = os.fstat(f.fileno()).st_size
        while pos + 9 <= size:
            head = f.read(9)
            op, ln = struct.unpack("<BQ", head)
            if ln > size - pos - 9:
                out["problem"] = "ends inside a record at byte %d (unfinished file)" % pos
                break
            body = f.read(ln)
            if on_record:
                on_record(op, pos, body)
            if op == OP_CHUNK:
                t0, t1, usize, ucrc = struct.unpack_from("<QQQI", body, 0)
                comp, o = _rstr(body, 28)
                n, = struct.unpack_from("<Q", body, o)
                try:
                    raw = _decompress(comp, body[o + 8:o + 8 + n], usize)
                    if verify and ucrc and zlib.crc32(raw) != ucrc:
                        raise FormatError("CRC mismatch")
                except Exception as e:
                    out["bad_chunks"] += 1
                    out["problem"] = out["problem"] or "damaged chunk at byte %d: %s" % (pos, e)
                    raw = b""
                out["chunks"] += 1
                q, end = 0, len(raw)
                while q + 9 <= end:
                    rop, rln = struct.unpack_from("<BQ", raw, q)
                    if q + 9 + rln > end:
                        break
                    if rop == OP_MESSAGE:
                        ch, seq, lt, pt = struct.unpack_from("<HIQQ", raw, q + 9)
                        out["messages"] += 1
                        out["counts"][ch] = out["counts"].get(ch, 0) + 1
                        if out["t_first"] is None or lt < out["t_first"]:
                            out["t_first"] = lt
                        if out["t_last"] is None or lt > out["t_last"]:
                            out["t_last"] = lt
                        if on_message:
                            on_message(ch, seq, lt, pt, raw[q + 31:q + 9 + rln])
                    q += 9 + rln
            elif op == OP_ATTACHMENT:
                lt, ct = struct.unpack_from("<QQ", body, 0)
                name, o = _rstr(body, 16)
                media, o = _rstr(body, o)
                n, = struct.unpack_from("<Q", body, o)
                data = body[o + 8:o + 8 + n]
                crc, = struct.unpack_from("<I", body, o + 8 + n)
                ok = (crc == 0) or zlib.crc32(body[:o + 8 + n]) == crc
                out["attachments"].append({"name": name, "media_type": media, "data": data, "crc_ok": ok})
            elif op == OP_METADATA:
                name, o = _rstr(body, 0)
                out["metadata"][name], _ = _rmap(body, o)
            elif op == OP_FOOTER:
                out["finished"] = True
                pos += 9 + ln
                break
            pos += 9 + ln
        out["end"] = pos
        if out["finished"] and f.read(8) != MAGIC:
            out["finished"] = False
            out["problem"] = out["problem"] or "closing magic missing"
    return out


def messages(path, channels=None, damaged=None):
    """Iterator over (channel, sequence, log_ns, publish_ns, data). A damaged
    chunk is skipped (counted in damaged["chunks"] when a dict is given)."""
    buf = []

    def keep(ch, seq, lt, pt, data):
        if channels is None or ch in channels:
            buf.append((ch, seq, lt, pt, data))
    # chunk by chunk, so memory stays small
    with open(path, "rb") as f:
        if f.read(8) != MAGIC:
            raise FormatError("not a .clraw file")
    for item in _iter_chunks(path):
        buf.clear()
        try:
            _walk_chunk(item, keep)
        except Exception:                 # damage costs this chunk only
            buf.clear()
            if damaged is not None:
                damaged["chunks"] = damaged.get("chunks", 0) + 1
            continue
        for m in buf:
            yield m


def _iter_chunks(path):
    with open(path, "rb") as f:
        f.seek(8)
        size = os.fstat(f.fileno()).st_size
        pos = 8
        while pos + 9 <= size:
            op, ln = struct.unpack("<BQ", f.read(9))
            if ln > size - pos - 9:
                return
            if op == OP_CHUNK:
                yield f.read(ln)
            elif op in (OP_FOOTER, OP_DATA_END):
                return
            else:
                f.seek(ln, 1)
            pos += 9 + ln


def _walk_chunk(body, cb):
    t0, t1, usize, ucrc = struct.unpack_from("<QQQI", body, 0)
    comp, o = _rstr(body, 28)
    n, = struct.unpack_from("<Q", body, o)
    raw = _decompress(comp, body[o + 8:o + 8 + n], usize)
    if ucrc and zlib.crc32(raw) != ucrc:
        raise FormatError("damaged chunk (CRC)")
    q, end = 0, len(raw)
    while q + 9 <= end:
        rop, rln = struct.unpack_from("<BQ", raw, q)
        if rop == OP_MESSAGE:
            ch, seq, lt, pt = struct.unpack_from("<HIQQ", raw, q + 9)
            cb(ch, seq, lt, pt, raw[q + 31:q + 9 + rln])
        q += 9 + rln


def attachment(path, name):
    for a in scan(path, verify=False)["attachments"]:
        if a["name"] == name:
            return a["data"]
    return None


# ---------------------------------------------------------------------------
# Repair (power loss, crash): keep every complete chunk, write the summary
# ---------------------------------------------------------------------------
def repair(partial_path):
    """Finish a .clraw.partial left by a crash or power loss: keep every
    complete, undamaged record up to the first problem (a torn or damaged
    chunk, junk such as the zeros a FAT drive leaves, or an interrupted
    summary), rebuild the indexes and summary, give it its final name.
    Returns the final path, or None if nothing usable was in it."""
    final = partial_path[:-len(".partial")] if partial_path.endswith(".partial") else partial_path
    chunks, atts, metas, counts = [], [], [], {}
    t_first = t_last = None
    end = 8
    pending = None
    finished = False
    with open(partial_path, "rb") as f:
        if f.read(8) != MAGIC:
            f.close()
            os.remove(partial_path)
            return None
        size = os.fstat(f.fileno()).st_size
        pos = 8
        while pos + 9 <= size:
            f.seek(pos)
            op, ln = struct.unpack("<BQ", f.read(9))
            if not OP_HEADER <= op <= OP_DATA_END or ln > size - pos - 9 or (op == OP_HEADER) != (pos == 8):
                break                                          # junk or torn: cut here
            if op == OP_DATA_END:                              # a summary was being written: cut before it
                break
            if op == OP_FOOTER:
                f.seek(pos + 9 + ln)
                finished = f.read(8) == MAGIC
                break
            body = f.read(ln)
            try:
                if op == OP_CHUNK:
                    t0, t1, usize, ucrc = struct.unpack_from("<QQQI", body, 0)
                    comp, o = _rstr(body, 28)
                    n, = struct.unpack_from("<Q", body, o)
                    raw = _decompress(comp, body[o + 8:o + 8 + n], usize)
                    if (ucrc and zlib.crc32(raw) != ucrc) or len(raw) != usize:
                        break
                    c = {}
                    q = 0
                    while q + 9 <= len(raw):
                        rop, rln = struct.unpack_from("<BQ", raw, q)
                        if rop == OP_MESSAGE:
                            ch, = struct.unpack_from("<H", raw, q + 9)
                            c[ch] = c.get(ch, 0) + 1
                        q += 9 + rln
                    for ch, k in c.items():
                        counts[ch] = counts.get(ch, 0) + k
                    if pending:
                        chunks.append(pending)
                    pending = [t0, t1, pos, 9 + ln, {}, 0, comp, n, usize]
                    t_first = t0 if t_first is None else min(t_first, t0)
                    t_last = t1 if t_last is None else max(t_last, t1)
                elif op == OP_MSG_INDEX:
                    if pending:
                        ch, = struct.unpack_from("<H", body, 0)
                        pending[4][ch] = pos
                        pending[5] += 9 + ln
                elif op == OP_ATTACHMENT:
                    lt, ct = struct.unpack_from("<QQ", body, 0)
                    name, o = _rstr(body, 16)
                    media, o = _rstr(body, o)
                    n, = struct.unpack_from("<Q", body, o)
                    crc, = struct.unpack_from("<I", body, o + 8 + n)
                    if crc and zlib.crc32(body[:o + 8 + n]) != crc:
                        break
                    atts.append((pos, 9 + ln, lt, ct, n, name, media))
                elif op == OP_METADATA:
                    name, _ = _rstr(body, 0)
                    metas.append((pos, 9 + ln, name))
            except Exception:                                  # damaged: cut here
                break
            pos += 9 + ln
            end = pos
    if pending:
        chunks.append(pending)
    if finished:
        os.replace(partial_path, final)
        return final
    with open(partial_path, "r+b") as f:
        f.truncate(end)
        f.seek(0)
        crc, left = 0, end
        while left:
            b = f.read(min(left, 4 << 20))
            if not b:
                break
            crc = zlib.crc32(b, crc)
            left -= len(b)
        f.seek(end)
        _finish(f, end, crc, [tuple(c) for c in chunks], atts, metas, counts, t_first, t_last)
        f.flush()
        os.fsync(f.fileno())
    os.replace(partial_path, final)
    return final


def next_number(folder, line, digits=3, prefix=None):
    pre = prefix + "_" if prefix else "%0*d_" % (digits, line)
    best = 0
    try:
        names = os.listdir(folder)
    except OSError:
        return 1
    for n in names:
        base = n[:-len(".partial")] if n.endswith(".partial") else n
        if base.startswith(pre) and base.endswith(EXT):
            try:
                best = max(best, int(base[-len("0001" + EXT):-len(EXT)]))
            except ValueError:
                pass
    return best + 1


# ---------------------------------------------------------------------------
# A recording: a series of files
# ---------------------------------------------------------------------------
class Recorder:
    """put() takes (channel, log_ns, publish_ns, data). Splits by size or
    time between messages; nothing is repeated across files."""

    def __init__(self, folder, line, metadata, attachments=(), split_bytes=None, split_seconds=None,
                 digits=3, library="mapper", name_ns=None, prefix=None):
        self.folder, self.line, self.digits = folder, line, digits
        self.prefix = prefix
        self.name_ns = name_ns              # the first file is named by the Start time
        self.metadata = dict(metadata)
        self.attachments = list(attachments)
        self.split_bytes, self.split_seconds = split_bytes, split_seconds
        self.library = library
        self.cur = None
        self.files = []
        self.hold_split = False            # set during the run-out: it stays in the last file
        self.lock = threading.Lock()
        try:
            for n in sorted(os.listdir(folder)):
                if n.endswith(EXT + ".partial"):
                    repair(os.path.join(folder, n))
        except OSError:
            pass
        self.number = next_number(folder, line, digits, prefix) - 1

    def _open(self, t_ns):
        self.number += 1
        if self.name_ns is not None:
            t_ns, self.name_ns = max(t_ns, self.name_ns), None
        name = file_name(self.line, t_ns / NS, self.number, self.digits, self.prefix)
        md = dict(self.metadata, file_number=str(self.number), file_name=name)
        w = Writer(os.path.join(self.folder, name), md, self.attachments, library=self.library)
        w.opened = t_ns
        self.files.append(name)
        return w

    def put(self, channel, log_ns, publish_ns, data):
        with self.lock:
            if self.cur is None:
                self.cur = self._open(log_ns)
            elif not self.hold_split and (
                    (self.split_bytes and self.cur.size >= self.split_bytes) or
                    (self.split_seconds and log_ns - self.cur.opened >= self.split_seconds * NS)):
                self.cur.close()
                self.cur = self._open(log_ns)
            self.cur.add(channel, log_ns, publish_ns, data)

    def idle_flush(self):
        with self.lock:
            if self.cur is not None:
                self.cur.idle_flush()

    def add_attachment(self, name, media_type, data):
        """Kept for the following files too (e.g. the LiDAR angle file once downloaded)."""
        with self.lock:
            self.attachments = [a for a in self.attachments if a[0] != name] + [(name, media_type, data)]
            if self.cur is not None:
                self.cur.add_attachment(name, media_type, data)

    def close(self):
        with self.lock:
            if self.cur is not None:
                self.cur.close()
                self.cur = None
            return list(self.files)


# ---------------------------------------------------------------------------
# BeamworX .bwxraw from .clraw
# ---------------------------------------------------------------------------
def recording_times(paths):
    """(start_ns, stop_ns) of a recording: where its data begins and ends,
    from the recorder's own events (None if absent)."""
    start = stop = None
    for p in paths:
        for ch, seq, lt, pt, data in messages(p, channels={CH_EVENTS}):
            try:
                e = json.loads(data)
            except ValueError:
                continue
            if e.get("event") == "clraw_start" and start is None:
                start = lt
            elif e.get("event") == "clraw_stop":
                stop = lt
    return start, stop


def to_bwxraw(clraw_paths, out_dir, config_xml=None, vehicle=None, progress=None):
    """BeamworX .bwxraw files from one recording's .clraw files: one .bwxraw
    (same base name, survey configuration .xml beside it) per .clraw, laid out
    as NavAQ writes them: navigation from 3 s before the start of each file
    (run-in) to 3 s after its end (run-out). Returns the paths written.
    config_xml / vehicle default to what the files carry."""
    from . import bwxraw
    paths = sorted(clraw_paths, key=os.path.basename)
    if not paths:
        return []
    first = scan(paths[0], verify=False)
    md = first["metadata"].get("clraw", {})
    if config_xml is None:
        config_xml = next((a["data"] for a in first["attachments"] if a["name"] == "beamworx_survey_config.xml"), None)
    if config_xml is None:
        raise FormatError("no BeamworX survey configuration in %s" % os.path.basename(paths[0]))
    vehicle = vehicle or md.get("vehicle") or "Mobile Mapper"
    line = int(md.get("line") or 0)
    digits = int(md.get("line_digits") or 3)
    start_ns, stop_ns = recording_times(paths)
    os.makedirs(out_dir, exist_ok=True)

    class Series(bwxraw.Recorder):
        def _open(self, t):
            name = self.pending_name
            number = int(name[-len("0001.bwxraw"):-len(".bwxraw")])
            self.number = number
            w = bwxraw.Writer(os.path.join(self.folder, name), self.config_xml, self.vehicle, self.sensor,
                              self.line, number, t, run_in=self.nav.snapshot(), el_of=self.el_of)
            w.opened = t
            self.files.append(name)
            with open(os.path.join(self.folder, name[:-len(".bwxraw")] + ".xml"), "wb") as f:
                f.write(self.config_xml)
            return w

        def split(self, t):
            if self.cur is not None:
                self._end_file(t)

    rec = Series(out_dir, config_xml, vehicle, "Hesai Laser Scanner", line, digits=digits)
    t0 = {"v": None}
    rec.el_of = lambda t: t - t0["v"] if t0["v"] is not None else 0.0
    total = sum(os.path.getsize(p) for p in paths) or 1
    done = 0
    stopped = started = False
    for p in paths:
        rec.pending_name = os.path.basename(p)[:-len(EXT)] + ".bwxraw"
        if rec.cur is not None:
            pass                                    # split at this file's first data
        new_file = True
        for ch, seq, lt, pt, data in messages(p, channels={CH_POINTS, CH_STATUS, CH_ANPP}):
            if ch == CH_ANPP:
                if len(data) < 5:
                    continue
                dev, code = bwxraw.CERTUS_DEVICE, bwxraw.anpp_code(data[1])
            else:
                dev, code = bwxraw.HESAI_DEVICE, bwxraw.hesai_code(len(data))
            if code is None:
                continue
            arr = lt / NS
            if t0["v"] is None:
                t0["v"] = arr
            rec_ = (dev, code, arr, arr - t0["v"], data)
            if not started:
                if start_ns is not None and lt < start_ns and ch != CH_POINTS:   # before Start: run-in
                    rec.nav.add(rec_)
                    continue
                started = True
            if stop_ns is not None and lt > stop_ns and not stopped and ch != CH_POINTS:
                rec.stop(stop_ns / NS)
                stopped = True
            if new_file and not stopped:
                rec.split(arr)
                new_file = False
            rec.put(*rec_[:3], data, elapsed=rec_[3])
        done += os.path.getsize(p)
        if progress:
            progress(done / total)
    files = rec.close()
    return [os.path.join(out_dir, n) for n in files]
