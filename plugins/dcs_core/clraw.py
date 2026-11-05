"""The mapper's .clraw recordings (MCAP container, profile 'clraw'), for every DCS plugin.

Byte layouts follow the project's ".clraw & Sensor Reference for Plugin
Developers" (mapper 2.9.0, format version 1). Files are read front to back,
so unfinished files (no summary) still open, and a damaged chunk is skipped
and counted, never fatal.

  ClrawFile     one file: metadata, attachments, events, message chunks
  Recording     one survey line (one or more files): calibration, hardware,
                point packet chunks, ANPP navigation states
  find_clraw, group_lines, group_recordings   files and folders -> lines
  is_navigation, split_navigation             mapper 2.10 navigation recordings
Packet decoding is in dcs_core.hesai and dcs_core.anpp.

Mapper 2.10 (4 Nov 2026) names a line's files <line id>_0001.clraw (e.g. Deck_Test_L001_20261104_124512_0001)
inside <Project>/lines/<line id>/clraw/, and records the whole Certus stream while a project is open in
<Project>/nav/<Project>_NAV_<UTC start>_0001.clraw (metadata kind "navigation", line 0, no Lidar data).
Older captures (NNN_YYYYMMDD_HHMMSS_0001.clraw) can sit inside lines/ too. So a line is found from its
metadata (capture, line), never from its file name, and navigation recordings are never survey lines.
"""
import csv
import io
import json
import os
import re

import numpy as np

from . import anpp, mcap
from .hesai import (DEFAULT_CAL, DEFAULT_CALIB, HESAI_LEN, decode_hesai,  # noqa: F401 - re-exported
                    hesai_packet_times)
from .anpp import EULER_SD, SYS_STATE  # noqa: F401 - re-exported

__all__ = ["MAGIC", "FORMAT_VERSIONS", "TOPIC_POINTS", "TOPIC_STATUS", "TOPIC_ANPP", "TOPIC_EVENTS", "TOPIC_QUALITY",
           "CH_POINTS", "CH_STATUS", "CH_ANPP", "CH_EVENTS", "CH_QUALITY", "TOPICS", "ClrawError",
           "calibration_rows", "parse_calibration", "ClrawFile", "Recording", "find_clraw", "group_lines",
           "group_recordings", "file_info", "is_navigation", "split_navigation", "NAVIGATION", "SONAR", "is_sonar",
           "sonar_topics", "TOPIC_SONAR", "TOPIC_SONARVIEW", "TOPIC_SONARVIEW_OTHER", "TOPIC_MAVLINK", "TOPIC_NMEA",
           "TOPIC_GNSS_LLH", "TOPIC_GNSS_PPK", "DEFAULT_CAL", "DEFAULT_CALIB", "HESAI_LEN", "decode_hesai", "hesai_packet_times",
           "EULER_SD", "SYS_STATE"]

MAGIC = mcap.MAGIC
FORMAT_VERSIONS = ("1", "")

TOPIC_POINTS = "/lidar/points"
TOPIC_STATUS = "/lidar/status"
TOPIC_ANPP = "/ins/anpp"
TOPIC_EVENTS = "/capture/events"
TOPIC_QUALITY = "/capture/quality"
# Channel numbers as the mapper writes them. ClrawFile finds the channels by topic and reports
# messages with these numbers, so a file that numbers its channels differently still reads.
CH_POINTS, CH_STATUS, CH_ANPP, CH_EVENTS, CH_QUALITY = 1, 2, 3, 4, 5
TOPICS = {TOPIC_POINTS: CH_POINTS, TOPIC_STATUS: CH_STATUS, TOPIC_ANPP: CH_ANPP,
          TOPIC_EVENTS: CH_EVENTS, TOPIC_QUALITY: CH_QUALITY}
_CH_TOPIC = {v: k for k, v in TOPICS.items()}

