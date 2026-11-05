"""SonarView recordings (.svlog / .svlz) to .clraw: the marine sonar recording format of the DCS plugins.

One recording file -> <name>_0001.clraw (_0002 ... at 2 GB), kind "sonar". Nothing is decoded away: every Ping
packet is stored exactly as recorded (header and checksum included), in recording order:

    /sonar/<device id>/ping   every packet from that sonar (os3d_point_set 3104, end_ping_info 3010, attitude
                              504 / 505 = its own motion sensor, side-scan 2198, ...): one channel per sonar
    /vehicle/mavlink          packet 150: the vehicle's MAVLink messages as JSON (position, course, attitude ...)
    /gnss/nmea                packet 109: text from a GNSS / heading device as SonarView wrapped it (NMEA 0183
                              sentences, or e.g. NMEA 2000 frames as text)
    /sonarview/session        packets 10 / 12: SonarView's session and platform JSON
    /sonarview/other          anything else (from no sonar of the session)
    /gnss/llh                 lines of the GNSS receiver's own solution log (Emlid .LLH), when given
    /gnss/ppk                 lines of a PPK solution (RTKLIB .pos, UTC), when given
    /capture/events           clraw_start / clraw_stop (first and last sonar packet)

Attachments: hardware.json (platform "marine": reference point = the GNSS antenna; each sonar's lever arm to
its acoustic centre [F, S, D] m and its mount as SonarView recorded it), sonarview_session.json, sources.json
(the input files with size and SHA-256), the GNSS RINEX files given (rover and base observations cut to the
recording +- 10 min, navigation files whole) as gnss/<name>.

Times. SonarView stores no arrival time. log_time is the best UTC known for each packet, never decreasing:
the packet's own UTC (Omniscan 3D packets 3104, 3010, 504 carry it when the sonar is on NTP), else MAVLink
SYSTEM_TIME (UTC <-> vehicle boot clock), else an estimate from SonarView's session start time plus the vehicle
boot clock; a packet without a time of its own takes the last time before it. publish_time = the packet's own
UTC when it has one, else log_time. Metadata time_basis says which applied. Geometry must use the packets' own
times (the raw packets are all there)."""
import hashlib
import json
import os
import re
import time

from dcs_core import ping

from . import NAME, __version__, software
from .clrawwriter import EXT, NS, Writer

SPLIT_BYTES = 2 << 30
GNSS_MARGIN_S = 600.0           # RINEX observations kept around the recording (PPK convergence)
LINE_MARGIN_S = 60.0            # LLH / PPK lines kept around the recording

SCHEMAS = [
    (1, "clraw.Cerulean.PingPacket", "x-clraw-doc",
     "One Cerulean / Blue Robotics Ping protocol packet exactly as SonarView recorded it: 'BR', u16 payload length, "
     "u16 message id, u8 source device, u8 destination device, payload, u16 checksum (sum of all bytes before it). "
     "Little-endian. Layouts: github.com/bluerobotics/ping-protocol and docs.ceruleansonar.com."),
    (2, "clraw.Text", "x-clraw-doc", "One line of text (UTF-8) from a GNSS log, as in the source file."),
    (3, "clraw.Json", "jsonschema", json.dumps({"type": "object"})),
]
T_SESSION, T_MAV, T_NMEA, T_OTHER, T_LLH, T_PPK, T_EVENTS = (
    "/sonarview/session", "/vehicle/mavlink", "/gnss/nmea", "/sonarview/other", "/gnss/llh", "/gnss/ppk",
    "/capture/events")


