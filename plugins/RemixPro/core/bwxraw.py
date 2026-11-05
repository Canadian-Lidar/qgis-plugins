"""BeamworX raw files (.bwxraw), written the way NavAQ writes them. Taken unchanged from mapper 2.10.0.

The layout was worked out from a NavAQ 2024 recording made with this same
hardware (Hesai PandarXT-32 + Advanced Navigation Certus Evo); the notes are in
the project doc "bwxraw format notes". Everything is little-endian.

  File header, 256 bytes
    0x00  "BWX RAW\\0"
    0x08  00 01 00 00 01 02          (constant in NavAQ files)
    0x16  u32  number of raw data records (type 3)
    0x1E  f64  time of the first record   (Unix seconds, UTC)
    0x26  f64  time of the last record
    0x2E  18 01 01 03, then the vehicle name (zero padded to 0x50)
    0x50  00 01 00 07 00 00 00 08, then the survey sensor name (to 0xE9)
    0xE9  u16 line number, u16 file number, u16 2, 16-byte file ID, u8 1

  Records from 0x100 on: u32 total length, u8 type, u8 version (1),
  u16 header length, then the record.
    4  survey configuration: 30-byte header (ff ff ff ff, f64 time), then the
       configuration XML in Qt qCompress form (big-endian u32 length + zlib)
    5  active planned line (all zero: no planned line)
    6  clock synchronisation
    3  one raw packet, exactly as received: u16 device ID, u32 message code
       (decoder ID << 16 | message), f64 arrival time (Unix UTC), f64 seconds
       since the logger started, then the packet bytes
    7  run-in / run-out marker
    8  line start information

  A file starts with: 4, 5, 6, three seconds of navigation records (run-in),
  7, 8, then all data; it ends with 6, 7 and three seconds of navigation
  (run-out). The run-in and run-out let each file be processed on its own.
"""
from __future__ import annotations

import os
import struct
import threading
import time
import uuid
import zlib
from collections import deque

MAGIC = b"BWX RAW\x00"
HEADER_SIZE = 0x100
RAW_HEADER = struct.Struct("<IBBHHIdd")         # 30 bytes
assert RAW_HEADER.size == 30

# Device IDs and message codes as NavAQ's drivers record them
CERTUS_DEVICE = 2
HESAI_DEVICE = 3
CERTUS_DECODER = 130            # NavAQDrv_AdNavSpatial.dll
HESAI_DECODER = 154             # NavAQDrv_Hesai.dll
ANPP_CODES = {20: 10420, 26: 10421, 58: 10422, 21: 10423, 28: 10424}
HESAI_POINTS = 10760
HESAI_GPS = 10761
RUN_SECONDS = 3.0
CLOCK_INTERVAL = 10.0
FSYNC_SECONDS = 5.0          # data reaches the disk at least this often
MAX_RECORD = 64 << 20        # no real record is anywhere near this

LASER_SYSTEM_ID = 7
LASER_AUX_SYSTEM_ID = 8


def anpp_code(packet_id):
    """Message code for a Certus packet, or None if NavAQ does not decode it."""
    c = ANPP_CODES.get(packet_id)
    return None if c is None else (CERTUS_DECODER << 16) | c


def hesai_code(payload_len):
    if payload_len == 1080:
        return (HESAI_DECODER << 16) | HESAI_POINTS
    if payload_len == 512:
        return (HESAI_DECODER << 16) | HESAI_GPS
    return None


def file_name(line, start_unix, number, prefix_digits=3):
    # NNN = survey line number (Settings > BeamworX), 0001 = file of that recording
    """NNN_YYYYMMDD_HHMMSS_0001.bwxraw, with the UTC start time."""
    return "%0*d_%s_%04d.bwxraw" % (prefix_digits, line,
                                    time.strftime("%Y%m%d_%H%M%S", time.gmtime(start_unix)), number)


def qcompress(data):
    return struct.pack(">I", len(data)) + zlib.compress(data, 6)


def _padded(text, size):
    b = text.encode("latin-1", "replace")[:size - 1]
    return b + b"\x00" * (size - len(b))


def _record(rtype, body, hlen=None):
    L = 8 + len(body)
    return struct.pack("<IBBH", L, rtype, 1, hlen if hlen is not None else L) + body


