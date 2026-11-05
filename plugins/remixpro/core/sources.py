"""What can be converted in a folder or a list of files.

RemixPro accepts:
  - a mapper project folder (2.10: <Project>/lines/<line id>/, <Project>/nav/), or any folder inside it;
  - a USB copy (Mapper captures/<Project>/...; no .pcap by default; hidden .mapper-mirror.json and
    *.partial files are ignored; a line still being copied is marked incomplete);
  - a line or project download (<Project>_L003.zip or <Project>.zip, unzipped);
  - captures from before 2.10 (YYYYMMDD_HHMMSS_<name>/ or grouped into lines/);
  - single .clraw or .anpp files.

Each recording found becomes one Item:
  line        a survey line (.clraw with Lidar data): BeamworX, Inertial Explorer, Qinertia
  navigation  a navigation recording (<Project>_NAV_..., kind navigation, no Lidar): Inertial Explorer, Qinertia
  log         a Certus log (.anpp) with no .clraw beside it: Inertial Explorer, Qinertia

Lines are grouped from each file's metadata by DCS Core (clraw.group_recordings), never from file names,
and come in recording order. For Inertial Explorer and Qinertia the Certus log (.anpp) is used when it is
there, as the mapper did; otherwise the /ins/anpp channel of the .clraw files.
"""
import json
import os
import re

from dcs_core import clraw as dclraw

BEAMWORX, IE, QINERTIA, CLRAW = "beamworx", "inertial_explorer", "qinertia", "clraw"
OUTPUTS = (BEAMWORX, IE, QINERTIA, CLRAW)
OUTPUT_LABELS = {BEAMWORX: "BeamworX", IE: "Inertial Explorer", QINERTIA: "Qinertia", CLRAW: ".clraw (DCS)"}
LINE, NAVIGATION, LOG, SONAR = "line", "navigation", "log", "sonar"
KIND_LABELS = {LINE: "Survey line", NAVIGATION: "Navigation", LOG: "Certus log", SONAR: "Sonar recording"}
OFFSETS_FILE = "sonar_offsets.json"     # {"<device id>" | "<nickname>" | "port" | "starboard": [F, S, D] m}

# folders that never hold recordings: DCS processing projects (and RemixPro's own output), the mapper's old
# conversions, the Lidar safety copies
SKIP_DIRS = {"processing", "processed", "beamworx", "lidar", "__pycache__", "remixpro"}
_EPH_NAMES = re.compile(r"(^ephemeris_cache\.anpp$|_ephemeris\.anpp$)")
_SEG = re.compile(r"^(?P<prefix>.+?)_(?P<number>\d{4})\.anpp$")       # ins_0001, <prefix>_0001, <line>_ins_0001


class Item:
    """One recording that can be converted."""

    def __init__(self, kind, name, folder):
        self.kind = kind
        self.name = name                # output base name: line id, navigation prefix, capture folder
        self.folder = folder            # the recording's own folder (line folder, nav/, ...)
        self.clraw = []
        self.anpp = []
        self.ephemeris = None
        self.hardware = {}
        self.session = {}
        self.metadata = {}
        self.project = ""
        self.line = None
        self.start = None               # UTC s of the first data
        self.complete = True
        self.notes = []                 # plain-language remarks shown to the user
        self.lever_primary = None
        self.lever_secondary = None
        self.lever_source = ""
        self.config_source = ""         # BeamworX survey configuration: "recorded" / "built from hardware.json"
        # marine sonar recordings (SonarView .svlz / .svlog, alone or from a TidalCal project)
        self.sonar_file = None
        self.rinex, self.llh, self.ppk = [], [], []
        self.offsets, self.offsets_source = {}, ""
        self.extra_hw, self.extra_attachments = None, []

    @property
    def key(self):
        return "%s|%s|%s" % (self.kind, os.path.normcase(os.path.abspath(self.folder)), self.name)

    @property
    def outputs(self):
        """What can be made from it."""
        out = []
        if self.kind == SONAR:
            return [CLRAW] if self.sonar_file else []
        if self.kind == LINE and self.clraw and self.config_source:
            out.append(BEAMWORX)
        if self.anpp or self.clraw:
            out += [IE, QINERTIA]
        return out

    @property
    def inputs(self):
        """The files the outputs are made from (for the up-to-date check and the size shown)."""
        if self.kind == SONAR:
            return [self.sonar_file] + self.rinex + self.llh + self.ppk
        return list(self.clraw) + list(self.anpp) + ([self.ephemeris] if self.ephemeris else [])

    @property
    def size(self):
        if self.kind == SONAR:
            return _size(self.sonar_file)
        return sum(_size(p) for p in self.clraw + self.anpp)

    def certus_source(self):
        """("anpp", paths) or ("clraw", paths): what Inertial Explorer / Qinertia are made from."""
        return ("anpp", list(self.anpp)) if self.anpp else ("clraw", list(self.clraw))

    def describe(self):
        return {"kind": self.kind, "name": self.name, "folder": self.folder, "clraw": [os.path.basename(p) for p in
                self.clraw], "anpp": [os.path.basename(p) for p in self.anpp],
                "ephemeris": os.path.basename(self.ephemeris) if self.ephemeris else None, "project": self.project,
                "line": self.line, "outputs": self.outputs, "complete": self.complete, "notes": self.notes,
                "lever_primary": self.lever_primary, "lever_source": self.lever_source}