class Cancelled(Exception):
    pass


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# ----------------------------------------------------------------------------- pass 1: what is in the file
def survey(path, cancelled=None):
    """One pass over a recording: sessions, sonar device ids, packet counts, UTC span, time anchors."""
    r = ping.Reader(path)
    info = {"sessions": [], "platform": [], "sonars": {}, "counts": {}, "utc_first": None, "utc_last": None,
            "mav_anchor": None, "mav_boot_first": None, "packets": 0, "uptime_first": {}}
    for n, (mid, src, _dst, pl, _pk) in enumerate(r):
        if cancelled is not None and n % 50000 == 0 and cancelled():
            raise Cancelled()
        info["counts"][mid] = info["counts"].get(mid, 0) + 1
        if mid == ping.SONARLINK_JSON:
            try:
                info["sessions"].append(ping.json_payload(pl))
            except ValueError:
                pass
            continue
        if mid == ping.PLATFORM_JSON:
            try:
                info["platform"].append(ping.json_payload(pl))
            except ValueError:
                pass
            continue
        if mid == ping.MAVLINK_JSON:
            try:
                typ, m, _h = ping.mavlink(pl)
            except ValueError:
                continue
            boot = m.get("time_boot_ms")
            if boot is not None and info["mav_boot_first"] is None:
                info["mav_boot_first"] = boot
            if typ == "SYSTEM_TIME" and m.get("time_unix_usec", 0) > 946684800000000 and info["mav_anchor"] is None:
                info["mav_anchor"] = (m["time_unix_usec"] / 1e6, m.get("time_boot_ms", 0))
            continue
        if mid == ping.NMEA:
            continue
        s = info["sonars"].setdefault(src, {"packets": 0, "ids": {}})
        s["packets"] += 1
        s["ids"][mid] = s["ids"].get(mid, 0) + 1
        up = ping.packet_uptime_ms(mid, pl)
        if up is not None and src not in info["uptime_first"]:
            info["uptime_first"][src] = up
        u = ping.packet_utc_ms(mid, pl)
        if u is not None:
            info["utc_first"] = u if info["utc_first"] is None else min(info["utc_first"], u)
            info["utc_last"] = u if info["utc_last"] is None else max(info["utc_last"], u)
    info["packets"] = r.packets
    info["skipped_bytes"] = r.skipped_bytes
    info["truncated"] = r.truncated
    return info


def session_start(info):
    for s in info["sessions"]:
        ts = s.get("timestamp")
        if ts:
            try:
                import datetime
                return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
    return None


def session_devices(info):
    """{device id: SonarView's device record} (device_id key, else the order in session_devices)."""
    out = {}
    for s in info["sessions"]:
        for i, d in enumerate(s.get("session_devices") or []):
            did = d.get("device_id", i if len(s.get("session_devices") or []) > 1 else 0)
            out.setdefault(int(did), d)
    return out


# ----------------------------------------------------------------------------- GNSS side files
def read_lines_in_span(path, t0, t1, kind):
    """[(utc s, line bytes)] of an LLH or RTKLIB .pos file within [t0, t1] (UTC). Header lines are skipped."""
    import datetime
    out = []
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith(("%", "#")):
                continue
            a = s.split()
            try:
                t = datetime.datetime.strptime(a[0] + " " + a[1], "%Y/%m/%d %H:%M:%S.%f").replace(
                    tzinfo=datetime.timezone.utc).timestamp()
            except (ValueError, IndexError):
                continue
            if t0 <= t <= t1:
                out.append((t, s.encode("utf-8")))
    return out


def gps_to_utc(t_gps, leap=18):
    return t_gps - leap


def cut_rinex_obs(path, t0, t1):
    """RINEX 3 observation file cut to epochs within [t0, t1] (UTC s; epochs are GPS time). RINEX 2 and
    anything unrecognised: the whole file. Returns bytes."""
    import calendar
    with open(path, "rb") as f:
        data = f.read()
    head_end = data.find(b"END OF HEADER")
    if head_end < 0 or not data[:80].strip().startswith(b"3"):
        return data
    nl = data.find(b"\n", head_end)
    head, body = data[:nl + 1], data[nl + 1:]
    out = [head]
    keep = False
    for line in body.splitlines(True):
        if line.startswith(b">"):
            try:
                y, mo, d, h, mi = (int(x) for x in line[2:].split()[:5])
                sec = float(line[2:].split()[5])
                t = gps_to_utc(calendar.timegm((y, mo, d, h, mi, 0)) + sec)
                keep = t0 <= t <= t1
            except (ValueError, IndexError):
                keep = True
        if keep:
            out.append(line)
    return b"".join(out)


def rinex_kind(path):
    """'obs', 'nav' or None from the RINEX header line."""
    try:
        with open(path, "rb") as f:
            first = f.read(80)
    except OSError:
        return None
    t = first[20:40].upper()
    if b"OBSERVATION" in t or (len(first) > 20 and first[20:21] == b"O"):
        return "obs"
    if b"NAV" in t or first[20:21] in (b"N", b"G", b"L"):
        return "nav"
    return None


