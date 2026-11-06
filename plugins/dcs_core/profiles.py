"""System profiles: the measured offsets and mounting of one mapper system, saved once and used again.

A profile holds what is true of the hardware rather than of a job - antenna offsets, the rear axle, the
Lidar lever arm, mounting angles and latency, the IMU model and the vehicle - so after checking them once
(Check antennas in PosPro, a patch test in AlignPro) the next job from the same system starts right.

Kept on this computer in the QGIS settings folder (``<QGIS profile>/canadian_lidar/system_profiles/``, one
JSON file per profile); profiles can be exported to a file and imported on another computer.

A profile records the system it was made for (the mapper unit's serial and name from session.json, the
Certus serial from hardware.json); ``match()`` finds the profile for a job's recordings so the plugins can
offer it. Every section is optional: a plugin saves only the sections it owns and leaves the others as they
are (PosPro: antennas, rear axle, IMU, vehicle; AlignPro: Lidar).

    profile = {
      "schema": "dcs.system_profile/1", "name": "Van 1 (NC100XTLM0006)",
      "system": {"unit_serial": "NC100XTLM0006", "unit_name": "mapper-1", "certus_serial": "0012..."},
      "created": iso, "updated": iso, "notes": "",
      "sections": {
        "antennas": {"gnss1_m": [F,S,D], "gnss2_m": [F,S,D], "note": "checked: as installed", "by": "PosPro 2.0.2",
                     "updated": iso},
        "rear_axle": {"axle_m": [F,S,D], ...},
        "imu":      {"preset": "certus_evo", ...},
        "vehicle":  {"type": "van", "length": 5.2, "width": 2.0, ...},
        "lidar":    {"lever_m": [F,S,D], "mount_deg": [roll, pitch, heading], "latency_ms": 0.0,
                     "convention": "navaq_hesai", "source": "calibration" | "entered", ...}}}

Frames and units as in the pipeline contract: vehicle frame X forward, Y starboard, Z down from the Certus
reference point, metres; mounting angles in degrees, convention navaq_hesai.
"""
import contextlib
import datetime as _dt
import json
import os
import re

from . import files, locks

SCHEMA = "dcs.system_profile/1"
SECTIONS = ("antennas", "rear_axle", "imu", "vehicle", "lidar")
SECTION_TITLES = {"antennas": "GNSS antenna offsets", "rear_axle": "Rear axle", "imu": "IMU model",
                  "vehicle": "Vehicle", "lidar": "Lidar lever arm, mounting and latency"}
# the value keys compared and shown, per section: (key, label, unit)
FIELDS = {
    "antennas": (("gnss1_m", "GPS1 primary", "m"), ("gnss2_m", "GPS2 secondary", "m")),
    "rear_axle": (("axle_m", "Rear axle centre", "m"),),
    "imu": (("preset", "IMU model", ""),),
    "vehicle": (("type", "Vehicle type", ""), ("length", "Length", "m"), ("width", "Width", "m")),
    "lidar": (("lever_m", "Lidar lever arm", "m"), ("mount_deg", "Mounting roll, pitch, heading", "°"),
              ("latency_ms", "Latency", "ms")),
}
TOL = {"m": 0.0005, "°": 0.00005, "ms": 0.005}      # below these two values are the same (display precision)

_folder_override = None


def _now():
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def folder():
    """Where the profiles are kept (created on first use)."""
    if _folder_override:
        d = _folder_override
    else:
        base = None
        try:
            from qgis.core import QgsApplication
            base = QgsApplication.qgisSettingsDirPath()
        except Exception:       # noqa: BLE001  (outside QGIS: tests, tools)
            pass
        if not base:
            base = os.path.join(os.path.expanduser("~"), ".canadian_lidar")
        d = os.path.join(base, "canadian_lidar", "system_profiles")
    os.makedirs(d, exist_ok=True)
    return d


def set_folder(path):
    """Use another folder (tests)."""
    global _folder_override
    _folder_override = path


def _fname(name):
    """A file name for a new profile called name: never one another profile's file already has (names that
    differ only in case or in characters a file name cannot hold get " (2)", " (3)" ...)."""
    s = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .") or "profile"
    s = s[:80]
    d = folder()
    taken = {f.lower() for f in os.listdir(d)}
    cand, n = s + ".json", 2
    while cand.lower() in taken:
        cand = "%s (%d).json" % (s, n)
        n += 1
    return os.path.join(d, cand)


