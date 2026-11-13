"""Run the conversions for the chosen recordings.

Output layout (out_root is the folder the user chose; default: Processing/<Name>/RemixPro/ of the open DCS
project, else <mapper project>/Processing/RemixPro/):

    <out_root>/<recording>/beamworx/            <clraw base name>.bwxraw + .xml, one per .clraw
    <out_root>/<recording>/inertial_explorer/   <recording>.imr, .obs (+ _rx2.obs), .nav, README_INERTIAL_EXPLORER.txt
    <out_root>/<recording>/qinertia/            <recording>_imu.qimu, .obs (+ _rx2.obs), .nav, README_QINERTIA.txt

Each output folder holds .made_from.json: what it was made from (file names and sizes, the lever arm, the
ephemeris file's size, the RemixPro version). Asking again costs nothing when that still matches; it is
made again when an input changed (a line copied in full after a first look, say).

Every output is written to a work folder beside its final place and moved there only when complete, so a
cancel or an error leaves the earlier result (or nothing), never half a set. The inputs are only read.
"""
import json
import os
import shutil
import time

from . import NAME, __version__, beamworx, inertial, notes
from .sources import BEAMWORX, CLRAW, IE, OUTPUT_LABELS, QINERTIA

NOTE = ".made_from.json"
LEAP_SECONDS = 18


class Cancelled(Exception):
    """The user pressed Cancel."""


def _sizes(paths):
    out = []
    for p in paths:
        try:
            out.append([os.path.basename(p), os.path.getsize(p)])
        except OSError:
            out.append([os.path.basename(p), None])
    return out


def signature(item, kind):
    """What an output depends on."""
    if kind == CLRAW:
        files = item.inputs
        mounts = {}
        for k, v in sorted((item.peek or {}).items()):
            if k in ("sessions", "platform"):
                mounts[k] = len(json.dumps(v, sort_keys=True))
        extra = {"offsets": item.offsets, "fallback_offsets": item.fallback_offsets,
                 "gnss": sorted("%s|%s|%s" % (g.role, g.name, g.size) for g in item.gnss), "mounts": mounts}
    elif kind == BEAMWORX:
        files = item.clraw
        extra = {}
    else:
        src, files = item.certus_source()
        extra = {"source": src, "lever": item.lever_primary, "lever2": item.lever_secondary,
                 "eph": os.path.getsize(item.ephemeris) if item.ephemeris and os.path.isfile(item.ephemeris) else 0}
    return dict({"program": NAME, "version": __version__, "kind": kind, "files": _sizes(files)}, **extra)


def target(out_root, item, kind):
    return os.path.join(out_root, item.name, kind)