# ----------------------------------------------------------------------------- hardware record
def _num(v):
    """SonarView stores some mount values as text ("45", "-.25")."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def hardware_json(info, offsets=None, gnss=None, extra=None):
    """The marine hardware record. offsets: {device id (int or str) or nickname or "port"/"starboard":
    [F, S, D] m from the GNSS antenna to the sonar's acoustic centre}."""
    offsets = {str(k).lower(): v for k, v in (offsets or {}).items()}
    devs = session_devices(info)
    sonars = []
    # newer SonarView keeps the Omniscan 3D mounts in the platform JSON (packet 12), by channel number in the
    # order of the session's os3d devices
    os3d = [k for k, v in sorted(devs.items()) if str(v.get("product_id", "")).startswith("os3d")]
    plat = {}
    for pj in info.get("platform") or []:
        for o in pj.get("os3d_device_options") or []:
            plat[int(o.get("channel_number", 0))] = o.get("options") or {}
    for did in sorted(info["sonars"]):
        d = devs.get(did, {})
        opt = d.get("options") or {}
        if not any(k.startswith("mount_") for k in opt) and did in os3d and os3d.index(did) in plat:
            opt = plat[os3d.index(did)]
        rec = {"device_id": did, "topic": "/sonar/%d/ping" % did, "product_id": d.get("product_id"),
               "nickname": d.get("nickname"), "url": d.get("url"),
               "packets": info["sonars"][did]["packets"],
               "packet_ids": {str(k): v for k, v in sorted(info["sonars"][did]["ids"].items())},
               "mount_deg_sonarview": {k: _num(opt.get("mount_%s_deg" % k)) for k in ("roll", "pitch", "yaw")}
               if opt else None,
               "mount_m_sonarview": [_num(opt.get(k)) for k in ("mount_x", "mount_y", "mount_z")] if opt else None,
               "motion_sensor": "internal (RX16 IMU): packets 504 and 505 on this sonar's channel"
               if str(d.get("product_id", "")).startswith("os3d") else None}
        lever = None
        for key in (str(did), str(d.get("nickname", "")).lower()):
            if key and key in offsets:
                lever = offsets[key]
        if lever is None:
            side = None
            if _num(opt.get("mount_y")) not in (None, 0):
                side = "starboard" if _num(opt["mount_y"]) > 0 else "port"
            if side and side in offsets:
                lever = offsets[side]
        if lever is not None:
            rec["acoustic_centre_m"] = [float(v) for v in lever]
            rec["acoustic_centre_source"] = "measured (entered in RemixPro)"
            rec["side"] = "starboard" if float(lever[1]) > 0 else "port" if float(lever[1]) < 0 else "centre"
        else:
            rec["acoustic_centre_m"] = None
            rec["acoustic_centre_source"] = "not entered: SonarView's mount_m_sonarview is kept as recorded, its " \
                                            "frame and origin are not verified"
        sonars.append(rec)
    hw = {"platform": "marine",
          "reference_point": "GNSS antenna reference point (ARP) of the position receiver",
          "conventions": "vehicle frame Forward/Starboard/Down from the reference point, metres; roll + starboard "
                         "down, pitch + bow up, heading + clockwise, degrees. Cerulean attitude packets give the "
                         "world up vector in the sonar's frame (x forward, y port, z up).",
          "gnss": dict({"model": "Emlid Reach M2", "lever_m": [0.0, 0.0, 0.0]}, **(gnss or {})),
          "sonars": sonars}
    if extra:
        hw.update(extra)
    return hw


# ----------------------------------------------------------------------------- pass 2: write
class _Clock:
    """log_time for each packet (see the module notes)."""

    def __init__(self, info):
        self.basis = "sonar UTC (NTP)" if info["utc_first"] else None
        self.anchor = info["mav_anchor"]
        start = session_start(info)
        if self.basis is None and self.anchor:
            self.basis = "MAVLink SYSTEM_TIME"
        self.uptime0 = dict(info.get("uptime_first") or {})
        self.start = start
        if self.basis is None and self.uptime0 and start is not None:
            self.basis = "estimated: SonarView session start + each sonar's power-up clock (not synchronised)"
        if self.basis is None:
            self.basis = "estimated: SonarView session start + vehicle boot clock (not synchronised)"
        if self.anchor is None and start is not None and info["mav_boot_first"] is not None:
            self.anchor = (start, info["mav_boot_first"])
        first = info["utc_first"] / 1000.0 if info["utc_first"] else (start or time.time())
        self.last = int(first * NS)

    def of(self, mid, pl, src=None):
        """(log_ns, publish_ns)."""
        own = ping.packet_utc_ms(mid, pl)
        t = None
        if own is not None:
            t = own * 1000000
        elif self.basis.startswith("estimated: SonarView session start + each") and src in self.uptime0:
            up = ping.packet_uptime_ms(mid, pl)
            if up is not None:
                t = int((self.start + (up - self.uptime0[src]) / 1000.0) * NS)
                own = t // 1000000
        elif mid == ping.MAVLINK_JSON and self.anchor is not None:
            try:
                _typ, m, _h = ping.mavlink(pl)
                b = m.get("time_boot_ms")
                if b is not None:
                    t = int((self.anchor[0] + (b - self.anchor[1]) / 1000.0) * NS)
            except ValueError:
                pass
        if t is not None and t > self.last:
            self.last = t
        log = self.last
        return log, (t if own is not None else log)


def to_clraw(path, out_dir, base_name=None, line=1, project="", offsets=None, gnss=None, llh=(), ppk=(),
             rinex=(), extra_hw=None, extra_attachments=(), progress=None, cancelled=None, split_bytes=SPLIT_BYTES):
    """Convert one SonarView recording. Returns a summary dict (files written, counts, time span and basis)."""
    stop = cancelled or (lambda: False)
    base_name = base_name or re.sub(r"\.(svlog|svlz)$", "", os.path.basename(path), flags=re.I)
    info = survey(path, stop)
    if not info["sonars"]:
        raise ValueError("%s holds no sonar packets" % os.path.basename(path))
    clock = _Clock(info)
    t_first = (info["utc_first"] / 1000.0) if info["utc_first"] else (session_start(info) or 0.0)
    sonar_ids = sorted(info["sonars"])
    channels = [(1, 3, T_SESSION, "json"), (2, 3, T_MAV, "json"), (3, 1, T_NMEA, "x-cerulean-ping"),
                (4, 1, T_OTHER, "x-cerulean-ping"), (5, 2, T_LLH, "text"), (6, 2, T_PPK, "text"),
                (7, 3, T_EVENTS, "json")]
    ch_sonar = {}
    for i, did in enumerate(sonar_ids):
        ch_sonar[did] = 10 + i
        channels.append((10 + i, 1, "/sonar/%d/ping" % did, "x-cerulean-ping"))
    hw = hardware_json(info, offsets, gnss, extra_hw)
    sources = [{"file": os.path.basename(path), "size": os.path.getsize(path), "sha256": _sha256(path),
                "role": "SonarView recording"}]
    atts = [("hardware.json", "application/json", json.dumps(hw, indent=1).encode()),
            ("sonarview_session.json", "application/json",
             json.dumps({"sessions": info["sessions"], "platform": info["platform"]}, indent=1).encode())]
    # GNSS side files: written once we know the recording's span (pass 2 below gives the last time)
    span0 = t_first
    md = {"format": "clraw", "format_version": "1", "kind": "sonar", "platform": "marine",
          "writer": software(), "capture": base_name, "capture_name": base_name, "project": project or "",
          "project_id": project or "", "line": int(line), "line_digits": 3, "source": "SonarView recording",
          "source_file": os.path.basename(path), "sonars": ",".join(str(d) for d in sonar_ids),
          "time": "log_time = best UTC known for the packet (never decreasing); publish_time = the packet's own "
                  "UTC when it carries one, else log_time",
          "time_basis": clock.basis}
    os.makedirs(out_dir, exist_ok=True)
    total = os.path.getsize(path) or 1
    files, writer = [], [None]
    number = [0]
    n_written = {}
    last_sonar = [None]
    first_sonar = [None]
    gnss_atts = []

    def open_next():
        number[0] += 1
        name = "%s_%04d%s" % (base_name, number[0], EXT)
        w = Writer(os.path.join(out_dir, name), dict(md, file_number=str(number[0]), file_name=name),
                   atts + gnss_atts + list(extra_attachments) +
                   [("sources.json", "application/json", json.dumps(sources + [s for _a, s in gnss_src],
                                                                    indent=1).encode())],
                   library=software(), schemas=SCHEMAS, channels=channels)
        files.append(os.path.join(out_dir, name))
        return w

    # GNSS side files need the span: take it from pass 1 (sonar UTC) or the session start + MAVLink boot span
    t_last = (info["utc_last"] / 1000.0) if info["utc_last"] else None
    gnss_src = []
    if t_last is None:
        t_last = span0 + 24 * 3600          # unknown span: keep a day (lines are cut only by time)
    for p in rinex:
        kind = rinex_kind(p)
        data = cut_rinex_obs(p, span0 - GNSS_MARGIN_S, t_last + GNSS_MARGIN_S) if kind == "obs" else open(p, "rb").read()
        gnss_atts.append(("gnss/" + os.path.basename(p), "text/plain", data))
        gnss_src.append((p, {"file": os.path.basename(p), "size": os.path.getsize(p), "sha256": _sha256(p),
                             "role": "GNSS RINEX %s (cut to the recording +- %d s)" % (kind or "?", GNSS_MARGIN_S)
                             if kind == "obs" else "GNSS RINEX %s" % (kind or "?")}))
    side_lines = []
    for paths, ch, role in ((llh, 5, "GNSS solution log (LLH)"), (ppk, 6, "PPK solution (.pos)")):
        for p in paths:
            rows = read_lines_in_span(p, span0 - LINE_MARGIN_S, t_last + LINE_MARGIN_S, role)
            side_lines += [(t, ch, b) for t, b in rows]
            gnss_src.append((p, {"file": os.path.basename(p), "size": os.path.getsize(p), "sha256": _sha256(p),
                                 "role": role, "lines_kept": len(rows)}))
    side_lines.sort(key=lambda r: r[0])
    si = [0]

    def put(ch, log_ns, pub_ns, data):
        w = writer[0]
        if w is None or (split_bytes and w.size >= split_bytes):
            if w is not None:
                w.close()
            writer[0] = w = open_next()
        w.add(ch, log_ns, pub_ns, data)
        n_written[ch] = n_written.get(ch, 0) + 1

    def flush_side(until_ns):
        while si[0] < len(side_lines) and int(side_lines[si[0]][0] * NS) <= until_ns:
            t, ch, b = side_lines[si[0]]
            put(ch, int(t * NS), int(t * NS), b)
            si[0] += 1

    r = ping.Reader(path)
    try:
        for n, (mid, src, _dst, pl, pk) in enumerate(r):
            if n % 20000 == 0:
                if stop():
                    raise Cancelled()
                if progress:
                    progress(min(0.99, r.tell() / total))
            log_ns, pub_ns = clock.of(mid, pl, src)
            flush_side(log_ns)
            if mid in (ping.SONARLINK_JSON, ping.PLATFORM_JSON):
                put(1, log_ns, pub_ns, bytes(pl).rstrip(b"\x00"))
            elif mid == ping.MAVLINK_JSON:
                put(2, log_ns, pub_ns, bytes(pl).rstrip(b"\x00"))
            elif mid == ping.NMEA:
                put(3, log_ns, pub_ns, pk)
            elif src in ch_sonar:
                if first_sonar[0] is None:
                    first_sonar[0] = log_ns
                    put(7, log_ns, log_ns, json.dumps({"event": "clraw_start", "t": log_ns / NS}).encode())
                put(ch_sonar[src], log_ns, pub_ns, pk)
                last_sonar[0] = log_ns
            else:
                put(4, log_ns, pub_ns, pk)
        flush_side(1 << 62)
        if last_sonar[0] is not None:
            put(7, last_sonar[0], last_sonar[0], json.dumps({"event": "clraw_stop", "t": last_sonar[0] / NS}).encode())
        writer[0].close()
    except BaseException:
        w = writer[0]
        if w is not None:
            try:
                w.f.close()
            except Exception:       # noqa: BLE001
                pass
        raise
    if progress:
        progress(1.0)
    by_topic = {t: n_written.get(c, 0) for c, _s, t, _e in channels}
    return {"files": files, "packets": info["packets"], "skipped_bytes": info["skipped_bytes"] + r.skipped_bytes,
            "truncated": info["truncated"], "sonars": sonar_ids, "messages": by_topic, "time_basis": clock.basis,
            "first_utc": first_sonar[0] / NS if first_sonar[0] else None,
            "last_utc": last_sonar[0] / NS if last_sonar[0] else None, "hardware": hw,
            "sources": sources + [s for _a, s in gnss_src], "program": NAME, "version": __version__}