@contextlib.contextmanager
def _locked():
    """Profiles are written by PosPro and AlignPro (and other QGIS windows): one writer at a time."""
    lk = locks.Lock(folder(), "system profiles", "DCS Core", name=".profiles.lock", wait=10.0)
    tl = locks.thread_lock(lk.path)
    if not tl.acquire(timeout=10.0):
        raise locks.Locked({"stage": "system profiles", "plugin": "another task in this QGIS"})
    try:
        with lk:
            yield
    finally:
        tl.release()


def new(name, system=None, notes=""):
    t = _now()
    return {"schema": SCHEMA, "name": name, "system": dict(system or {}), "created": t, "updated": t,
            "notes": notes, "sections": {}}


def _valid(p):
    return isinstance(p, dict) and str(p.get("schema", "")).startswith("dcs.system_profile/") and \
        isinstance(p.get("sections"), dict) and p.get("name")


def list_profiles():
    """All saved profiles, by name."""
    out = []
    for f in sorted(os.listdir(folder())):
        if not f.lower().endswith(".json") or f.startswith("."):
            continue
        try:
            with open(os.path.join(folder(), f), "r", encoding="utf-8") as fh:
                p = json.load(fh)
        except (OSError, ValueError):
            continue
        if _valid(p):
            p["_path"] = os.path.join(folder(), f)
            out.append(p)
    out.sort(key=lambda p: p["name"].lower())
    return out


def load(name):
    for p in list_profiles():
        if p["name"] == name:
            return p
    return None


def save(profile):
    """Write a profile (atomic). Returns its path."""
    p = {k: v for k, v in profile.items() if not k.startswith("_")}
    if not _valid(p):
        raise ValueError("not a system profile")
    p["updated"] = _now()
    with _locked():
        old = load(p["name"])
        if old and old.get("_path"):
            path = old["_path"]                 # the profile of that name keeps its file
        elif profile.get("_path") and os.path.dirname(profile["_path"]) == folder() and \
                os.path.exists(profile["_path"]) and (_read(profile["_path"]) or {}).get("name") == p["name"]:
            path = profile["_path"]
        else:
            path = _fname(p["name"])
        files.atomic_write_json(path, p, indent=2)
    profile["_path"] = path
    profile["updated"] = p["updated"]
    return path


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def save_sections(name, sections, by="", system=None, notes=None):
    """Put sections (name -> values) into the profile called name, creating it when there is none, and save it.
    The file is re-read under the profiles lock and only these sections are replaced, so PosPro and AlignPro
    saving at the same time both keep their sections. system: the recording system's identity, recorded when the
    profile has none yet (missing keys are added). Returns the saved profile."""
    with _locked():
        p = load(name) or new(name, system, notes or "")
        if system:
            cur = p.setdefault("system", {})
            for k, v in dict(system).items():
                if v and not cur.get(k):
                    cur[k] = v
        if notes is not None:
            p["notes"] = notes
        update_sections(p, sections, by)
        save(p)
    return p


def update_sections(profile, sections, by=""):
    """Put sections (name -> values) into a profile, stamped with who and when; other sections are kept."""
    t = _now()
    for k, v in sections.items():
        if k not in SECTIONS or v is None:
            continue
        d = dict(v)
        d["by"] = by or d.get("by", "")
        d["updated"] = t
        profile["sections"][k] = d
    return profile


def delete(name):
    with _locked():
        p = load(name)
        if p and p.get("_path"):
            os.remove(p["_path"])
            return True
    return False