class Writer:
    """One .bwxraw file. Not thread-safe by itself; BwxRecorder serialises.

    `elapsed0` is the monotonic clock value treated as zero for the
    "seconds since start" field (shared by all files of a logger run)."""

    def __init__(self, path, config_xml, vehicle, sensor, line, number,
                 start_time, elapsed0=None, file_id=None, run_in=(), el_of=None):
        self.path = path
        self.el_of = el_of               # time -> elapsed seconds (conversion from .clraw)
        self.line, self.number = line, number
        self.elapsed0 = time.monotonic() if elapsed0 is None else elapsed0
        self.file_id = file_id or uuid.uuid4().bytes
        self.count = 0
        self.first = None
        self.last = None
        self.clock_seq = 1
        self.next_clock = None
        self.next_sync = time.monotonic() + FSYNC_SECONDS
        self.f = open(path + ".partial", "wb")
        self._header(vehicle, sensor)
        self.f.write(self._config(config_xml, start_time))
        self.f.write(_record(5, b"\x00" * 6 + struct.pack("<d", start_time) + b"\x00" * (312 - 22), 312))
        self.f.write(self._clock(1, start_time))
        for rec in run_in:                       # (device, code, arrival, elapsed, payload)
            self.raw(*rec)
        self.f.write(self._marker(1, start_time))
        self.f.write(self._line_info(start_time))
        self.next_clock = start_time + CLOCK_INTERVAL
        self.closed = False

    # ---- fixed parts -------------------------------------------------------
    def _header(self, vehicle, sensor):
        h = bytearray(HEADER_SIZE)
        h[0:8] = MAGIC
        h[8:14] = b"\x00\x01\x00\x00\x01\x02"
        h[0x2E:0x32] = b"\x18\x01\x01\x03"
        h[0x32:0x50] = _padded(vehicle, 0x50 - 0x32)
        h[0x50:0x58] = b"\x00\x01\x00\x07\x00\x00\x00\x08"
        h[0x58:0xE9] = _padded(sensor, 0xE9 - 0x58)
        struct.pack_into("<HHH", h, 0xE9, self.line % 65536, self.number, 2)
        h[0xEF:0xFF] = self.file_id
        h[0xFF] = 1
        self.f.write(bytes(h))

    def _config(self, xml, t):
        body = b"\xff\xff\xff\xff" + struct.pack("<d", t) + b"\x00" * 10 + qcompress(xml)
        return _record(4, body, 30)

    def _clock(self, seq, t):
        """Clock synchronisation record, laid out as NavAQ writes it (the
        logger's clock follows GNSS time, so the reported offset is zero)."""
        el = self._el(t)
        b = bytearray(164)
        struct.pack_into("<IBBH", b, 0, 164, 6, 1, 60)
        struct.pack_into("<Idd", b, 8, seq, el, t)
        struct.pack_into("<HHIII", b, 60, 3, 40, 1, 4, 4)
        struct.pack_into("<d", b, 76, 10.0)
        struct.pack_into("<I", b, 92, seq)
        struct.pack_into("<dd", b, 116, t, el)
        struct.pack_into("<d", b, 140, t)
        struct.pack_into("<d", b, 156, el)
        return bytes(b)

    def _el(self, t):
        return self.el_of(t) if self.el_of else time.monotonic() - self.elapsed0

    def _marker(self, kind, t):
        b = bytearray(96 - 8)
        struct.pack_into("<Idd", b, 0, kind, self._el(t), t)
        struct.pack_into("<II", b, 20, self.line, self.number)
        struct.pack_into("<ff", b, 32, RUN_SECONDS, RUN_SECONDS)
        return _record(7, bytes(b), 96)

    def _line_info(self, t, position=None):
        b = bytearray(128)
        struct.pack_into("<IBBH", b, 0, 128, 8, 1, 128)
        struct.pack_into("<ddI", b, 8, t, t, 11)
        struct.pack_into("<dII", b, 36, 60.0, 2, 0)
        if position:
            struct.pack_into("<ddd", b, 52, *position)
        struct.pack_into("<I", b, 76, 1)
        return bytes(b)

    # ---- data --------------------------------------------------------------
    def raw(self, device, code, arrival, elapsed, payload):
        if self.next_clock is not None and arrival >= self.next_clock:
            self.clock_seq += 1
            self.f.write(self._clock(self.clock_seq, arrival))
            self.next_clock = arrival + CLOCK_INTERVAL
        self.f.write(RAW_HEADER.pack(30 + len(payload), 3, 1, 30, device, code, arrival, elapsed))
        self.f.write(payload)
        self.count += 1
        if time.monotonic() >= self.next_sync:
            self.sync()
        if self.first is None:
            self.first = arrival
        self.last = arrival

    def sync(self):
        """Header up to date and everything written so far on the disk."""
        self.update_header()
        self.f.flush()
        try:
            os.fdatasync(self.f.fileno())
        except (AttributeError, OSError):
            os.fsync(self.f.fileno())
        self.next_sync = time.monotonic() + FSYNC_SECONDS

    @property
    def size(self):
        return self.f.tell()

    def update_header(self):
        """Write the record count and first/last times into the header (kept
        current so a file cut short by a power loss is still described)."""
        pos = self.f.tell()
        self.f.seek(0x16)
        self.f.write(struct.pack("<I", self.count))
        self.f.seek(0x1E)
        self.f.write(struct.pack("<dd", self.first or 0.0, self.last or 0.0))
        self.f.seek(pos)

    def end_marker(self, t):
        """Clock and run-out marker; run-out records follow via raw()."""
        self.clock_seq += 1
        self.f.write(self._clock(self.clock_seq, t))
        self.next_clock = None
        self.f.write(self._marker(3, t))

    def close(self):
        if self.closed:
            return
        self.update_header()
        self.f.flush()
        os.fsync(self.f.fileno())
        self.f.close()
        os.replace(self.path + ".partial", self.path)
        self.closed = True


