""".clraw writer for files RemixPro makes (marine sonar recordings). The mapper's writer (mapper/clraw.py, 2.10.0)
with the channels given by the caller instead of the mapper's fixed Lidar / Certus set: same MCAP records,
chunking (zstd, CRC per chunk), summary and footer, so DCS Core and any MCAP library read the files."""
import os
import struct
import time
import zlib

from dcs_core import zstd as _dzstd  # noqa: F401 - decoder side lives in DCS Core

try:
    import zstandard as _zstd
except ImportError:                     # pragma: no cover - the QGIS install bundles it with DCS Core
    _zstd = None
    try:
        _dzstd._add_ext_path()
        import zstandard as _zstd       # noqa: F811
    except Exception:                   # noqa: BLE001
        _zstd = None

FORMAT_VERSION = "1"
MAGIC = b"\x89MCAP0\r\n"
PROFILE = "clraw"
EXT = ".clraw"
OP_HEADER, OP_FOOTER, OP_SCHEMA, OP_CHANNEL, OP_MESSAGE = 0x01, 0x02, 0x03, 0x04, 0x05
OP_CHUNK, OP_MSG_INDEX, OP_CHUNK_INDEX, OP_ATTACHMENT, OP_ATT_INDEX = 0x06, 0x07, 0x08, 0x09, 0x0A
OP_STATISTICS, OP_METADATA, OP_META_INDEX, OP_SUMMARY_OFFSET, OP_DATA_END = 0x0B, 0x0C, 0x0D, 0x0E, 0x0F
CHUNK_BYTES = 4 << 20
CHUNK_NS = 1000000000
ZSTD_LEVEL = 3
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


def compressor():
    return _zstd.ZstdCompressor(level=ZSTD_LEVEL) if _zstd is not None else None


class Writer:
    """One .clraw file. schemas: [(id, name, encoding, text)]; channels: [(id, schema id, topic, encoding)]."""

    def __init__(self, path, metadata, attachments=(), library="RemixPro", compress=True, schemas=(), channels=()):
        self.schemas, self.channels = list(schemas), list(channels)
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
        for sid, name, enc, data in self.schemas:
            d = data.encode("utf-8")
            self._write(_rec(OP_SCHEMA, struct.pack("<H", sid) + _str(name) + _str(enc) + struct.pack("<I", len(d)) + d))
        for cid, sid, topic, enc in self.channels:
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
            self.flush_chunk(sync=False)        # office conversion: no fsync per chunk (close() syncs)

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
                self.counts, self.t_first, self.t_last, self.schemas, self.channels)
        self.f.flush()
        os.fsync(self.f.fileno())
        self.f.close()
        os.replace(self.path + ".partial", self.path)
        self.closed = True


def _finish(f, pos, data_crc, chunk_indexes, att_indexes, meta_indexes, counts, t_first, t_last, schemas, channels):
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
                           struct.pack("<I", len(d.encode())) + d.encode()) for sid, n, e, d in schemas])
    group(OP_CHANNEL, [_rec(OP_CHANNEL, struct.pack("<HH", cid, sid) + _str(t) + _str(e) + _map({}))
                       for cid, sid, t, e in channels])
    cmc = b"".join(struct.pack("<HQ", ch, n) for ch, n in sorted(counts.items()))
    stats = struct.pack("<QHIIII", sum(counts.values()), len(schemas), len(channels), len(att_indexes),
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


