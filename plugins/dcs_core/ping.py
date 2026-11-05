"""Cerulean / Blue Robotics Ping protocol: SonarView recordings (.svlog, .svlz) and the packets in them.

A .svlog is a plain series of Ping packets; a .svlz is the same bytes gzip-compressed (SonarView 1.14.20 on).
Each packet (little-endian):

    0  'B' 'R'
    2  u16 payload length
    4  u16 message id
    6  u8  source device id      (SonarView: the device's id in the session; 0 for the session / vehicle)
    7  u8  destination device id
    8  payload
    .. u16 checksum = sum of every byte before it, mod 65536

SonarView starts each file with packet 10 (SONARLINK JSON: the session, its devices with their mounts), may add
packet 12 (platform JSON), then records the sonar packets and the vehicle data: packet 150 (MAVLink message as
JSON, from mavlink2rest) and packet 109 (an NMEA sentence).

Decoded here (layouts from github.com/bluerobotics/ping-protocol omniscan3d.json / omniscan450.json and
docs.ceruleansonar.com):
    10   SONARLINK / JSON wrapper      session JSON
    12   platform JSON
    109  NMEA wrapper                  text
    150  MAVLink wrapper               JSON {"header": {...}, "message": {"type": ..., ...}}
    504  attitude_report               up vector (x fwd, y port, z up), utc_msec, pwr_up_msec, channel
    2198 os_mono_profile               Omniscan side-scan intensity profile
    3010 end_ping_info                 Omniscan 3D per-ping info (range, up vector, temperature, utc_msec ...)
    3104 os3d_point_set                Omniscan 3D detections: angle, time of flight, power, type
Packet 505 (a second attitude report, ~100 Hz per Omniscan 3D) has no published layout yet: it is kept as recorded.
Nothing here needs Qt."""
import gzip
import json
import math
import struct

HEADER = 8
MAGIC = b"BR"
SONARLINK_JSON = 10
PLATFORM_JSON = 12
NMEA = 109
MAVLINK_JSON = 150
ATTITUDE_REPORT = 504
ATTITUDE_REPORT_2 = 505
OS_MONO_PROFILE = 2198
END_PING_INFO = 3010
OS3D_POINT_SET = 3104
EXTENSIONS = (".svlog", ".svlz")


class PingError(ValueError):
    pass


def checksum(data):
    return sum(data) & 0xFFFF


def build(message_id, payload=b"", src=0, dst=0):
    """One Ping packet, header and checksum included."""
    head = MAGIC + struct.pack("<HHBB", len(payload), message_id, src, dst)
    return head + payload + struct.pack("<H", checksum(head + payload))


def open_log(path):
    """A binary file object over the decompressed bytes of a .svlog or .svlz (gzip detected from the content).
    Closing it closes the file underneath too."""
    f = open(path, "rb")
    gz = f.read(2) == b"\x1f\x8b"
    f.seek(0)
    if not gz:
        return f

    class _Gz(gzip.GzipFile):
        def close(self):
            try:
                super().close()
            finally:
                f.close()
    return _Gz(fileobj=f, mode="rb")


class Reader:
    """Iterates the packets of one recording file: (message_id, src, dst, payload, packet bytes).
    A bad checksum or junk is skipped one byte at a time and counted in skipped_bytes; an unfinished final
    packet (a file still being written, or a cut-off gzip stream) ends the file and sets truncated.
    tell() is the position in the compressed file, for progress."""

    def __init__(self, path, block=1 << 20):
        self.path = path
        self.block = block
        self.skipped_bytes = 0
        self.packets = 0
        self.truncated = False
        self._raw = None

    def tell(self):
        try:
            return self._raw.fileobj.tell() if hasattr(self._raw, "fileobj") and self._raw.fileobj else self._raw.tell()
        except (AttributeError, ValueError, OSError):
            return 0

    def __iter__(self):
        self._raw = f = open_log(self.path)
        buf = bytearray()
        try:
            while True:
                try:
                    chunk = f.read(self.block)
                except (EOFError, OSError, gzip.BadGzipFile):       # cut-off gzip: keep what was complete
                    self.truncated = True
                    chunk = b""
                if not chunk:
                    break
                buf += chunk
                pos, n = 0, len(buf)
                while n - pos >= HEADER + 2:
                    if buf[pos] != 0x42 or buf[pos + 1] != 0x52:
                        nxt = buf.find(MAGIC, pos + 1)
                        skip = (nxt if nxt >= 0 else n - 1) - pos
                        self.skipped_bytes += skip
                        pos += skip
                        continue
                    ln, mid, src, dst = struct.unpack_from("<HHBB", buf, pos + 2)
                    end = pos + HEADER + ln + 2
                    if end > n:
                        break
                    if checksum(memoryview(buf)[pos:end - 2]) != struct.unpack_from("<H", buf, end - 2)[0]:
                        self.skipped_bytes += 1
                        pos += 1
                        continue
                    pkt = bytes(buf[pos:end])
                    self.packets += 1
                    yield mid, src, dst, pkt[HEADER:-2], pkt
                    pos = end
                del buf[:pos]
            if buf:
                self.truncated = True
                self.skipped_bytes += len(buf)
        finally:
            f.close()


def split(packet):
    """Complete packet bytes -> (message_id, src, dst, payload); PingError when it is not a good packet."""
    b = bytes(packet)
    if len(b) < HEADER + 2 or b[:2] != MAGIC:
        raise PingError("not a Ping packet")
    ln, mid, src, dst = struct.unpack_from("<HHBB", b, 2)
    if len(b) != HEADER + ln + 2 or checksum(b[:-2]) != struct.unpack_from("<H", b, len(b) - 2)[0]:
        raise PingError("bad length or checksum")
    return mid, src, dst, b[HEADER:-2]