def _size(p):
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _hidden(name):
    return name.startswith(".") or name.endswith(".partial")


def collect(paths, sonar=None):
    """(.clraw files, .anpp files) under the given files and folders; SonarView recordings go into the sonar list. Hidden files (.mapper-mirror.json),
    unfinished files (*.partial) and folders that never hold recordings are skipped."""
    clr, anp = set(), set()

    def take(p):
        n = os.path.basename(p)
        if _hidden(n):
            return
        low = n.lower()
        if low.endswith(".clraw"):
            clr.add(os.path.abspath(p))
        elif low.endswith(".anpp"):
            anp.add(os.path.abspath(p))
        elif sonar is not None and low.endswith((".svlz", ".svlog")):
            sonar.add(os.path.abspath(p))

    for p in paths:
        if os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d.lower() not in SKIP_DIRS)
                for fn in files:
                    take(os.path.join(root, fn))
        elif os.path.isfile(p):
            take(p)
    return sorted(clr), sorted(anp)


def _capture_dir(folder):
    """The capture (line) folder of a recording's folder: the parent of clraw/ or ins/."""
    return os.path.dirname(folder) if os.path.basename(folder).lower() in ("clraw", "ins") else folder


def _project_of(folder):
    """(project id, name) from the nearest project.json above folder, or ("", "")."""
    d = folder
    for _ in range(5):
        pj = os.path.join(d, "project.json")
        if os.path.isfile(pj):
            info = _read_json(pj)
            return str(info.get("id") or os.path.basename(d)), str(info.get("name") or info.get("id") or "")
        up = os.path.dirname(d)
        if up == d:
            break
        d = up
    return "", ""


def _line_logs(cdir, prefix):
    """The Certus log (.anpp) files and ephemeris of a capture folder: ins/ins_0001.anpp (on the unit and in
    USB copies), ins/<line id>_ins_0001.anpp (downloads) and ins_0001.anpp at the top (mapper 2.0)."""
    logs, eph = [], None
    for d in (cdir, os.path.join(cdir, "ins")):
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            if _hidden(n) or not n.endswith(".anpp"):
                continue
            if _EPH_NAMES.search(n):
                if eph is None or d.endswith("ins"):
                    eph = os.path.join(d, n)
                continue
            if re.match(r"^ins_\d{4}\.anpp$", n) or (prefix and re.match(r"^%s_ins_\d{4}\.anpp$" % re.escape(prefix), n)):
                logs.append(os.path.join(d, n))
    return logs, eph


def _nav_logs(folder, prefix):
    logs = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        names = []
    for n in names:
        if not _hidden(n) and re.match(r"^%s_\d{4}\.anpp$" % re.escape(prefix), n):
            logs.append(os.path.join(folder, n))
    eph = os.path.join(folder, prefix + "_ephemeris.anpp")
    return logs, eph if os.path.isfile(eph) else None


def _levers(item, settings=None):
    hw = item.hardware or {}
    st = settings if settings is not None else (item.session.get("settings") or {})
    p, s = hw.get("ins_to_primary_antenna_m"), hw.get("ins_to_secondary_antenna_m")
    if p:
        item.lever_primary, item.lever_source = [float(v) for v in p], "hardware.json in the .clraw"
    elif st.get("ins_to_primary_antenna_m"):
        item.lever_primary, item.lever_source = [float(v) for v in st["ins_to_primary_antenna_m"]], "session.json"
    if s:
        item.lever_secondary = [float(v) for v in s]
    elif st.get("ins_to_secondary_antenna_m"):
        item.lever_secondary = [float(v) for v in st["ins_to_secondary_antenna_m"]]
    if item.lever_primary is None:
        item.notes.append("No antenna lever arm recorded: the Inertial Explorer and Qinertia notes say so and the "
                          "IMR header carries none.")