def repair(partial_path):
    """Finish a file cut short (power loss, crash): keep every complete
    record, correct the header and give it its final name. Walks the file
    record by record (never reads it whole: files can be gigabytes). Returns
    the final path, or None if nothing usable was in it."""
    final = partial_path[:-len(".partial")] if partial_path.endswith(".partial") else partial_path
    with open(partial_path, "r+b") as f:
        head = f.read(HEADER_SIZE)
        if len(head) < HEADER_SIZE or head[:8] != MAGIC:
            f.close()
            os.remove(partial_path)
            return None
        size = os.fstat(f.fileno()).st_size
        off, count, first, last = HEADER_SIZE, 0, None, None
        while off + 8 <= size:
            f.seek(off)
            h = f.read(30)
            if len(h) < 8:
                break
            L, = struct.unpack_from("<I", h, 0)
            if L < 8 or off + L > size or L > MAX_RECORD:
                break
            if h[4] == 3 and L >= 30 and len(h) == 30:
                t, = struct.unpack_from("<d", h, 14)
                count += 1
                first = t if first is None else first
                last = t
            off += L
        f.truncate(off)
        f.seek(0x16)
        f.write(struct.pack("<I", count))
        f.seek(0x1E)
        f.write(struct.pack("<dd", first or 0.0, last or 0.0))
        f.flush()
        os.fsync(f.fileno())
    os.replace(partial_path, final)
    return final


def next_number(folder, line, digits=3):
    """The next file number for this line in the folder (after a restart)."""
    pre = "%0*d_" % (digits, line)
    best = 0
    try:
        names = os.listdir(folder)
    except OSError:
        return 1
    for n in names:
        base = n[:-len(".partial")] if n.endswith(".partial") else n
        if base.startswith(pre) and base.endswith(".bwxraw"):
            try:
                best = max(best, int(base[-len("0001.bwxraw"):-len(".bwxraw")]))
            except ValueError:
                pass
    return best + 1


class NavBuffer:
    """The last RUN_SECONDS of navigation records (Certus packets and Hesai
    GPS packets) for the run-in of the next file."""

    def __init__(self, seconds=RUN_SECONDS):
        self.seconds = seconds
        self.q = deque()

    def add(self, rec):
        self.q.append(rec)
        cut = rec[2] - self.seconds
        while self.q and self.q[0][2] < cut:
            self.q.popleft()

    def snapshot(self):
        return list(self.q)


def is_nav(rec):
    return rec[0] == CERTUS_DEVICE or (rec[1] & 0xFFFF) == HESAI_GPS