_FNAME = re.compile(r"^(\d+)_(\d{8})_(\d{6})_(\d{4})\.clraw$")          # before mapper 2.10
_FNAME_210 = re.compile(r"^(?P<prefix>.+?)_(?P<number>\d{4})\.clraw$")     # <line id or NAV prefix>_0001
_LINE_210 = re.compile(r"_L(\d+)_\d{8}_\d{6}$")
NAVIGATION = "navigation"
# Marine sonar recordings (DCS Core 1.5.0): SonarView .svlog/.svlz converted by RemixPro. Kind "sonar"; the Ping
# packets of each sonar on its own channel, the vehicle and GNSS data beside them. Not Lidar survey lines: the
# grouping functions leave them out unless asked (sonar=True). Layout in dcs/marine-clraw.md.
SONAR = "sonar"
TOPIC_SONAR = "/sonar/%d/ping"                  # every Ping packet of sonar <device id>, complete (header + checksum)
TOPIC_SONARVIEW = "/sonarview/session"          # SonarView's own JSON packets (10, 12) as recorded
TOPIC_SONARVIEW_OTHER = "/sonarview/other"      # any other Ping packet not from a sonar of the session
TOPIC_MAVLINK = "/vehicle/mavlink"              # Ping packet 150: a MAVLink message as JSON (mavlink2rest)
TOPIC_NMEA = "/gnss/nmea"                       # Ping packet 109: an NMEA sentence
TOPIC_GNSS_LLH = "/gnss/llh"                    # one line of the GNSS receiver's own solution log (Emlid .LLH)
TOPIC_GNSS_PPK = "/gnss/ppk"                    # one line of a PPK solution (RTKLIB .pos, UTC, llh)


class ClrawError(ValueError):
    pass


# ----------------------------------------------------------------------------- calibration
def calibration_rows(text):
    """Hesai channel calibration CSV -> [(elevation, azimuth offset)] in channel order ([] if none)."""
    rows = []
    for r in csv.reader(io.StringIO(text)):
        if len(r) < 3:
            continue
        try:
            rows.append((int(r[0].strip()), float(r[1]), float(r[2])))
        except ValueError:
            continue
    rows.sort()
    return [(e, a) for _i, e, a in rows]


def parse_calibration(text):
    """Hesai channel calibration CSV -> (32, 2) array of (elevation, azimuth offset) degrees;
    the default table when the text has no rows."""
    rows = calibration_rows(text or "")
    return np.array(rows, float) if rows else DEFAULT_CALIB.copy()


def _check_format(meta, name):
    fv = meta.get("format_version", "1")
    if fv not in FORMAT_VERSIONS:
        raise ClrawError("%s: .clraw format_version %s is newer than this version of DCS Core understands; "
                         "update DCS Core" % (name, fv))