def _completeness(item, cdir):
    """A 2.10 line still being copied to a USB drive: complete once continuity.json and manifest.sha256 exist."""
    layout = str((item.session or {}).get("layout") or "")
    if layout and layout >= "2.10" and os.path.isfile(os.path.join(cdir, "session.json")):
        missing = [n for n in ("continuity.json", "manifest.sha256") if not os.path.isfile(os.path.join(cdir, n))]
        if missing:
            item.complete = False
            item.notes.append("Incomplete: %s not there yet (the line may still be recording or being copied)."
                              % " and ".join(missing))
    try:
        partial = [n for n in os.listdir(os.path.join(cdir, "clraw") if os.path.isdir(os.path.join(cdir, "clraw"))
                                         else cdir) if n.endswith(".partial")]
    except OSError:
        partial = []
    if partial:
        item.complete = False
        item.notes.append("A file is still being written (%s): only what is finished is converted." % partial[0])


def scan(paths):
    """Items found under the given files and folders, in recording order."""
    svl = set()
    clr, anp = collect(paths, svl)
    items = _sonar_items(paths, svl)
    used_anpp = set()
    groups = dclraw.group_recordings(clr, navigation=True)
    for (folder, _key), files in groups.items():
        info = dclraw.file_info(files[0])
        try:
            cf = dclraw.ClrawFile(files[0])
        except (dclraw.ClrawError, OSError, ValueError) as e:
            it = Item(LINE, os.path.basename(files[0])[:-6], folder)
            it.notes.append("Cannot be read: %s" % e)
            it.clraw = []
            items.append(it)
            continue
        md = cf.metadata
        if cf.is_navigation:
            prefix = md.get("capture") or info["capture"]
            it = Item(NAVIGATION, prefix, folder)
            it.anpp, it.ephemeris = _nav_logs(folder, prefix)
            it.notes.append("Navigation recording: the whole Certus stream while the project was open (no Lidar).")
        else:
            cdir = _capture_dir(folder)
            sess = _read_json(os.path.join(cdir, "session.json")) or cf.session
            name = str(sess.get("file_prefix") or md.get("capture") or os.path.basename(cdir))
            it = Item(LINE, name, cdir)
            it.session = sess
            it.anpp, it.ephemeris = _line_logs(cdir, sess.get("file_prefix") or md.get("capture"))
            try:
                it.line = int(md.get("line"))
            except (TypeError, ValueError):
                it.line = info["line"]
            if cf.attachments.get("beamworx_survey_config.xml"):
                it.config_source = "recorded"
            elif cf.hardware:
                it.config_source = "built from hardware.json"
                it.notes.append("No BeamworX survey configuration in the .clraw: it is built from hardware.json.")
            else:
                it.notes.append("No BeamworX survey configuration or hardware.json in the .clraw: no BeamworX files.")
            if dclraw.TOPIC_POINTS not in cf.channels.values():
                it.config_source = ""
                it.notes.append("No Lidar channel in this .clraw: no BeamworX files.")
            _completeness(it, cdir)
        it.clraw = list(files)
        it.metadata = md
        it.hardware = cf.hardware
        if not it.session:
            it.session = cf.session
        it.project = md.get("project") or md.get("project_id") or _project_of(it.folder)[1]
        it.start = info["start"]
        _levers(it)
        used_anpp.update(it.anpp)
        if it.ephemeris:
            used_anpp.add(it.ephemeris)
        items.append(it)
    # Certus logs with no .clraw beside them (captures before 2.8, single .anpp files, a nav/ folder whose
    # .clraw was not copied)
    logs = {}
    for p in anp:
        if p in used_anpp or _EPH_NAMES.search(os.path.basename(p)):
            continue
        folder = os.path.dirname(p)
        m = _SEG.match(os.path.basename(p))
        prefix = m.group("prefix") if m else os.path.basename(p)[:-5]
        logs.setdefault((folder, prefix), []).append(p)
    for (folder, prefix), files in logs.items():
        cdir = _capture_dir(folder)
        if prefix == "ins" or prefix.endswith("_ins"):
            sess = _read_json(os.path.join(cdir, "session.json"))
            name = str(sess.get("file_prefix") or (prefix[:-4] if prefix.endswith("_ins") else os.path.basename(cdir)))
            it = Item(LOG, name, cdir)
            it.session = sess
            _, it.ephemeris = _line_logs(cdir, prefix[:-4] if prefix.endswith("_ins") else None)
        else:
            it = Item(LOG, prefix, folder)
            eph = os.path.join(folder, prefix + "_ephemeris.anpp")
            it.ephemeris = eph if os.path.isfile(eph) else None
            if "_NAV_" in prefix:
                it.kind = NAVIGATION
                it.notes.append("Navigation recording (Certus log only; its .clraw is not here).")
        it.anpp = sorted(files, key=os.path.basename)
        it.project = _project_of(it.folder)[1]
        _levers(it)
        if it.ephemeris is None:
            it.notes.append("No ephemeris file beside the log: the RINEX navigation file has only the ephemerides "
                            "the Certus sent during the recording.")
        items.append(it)
    for it in items:
        if it.start is None:
            it.start = _log_start(it)
    items.sort(key=lambda i: (i.start if i.start is not None else float("inf"), i.name))
    return items