# ----------------------------------------------------------------------------- payloads
def json_payload(payload):
    """Packets 10, 12, 150: the JSON object (a trailing NUL is allowed)."""
    return json.loads(bytes(payload).rstrip(b"\x00").decode("utf-8"))


def mavlink(payload):
    """Packet 150 -> (message type, message dict, header dict)."""
    o = json_payload(payload)
    m = o.get("message") or {}
    return m.get("type"), m, o.get("header") or {}


def nmea(payload):
    return bytes(payload).rstrip(b"\x00").decode("ascii", "replace").strip()


def up_vector_angles(x, y, z):
    """Roll and pitch (degrees) from Cerulean's world-up vector in the device frame (x forward, y port, z up):
    pitch = asin(-x), roll = atan2(y, z) (Cerulean's formulas: roll + starboard down, pitch + forward down)."""
    return math.degrees(math.atan2(y, z)), math.degrees(math.asin(max(-1.0, min(1.0, -x))))


_A504 = struct.Struct("<6fQIB")


def attitude_report(payload):
    """Packet 504."""
    x, y, z, _r1, _r2, _r3, utc, up, ch = _A504.unpack_from(payload, 0)
    roll, pitch = up_vector_angles(x, y, z)
    return {"up": (x, y, z), "utc_msec": utc, "pwr_up_msec": up, "channel": ch, "roll_deg": roll, "pitch_deg": pitch}


_E3010 = struct.Struct("<I2f3fI2f4f f iHHHBBIQ")


def end_ping_info(payload):
    """Packet 3010 (Omniscan 3D layout, 84 bytes). Shorter forms from other sonars return None."""
    if len(payload) < _E3010.size:
        return None
    v = _E3010.unpack_from(payload, 0)
    keys = ("reserved1", "range_start_m", "range_end_m", "up_x", "up_y", "up_z", "ping_number", "water_degC",
            "water_bar", "heave_m", "mag_x", "mag_y", "mag_z", "ping_hz_realized", "gain_index", "pulse_usec",
            "n_range_bins", "samples_per_range_bin", "device_number", "unused", "pwr_up_msec", "utc_msec")
    return dict(zip(keys, v))


_P3104 = struct.Struct("<IfhHIQIBBBB3f9I")
ATOF = struct.Struct("<fffB3x")


def os3d_point_set(payload):
    """Packet 3104 -> header dict and "points": [(angle rad, time of flight s, power, type)]. Version 1 only."""
    h = _P3104.unpack_from(payload, 0)
    out = {"ping_number": h[0], "sos_mps": h[1], "num_points": h[2], "utc_msec": h[5], "pwr_up_msec": h[6],
           "version": h[7], "device_number": h[8], "thresholds": (h[11], h[12], h[13])}
    if out["version"] != 1:
        raise PingError("os3d_point_set version %d is not supported" % out["version"])
    n = max(0, out["num_points"])
    if len(payload) < _P3104.size + ATOF.size * n:
        raise PingError("os3d_point_set shorter than its point count")
    out["points"] = [ATOF.unpack_from(payload, _P3104.size + ATOF.size * i) for i in range(n)]
    return out


_M2198 = struct.Struct("<IIIIIHHHBB6f")


def os_mono_profile(payload):
    """Packet 2198 (side-scan profile) -> header dict and "samples" (u16 values)."""
    h = _M2198.unpack_from(payload, 0)
    keys = ("ping_number", "start_mm", "length_mm", "timestamp_ms", "ping_hz", "gain_index", "num_results",
            "sos_dmps", "channel_number", "reserved", "pulse_duration_sec", "analog_gain", "max_pwr_db",
            "min_pwr_db", "transducer_heading_deg", "vehicle_heading_deg")
    out = dict(zip(keys, h))
    n = out["num_results"]
    out["samples"] = struct.unpack_from("<%dH" % n, payload, _M2198.size) if len(payload) >= _M2198.size + 2 * n else ()
    return out


def packet_utc_ms(mid, payload):
    """The UTC time (ms since 1970) a packet carries itself, or None (not every packet has one; 0 = not set)."""
    try:
        if mid == OS3D_POINT_SET and len(payload) >= 24:
            v = struct.unpack_from("<Q", payload, 16)[0]
        elif mid == END_PING_INFO and len(payload) >= _E3010.size:
            v = struct.unpack_from("<Q", payload, _E3010.size - 8)[0]
        elif mid == ATTITUDE_REPORT and len(payload) >= 32:
            v = struct.unpack_from("<Q", payload, 24)[0]
        else:
            return None
    except struct.error:
        return None
    return v if 946684800000 < v < 4102444800000 else None        # 2000 .. 2100


def packet_uptime_ms(mid, payload):
    """The sonar's own milliseconds-since-power-up a packet carries (3104, 3010, 504, 2198), or None."""
    try:
        if mid == OS3D_POINT_SET and len(payload) >= 28:
            return struct.unpack_from("<I", payload, 24)[0]
        if mid == END_PING_INFO and len(payload) >= _E3010.size:
            return struct.unpack_from("<I", payload, _E3010.size - 12)[0]
        if mid == ATTITUDE_REPORT and len(payload) >= 36:
            return struct.unpack_from("<I", payload, 32)[0]
        if mid == OS_MONO_PROFILE and len(payload) >= 16:
            return struct.unpack_from("<I", payload, 12)[0]
    except struct.error:
        pass
    return None
