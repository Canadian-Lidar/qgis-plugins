"""The mapper's .clraw recordings (MCAP container, profile 'clraw'), for every DCS plugin.

Byte layouts follow the project's ".clraw & Sensor Reference for Plugin
Developers" (mapper 2.9.0, format version 1). Files are read front to back,
so unfinished files (no summary) still open, and a damaged chunk is skipped
and counted, never fatal.

  ClrawFile     one file: metadata, attachments, events, message chunks
  Recording     one survey line (one or more files): calibration, hardware,
                point packet chunks, ANPP navigation states
  find_clraw, group_lines, group_recordings   files and folders -> lines
Packet decoding is in dcs_core.hesai and dcs_core.anpp.
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
           "group_recordings", "DEFAULT_CAL", "DEFAULT_CALIB", "HESAI_LEN", "decode_hesai", "hesai_packet_times",
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

_FNAME = re.compile(r"^(\d+)_(\d{8})_(\d{6})_(\d{4})\.clraw$")


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


def group_recordings(paths):
    """Group .clraw paths into recordings (survey lines) read in name order.
    Returns {(folder, line): [paths...]}. ``.partial`` files are ignored."""
    groups = {}
    for p in paths:
        b = os.path.basename(p)
        if b.endswith(".partial"):
            continue
        m = _FNAME.match(b)
        key = (os.path.dirname(os.path.abspath(p)), m.group(1) if m else b)
        groups.setdefault(key, []).append(p)
    for k in groups:
        groups[k].sort(key=lambda q: os.path.basename(q))
    return groups