# ----------------------------------------------------------------------------- one file
class ClrawFile:
    """One .clraw file. Opening reads the record headers only (chunks are not decompressed)."""

    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        self.size = os.path.getsize(path)
        try:
            self._m = mcap.McapFile(path).scan()
        except ValueError as e:
            raise ClrawError("%s: not an MCAP/.clraw file (%s)" % (self.name, e))
        self.profile = self._m.profile
        self.metadata = dict(self._m.metadata.get("clraw", {}))
        self.attachments = {k: bytes(v) for k, v in self._m.attachments.items()}
        self.channels = {cid: c["topic"] for cid, c in self._m.channels.items()}       # id -> topic
        self._as_ch = {cid: TOPICS.get(t, cid) for cid, t in self.channels.items()}   # file id -> CH_*
        self.chunks = 0
        self._bad = 0
        _check_format(self.metadata, self.name)

    @property
    def bad_chunks(self):
        return self._m.bad_chunks + self._bad

    @property
    def n_chunks(self):
        return len(self._m.chunk_pos)

    def span(self):
        """(first, last) message log time in UTC s from the chunk index, or (None, None)."""
        cp = self._m.chunk_pos
        return (min(c[5] for c in cp) * 1e-9, max(c[6] for c in cp) * 1e-9) if cp else (None, None)

    # ---------------------------------------------------------------- properties
    def attachment_json(self, name):
        d = self.attachments.get(name)
        if not d:
            return {}
        try:
            return json.loads(d.decode("utf-8"))
        except ValueError:
            return {}

    @property
    def hardware(self):
        return self.attachment_json("hardware.json")

    @property
    def session(self):
        return self.attachment_json("session.json")

    @property
    def is_navigation(self):
        """A mapper 2.10 navigation recording (the Certus stream while a project is open): not a survey line."""
        return self.metadata.get("kind") == NAVIGATION

    @property
    def line(self):
        v = self.metadata.get("line")
        if v not in (None, ""):
            try:
                return int(v)
            except ValueError:
                pass
        return _line_from_name(self.name)

    @property
    def file_number(self):
        v = self.metadata.get("file_number")
        try:
            return int(v)
        except (TypeError, ValueError):
            m = re.search(r"_(\d+)\.clraw$", self.name)
            return int(m.group(1)) if m else 1

    def scan_late_attachments(self):
        """Kept for older callers: attachments written between chunks are already read on open."""

    def calibration(self):
        """Hesai channel calibration [(elev_deg, az_deg)] * 32 from this file, or None."""
        d = self.attachments.get("lidar_calibration.csv")
        if not d:
            return None
        return calibration_rows(d.decode("utf-8", "replace")) or None

    # ------------------------------------------------------------------- reading
    def _file_ids(self, channels):
        ids = set()
        for c in channels:
            t = _CH_TOPIC.get(c)
            cid = self._m.topic_id(t) if t else None
            ids.add(cid if cid is not None else c)
        return ids

    def iter_chunks(self, channels=None, progress=None):
        """Yield one list per chunk: [(channel, log_ns, publish_ns, payload memoryview)].
        channel is CH_POINTS, CH_ANPP ... whatever number the file gave the topic."""
        want = self._file_ids(channels) if channels else None
        with open(self.path, "rb") as f:
            for ci, cp in enumerate(self._m.chunk_pos):
                self.chunks += 1
                if progress:
                    progress((cp[0] + cp[1]) / max(self.size, 1))
                data = self._m.read_chunk(f, ci)
                if data is None:
                    continue
                try:
                    out = [(self._as_ch.get(ch, ch), lt, pt, p) for ch, lt, pt, p in mcap.iter_messages(data, want)]
                except Exception:       # noqa: BLE001 - damaged records inside a good chunk
                    self._bad += 1
                    continue
                if out:
                    yield out

    def iter_messages(self, channels=None):
        for chunk in self.iter_chunks(channels):
            for m in chunk:
                yield m

    def events(self):
        ev = []
        for _ch, _lt, _pt, d in self.iter_messages({CH_EVENTS}):
            try:
                ev.append(json.loads(bytes(d).decode("utf-8")))
            except ValueError:
                pass
        return ev


