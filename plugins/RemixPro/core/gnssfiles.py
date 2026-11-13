"""GNSS receiver files for a sonar recording: RINEX observations / navigation, the receiver's solution log (.LLH)
and PPK solutions (.pos), found beside the recording, in the folders near it (an Emlid download unzipped into
Rover/ and Base/, a TidalCal project's 02_GNSS_Rover_Base, ...) and inside Emlid's download zips.

Every file is a GnssFile: its role (rover or base), its type (obs, nav, llh, pos) and where it is (a path, or a
member of a zip). Base or rover comes from the folder and file names ("base" anywhere in them: Base/,
Reach_Base_20261007123356.zip); everything else is the rover. Only the rover's solution logs describe the boat:
the base's own .LLH is its fixed position and is never used as the vehicle's.

Emlid writes the receiver's name into the file names inside its zips, and that name can hold a line break
("Reach\\n_raw_....26O"): names are cleaned (control characters removed) when the files are extracted.
"""
import os
import re
import zipfile

RINEX = re.compile(r"(\.\d\d[oOnNgGlLpPbB]|\.(obs|nav|rnx))$", re.I)
SKIP = {"processing", "remixpro", "clraw", "beamworx", "inertial_explorer", "qinertia", "__pycache__",
        "01_raw_sonar", "05_processed_data", "06_exports", "07_reports", "sv exports"}
MAX_DEPTH = 3


def clean_name(name):
    """A member name without control characters (Emlid's device name can hold a line break)."""
    return re.sub(r"[\x00-\x1f\x7f]", "", os.path.basename(name.replace("\\", "/")))


def file_type(name):
    low = clean_name(name).lower()
    if low.endswith(".llh"):
        return "llh"
    if low.endswith(".pos"):
        return "pos"
    if RINEX.search(low):
        ext = os.path.splitext(low)[1]
        if ext.endswith("o") or ext in (".obs", ".rnx"):
            return "obs"
        return "nav"
    return None


def is_base(*names):
    """'base' as a word part of any folder / zip / file name (Base, Reach_Base_..., base_station), but not the
    corrections a rover received (Reach_base_....RTCM3 is never used anyway)."""
    for n in names:
        for part in re.split(r"[\\/]", n or ""):
            if re.search(r"(^|[^a-z])base([^a-z]|$)", clean_name(part).lower()):
                return True
    return False


class GnssFile:
    def __init__(self, path, member=None, role="rover"):
        self.path = path                # the file, or the zip holding it
        self.member = member            # name inside the zip, or None
        self.name = clean_name(member or path)
        self.type = file_type(self.name)
        self.role = role

    @property
    def label(self):
        return self.name + (" (in %s)" % os.path.basename(self.path) if self.member else "")

    @property
    def size(self):
        try:
            if self.member:
                with zipfile.ZipFile(self.path) as z:
                    return z.getinfo(self.member).file_size
            return os.path.getsize(self.path)
        except (OSError, KeyError, zipfile.BadZipFile):
            return 0

    def key(self):
        return (os.path.normcase(os.path.abspath(self.path)), self.member or "")

    def extract(self, folder):
        """A real file for it (zip members are written into folder, with the cleaned name)."""
        if not self.member:
            return self.path
        os.makedirs(folder, exist_ok=True)
        out = os.path.join(folder, self.role + "_" + self.name)
        with zipfile.ZipFile(self.path) as z, z.open(self.member) as src, open(out, "wb") as dst:
            while True:
                b = src.read(1 << 20)
                if not b:
                    break
                dst.write(b)
        return out


def _from_zip(path):
    out = []
    try:
        with zipfile.ZipFile(path) as z:
            for m in z.namelist():
                if m.endswith("/") or file_type(m) is None:
                    continue
                role = "base" if is_base(path, m) else "rover"
                out.append(GnssFile(path, m, role))
    except (OSError, zipfile.BadZipFile):
        pass
    return out


def search_roots(recording):
    """Where to look for a recording's GNSS files: its own folder, and the folder above it (where a survey's
    Rover/, Base/ and GNSS/ folders usually sit beside the sonar folder)."""
    folder = os.path.dirname(os.path.abspath(recording))
    up = os.path.dirname(folder)
    return [folder] + ([up] if up and up != folder else [])