class Recorder:
    """Writes one recording as a series of .bwxraw files.

    put() takes (device, code, arrival, payload). Files split when the
    configured size or time is reached; each split gets the last three
    seconds of navigation as run-in, and the file it follows keeps
    receiving navigation for three seconds as run-out."""

    def __init__(self, folder, config_xml, vehicle, sensor, line,
                 split_bytes=None, split_seconds=None, digits=3):
        self.folder = folder
        self.digits = digits
        self.config_xml = config_xml
        self.vehicle, self.sensor, self.line = vehicle, sensor, line
        self.split_bytes = split_bytes
        self.split_seconds = split_seconds
        self.elapsed0 = time.monotonic()
        self.nav = NavBuffer()
        self.cur = None
        # a recording resumed after a restart: finish what the last one left,
        # then carry on with the next file number
        try:
            for n in sorted(os.listdir(folder)):
                if n.endswith(".bwxraw.partial"):
                    repair(os.path.join(folder, n))
        except OSError:
            pass
        self.tails = []            # [(writer, run-out end time)]: files still taking navigation
        self.number = next_number(folder, line, digits) - 1
        self.files = []
        self.stopping = None          # run-out end time once stop() was called
        self.lock = threading.Lock()

    def _open(self, t):
        self.number += 1
        name = file_name(self.line, t, self.number, self.digits)
        w = Writer(os.path.join(self.folder, name), self.config_xml, self.vehicle, self.sensor,
                   self.line, self.number, t, self.elapsed0, run_in=self.nav.snapshot())
        w.opened = t
        self.files.append(name)
        try:                                   # the same configuration beside the file, as XML
            xml_path = os.path.join(self.folder, name[:-len(".bwxraw")] + ".xml")
            with open(xml_path + ".partial", "wb") as f:
                f.write(self.config_xml)
            os.replace(xml_path + ".partial", xml_path)
        except OSError:
            pass
        return w

    def put(self, device, code, arrival, payload, elapsed=None):
        with self.lock:
            if elapsed is None:
                elapsed = time.monotonic() - self.elapsed0
            rec = (device, code, arrival, elapsed, payload)
            nav = is_nav(rec)
            if nav:
                self._feed_tails(rec)
            if self.stopping is not None:          # after Stop: run-out only
                return
            if self.cur is None:
                self.cur = self._open(arrival)
            elif ((self.split_bytes and self.cur.size >= self.split_bytes) or
                  (self.split_seconds and arrival - self.cur.opened >= self.split_seconds)):
                self._end_file(arrival)
                self.cur = self._open(arrival)
            self.cur.raw(*rec)
            if nav:
                self.nav.add(rec)

    def _end_file(self, t):
        """The current file ends at t; it takes navigation for 3 s more (run-out)."""
        old, self.cur = self.cur, None
        old.end_marker(t)
        self.tails.append((old, t + RUN_SECONDS))

    def _feed_tails(self, rec):
        keep = []
        for w, until in self.tails:
            if rec[2] <= until:
                w.raw(*rec)
                keep.append((w, until))
            else:
                w.close()
        self.tails = keep

    def _finish_tail(self):
        while self.tails:
            self.tails.pop(0)[0].close()

    def stop(self, t=None):
        """Stop recording data. Like NavAQ, the last file then takes three more
        seconds of navigation (run-out); call close() after that, or at once."""
        with self.lock:
            if self.cur:
                t = t or self.cur.last or time.time()
                self._end_file(t)
            self.stopping = t or time.time()

    @property
    def run_out_done(self):
        return not self.tails

    def close(self, t=None):
        """Finish: closes the current file (and any run-out still open)."""
        if self.stopping is None:
            self.stop(t)
        with self.lock:
            self._finish_tail()
            return list(self.files)


# ---------------------------------------------------------------------------
# Reading (for checks, tests and the live map)
# ---------------------------------------------------------------------------
def read_records(path_or_bytes, start=HEADER_SIZE):
    if isinstance(path_or_bytes, (bytes, bytearray)):
        f = path_or_bytes
    else:
        with open(path_or_bytes, "rb") as fh:
            f = fh.read()
    off = start
    while off + 8 <= len(f):
        L, = struct.unpack_from("<I", f, off)
        if L < 8 or off + L > len(f):
            raise ValueError("bad record at %#x" % off)
        yield off, L, f[off + 4], f
        off += L


def read_header(f):
    if f[:8] != MAGIC:
        raise ValueError("not a .bwxraw file")
    count, = struct.unpack_from("<I", f, 0x16)
    first, last = struct.unpack_from("<dd", f, 0x1E)
    line, number = struct.unpack_from("<HH", f, 0xE9)
    return {"records": count, "first": first, "last": last, "line": line, "number": number,
            "vehicle": f[0x32:0x50].split(b"\x00")[0].decode("latin-1"),
            "sensor": f[0x58:0xE9].split(b"\x00")[0].decode("latin-1"),
            "file_id": f[0xEF:0xFF].hex()}


def read_config(f):
    for off, L, t, _ in read_records(f):
        if t == 4:
            body = f[off + 30:off + L]
            n, = struct.unpack(">I", body[:4])
            xml = zlib.decompress(body[4:])
            if len(xml) != n:
                raise ValueError("configuration length mismatch")
            return xml
    return None


def raw_packets(f):
    for off, L, t, _ in read_records(f):
        if t == 3:
            _, _, _, _, dev, code, arr, el = RAW_HEADER.unpack_from(f, off)
            yield dev, code, arr, el, f[off + 30:off + L]