# ----------------------------------------------------------------------------- one survey line
class Recording:
    """One survey line: one or more .clraw files read in name order."""

    def __init__(self, paths):
        if isinstance(paths, str):
            paths = [paths]
        self.paths = sorted(paths, key=lambda q: os.path.basename(q))
        self.files = [mcap.McapFile(p).scan() for p in self.paths]
        f0 = self.files[0]
        if f0.profile and f0.profile != "clraw":
            raise ClrawError("%s: MCAP profile %r is not clraw" % (self.paths[0], f0.profile))
        self.meta = dict(f0.metadata.get("clraw", {}))
        _check_format(self.meta, os.path.basename(self.paths[0]))
        self.hardware = self._json("hardware.json")
        self.session = self._json("session.json")
        cal = None
        for f in self.files:
            if "lidar_calibration.csv" in f.attachments:
                cal = f.attachments["lidar_calibration.csv"].decode("utf-8", "replace")
                break
        rows = calibration_rows(cal) if cal else []
        self.calib = np.array(rows, float) if rows else DEFAULT_CALIB.copy()
        self.calib_source = "file" if rows else "default table"
        self.line = self.meta.get("line") or os.path.basename(self.paths[0]).split("_")[0]
        self._events = None

    def _json(self, name):
        for f in self.files:
            if name in f.attachments:
                try:
                    return json.loads(f.attachments[name].decode("utf-8"))
                except ValueError:
                    return {}
        return {}

    @property
    def n_chunks(self):
        return sum(len(f.chunk_pos) for f in self.files)

    def lever_arm(self):
        v = self.hardware.get("ins_to_lidar_m") or [0.0, 0.0, 0.0]
        return [float(a) for a in v]

    def mount(self):
        m = self.hardware.get("lidar_mount_navaq_deg") or {}
        if isinstance(m, (list, tuple)):
            return [float(a) for a in m[:3]]
        return [float(m.get("roll", 0.0)), float(m.get("pitch", 0.0)), float(m.get("heading", 0.0))]

    def crs(self):
        cs = self.hardware.get("coordinate_system") or {}
        d = cs.get("definition") if isinstance(cs, dict) else None
        return d or self.meta.get("coordinate_system") or "EPSG:2953"

    # -- iteration -------------------------------------------------------
    def iter_chunks(self, topics):
        """Yield (file index, chunk index, decompressed records, wanted channel ids)."""
        for fi, f in enumerate(self.files):
            want = {f.topic_id(t) for t in topics} - {None}
            if not want:
                continue
            with open(f.path, "rb") as fh:
                for ci in range(len(f.chunk_pos)):
                    data = f.read_chunk(fh, ci)
                    if data is None:
                        continue
                    yield fi, ci, data, want

    def iter_payloads(self, topic):
        for _fi, _ci, data, want in self.iter_chunks([topic]):
            for _ch, _lt, _pt, p in mcap.iter_messages(data, want):
                yield p

    def chunk_list(self):
        return [(fi, ci) for fi, f in enumerate(self.files) for ci in range(len(f.chunk_pos))]

    def read_lidar_chunk(self, fh_cache, fi, ci):
        """Return an (N, 1080) uint8 array of the point packets in one chunk."""
        f = self.files[fi]
        cid = f.topic_id(TOPIC_POINTS)
        fh = fh_cache.get(fi)
        if fh is None:
            fh = fh_cache[fi] = open(f.path, "rb")
        data = f.read_chunk(fh, ci)
        if data is None or cid is None:
            return np.zeros((0, HESAI_LEN), np.uint8)
        bufs = [bytes(p) for _ch, _lt, _pt, p in mcap.iter_messages(data, {cid}) if len(p) == HESAI_LEN]
        if not bufs:
            return np.zeros((0, HESAI_LEN), np.uint8)
        return np.frombuffer(b"".join(bufs), np.uint8).reshape(-1, HESAI_LEN)

    def events(self):
        if self._events is not None:
            return self._events
        ev = []
        for p in self.iter_payloads(TOPIC_EVENTS):
            try:
                ev.append(json.loads(bytes(p).decode("utf-8")))
            except ValueError:
                pass
        self._events = ev
        return ev

    def recording_window(self):
        t0 = t1 = None
        for e in self.events():
            if e.get("event") == "clraw_start":
                t0 = float(e["t"])
            elif e.get("event") == "clraw_stop":
                t1 = float(e["t"])
        return t0, t1

    def marks(self):
        return [e for e in self.events() if e.get("event") == "mark"]

    def nav_states(self):
        """Every ANPP packet 20 (System State) -> (structured SYS_STATE array, t UTC s), or None."""
        return anpp.nav_states(self.iter_payloads(TOPIC_ANPP))


# ----------------------------------------------------------------------------- files -> lines
def _line_from_name(name):
    """Last resort only (the metadata line is the truth): <id>_L001_<date>_<time>_0001 or NNN_..._0001."""
    m = _FNAME_210.match(name)
    if m:
        m2 = _LINE_210.search(m.group("prefix"))
        if m2:
            return int(m2.group(1))
    m = re.match(r"(\d+)_", name)
    return int(m.group(1)) if m else 0


_meta_cache = {}