def _log_start(item):
    st = item.session.get("started_unix") if item.session else None
    if st:
        return float(st)
    paths = item.anpp or item.clraw
    try:
        return os.path.getmtime(paths[0]) if paths else None
    except OSError:
        return None


def default_output_root(paths):
    """Where outputs go when no DCS project is open: <mapper project>/Processing/RemixPro/ when the inputs are
    inside a mapper project, else a RemixPro folder beside the inputs. Never inside a line's own folders."""
    return _default_root(paths)


def _default_root(paths):
    paths = [os.path.abspath(p) for p in paths if p]
    if not paths:
        return ""
    first = paths[0] if os.path.isdir(paths[0]) else os.path.dirname(paths[0])
    d = first
    for _ in range(5):
        if os.path.isfile(os.path.join(d, "project.json")) and os.path.isdir(os.path.join(d, "lines")):
            return os.path.join(d, "Processing", "RemixPro")
        up = os.path.dirname(d)
        if up == d:
            break
        d = up
    try:
        common = os.path.commonpath([p if os.path.isdir(p) else os.path.dirname(p) for p in paths])
    except ValueError:          # different drives
        common = first
    base = os.path.basename(common).lower()
    if base in ("clraw", "ins", "nav", "lines"):
        common = os.path.dirname(common)
    return os.path.join(common, "RemixPro")


# ----------------------------------------------------------------------------- marine sonar recordings
_RINEX = re.compile(r"(\.\d\d[oOnNgGlLpP]|\.(obs|nav|rnx))$", re.I)


def _offsets_near(folder):
    """sonar_offsets.json in the recording's folder or up to three folders above it: ({...}, path) or ({}, "")."""
    d = folder
    for _ in range(4):
        p = os.path.join(d, OFFSETS_FILE)
        if os.path.isfile(p):
            data = _read_json(p)
            return {str(k): v for k, v in data.items() if isinstance(v, (list, tuple)) and len(v) == 3}, p
        up = os.path.dirname(d)
        if up == d:
            break
        d = up
    return {}, ""


def _side_gnss(folder):
    """GNSS files beside a recording: RINEX, .LLH, .pos (used only where their times overlap the recording)."""
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        names = []
    rinex = [os.path.join(folder, n) for n in names if _RINEX.search(n) and not _hidden(n)]
    llh = [os.path.join(folder, n) for n in names if n.lower().endswith(".llh") and not _hidden(n)]
    ppk = [os.path.join(folder, n) for n in names if n.lower().endswith(".pos") and not _hidden(n)]
    return rinex, llh, ppk


def _sonar_items(paths, svl):
    from . import tidalcal
    items, used = [], set()
    for pdir in tidalcal.find_projects(paths):
        try:
            proj = tidalcal.Project(pdir)
        except ValueError:
            continue
        for rec in proj.recordings():
            used.add(rec)
            it = Item(SONAR, re.sub(r"\.(svlz|svlog)$", "", os.path.basename(rec), flags=re.I), os.path.dirname(rec))
            it.sonar_file = rec
            it.project = proj.name
            it.rinex, it.llh, it.ppk = proj.gnss_for(rec)
            off, src = _offsets_near(pdir)
            it.offsets, it.offsets_source = (off, src) if off else (proj.offsets(), "TidalCal vessel table")
            it.extra_hw = proj.calibration()
            it.extra_attachments = proj.attachments()
            it.notes.append("From TidalCal project %s." % proj.name)
            items.append(it)
    for rec in sorted(svl):
        if rec in used:
            continue
        folder = os.path.dirname(rec)
        it = Item(SONAR, re.sub(r"\.(svlz|svlog)$", "", os.path.basename(rec), flags=re.I), folder)
        it.sonar_file = rec
        it.rinex, it.llh, it.ppk = _side_gnss(folder)
        it.offsets, it.offsets_source = _offsets_near(folder)
        items.append(it)
    for it in items:
        it.start = _mtime(it.sonar_file)
        if not it.offsets:
            it.notes.append("No sonar offsets entered (%s): each sonar's acoustic centre is recorded as not "
                            "entered." % OFFSETS_FILE)
        else:
            it.notes.append("Sonar offsets from %s." % (os.path.basename(it.offsets_source) or it.offsets_source))
        if not (it.rinex or it.llh or it.ppk):
            it.notes.append("No GNSS files beside it: position comes from the vehicle data SonarView recorded.")
    return items


def _mtime(p):
    try:
        return os.path.getmtime(p)
    except OSError:
        return None