def find(recording, extra_roots=()):
    """GnssFiles near a recording (see search_roots), deduplicated. Files that do not cover the recording's
    time are dropped later, when the recording's span is known."""
    seen, out = set(), []
    folder = os.path.dirname(os.path.abspath(recording))

    def add(g):
        if g.type and g.key() not in seen:
            seen.add(g.key())
            out.append(g)

    roots = search_roots(recording) + [r for r in extra_roots if r]
    for i, root in enumerate(roots):
        depth_max = 0 if (i == 0 and root == folder) else MAX_DEPTH
        for dirpath, dirs, files in os.walk(root):
            depth = os.path.relpath(dirpath, root).count(os.sep) + (0 if dirpath == root else 1)
            dirs[:] = sorted(d for d in dirs if d.lower() not in SKIP and not d.startswith(".")
                             and os.path.normcase(os.path.join(dirpath, d)) != os.path.normcase(folder)
                             and depth < depth_max)
            for n in sorted(files):
                p = os.path.join(dirpath, n)
                if n.startswith(".") or n.endswith(".partial"):
                    continue
                if n.lower().endswith(".zip"):
                    for g in _from_zip(p):
                        add(g)
                elif file_type(n):
                    add(GnssFile(p, None, "base" if is_base(os.path.relpath(p, root)) else "rover"))
    # an unzipped download and its zip: keep the unzipped files only
    plain = {(g.role, g.name) for g in out if not g.member}
    return [g for g in out if not (g.member and (g.role, g.name) in plain)]


def rinex_span(gf):
    """(first, last) observation time (UTC s) from a RINEX observation header, or None."""
    import calendar
    head = b""
    try:
        if gf.member:
            with zipfile.ZipFile(gf.path) as z, z.open(gf.member) as f:
                head = f.read(8192)
        else:
            with open(gf.path, "rb") as f:
                head = f.read(8192)
    except (OSError, KeyError, zipfile.BadZipFile):
        return None
    t = {}
    for line in head.split(b"\n"):
        lab = line[60:].strip()
        if lab in (b"TIME OF FIRST OBS", b"TIME OF LAST OBS"):
            try:
                v = line[:43].split()
                s = calendar.timegm((int(v[0]), int(v[1]), int(v[2]), int(v[3]), int(v[4]), 0)) + float(v[5])
                t[lab] = s - 18 if b"GPS" in line[43:60] else s
            except (ValueError, IndexError):
                pass
        if lab == b"END OF HEADER":
            break
    if b"TIME OF FIRST OBS" in t:
        return t[b"TIME OF FIRST OBS"], t.get(b"TIME OF LAST OBS")
    return None


def log_span(gf):
    """(first, last) UTC s of an .LLH / .pos solution log (first and last dated line), or None."""
    import datetime
    rx = re.compile(rb"^(\d{4}/\d\d/\d\d) (\d\d:\d\d:\d\d(\.\d+)?)")

    def t_of(line):
        m = rx.match(line.strip())
        if not m:
            return None
        return datetime.datetime.strptime((m.group(1) + b" " + m.group(2)).decode()[:23],
                                          "%Y/%m/%d %H:%M:%S.%f" if m.group(3) else "%Y/%m/%d %H:%M:%S").replace(
            tzinfo=datetime.timezone.utc).timestamp()
    try:
        if gf.member:
            with zipfile.ZipFile(gf.path) as z:
                data = z.read(gf.member)
        else:
            with open(gf.path, "rb") as f:
                data = f.read()
    except (OSError, KeyError, zipfile.BadZipFile):
        return None
    lines = data.split(b"\n")
    first = next((t for t in (t_of(x) for x in lines[:200]) if t is not None), None)
    last = next((t for t in (t_of(x) for x in reversed(lines[-20000:])) if t is not None), None)
    if first is None:
        return None
    # .LLH times are GPS time (Emlid's default); .pos files say which in their header ("GPST" / "UTC")
    if gf.type == "llh" or (gf.type == "pos" and b"GPST" in data[:4000]):
        first, last = first - 18, (last - 18 if last is not None else None)
    return first, last


def covering(files, t0, t1, margin=600.0):
    """The files whose time covers [t0, t1] at least in part (navigation RINEX always: it has no span)."""
    out = []
    for g in files:
        if g.type == "nav":
            out.append(g)
            continue
        sp = rinex_span(g) if g.type == "obs" else log_span(g)
        if sp is None:
            if g.type == "obs":
                out.append(g)           # no first / last time in the header: the cut to the recording decides
            continue
        a, b = sp
        if b is None or (a <= t1 + margin and b >= t0 - margin):
            out.append(g)
    return out