def read_note(folder):
    try:
        with open(os.path.join(folder, NOTE), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def is_current(out_root, item, kind):
    folder = target(out_root, item, kind)
    note = read_note(folder)
    return bool(note) and note.get("signature") == signature(item, kind) and all(
        os.path.isfile(os.path.join(folder, f)) for f in note.get("files", []))


def state(out_root, item, kind):
    """"current", "out of date" (made from other inputs) or "not made"."""
    if is_current(out_root, item, kind):
        return "current"
    return "out of date" if read_note(target(out_root, item, kind)) else "not made"


def _check_out_root(out_root, item):
    """Never write where the recording itself lives."""
    o = os.path.normcase(os.path.abspath(out_root))
    for p in item.clraw + item.anpp:
        d = os.path.normcase(os.path.dirname(os.path.abspath(p)))
        if o == d:
            raise ValueError("The output folder is the folder that holds the recording (%s). Choose another one."
                             % os.path.dirname(p))


def _work(final):
    w = final + ".making"
    shutil.rmtree(w, ignore_errors=True)
    os.makedirs(w)
    return w


def _publish(work, final, files, sig, extra):
    """Move a finished work folder into place (the earlier result, if any, goes only now)."""
    with open(os.path.join(work, NOTE), "w", encoding="utf-8") as f:
        json.dump(dict({"signature": sig, "files": files, "made_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                                                     time.gmtime())}, **extra),
                  f, indent=1)
    old = final + ".old"
    shutil.rmtree(old, ignore_errors=True)
    if os.path.isdir(final):
        os.replace(final, old)
    os.replace(work, final)
    shutil.rmtree(old, ignore_errors=True)


def _meta(item, summary):
    s = item.session or {}
    return {"id": item.name,
            "started_utc": s.get("started_utc") or item.metadata.get("started_utc") or summary.get("first_valid_utc"),
            "clock_source": s.get("clock_source") or "INS (GNSS time)",
            "lever_primary": item.lever_primary, "lever_secondary": item.lever_secondary,
            **({"rtk": s["rtk"]} if isinstance(s.get("rtk"), dict) else {})}


def run(item, kinds, out_root, progress=None, cancelled=None, force=False):
    """Make the chosen outputs of one recording. Returns {kind: {"state": "made" | "current", "folder",
    "files", "warnings"}}. Raises Cancelled when cancelled() becomes True."""
    _check_out_root(out_root, item)
    kinds = [k for k in kinds if k in item.outputs]
    todo = [k for k in kinds if force or not is_current(out_root, item, k)]
    result = {k: {"state": "current", "folder": target(out_root, item, k),
                  "files": (read_note(target(out_root, item, k)) or {}).get("files", []), "warnings": []}
              for k in kinds if k not in todo}
    if not todo:
        if progress:
            progress(1.0)
        return result
    stop = cancelled or (lambda: False)
    if CLRAW in todo:
        try:
            result[CLRAW] = _make_clraw(item, out_root, lambda f: progress and progress(f), stop)
        except BaseException:
            for d in (os.path.join(out_root, item.name), out_root):
                try:
                    os.rmdir(d)
                except OSError:
                    pass
            raise
        if progress:
            progress(1.0)
        return result
    weights = {BEAMWORX: item.size or 1}
    if IE in todo or QINERTIA in todo:
        weights["certus"] = sum(os.path.getsize(p) for p in item.certus_source()[1] if os.path.isfile(p)) or 1
    total = sum(weights.values())
    base = [0.0]

    def part(w):
        def prog(f):
            if stop():
                raise Cancelled()
            if progress:
                progress(min(1.0, (base[0] + w * f) / total))
        return prog

    try:
        if BEAMWORX in todo:
            result[BEAMWORX] = _make_beamworx(item, out_root, part(weights[BEAMWORX]), stop)
            base[0] += weights[BEAMWORX]
        if IE in todo or QINERTIA in todo:
            result.update(_make_certus(item, out_root, [k for k in (IE, QINERTIA) if k in todo],
                                       part(weights["certus"]), stop))
    except BaseException:
        for d in (os.path.join(out_root, item.name), out_root):     # no empty folders left by a cancel
            try:
                os.rmdir(d)
            except OSError:
                pass
        raise
    if progress:
        progress(1.0)
    return result


def _make_beamworx(item, out_root, prog, stop):
    final = target(out_root, item, BEAMWORX)
    os.makedirs(os.path.dirname(final), exist_ok=True)
    work = _work(final)
    try:
        r = beamworx.to_bwxraw(item.clraw, work, progress=prog, cancelled=stop)
    except beamworx.Cancelled:
        shutil.rmtree(work, ignore_errors=True)
        raise Cancelled()
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    files = []
    for p in r["files"]:
        files += [os.path.basename(p), os.path.basename(p)[:-len(".bwxraw")] + ".xml"]
    warnings = []
    if r["damaged_chunks"]:
        warnings.append("%d damaged .clraw chunk(s) skipped (each costs about a second of data)."
                        % r["damaged_chunks"])
    if r["config"] != "recorded":
        warnings.append("Survey configuration %s." % r["config"])
    extra = {"made_from": [os.path.basename(p) for p in item.clraw], "points": r["points"],
             "navigation": r["navigation"], "damaged_chunks": r["damaged_chunks"], "config": r["config"]}
    _publish(work, final, files, signature(item, BEAMWORX), extra)
    return {"state": "made", "folder": final, "files": files, "warnings": warnings}


def _make_certus(item, out_root, kinds, prog, stop):
    """One pass over the Certus stream makes both Inertial Explorer and Qinertia files."""
    src, paths = item.certus_source()
    root = os.path.join(out_root, item.name)
    os.makedirs(root, exist_ok=True)
    work = _work(os.path.join(root, "certus"))
    try:
        s = inertial.convert(paths if src == "anpp" else [], work, item.name, leap=LEAP_SECONDS,
                             lever_arm_vehicle_m=item.lever_primary, want_imr=IE in kinds,
                             want_qimu=QINERTIA in kinds, want_rinex=True, want_track=False, progress=prog,
                             ephemeris_paths=[item.ephemeris] if item.ephemeris else [],
                             clraw_paths=paths if src == "clraw" else None, cancelled=stop)
    except (inertial.Cancelled, Cancelled):
        shutil.rmtree(work, ignore_errors=True)
        raise Cancelled()
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    try:
        imu = s.get("imu") or {}
        warnings = []
        if not imu.get("samples"):
            warnings.append("No raw IMU samples (Certus packet 28) in this recording.")
        if imu.get("dropped_before_time_valid"):
            warnings.append("%d IMU samples recorded before the Certus knew the time were left out."
                            % imu["dropped_before_time_valid"])
        if imu.get("dropped_out_of_order"):
            warnings.append("%d IMU samples out of time order were left out." % imu["dropped_out_of_order"])
        if not s.get("rinex_obs"):
            warnings.append("No raw GNSS observations (Certus packet 60) in this recording.")
        skipped = sum(o.get("unmapped_signal_records", 0) for o in s.get("rinex_obs") or [])
        if skipped:
            warnings.append("%d observations of signal types RINEX has no code for were skipped." % skipped)
        if s.get("damaged_chunks"):
            warnings.append("%d damaged .clraw chunk(s) skipped." % s["damaged_chunks"])
        if not item.lever_primary:
            warnings.append("No antenna lever arm recorded: enter it in the processing software.")
        f = s.get("files", {})
        meta = _meta(item, s)
        out = {}
        for kind in kinds:
            final = target(out_root, item, kind)
            kw = _work(final)
            keys = ("imr", "obs", "obs_rx2", "nav") if kind == IE else ("qimu", "obs", "obs_rx2", "nav")
            files = []
            for k in keys:
                if f.get(k):
                    shutil.copyfile(os.path.join(work, f[k]), os.path.join(kw, f[k]))
                    files.append(f[k])
            files.append((notes.write_ie if kind == IE else notes.write_qinertia)(kw, meta, s))
            summary = {k: v for k, v in s.items() if k not in ("files",)}
            _publish(kw, final, files, signature(item, kind), {"summary": summary})
            out[kind] = {"state": "made", "folder": final, "files": files, "warnings": list(warnings)}
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _make_clraw(item, out_root, prog, stop):
    from . import sonar
    final = target(out_root, item, CLRAW)
    os.makedirs(os.path.dirname(final), exist_ok=True)
    work = _work(final)
    from . import gnssfiles
    try:
        # the GNSS files whose time covers the recording, zip members extracted into the work folder
        pk = item.peek or {}
        t0 = pk.get("utc_first")
        gf = gnssfiles.covering(item.gnss, t0, t0 + 24 * 3600) if t0 else list(item.gnss)
        ex = os.path.join(work, ".gnss")
        rinex = [(p, "rover") for p in item.rinex] + [(g.extract(ex), g.role) for g in gf if g.type in ("obs", "nav")]
        llh = list(item.llh) + [g.extract(ex) for g in gf if g.type == "llh" and g.role == "rover"]
        ppk = list(item.ppk) + [g.extract(ex) for g in gf if g.type == "pos" and g.role == "rover"]
        seen = set()
        rinex = [r for r in rinex if not (os.path.basename(r[0]) in seen or seen.add(os.path.basename(r[0])))]
        r = sonar.to_clraw(item.parts or item.sonar_file, work, base_name=item.name, project=item.project,
                           offsets=item.offsets, llh=llh, ppk=ppk, rinex=rinex, extra_hw=item.extra_hw,
                           extra_attachments=item.extra_attachments, progress=prog, cancelled=stop,
                           fallback_offsets=item.fallback_offsets,
                           fallback_source=item.fallback_source or "TidalCal vessel table")
        shutil.rmtree(ex, ignore_errors=True)
    except sonar.Cancelled:
        shutil.rmtree(work, ignore_errors=True)
        raise Cancelled()
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    warnings = []
    if r["time_basis"].startswith("estimated"):
        warnings.append("No NTP time in the sonar packets and no MAVLink SYSTEM_TIME: times are estimated from "
                        "SonarView's session start.")
    if r["skipped_bytes"]:
        warnings.append("%d bytes that were not good packets were skipped." % r["skipped_bytes"])
    if r["truncated"]:
        warnings.append("The recording ends inside a packet (unfinished file): everything complete was kept.")
    missing = [s["device_id"] for s in r["hardware"]["sonars"] if not s.get("acoustic_centre_m")]
    if missing:
        warnings.append("No offsets for sonar(s) %s: enter the Mount Offset in SonarView, or put %s beside the "
                        "recording." % (missing, "sonar_offsets.json"))
    npos = sum(v for k, v in r.get("mav_types", {}).items() if k in sonar.POSITION_TYPES)
    m = r["messages"]
    pos = []
    if npos:
        pos.append("%s autopilot positions" % "{:,}".format(npos))
    if m.get(sonar.T_LLH):
        pos.append("%s receiver log lines" % "{:,}".format(m[sonar.T_LLH]))
    if m.get(sonar.T_PPK):
        pos.append("%s PPK solutions" % "{:,}".format(m[sonar.T_PPK]))
    if m.get(sonar.T_NMEA):
        pos.append("%s NMEA sentences" % "{:,}".format(m[sonar.T_NMEA]))
    rin = ["%s %s" % (s.get("gnss_role"), s["file"]) for s in r["sources"] if s.get("gnss_role")]
    if not pos:
        warnings.append("No position in this recording and no receiver log for its time.")
    files = [os.path.basename(p) for p in r["files"]]
    extra = {"made_from": [s["file"] for s in r["sources"]], "messages": r["messages"], "sonars": r["sonars"],
             "time_basis": r["time_basis"], "first_utc": r["first_utc"], "last_utc": r["last_utc"],
             "positions": pos, "rinex": rin,
             "offsets": {str(s["device_id"]): [s.get("acoustic_centre_m"), s.get("acoustic_centre_source")]
                         for s in r["hardware"]["sonars"]}}
    _publish(work, final, files, signature(item, CLRAW), extra)
    return {"state": "made", "folder": final, "files": files, "warnings": warnings,
            "info": ["Position: " + ("; ".join(pos) or "none")] +
                    (["GNSS RINEX: " + ", ".join(rin)] if rin else [])}


def describe_result(item, result):
    """One line per output for the log."""
    lines = []
    for kind in (BEAMWORX, IE, QINERTIA, CLRAW):
        r = result.get(kind)
        if not r:
            continue
        word = "made" if r["state"] == "made" else "already up to date"
        lines.append("%s %s: %s, %d file(s) in %s" % (item.name, OUTPUT_LABELS[kind], word, len(r["files"]),
                                                   r["folder"]))
        lines += ["    " + w for w in r.get("info", [])]
        lines += ["    " + w for w in r["warnings"]]
    return lines