def file_info(path):
    """{"kind", "capture", "line", "project_id", "start" (UTC s of the first data, or None), "digits"} of a
    .clraw from its header (cached by size and time);
    from the file name when the header cannot be read."""
    try:
        st = os.stat(path)
        key = (os.path.abspath(path), st.st_size, st.st_mtime_ns)
    except OSError:
        key = None
    if key is not None and key in _meta_cache:
        return _meta_cache[key]
    name = os.path.basename(path)
    start = None
    try:
        mf = mcap.McapFile(path).scan()
        md = dict(mf.metadata.get("clraw", {}))
        if mf.chunk_pos:
            start = min(cp[5] for cp in mf.chunk_pos) * 1e-9
    except (OSError, ValueError):
        md = {}
    m = _FNAME_210.match(name)
    old = _FNAME.match(name)
    if md.get("capture"):
        capture = md["capture"]
    elif old:
        capture = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(path)))) or old.group(1)
    else:
        capture = m.group("prefix") if m else name
    try:
        line = int(md.get("line"))
    except (TypeError, ValueError):
        line = _line_from_name(name)
    kind = md.get("kind") or (NAVIGATION if m and "_NAV_" in m.group("prefix") else "line")
    info = {"kind": kind, "capture": capture, "line": line, "project_id": md.get("project_id", ""), "start": start,
            "digits": int(md.get("line_digits") or 3) if str(md.get("line_digits") or "3").isdigit() else 3}
    if key is not None:
        _meta_cache[key] = info
    return info


def is_navigation(path):
    """True for a mapper 2.10 navigation recording (kind "navigation"): the Certus stream, no Lidar."""
    return file_info(path)["kind"] == NAVIGATION


def is_sonar(path):
    """True for a marine sonar recording (kind "sonar", from SonarView via RemixPro): no Lidar, no Certus."""
    return file_info(path)["kind"] == SONAR


def sonar_topics(cf):
    """{device id: topic} of the sonars in an open ClrawFile."""
    out = {}
    for t in cf.channels.values():
        m = re.match(r"^/sonar/(\d+)/ping$", t)
        if m:
            out[int(m.group(1))] = t
    return dict(sorted(out.items()))


def split_navigation(paths):
    """(survey line files, navigation recordings) of a list of .clraw paths."""
    lines, nav = [], []
    for p in paths:
        k = file_info(p)["kind"]
        if k == SONAR:
            continue                    # neither a Lidar line nor a Certus navigation recording
        (nav if k == NAVIGATION else lines).append(p)
    return lines, nav


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


def group_lines(files, navigation=False, sonar=False):
    """Group .clraw files into survey lines {key: [ClrawFile...]} in name order.
    The key is capture id + line number so lines from different capture folders never merge.
    Navigation recordings (mapper 2.10) are left out unless navigation=True."""
    lines = {}
    for p in files:
        cf = ClrawFile(p)
        if cf.is_navigation and not navigation:
            continue
        if cf.metadata.get("kind") == SONAR and not sonar:
            continue
        key = "%s:%03d" % (cf.metadata.get("capture", os.path.dirname(p)), cf.line)
        lines.setdefault(key, []).append(cf)
    for k in lines:
        lines[k].sort(key=lambda c: (c.file_number, c.name))
    return dict(sorted(lines.items(), key=lambda kv: kv[1][0].name))


def group_recordings(paths, navigation=False, sonar=False):
    """Group .clraw paths into recordings (survey lines) read in name order.
    Returns {(folder, line): [paths...]}, line as zero-padded text ("001"). The line comes from each file's
    metadata (capture, line), so the old NNN_YYYYMMDD_HHMMSS_0001 and the 2.10 <line id>_0001 names both
    group. ``.partial`` files are ignored; navigation recordings too unless navigation=True. The groups come
    in recording order."""
    groups = {}
    for p in paths:
        b = os.path.basename(p)
        if b.endswith(".partial"):
            continue
        info = file_info(p)
        if info["kind"] == NAVIGATION and not navigation:
            continue
        if info["kind"] == SONAR and not sonar:
            continue
        folder = os.path.dirname(os.path.abspath(p))
        key = (folder, "%0*d" % (max(1, info["digits"]), info["line"]) if info["kind"] != NAVIGATION
               else info["capture"])
        groups.setdefault(key, []).append(p)
    for k in groups:
        groups[k].sort(key=lambda q: os.path.basename(q))
    # in recording order (the first data of each line), so lines from old and 2.10 folders interleave in time
    start = lambda ps: min([file_info(q)["start"] for q in ps if file_info(q)["start"] is not None] or [float("inf")])  # noqa: E731
    return dict(sorted(groups.items(), key=lambda kv: (start(kv[1]), kv[0])))
