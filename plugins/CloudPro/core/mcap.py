"""Minimal, fast MCAP reader for .clraw files (https://mcap.dev/spec).

Reads front to back (no summary needed, so unfinished or repaired files
work), yields decompressed chunk record blocks and decodes the Message
records inside them.  Damaged chunks (CRC mismatch, truncated) are skipped,
as the mapper's own kit does.
"""
import struct
import zlib

from . import zstd

MAGIC = b"\x89MCAP0\r\n"
OP_HEADER, OP_FOOTER, OP_SCHEMA, OP_CHANNEL, OP_MESSAGE, OP_CHUNK = 0x01, 0x02, 0x03, 0x04, 0x05, 0x06
OP_MSG_INDEX, OP_CHUNK_INDEX, OP_ATTACHMENT, OP_ATTACH_INDEX, OP_STATS = 0x07, 0x08, 0x09, 0x0A, 0x0B
OP_METADATA, OP_META_INDEX, OP_SUMMARY_OFFSET, OP_DATA_END = 0x0C, 0x0D, 0x0E, 0x0F

_u16 = struct.Struct("<H")
_u32 = struct.Struct("<I")
_u64 = struct.Struct("<Q")
_rec = struct.Struct("<BQ")
_msg = struct.Struct("<HIQQ")


def _str(b, o):
    n = _u32.unpack_from(b, o)[0]
    o += 4
    return b[o:o + n].decode("utf-8", "replace"), o + n


def _map(b, o):
    n = _u32.unpack_from(b, o)[0]
    o += 4
    end = o + n
    out = {}
    while o < end:
        k, o = _str(b, o)
        v, o = _str(b, o)
        out[k] = v
    return out, end


class McapFile:
    """Sequential reader. ``scan()`` collects header, schemas, channels,
    metadata, attachments and chunk positions in one pass without
    decompressing; ``chunks()`` then decompresses on demand."""

    def __init__(self, path):
        self.path = path
        self.profile = ""
        self.library = ""
        self.schemas = {}
        self.channels = {}        # id -> dict(topic, schema_id, encoding)
        self.metadata = {}        # name -> map
        self.attachments = {}     # name -> bytes
        self.chunk_pos = []       # (offset of compressed records, compressed length, compression, usize, ucrc, start, end)
        self.bad_chunks = 0
        self._scanned = False

    # ------------------------------------------------------------------
    def scan(self):
        if self._scanned:
            return self
        with open(self.path, "rb") as f:
            if f.read(8) != MAGIC:
                raise ValueError("%s is not an MCAP/.clraw file" % self.path)
            pos = 8
            while True:
                hdr = f.read(9)
                if len(hdr) < 9:
                    break
                op, ln = _rec.unpack(hdr)
                body_pos = pos + 9
                if op == OP_CHUNK:
                    head = f.read(min(ln, 4096))
                    start, end, usize, ucrc = struct.unpack_from("<QQQI", head, 0)
                    comp, o = _str(head, 28)
                    rlen = _u64.unpack_from(head, o)[0]
                    o += 8
                    self.chunk_pos.append((body_pos + o, rlen, comp, usize, ucrc, start, end))
                    f.seek(body_pos + ln)
                elif op in (OP_SCHEMA, OP_CHANNEL, OP_HEADER, OP_METADATA, OP_ATTACHMENT):
                    b = f.read(ln)
                    if len(b) < ln:
                        break
                    self._record(op, b)
                elif op == OP_FOOTER:
                    break
                else:
                    f.seek(body_pos + ln)
                if op == OP_DATA_END:
                    break       # summary section follows: everything needed is already known
                pos = body_pos + ln
        self._scanned = True
        return self

    def _record(self, op, b):
        if op == OP_HEADER:
            self.profile, o = _str(b, 0)
            self.library, o = _str(b, o)
        elif op == OP_SCHEMA:
            sid = _u16.unpack_from(b, 0)[0]
            name, o = _str(b, 2)
            enc, o = _str(b, o)
            self.schemas[sid] = (name, enc)
        elif op == OP_CHANNEL:
            cid, sid = struct.unpack_from("<HH", b, 0)
            topic, o = _str(b, 4)
            enc, o = _str(b, o)
            self.channels[cid] = {"topic": topic, "schema_id": sid, "encoding": enc}
        elif op == OP_METADATA:
            name, o = _str(b, 0)
            m, o = _map(b, o)
            self.metadata[name] = m
        elif op == OP_ATTACHMENT:
            o = 16
            name, o = _str(b, o)
            media, o = _str(b, o)
            n = _u64.unpack_from(b, o)[0]
            o += 8
            data = b[o:o + n]
            crc = _u32.unpack_from(b, o + n)[0] if o + n + 4 <= len(b) else 0
            if crc and (zlib.crc32(b[:o + n]) & 0xFFFFFFFF) != crc:
                return
            self.attachments[name] = data

    def topic_id(self, topic):
        for cid, c in self.channels.items():
            if c["topic"] == topic:
                return cid
        return None

    # ------------------------------------------------------------------
    def read_chunk(self, f, i):
        """Return the decompressed records of chunk i (bytes) or None."""
        off, rlen, comp, usize, ucrc, _s, _e = self.chunk_pos[i]
        f.seek(off)
        raw = f.read(rlen)
        if len(raw) < rlen:
            self.bad_chunks += 1
            return None
        try:
            if comp == "zstd":
                data = zstd.decompress(raw, usize)
            elif comp == "":
                data = raw
            elif comp == "lz4":
                raise ValueError("lz4 chunks are not supported")
            else:
                raise ValueError("unknown chunk compression %r" % comp)
        except Exception:
            self.bad_chunks += 1
            return None
        if ucrc and (zlib.crc32(data) & 0xFFFFFFFF) != ucrc:
            self.bad_chunks += 1
            return None
        return data


def iter_messages(data, want=None):
    """Yield (channel, log_time, publish_time, payload memoryview) for the
    Message records of one decompressed chunk; ``want`` = set of channel ids."""
    mv = memoryview(data)
    o = 0
    n = len(data)
    while o + 9 <= n:
        op, ln = _rec.unpack_from(data, o)
        b = o + 9
        if op == OP_MESSAGE:
            ch, _seq, lt, pt = _msg.unpack_from(data, b)
            if want is None or ch in want:
                yield ch, lt, pt, mv[b + 22:b + ln]
        o = b + ln
