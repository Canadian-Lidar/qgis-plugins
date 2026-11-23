"""Minimal, fast MCAP reader for .clraw files (https://mcap.dev/spec).
Shared by every DCS plugin through dcs_core.clraw.

A finished file is opened from its summary (chunk / attachment / metadata
indexes at the end; 1.19.4); an unfinished or repaired one is read front to
back (no summary needed). Yields decompressed chunk record blocks and decodes
the Message records inside them.  Damaged chunks (CRC mismatch, truncated) are skipped,
as the mapper's own kit does.
"""
import os
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


def _attachment(b):
    """(name, data) of an Attachment record body, or None when its CRC does not match."""
    o = 16
    name, o = _str(b, o)
    _media, o = _str(b, o)
    n = _u64.unpack_from(b, o)[0]
    o += 8
    data = bytes(b[o:o + n])
    crc = _u32.unpack_from(b, o + n)[0] if o + n + 4 <= len(b) else 0
    if crc and (zlib.crc32(b[:o + n]) & 0xFFFFFFFF) != crc:
        return None
    return name, data


class LazyAttachments(dict):
    """name -> bytes, each attachment read from the file the first time it is asked for (1.19.4, audit
    23 Nov 2026). A marine recording carries the Reach RINEX files (106 MB at Lumston): every opening read
    and kept them, whatever was needed. Behaves as the dict it replaces; an attachment whose CRC does not match
    is not there (as before)."""

    def __init__(self, path):
        super().__init__()
        self._path = path
        self._where = {}                # name -> (offset of the record body, body length), not yet read

    def _add(self, name, body_off, body_len):
        self._where[name] = (body_off, body_len)
        super().pop(name, None)

    def _load(self, name):
        off, ln = self._where.pop(name)
        with open(self._path, "rb") as f:
            f.seek(off)
            b = f.read(ln)
        r = _attachment(b) if len(b) == ln else None
        if r is not None:
            super().__setitem__(name, r[1])
            return True
        return False

    def __getitem__(self, name):
        if name in self._where and not self._load(name):
            raise KeyError(name)
        return super().__getitem__(name)

    def __contains__(self, name):
        if name in self._where:
            return self._load(name)
        return super().__contains__(name)

    def get(self, name, default=None):
        try:
            return self[name]
        except KeyError:
            return default

    def keys(self):
        return list(super().keys()) + [k for k in self._where if not super().__contains__(k)]

    def __iter__(self):
        return iter(self.keys())

    def __len__(self):
        return len(self.keys())

    def items(self):
        return [(k, self[k]) for k in self.keys() if k in self]

    def values(self):
        return [v for _k, v in self.items()]

    def __bool__(self):
        return len(self) > 0


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
        self.attachments = LazyAttachments(path)     # name -> bytes, read when asked for
        self.chunk_pos = []       # (offset of compressed records, compressed length, compression, usize, ucrc, start, end)
        self.bad_chunks = 0
        self.truncated = False    # the file ends inside a record, or a record length points past the end
        self.size = 0
        self._scanned = False

    # ------------------------------------------------------------------
    def scan(self):
        if self._scanned:
            return self
        try:
            if self._scan_summary():
                self._scanned = True
                if not self.channels and self.chunk_pos:
                    self._channels_from_chunk()
                return self
        except (OSError, struct.error, UnicodeDecodeError, ValueError, MemoryError, OverflowError):
            pass
        self.__init__(self.path)                # whatever the summary path left half filled
        with open(self.path, "rb") as f:
            if f.read(8) != MAGIC:
                raise ValueError("%s is not an MCAP/.clraw file" % self.path)
            size = self.size = os.fstat(f.fileno()).st_size
            pos = 8
            while True:
                hdr = f.read(9)
                if len(hdr) < 9:
                    self.truncated = len(hdr) > 0
                    break
                op, ln = _rec.unpack(hdr)
                body_pos = pos + 9
                if body_pos + ln > size:
                    # unfinished or damaged file: a record cannot be longer than what is left. A chunk whose
                    # compressed records are only partly there is still listed (read_chunk counts it as bad).
                    self.truncated = True
                    if op != OP_CHUNK:
                        break
                try:
                    if op == OP_CHUNK:
                        head = f.read(min(ln, 4096))
                        start, end, usize, ucrc = struct.unpack_from("<QQQI", head, 0)
                        comp, o = _str(head, 28)
                        rlen = _u64.unpack_from(head, o)[0]
                        o += 8
                        if o > len(head) or rlen > ln - o:
                            self.truncated = True
                            break
                        self.chunk_pos.append((body_pos + o, rlen, comp, usize, ucrc, start, end))
                        if body_pos + ln > size:
                            break
                        f.seek(body_pos + ln)
                    elif op == OP_ATTACHMENT:
                        head = f.read(min(ln, 4096))
                        name, _o = _str(head, 16)
                        self.attachments._add(name, body_pos, ln)
                        f.seek(body_pos + ln)
                    elif op in (OP_SCHEMA, OP_CHANNEL, OP_HEADER, OP_METADATA):
                        b = f.read(ln)
                        if len(b) < ln:
                            self.truncated = True
                            break
                        self._record(op, b)
                    elif op == OP_FOOTER:
                        break
                    else:
                        f.seek(body_pos + ln)
                except (struct.error, UnicodeDecodeError, MemoryError, OverflowError):
                    self.truncated = True       # a damaged record: keep what was read before it
                    break
                if op == OP_DATA_END:
                    break       # summary section follows: everything needed is already known
                pos = body_pos + ln
        self._scanned = True
        if not self.channels and self.chunk_pos:
            self._channels_from_chunk()
        return self

    def _scan_summary(self):
        """The fast path (DCS Core 1.19.4, audit 23 Nov 2026): a finished file ends with a summary - schemas,
        channels and an index of every chunk, attachment and metadata record - so only the footer, the summary and
        the few attachment / metadata records are read. The front-to-back scan read the header of every chunk:
        2,776 small reads spread over a 394 MB recording, seconds per file on a cold or synced (OneDrive, network)
        folder. -> False when there is no usable summary (unfinished or repaired files: the front-to-back scan)."""
        with open(self.path, "rb") as f:
            size = self.size = os.fstat(f.fileno()).st_size
            if size < 8 + 37 + 8:
                return False
            if f.read(8) != MAGIC:
                raise ValueError("%s is not an MCAP/.clraw file" % self.path)
            hdr = f.read(9)
            op, ln = _rec.unpack(hdr)
            if op != OP_HEADER or 17 + ln > size:
                return False
            header = f.read(ln)
            f.seek(size - 8 - 29)
            tail = f.read(37)
            if tail[29:] != MAGIC or tail[0] != OP_FOOTER:
                return False
            _op, flen, sstart, _sofs = struct.unpack_from("<BQQQ", tail, 0)
            if flen != 20 or not 8 < sstart < size - 37:
                return False
            f.seek(sstart)
            summ = f.read(size - 37 - sstart)
            chunks, atts, metas = [], [], []
            o, n = 0, len(summ)
            while o + 9 <= n:
                op, ln = _rec.unpack_from(summ, o)
                b = summ[o + 9:o + 9 + ln]
                if len(b) < ln:
                    return False
                if op in (OP_SCHEMA, OP_CHANNEL):
                    self._record(op, b)
                elif op == OP_CHUNK_INDEX:
                    t0, t1, cstart, _clen = struct.unpack_from("<QQQQ", b, 0)
                    q = 32 + 4 + _u32.unpack_from(b, 32)[0] + 8        # message index offsets, their length
                    comp, q = _str(b, q)
                    csize, usize = struct.unpack_from("<QQ", b, q)
                    off = cstart + 9 + 8 + 8 + 8 + 4 + 4 + len(comp.encode("utf-8")) + 8
                    chunks.append((off, csize, comp, usize, None, t0, t1))
                elif op == OP_ATTACH_INDEX:
                    aoff, alen = struct.unpack_from("<QQ", b, 0)
                    aname, _q = _str(b, 40)
                    atts.append((aoff, alen, aname))
                elif op == OP_META_INDEX:
                    moff, mlen = struct.unpack_from("<QQ", b, 0)
                    metas.append((moff, mlen))
                o += 9 + ln
            if not chunks:
                return False                        # nothing indexed: let the full scan decide
            self._record(OP_HEADER, header)
            for roff, rlen in sorted(metas):
                if roff + rlen > size or rlen < 9:
                    return False
                f.seek(roff)
                r = f.read(rlen)
                op, ln = _rec.unpack_from(r, 0)
                if op == OP_METADATA and 9 + ln <= len(r):
                    self._record(op, r[9:9 + ln])
            for roff, rlen, aname in sorted(atts):
                if roff + rlen > size or rlen < 9:
                    return False
                self.attachments._add(aname, roff + 9, rlen - 9)
            self.chunk_pos = sorted(chunks)
            if any(c[0] + c[1] > size for c in self.chunk_pos):
                return False
        return True

    def _channels_from_chunk(self):
        """The mapper writes Schema and Channel records before the first chunk; other MCAP writers
        (the Python mcap library, for one) put them inside the chunks. Learn them from the first
        chunk that decompresses."""
        with open(self.path, "rb") as f:
            for i in range(min(len(self.chunk_pos), 3)):
                data = self.read_chunk(f, i)
                if data is None:
                    continue
                o, n = 0, len(data)
                try:
                    while o + 9 <= n:
                        op, ln = _rec.unpack_from(data, o)
                        if op in (OP_SCHEMA, OP_CHANNEL):
                            self._record(op, bytes(data[o + 9:o + 9 + ln]))
                        o += 9 + ln
                except (struct.error, UnicodeDecodeError):
                    continue
                if self.channels:
                    return

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
            r = _attachment(b)
            if r is not None:
                dict.__setitem__(self.attachments, r[0], r[1])

    def topic_id(self, topic):
        for cid, c in self.channels.items():
            if c["topic"] == topic:
                return cid
        return None

    # ------------------------------------------------------------------
    def read_chunk(self, f, i):
        """Return the decompressed records of chunk i (bytes) or None."""
        off, rlen, comp, usize, ucrc, _s, _e = self.chunk_pos[i]
        if self.size and off + rlen > self.size:
            self.bad_chunks += 1
            return None
        if ucrc is None:                # indexed from the summary: the CRC is in the chunk's own header
            pre = 16 + len(comp.encode("utf-8"))
            f.seek(off - pre)
            raw = f.read(pre + rlen)
            if len(raw) < pre + rlen:
                self.bad_chunks += 1
                return None
            ucrc = _u32.unpack_from(raw, 0)[0]
            raw = raw[pre:]
        else:
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
        except zstd.ZstdUnavailable:
            raise                       # no decoder at all: not a damaged chunk
        except Exception:               # noqa: BLE001 - damaged chunk
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