def export(name, path):
    p = load(name)
    if p is None:
        raise ValueError("no profile named %s" % name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({k: v for k, v in p.items() if not k.startswith("_")}, fh, indent=2)
    return path


def import_file(path, replace=False):
    """Add a profile from an exported file. A profile with the same name is kept unless replace; the new one is
    then named "<name> (2)". Returns the saved profile."""
    with open(path, "r", encoding="utf-8") as fh:
        p = json.load(fh)
    if not _valid(p):
        raise ValueError("%s is not a system profile" % os.path.basename(path))
    p.pop("_path", None)
    if load(p["name"]) is not None and not replace:
        base, n = p["name"], 2
        while load("%s (%d)" % (base, n)) is not None:
            n += 1
        p["name"] = "%s (%d)" % (base, n)
    save(p)
    return p


# ------------------------------------------------------------------ which system recorded a job
def identity(hardware=None, session=None):
    """The system a recording came from: {unit_serial, unit_name, certus_serial} (missing ones left out).
    hardware = the capture's hardware.json, session = its session.json (survey lines only)."""
    out = {}
    unit = (session or {}).get("unit") if isinstance(session, dict) else None
    if isinstance(unit, dict):
        if unit.get("serial"):
            out["unit_serial"] = str(unit["serial"]).strip()
        if unit.get("name"):
            out["unit_name"] = str(unit["name"]).strip()
    rep = (hardware or {}).get("certus_reported") if isinstance(hardware, dict) else None
    dev = rep.get("device") if isinstance(rep, dict) else None
    if isinstance(dev, dict) and dev.get("serial"):
        out["certus_serial"] = str(dev["serial"]).strip()
    for k in ("unit_serial", "unit_name", "certus_serial"):      # hardware.json may carry them itself
        if not out.get(k) and isinstance(hardware, dict) and hardware.get(k):
            out[k] = str(hardware[k]).strip()
    return out


def identity_of_files(paths, limit=6):
    """identity() from the first recordings that carry it (.clraw attachments). Survey lines have both
    hardware.json and session.json; navigation recordings only hardware.json."""
    from . import clraw
    found = {}
    for p in [p for p in paths if str(p).lower().endswith(".clraw")][:limit]:
        try:
            f = clraw.ClrawFile(p)
            try:
                got = identity(f.hardware or {}, f.session or {})      # properties of ClrawFile
            finally:
                close = getattr(f, "close", None)
                if close:
                    close()
        except Exception:       # noqa: BLE001  (a damaged or partial file: try the next)
            continue
        for k, v in got.items():
            found.setdefault(k, v)
        if "unit_serial" in found and "certus_serial" in found:
            break
    return found


def describe_system(ident):
    if not ident:
        return "unknown system"
    parts = []
    if ident.get("unit_name"):
        parts.append(ident["unit_name"])
    if ident.get("unit_serial"):
        parts.append("unit %s" % ident["unit_serial"])
    if ident.get("certus_serial"):
        parts.append("Certus %s" % ident["certus_serial"])
    return ", ".join(parts)


def match(ident, profiles=None):
    """The profile made for this system: the same mapper unit serial, else the same Certus serial (the
    Certus can move between vehicles, so the unit wins). None when nothing matches."""
    if not ident:
        return None
    profiles = list_profiles() if profiles is None else profiles
    for key in ("unit_serial", "certus_serial"):
        v = ident.get(key)
        if not v:
            continue
        hits = [p for p in profiles if (p.get("system") or {}).get(key) == v]
        if hits:
            return max(hits, key=lambda p: p.get("updated", ""))
    return None


# ------------------------------------------------------------------ comparing
def _same(a, b, unit):
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y, unit) for x, y in zip(a, b))
    try:
        return abs(float(a) - float(b)) <= TOL.get(unit, 1e-9)
    except (TypeError, ValueError):
        return str(a) == str(b)


def fmt(v, unit=""):
    if v is None:
        return "–"
    if isinstance(v, (list, tuple)):
        nd = 4 if unit in ("°",) else 3
        return ", ".join(("%.*f" % (nd, float(x))) if isinstance(x, (int, float)) else str(x) for x in v)
    if isinstance(v, float):
        return "%.3f" % v
    return str(v)


def differences(profile, current, sections=None):
    """[(section, key, label, unit, profile value, current value)] where the profile and the current values
    differ, for the sections both have (or the given ones)."""
    out = []
    for s in sections or SECTIONS:
        pv = (profile.get("sections") or {}).get(s)
        cv = (current or {}).get(s)
        if not pv or cv is None:
            continue
        for key, label, unit in FIELDS[s]:
            if key not in pv:
                continue
            if not _same(pv.get(key), cv.get(key), unit):
                out.append((s, key, label, unit, pv.get(key), cv.get(key)))
    return out
