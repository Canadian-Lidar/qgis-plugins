"""Marine jobs (sonar recordings from the survey boat, .clraw kind "sonar"): the trajectory of the GNSS antenna and
each sonar head's attitude, finalized as dcs.trajectory/1 for AlignPro and CloudPro.

Position, from the best source the recording holds (Sean, 13 Nov 2026):
  1. PPK: PosPro's engine on the Reach rover RINEX against the base RINEX (forward and backward passes, combined
     here: both fixed -> averaged, else the better pass), the base at its surveyed position with its antenna
     height (to the ARP, from the base RINEX header) and antenna type;
  2. the rover's own solution log (.LLH, RTK in real time);
  3. the autopilot's position (MAVLink GLOBAL_POSITION_INT).
Heading: course over ground (from the position track) when the boat moves faster than 0.5 m/s, else the
autopilot's compass (MAVLink ATTITUDE yaw). Vessel roll / pitch: the autopilot's ATTITUDE.
Each sonar head's attitude: its own fused attitude (packet 504) at every ping; the 100 Hz raw IMU (505) is
checked against it.
"""
import json
import math
import os
import re
import shutil
import struct
import time

import numpy as np
from dcs_core.files import replace as _replace   # retried while Windows holds the target open (audit 23 Nov 2026)

COG_MIN_SPEED = 0.5            # m/s: below it the heading is the autopilot's compass
COG_SMOOTH_S = 2.0
RS2_PCO_UP_M = 0.134           # Emlid Reach RS2: mount (ARP) to the L1 phase centre, used when no ANTEX is at hand
POS = np.dtype([("t", "<f8"), ("lat", "<f8"), ("lon", "<f8"), ("h", "<f8"), ("q", "<i2"), ("sdn", "<f4"),
                ("sde", "<f4"), ("sdu", "<f4")])
SOURCES = {"ppk": "PPK (Reach rover against the base)", "llh": "Reach real-time solution (.LLH)",
           "mavlink": "autopilot position (MAVLink)"}
Q_NAMES = {1: "fixed", 2: "float", 3: "SBAS", 4: "DGPS", 5: "single", 6: "PPP", 9: "autopilot"}


class MarineError(Exception):
    pass


# ----------------------------------------------------------------------------- the job
def recordings(dcs, include_excluded=False):
    """[[paths of one recording]] of the job's marine .clraw, in recording order (the recordings left out on the
    Data page are not there unless include_excluded)."""
    from dcs_core import clraw
    files = dcs.sonar_clraw_files(include_excluded=include_excluded) if dcs is not None else []
    groups = clraw.group_recordings(files, sonar=True)
    return [sorted(v) for v in groups.values()]


def adopt_sonar_files(project, paths=None):
    """Sonar .clraw (RemixPro's SonarView recordings) given as rover files are the job's marine recordings
    (PosPro 2.10.2; Sean, 18 Nov 2026: recordings added on the mobile mapping Data page read as 0 IMU samples):
    they move from cfg rover files (and from paths, files being added) into the job's added recordings
    (DcsProject.set_sonar_selection), so the job opens as a marine job. -> number of files moved (0 without a
    DCS project)."""
    from dcs_core import clraw
    dcs = getattr(project, "dcs", None)
    if dcs is None:
        return 0
    rover = project.cfg["rover"]["files"]
    cand = list(rover) + [p for p in (paths or []) if p not in rover]
    son = []
    for p in cand:
        try:
            if p.lower().endswith(".clraw") and os.path.exists(p) and clraw.is_sonar(p):
                son.append(p)
        except Exception:       # noqa: BLE001
            continue
    if not son:
        return 0
    key = lambda f: os.path.normcase(os.path.abspath(f))       # noqa: E731
    have = {key(f) for f in dcs.sonar_added()}      # kept as added even when the captures also list them
    added = list(dcs.sonar_added()) + [p for p in son if key(p) not in have]
    excluded = [f for f in dcs.sonar_excluded() if key(f) not in {key(p) for p in son}]
    dcs.set_sonar_selection(added=added, excluded=excluded, plugin="PosPro")
    project.cfg["rover"]["files"] = [f for f in rover if f not in son]
    if not project.cfg["rover"]["files"]:
        # what Read files made of them as Certus logs (0 IMU samples) must not stand for the boat's span
        for k in ("extract", "time_start", "time_end"):
            project.cfg["rover"][k] = None if k != "extract" else {}
    project.save()
    return len(son)


def refresh_extent(project):
    """cfg rover marine_span / marine_runs from the recordings in the job (cheap: the chunk index and the
    first / last chunks), marine_xyz only when missing (it reads the autopilot). -> True when changed."""
    runs = recording_runs(project)
    span = [min(r[0] for r in runs), max(r[1] for r in runs)] if runs else None
    rv = project.cfg["rover"]
    changed = rv.get("marine_span") != span or rv.get("marine_runs") != runs
    rv["marine_span"], rv["marine_runs"] = span, runs
    if span and not rv.get("marine_xyz"):
        _s, xyz = rover_extent(project)
        rv["marine_xyz"] = list(xyz) if xyz else None
        changed = True
    return changed


def is_marine(project):
    """A job whose captures are marine sonar recordings only (no Lidar, no Certus)."""
    dcs = getattr(project, "dcs", None)
    if dcs is None:
        return False
    try:
        if not recordings(dcs):
            return False
        return not dcs.survey_clraw_files()
    except Exception:       # noqa: BLE001
        return False


def options(project):
    o = dict(project.cfg.get("marine") or {})
    o.setdefault("position", "auto")           # auto | ppk | llh | mavlink
    o.setdefault("cog_min_speed", COG_MIN_SPEED)
    o.setdefault("base", None)                 # {"lat", "lon", "h" (mark, ellipsoidal), "frame", "epoch"} or None
    return o


def settings_key(opts, project=None):
    """What a run depends on (Processing shows 'out of date' when it changed since)."""
    out = {k: opts.get(k) for k in ("position", "cog_min_speed", "base", "levers", "heads_off")}
    if project is not None:                     # the Processing page's GNSS settings (as mobile mapping)
        c = project.cfg.get("processing") or {}
        out["gnss"] = {k: c.get(k) for k in PROC_KEYS}
        rc = project.cfg.get("rover") or {}
        out["time_range"] = [rc.get("time_start"), rc.get("time_end")]
        out["ephemeris"] = [project.cfg.get("auto_ephemeris", True), project.cfg.get("nav_files")]
    b = selected_base(project) if project is not None else None
    if b:                                       # the Base page's selected base: what PPK uses
        out["base_station"] = {k: b.get(k) for k in ("id", "xyz", "frame", "epoch", "antenna", "delta_hen", "files")}
    return _jsonable(out)


PROC_KEYS = ("systems", "elmask", "ar_ratio", "ar", "frequencies", "passes", "advanced")


def ppk_proc(project):
    """RTKLIB settings for marine PPK from the Processing page (the same GNSS settings as mobile mapping)."""
    c = (project.cfg.get("processing") or {}) if project is not None else {}
    return {"frequencies": c.get("frequencies", 2), "elmask": c.get("elmask", 12), "systems": c.get("systems", "GREJ"),
            "ar": c.get("ar", True), "ar_ratio": c.get("ar_ratio", 3.0), "advanced": c.get("advanced") or {}}


# ----------------------------------------------------------------------------- the Base page's bases
RECORDING_BASE = "Recorded"           # the Base page name of the base RINEX stored in the recordings


def overlaps(spans, min_s=5.0):
    """Recordings that share time: [(name a, name b, seconds)] from {name: (t0, t1)}."""
    items = sorted(spans.items(), key=lambda kv: kv[1][0])
    out = []
    for i, (a, (a0, a1)) in enumerate(items):
        for b, (b0, b1) in items[i + 1:]:
            if b0 >= a1:
                break
            d = min(a1, b1) - max(a0, b0)
            if d > min_s:
                out.append((a, b, d))
    return out


def is_sbas_log(name):
    """Emlid's SBAS message log (.26B), not a RINEX navigation file: never given to the engine as one."""
    return bool(re.search(r"\.\d\d[bB]$|\.sbs$", str(name)))


def recording_base_files(project):
    """The base RINEX stored in the job's recordings, written once to work/marine/base/<recording>/ so the Base page
    (the same page as for mobile mapping) can list and read them: [(recording name, [paths])]."""
    from dcs_core.marine import SonarRecording
    out = []
    for paths in recordings(project.dcs):
        rec = SonarRecording(paths)
        files = rec.rinex("base")
        if not files:
            continue
        folder = project.wpath("marine", "base", rec.name, "x")[:-2]
        os.makedirs(folder, exist_ok=True)
        got = []
        for name, data in sorted(files.items()):
            path = os.path.join(folder, os.path.basename(name))
            if not os.path.exists(path) or os.path.getsize(path) != len(data):
                with open(path, "wb") as f:
                    f.write(data)
            got.append(path)
        out.append((rec.name, got))
    return out


def rover_extent(project):
    """For the Base page on a marine job: the recordings' span in GPS seconds (engine time base, as
    Project.capture_span) and the boat's approximate ECEF position (autopilot) -> ((t0, t1), xyz) or (None, None)."""
    from dcs_core.marine import SonarRecording
    t0 = t1 = None
    xyz = None
    for paths in recordings(project.dcs):
        rec = SonarRecording(paths)
        a, b = rec.span()
        t0 = a if t0 is None else min(t0, a)
        t1 = b if t1 is None else max(t1, b)
        if xyz is None:
            P = rec.vehicle()["pos"]
            if len(P):
                xyz = llh_to_ecef(float(np.median(P["lat"])), float(np.median(P["lon"])), float(np.median(P["alt"])))
    if t0 is None:
        return None, None
    g = 315964800.0 - 18.0                      # UTC -> GPS seconds since 1980-01-06
    return (t0 - g, t1 - g), xyz


def recording_runs(project):
    """Each recording used, for the coverage bars: [[t0, t1, name]] in GPS seconds (engine time base)."""
    from dcs_core.marine import SonarRecording
    g = 315964800.0 - 18.0
    out = []
    for paths in recordings(project.dcs):
        try:
            rec = SonarRecording(paths)
        except Exception:       # noqa: BLE001
            continue
        a, b = rec.span()
        out.append([a - g, b - g, rec.name])
    return sorted(out)


def recording_base_known(project):
    """A position for the recordings' base that is better than the RINEX header's: the one entered on the old
    marine Base page (PosPro 2.5-2.6), else the TidalCal project's surveyed base -> (lat, lon, h, frame, epoch, note)."""
    from dcs_core.marine import SonarRecording
    b = (project.cfg.get("marine") or {}).get("base")
    if b and b.get("lat") is not None:
        return b["lat"], b["lon"], b["h"], b.get("frame") or "NAD83(CSRS)", b.get("epoch") or "2010.0", \
            "entered on the marine Base page"
    for paths in recordings(project.dcs):
        tc = SonarRecording(paths).tidalcal
        llh = tc.get("surveyed_base_llh")
        if llh and len(llh) == 3:
            return float(llh[0]), float(llh[1]), float(llh[2]), "NAD83(CSRS)", "2010.0", "TidalCal project"
    return None


def selected_base(project):
    """The base chosen on the Base page (cfg bases / base_selected), with a position, or None."""
    try:
        b = project.base()
    except Exception:       # noqa: BLE001
        return None
    return b if b and b.get("xyz") and b.get("files") else None


def usable_bases(project):
    """The Base page's bases that can be used (ticked, with files and a position), the selected one first."""
    sel = project.cfg.get("base_selected")
    out = [b for b in project.cfg.get("bases") or [] if b.get("use", True) and b.get("xyz") and b.get("files")]
    out.sort(key=lambda b: 0 if b.get("id") == sel else 1)
    return out


def _utc_span_of_base(b):
    """A base's coverage in UTC seconds: its runs (cfg coverage, GPS seconds) -> [(t0, t1)]."""
    g = 315964800.0 - 18.0
    runs = b.get("coverage") or []
    return [(float(r[0]) + g, float(r[1]) + g) for r in runs if len(r) >= 2]


def base_overlap(b, t0, t1):
    """Seconds of a base's coverage inside [t0, t1] (UTC)."""
    return sum(max(0.0, min(t1, r1) - max(t0, r0)) for r0, r1 in _utc_span_of_base(b))


def choose_base(project, rec):
    """The base a recording is processed with (2.8.0): of the Base page's bases, the one covering the most of the
    recording (the selected base first when several cover it all) -> (base entry or None, reason)."""
    bases = usable_bases(project)
    if not bases:
        return None, "no base on the Base page"
    t0, t1 = rec.span()
    scored = [(base_overlap(b, t0, t1), -i, b) for i, b in enumerate(bases)]
    best = max(scored, key=lambda x: (x[0], x[1]))
    if best[0] <= 0 and any(b.get("coverage") for b in bases):
        have = "; ".join("%s %s" % (b.get("name"), " + ".join("%s-%s" % (_hm(r0), _hm(r1))
                                                            for r0, r1 in _utc_span_of_base(b)) or "?")
                         for b in bases)
        return None, ("no base on the Base page covers %s to %s (UTC); the bases cover: %s. Add that day's base "
                      "on the Base page" % (_hm(t0), _hm(t1), have))
    return best[2], ""


def _hm(t):
    import datetime as _dt
    return _dt.datetime.fromtimestamp(float(t), _dt.timezone.utc).strftime("%Y-%m-%d %H:%M")


def base_for(project, rec):
    """What PPK of one recording uses from the Base page: {obs, navs, xyz, antenna, delta_hen, lat, lon, h, frame,
    epoch, source, name} for the base covering the recording (choose_base), {"none": reason} when the Base page
    has bases but none covers it, or None (no base on the Base page: the recording's own base + base_of)."""
    from . import rinex
    if not usable_bases(project):
        return None
    b, why = choose_base(project, rec)
    if b is None:
        return {"none": why}
    obs = [f for f in b["files"] if pipeline_obs(f) and os.path.exists(f)]
    if not obs:
        return {"none": "base %s: its RINEX observation files are missing" % b.get("name")}
    t0, t1 = rec.span()
    pick = [f for f in obs if os.sep + rec.name + os.sep in f]          # this recording's own base file
    if not pick:                                                          # else the file covering it best
        best = None
        for f in obs:
            hi = rinex.header_info(f, scan_body=False) or {}
            first, last = hi.get("first"), hi.get("last")
            if not first:
                continue
            u = lambda d: d.replace(tzinfo=__import__("datetime").timezone.utc).timestamp() - 18   # noqa: E731
            a = u(first)
            z = u(last) if last else a
            cover = max(0.0, min(t1, z) - max(t0, a))
            key = (cover, a if a <= t0 + 60 else -1e18)
            if best is None or key > best[0]:
                best = (key, f)
        pick = [best[1] if best else obs[0]]
    folders = {os.path.dirname(f) for f in b["files"]}
    navs = [f for f in b["files"] if not pipeline_obs(f) and os.path.exists(f) and not is_sbas_log(f)]
    for d in folders:                                   # navigation files beside the observations
        for n in sorted(os.listdir(d)):
            q = os.path.join(d, n)
            if q not in navs and not pipeline_obs(q) and re.search(r"\.(\d\d[nNgGlLpPcC]|nav|rnx)$", n):
                navs.append(q)
    lat, lon, h = ecef_to_llh(*[np.array([v]) for v in b["xyz"]])
    src = {"rinex": "RINEX header (approximate)", "manual": "entered", "ppp": "built-in PPP",
           "csrs": "NRCan CSRS-PPP"}.get(b.get("pos_source"), b.get("pos_source"))
    return {"obs": pick[0], "navs": navs, "xyz": list(b["xyz"]), "antenna": (b.get("antenna") or "").strip(),
            "delta_hen": list(b.get("delta_hen") or [0.0, 0.0, 0.0]), "lat": float(lat[0]), "lon": float(lon[0]),
            "h": float(h[0]), "frame": b.get("frame") or "", "epoch": b.get("epoch") or None,
            "source": "Base page: %s (%s)" % (b.get("name"), src), "name": b.get("name"),
            "approximate": b.get("pos_source") == "rinex"}


def base_of(rec, opts):
    """The base station for PPK: entered on PosPro's marine page, else TidalCal's surveyed base."""
    b = opts.get("base")
    if b and b.get("lat") is not None:
        return dict(b, source="entered in PosPro")
    tc = rec.tidalcal
    llh = tc.get("surveyed_base_llh")
    if llh and len(llh) == 3:
        return {"lat": float(llh[0]), "lon": float(llh[1]), "h": float(llh[2]), "source": "TidalCal project",
                "frame": tc.get("horizontal_crs") or "", "hi_tidalcal": tc.get("base_hi_m")}
    return None


# ----------------------------------------------------------------------------- positions
def llh_to_ecef(lat, lon, h):
    a, f = 6378137.0, 1 / 298.257222101            # GRS80 (NAD83)
    e2 = f * (2 - f)
    la, lo = math.radians(lat), math.radians(lon)
    n = a / math.sqrt(1 - e2 * math.sin(la) ** 2)
    return ((n + h) * math.cos(la) * math.cos(lo), (n + h) * math.cos(la) * math.sin(lo),
            (n * (1 - e2) + h) * math.sin(la))


def ecef_to_llh(x, y, z):
    """GRS80 geodetic latitude, longitude (deg) and ellipsoidal height (m) of ECEF points (arrays)."""
    a, f = 6378137.0, 1 / 298.257222101
    e2 = f * (2 - f)
    lon = np.arctan2(y, x)
    p = np.hypot(x, y)
    lat = np.arctan2(z, p * (1 - e2))
    for _ in range(6):
        n = a / np.sqrt(1 - e2 * np.sin(lat) ** 2)
        h = p / np.cos(lat) - n
        lat = np.arctan2(z, p * (1 - e2 * n / (n + h)))
    n = a / np.sqrt(1 - e2 * np.sin(lat) ** 2)
    h = p / np.cos(lat) - n
    return np.degrees(lat), np.degrees(lon), h


def read_rtklib_pos(path):
    """RTKLIB .pos -> POS array in UTC. Reads both forms: ECEF with GPS week / seconds (PosPro's engine) and
    lat/lon/height with a date and time (GPST or UTC). Sigmas are rotated to north / east / up."""
    import calendar
    rows, gpst, ecef = [], True, False
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("%"):
                if "UTC" in line and "GPST" not in line:
                    gpst = False
                if "x-ecef" in line:
                    ecef = True
                continue
            a = line.split()
            if len(a) < 10:
                continue
            try:
                if "/" in a[0]:
                    y, mo, d = (int(x) for x in a[0].split("/"))
                    hh, mi, ss = a[1].split(":")
                    t = calendar.timegm((y, mo, d, int(hh), int(mi), 0)) + float(ss) - (18 if gpst else 0)
                else:
                    t = 315964800.0 + int(a[0]) * 604800.0 + float(a[1]) - 18.0
                rows.append([t] + [float(v) for v in a[2:13]])
            except ValueError:
                continue
    if not rows:
        return np.zeros(0, POS)
    R = np.array(rows)
    out = np.zeros(len(R), POS)
    out["t"] = R[:, 0]
    out["q"] = R[:, 4].astype(int)
    if ecef:
        lat, lon, h = ecef_to_llh(R[:, 1], R[:, 2], R[:, 3])
        out["lat"], out["lon"], out["h"] = lat, lon, h
        la, lo = np.radians(lat), np.radians(lon)
        # ENU rows of the rotation, then the covariance's diagonal in ENU
        E = np.stack([-np.sin(lo), np.cos(lo), np.zeros_like(lo)], 1)
        N = np.stack([-np.sin(la) * np.cos(lo), -np.sin(la) * np.sin(lo), np.cos(la)], 1)
        U = np.stack([np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)], 1)
        sx, sy, sz, sxy, syz, szx = (R[:, k] for k in range(6, 12))
        C = np.zeros((len(R), 3, 3))
        C[:, 0, 0], C[:, 1, 1], C[:, 2, 2] = sx ** 2, sy ** 2, sz ** 2
        for (i, j), v in (((0, 1), sxy), ((1, 2), syz), ((2, 0), szx)):
            c = np.sign(v) * v ** 2                 # RTKLIB writes sqrt(|cov|) with its sign
            C[:, i, j] = C[:, j, i] = c
        for col, vec in (("sde", E), ("sdn", N), ("sdu", U)):
            out[col] = np.sqrt(np.maximum(np.einsum("ni,nij,nj->n", vec, C, vec), 0))
    else:
        out["lat"], out["lon"], out["h"] = R[:, 1], R[:, 2], R[:, 3]
        out["sdn"], out["sde"], out["sdu"] = R[:, 6], R[:, 7], R[:, 8]
    return out


def combine_passes(fwd, bwd):
    """Forward and backward PPK: both fixed -> the mean, else the better pass (fixed > float > others, then the
    smaller sigma). -> POS on the union of epochs."""
    if bwd is None or not len(bwd):
        return fwd
    if fwd is None or not len(fwd):
        return bwd
    key_f = np.round(fwd["t"] * 20).astype(np.int64)
    key_b = np.round(bwd["t"] * 20).astype(np.int64)
    rank = {1: 0, 2: 1}
    out = {}
    for arr, keys in ((fwd, key_f), (bwd, key_b)):
        for k, r in zip(keys, arr):
            o = out.get(k)
            if o is None:
                out[k] = r.copy()
                continue
            if o["q"] == 1 and r["q"] == 1:
                m = o.copy()
                for c in ("lat", "lon", "h"):
                    m[c] = (o[c] + r[c]) / 2
                for c in ("sdn", "sde", "sdu"):
                    m[c] = min(o[c], r[c])
                out[k] = m
            elif (rank.get(int(r["q"]), 9), float(r["sdu"])) < (rank.get(int(o["q"]), 9), float(o["sdu"])):
                out[k] = r.copy()
    res = np.array([out[k] for k in sorted(out)], POS)
    return res


def positions_ppk(rec, wdir, base, ctx, atx=None, navs_extra=None, proc=None, forward_only=False):
    """PPK of the rover RINEX against the base RINEX with PosPro's engine. -> (POS, info) or (None, reason)."""
    from . import engine, pipeline, rinex
    rov, bas = rec.rinex("rover"), rec.rinex("base")
    robs = [n for n in rov if pipeline_obs(n)]
    bobs = [n for n in bas if pipeline_obs(n)]
    if base is not None and "none" in base:              # the Base page has bases, none covering this recording
        return None, base["none"]
    page = base is not None and "obs" in base            # the Base page's base for this recording
    if not robs:
        return None, "no rover RINEX observations in the recording"
    if not bobs and not page:
        return None, "no base RINEX observations in the recording"
    if base is None:
        return None, "no base position (choose or add a base on the Base page)"
    os.makedirs(wdir, exist_ok=True)

    def put(name, data):
        p = os.path.join(wdir, name)
        with open(p, "wb") as f:
            f.write(data)
        return p
    ro = put("rover.obs", rov[pick_rover_obs(rov, robs, *rec.span())])
    if page:
        bo = base["obs"]
        navs = [put("nav_%d_%s" % (i, n), d) for i, (n, d) in enumerate(rov.items())
                if not pipeline_obs(n) and not is_sbas_log(n)]
        navs += [f for f in base["navs"] if f not in (navs_extra or [])]
        delta = list(base["delta_hen"])
        ant = base["antenna"]
        note = "base %s: antenna %.3f m to the ARP%s (Base page)" % (base.get("name"), delta[0],
                                                                        ", %s" % ant if ant else "")
    else:
        bo = put("base.obs", bas[bobs[0]])
        navs = [put("nav_%d_%s" % (i, n), d) for i, (n, d) in enumerate(list(rov.items()) + list(bas.items()))
                if not pipeline_obs(n) and not is_sbas_log(n)]
        hi = rinex.header_info(bo, scan_body=False) or {}
        delta = list(hi.get("delta_hen") or [0.0, 0.0, 0.0])
        ant = (hi.get("antenna") or "").strip()
        note = "base antenna %.3f m to the ARP (base RINEX header)%s" % (delta[0], ", %s" % ant if ant else "")
    if not atx and ant.upper().startswith("EML_REACH_RS2"):
        delta[0] += RS2_PCO_UP_M            # no antenna calibrations: Emlid's ARP -> L1 phase centre
        note += "; no ANTEX: +%.3f m (Emlid RS2 ARP to L1 phase centre)" % RS2_PCO_UP_M
    navs += list(navs_extra or [])                  # the Data page's ephemeris: IGS broadcast, files added there
    proc = proc or {"frequencies": 2, "elmask": 12, "systems": "GREJ", "ar": True, "ar_ratio": 3.0}
    conf = os.path.join(wdir, "ppk.conf")
    with open(conf, "w", encoding="utf-8") as f:
        f.write(pipeline.rtklib_conf_ppk(proc, atx, delta, ant if atx else ""))
    xyz = base["xyz"] if page else llh_to_ecef(base["lat"], base["lon"], base["h"])
    t0, t1 = rec.span()
    g0 = 315964800.0 - 18.0                 # engine times: GPS seconds since 1980-01-06
    common = ["--ts", "%.3f" % (t0 - g0 - 120), "--te", "%.3f" % (t1 - g0 + 120), "--base"] + ["%.4f" % v for v in xyz]
    jobs = [("GNSS forward", ["gnss", "-k", conf, "-o", os.path.join(wdir, "fwd.pos"), "-d", "fwd"] + common +
             [ro, bo] + navs)]
    for f in ("fwd.pos", "bwd.pos"):
        try:
            os.remove(os.path.join(wdir, f))
        except OSError:
            pass
    if not forward_only:                            # Processing page Passes: forward and backward, combined
        jobs.append(("GNSS backward", ["gnss", "-k", conf, "-o", os.path.join(wdir, "bwd.pos"), "-d", "bwd"] +
                     common + [ro, bo] + navs))
    res = engine.run_parallel(jobs, ctx.progress if ctx else None, ctx.log if ctx else None,
                              ctx.cancelled if ctx else None)
    for label, r in res.items():
        if not r.ok():
            return None, "%s failed: %s" % (label, r.message())
    if not os.path.exists(os.path.join(wdir, "fwd.pos")):
        return None, "PPK gave no solution (no common epochs of rover and base?)"
    fwd = read_rtklib_pos(os.path.join(wdir, "fwd.pos"))
    bwd = read_rtklib_pos(os.path.join(wdir, "bwd.pos")) if os.path.exists(os.path.join(wdir, "bwd.pos")) else None
    pos = combine_passes(fwd, bwd)
    if not len(pos):
        return None, "PPK gave no solution"
    if page:
        base = {k: base[k] for k in ("lat", "lon", "h", "frame", "epoch", "source", "name", "approximate")}
    info = {"base": base, "note": note, "fixed_fwd_pct": _pct(fwd, 1), "fixed_bwd_pct": _pct(bwd, 1),
            "_fwd": fwd, "_bwd": bwd}             # the passes, for the engine's combination (not kept in the summary)
    return pos, info


def _rinex_first_utc(data):
    """TIME OF FIRST OBS of a RINEX observation file held in memory, as UTC seconds (GPS time - 18 s), or None."""
    import datetime as _dt
    head = bytes(data[:20000]).decode("ascii", "replace")
    for line in head.splitlines():
        if "TIME OF FIRST OBS" in line[60:]:
            try:
                v = line[:43].split()
                d = _dt.datetime(int(v[0]), int(v[1]), int(v[2]), int(v[3]), int(v[4]),
                                 tzinfo=_dt.timezone.utc).timestamp() + float(v[5])
                return d - 18.0
            except (ValueError, IndexError):
                return None
        if "END OF HEADER" in line[60:]:
            break
    return None


def pick_rover_obs(rov, robs, t0, t1):
    """Of a recording's rover RINEX observation files, the one for this recording: the latest that starts before
    it (a session file or a day file made from them both cover it), else the earliest."""
    if len(robs) == 1:
        return robs[0]
    firsts = [(_rinex_first_utc(rov[n]), n) for n in robs]
    before = [(f, n) for f, n in firsts if f is not None and f <= t0 + 60]
    if before:
        return max(before)[1]
    known = [(f, n) for f, n in firsts if f is not None]
    return min(known)[1] if known else robs[0]


def pipeline_obs(name):
    import re
    return bool(re.search(r"(\.\d\d[oO]|\.obs|_obs\.rnx)$", name)) or name.lower().endswith("o.rnx")


def _pct(a, q):
    return round(100.0 * float((a["q"] == q).mean()), 1) if a is not None and len(a) else 0.0


def unique_times(pos, tol=1e-3):
    """Positions in time order with each epoch once (a recording can hold the same Reach log twice, e.g. a
    session file and a day file made from it: every epoch twice)."""
    if pos is None or not len(pos):
        return pos
    pos = pos[np.argsort(pos["t"], kind="stable")]
    keep = np.r_[True, np.diff(pos["t"]) > tol]
    return pos[keep]


def rate_hz(t):
    """Position rate from the median time step (None when it cannot be told)."""
    d = np.diff(np.asarray(t, float))
    d = d[d > 1e-3]
    return round(1.0 / float(np.median(d)), 1) if len(d) else None


def positions_llh(rec):
    L = rec.llh()
    if not len(L):
        return None
    out = np.zeros(len(L), POS)
    for c in ("t", "lat", "lon", "h", "q", "sdn", "sde", "sdu"):
        out[c] = L[c]
    return unique_times(out)


def positions_mavlink(rec):
    P = rec.vehicle()["pos"]
    if not len(P):
        return None
    out = np.zeros(len(P), POS)
    out["t"], out["lat"], out["lon"], out["h"] = P["t"], P["lat"], P["lon"], P["alt"]
    out["q"] = 9
    out["sdn"] = out["sde"] = 0.5
    out["sdu"] = 1.0
    return unique_times(out)


def choose_positions(rec, wdir, opts, ctx=None, atx=None, base=None, navs_extra=None, proc=None,
                     forward_only=False):
    """(POS, source key, info, [notes]) by the order PPK > rover LLH > autopilot (or the one asked for)."""
    want = opts.get("position", "auto")
    notes = []
    order = ["ppk", "llh", "mavlink"] if want == "auto" else [want]
    for src in order:
        if src == "ppk":
            pos, info = positions_ppk(rec, os.path.join(wdir, "ppk"), base or base_of(rec, opts), ctx, atx,
                                      navs_extra, proc, forward_only)
            if pos is not None and _pct(pos, 1) >= 50:
                return pos, "ppk", info, notes
            notes.append("PPK not used: %s" % (info if pos is None else "only %.0f %% fixed" % _pct(pos, 1)))
        elif src == "llh":
            pos = positions_llh(rec)
            if pos is not None and len(pos) > 10:
                return pos, "llh", {}, notes
            notes.append("no Reach solution log in the recording")
        else:
            pos = positions_mavlink(rec)
            if pos is not None and len(pos) > 10:
                return pos, "mavlink", {}, notes
            notes.append("no autopilot position in the recording")
    raise MarineError("no position in %s: %s" % (rec.name, "; ".join(notes)))


# ----------------------------------------------------------------------------- heading and attitude
def enu(lat, lon, lat0, lon0):
    kn = 111132.954 - 559.822 * math.cos(2 * math.radians(lat0))
    ke = 111412.84 * math.cos(math.radians(lat0))
    return (lon - lon0) * ke, (lat - lat0) * kn


def heading_and_speed(pos, att, min_speed=COG_MIN_SPEED, smooth_s=COG_SMOOTH_S):
    """(heading rad [0, 2pi), speed m/s, source 0 = course over ground / 1 = compass, vn, ve) per position."""
    t = pos["t"]
    e, n = enu(pos["lat"], pos["lon"], float(pos["lat"][0]), float(pos["lon"][0]))
    ve, vn = np.gradient(e, t), np.gradient(n, t)
    dt = float(np.median(np.diff(t))) if len(t) > 2 else 0.2
    w = max(1, int(round(smooth_s / max(dt, 1e-3))))
    if w > 1 and len(t) > w:
        k = np.ones(w) / w
        ve, vn = np.convolve(ve, k, "same"), np.convolve(vn, k, "same")
    spd = np.hypot(ve, vn)
    cog = np.mod(np.arctan2(ve, vn), 2 * math.pi)
    src = (spd < min_speed).astype(np.int8)
    if att is not None and len(att):
        yaw = np.unwrap(att["yaw"].astype(float))
        comp = np.mod(np.interp(t, att["t"], yaw), 2 * math.pi)
        hd = np.where(src == 1, comp, cog)
    else:
        hd, src = cog, np.zeros(len(t), np.int8)
    return hd, spd, src, vn, ve


# ----------------------------------------------------------------------------- mounting check
TILT_DIFF_WARN_DEG = 1.5       # the two heads' tilts differ by more than this: the strip under the boat is lopsided


def _mount_of(checks):
    """One head's mounting check over the recordings: the one with the most soundings."""
    return max(checks, key=lambda m: m.get("soundings", 0)) if checks else None


def mounting_summary(heads, levers=None):
    """The pair of heads in use looking to opposite sides: how far apart across the boat, the tilt difference and
    the seabed both see at the job's depth -- dict, or {} when there is no such pair. levers = {head: [F, S, D]}
    to use instead of the heads' own (what the Installation page shows now)."""
    from dcs_core import marine as dmarine
    side = {}
    for d, h in heads.items():
        m = h.get("mounting") or {}
        if m.get("looks") in ("port", "starboard") and m.get("looks") not in side:
            side[m["looks"]] = d
    if len(side) < 2:
        return {}
    P, S = heads[side["port"]], heads[side["starboard"]]
    mp, ms = P["mounting"], S["mounting"]
    lv = levers or {}
    lp = lv.get(side["port"]) or P.get("lever_m")
    ls = lv.get(side["starboard"]) or S.get("lever_m")
    spacing = abs(float(ls[1]) - float(lp[1])) if lp and ls else 0.0
    depths = [m["depth_m"] for m in (mp, ms) if m.get("depth_m")]
    depth = float(np.median(depths)) if depths else None
    w, meet = dmarine.shared_strip(mp["reach_deg"], ms["reach_deg"], spacing, depth)
    wn, meetn = dmarine.shared_strip(mp["nominal_reach_deg"], ms["nominal_reach_deg"], spacing, depth)
    diff = abs(mp["tilt_deg"] - ms["tilt_deg"])
    return {"port": side["port"], "starboard": side["starboard"], "spacing_m": round(spacing, 3),
            "depth_m": None if depth is None else round(depth, 2),
            "tilt_diff_deg": round(diff, 2), "tilt_diff_warn": diff > TILT_DIFF_WARN_DEG,
            "shared_m": None if w is None else round(w, 2), "meet_depth_m": None if meet is None else round(meet, 2),
            "shared_nominal_m": None if wn is None else round(wn, 2),
            "meet_depth_nominal_m": None if meetn is None else round(meetn, 2)}


def head_attitude(rec, head):
    """Each head's attitude for CloudPro and AlignPro: the sonar's own fused attitude (504) at every ping,
    as (t UTC, up[3], roll, pitch) -- plus the check against its raw IMU (505)."""
    A = rec.attitude(head)
    check = {}
    try:
        I = rec.imu(head)
        if len(I) > 100 and len(A) > 10:
            U = fuse_505(I)
            ux, uy, uz = (np.interp(A["t"], I["t"], U[:, k]) for k in range(3))
            dr = np.degrees(np.arctan2(uy, uz)) - A["roll"]
            dp = np.degrees(np.arcsin(np.clip(-ux, -1, 1))) - A["pitch"]
            check = {"imu_505_rate_hz": round((len(I) - 1) / float(I["t"][-1] - I["t"][0]), 1),
                     "fused_505_vs_504_roll_deg": [round(float(np.median(dr)), 3),
                                                   round(float(np.percentile(np.abs(dr - np.median(dr)), 68)), 3)],
                     "fused_505_vs_504_pitch_deg": [round(float(np.median(dp)), 3),
                                                    round(float(np.percentile(np.abs(dp - np.median(dp)), 68)), 3)]}
    except Exception:       # noqa: BLE001
        pass
    return A, check


# ------------------------------------------------------------------------------------------------- motion check
MOTION_LIMITS = {"heads_deg": 0.2, "turn_deg": 0.5, "offset_s": 0.1}


def _up_from_rp(roll, pitch):
    """World up expressed in a body frame (X forward, Y starboard, Z down) from roll / pitch (rad)."""
    return np.column_stack([np.sin(pitch), -np.sin(roll) * np.cos(pitch), -np.cos(roll) * np.cos(pitch)])


def _kabsch(Uh, Ua):
    """The rotation R that best takes the head's up vectors onto the boat's (Ua ~ Uh R^T)."""
    H = Uh.T @ Ua
    U_, _S, Vt = np.linalg.svd(H)
    D = np.diag([1.0, 1.0, np.sign(np.linalg.det(Vt.T @ U_.T))])
    return (U_ @ D @ Vt).T


def _rp_of(U):
    return np.arctan2(-U[:, 1], -U[:, 2]), np.arcsin(np.clip(U[:, 0], -1, 1))


def _p68(x, centre=None):
    """68 % of |x - centre| (centre: x's median)."""
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    if len(x) <= 10:
        return None
    c = float(np.median(x)) if centre is None else centre
    return round(float(np.percentile(np.abs(x - c), 68)), 3)


def motion_check(rec, head_atts, pos, vn, ve, cog_min=1.0):
    """How well the motion applied to the soundings holds up, for one recording (PosPro 2.9.0; Sean 17 Nov).
    head_atts {did: 504 attitude (t, up, ...)}; pos / vn / ve: the boat's positions and velocities (PPK when
    there is). Each head's up vector is rotated into the autopilot's body frame (its fixed mounting, best fit over
    the recording, with the time offset that fits best) and compared:
      heads_deg      the two heads against each other (68 %): sensors, rigid mounting;
      straight_deg / turning_deg   head against the autopilot (68 %) on straight lines / in turns;
      turn_slope_deg_per_ms2       roll difference per m/s2 of sideways acceleration (v x rate of turn): the
                                   heads' own attitude takes the boat's turning acceleration for tilt;
      worst_turn_deg               the larger of: that slope times this survey's largest sideways accelerations
                                   (p99), and the roll difference seen in its turns (p99);
      offset_s                     time offset of the heads' clock against the autopilot's;
      heading_offset_deg / heading_scatter_deg   autopilot heading minus course over the ground above cog_min.
    -> dict (empty when the autopilot has no attitude)."""
    V = rec.vehicle()
    att = V["att"]
    if len(att) < 50 or not head_atts or len(pos) < 20:
        return {}
    ta = att["t"]
    Ua = _up_from_rp(att["roll"].astype(float), att["pitch"].astype(float))
    yaw = np.unwrap(att["yaw"].astype(float))
    k = np.ones(13) / 13.0
    yawr = np.convolve(np.gradient(yaw, ta), k, "same")
    spd = np.hypot(vn, ve)
    spd_s = np.convolve(spd, np.ones(9) / 9.0, "same")
    along = np.gradient(spd_s, pos["t"])
    out = {"heads": {}}
    rotated = {}
    for did, A in head_atts.items():
        if len(A) < 50:
            continue
        tf = A["t"]
        Uf = np.array([np.asarray(u, float) for u in A["up"]])
        Uf /= np.maximum(np.linalg.norm(Uf, axis=1), 1e-9)[:, None]
        inside = (tf > ta[0] + 1.5) & (tf < ta[-1] - 1.5)
        tf, Uf = tf[inside], Uf[inside]
        if len(tf) < 50:
            continue
        step = max(1, len(tf) // 6000)                  # the fit on a subsample, the figures on every attitude
        ts, Us = tf[::step], Uf[::step]

        def fit(dt, th=ts, Uh=Us):
            Ud = np.column_stack([np.interp(th + dt, ta, Ua[:, j]) for j in range(3)])
            R = _kabsch(Uh, Ud)
            e = np.degrees(np.arccos(np.clip(np.sum((Uh @ R.T) * Ud, 1), -1, 1)))
            return float(np.median(e)), R, Ud
        coarse = min(np.arange(-1.0, 1.0001, 0.04), key=lambda d: fit(d)[0])
        dt = min(np.arange(coarse - 0.04, coarse + 0.0401, 0.005), key=lambda d: fit(d)[0])
        _m, R, _Ud = fit(dt)
        th, Uh = tf, Uf
        Ud = np.column_stack([np.interp(th + dt, ta, Ua[:, j]) for j in range(3)])
        Ub = Uh @ R.T
        rb, pb = _rp_of(Ub)
        ra, pa = _rp_of(Ud)
        droll, dpitch = np.degrees(rb - ra), np.degrees(pb - pa)
        tt = th + dt
        alat = np.interp(tt, pos["t"], spd_s) * np.interp(tt, ta, yawr)
        alon = np.interp(tt, pos["t"], along)
        turning = np.abs(alat) > max(0.03, float(np.percentile(np.abs(alat), 90)))
        straight = np.abs(alat) < 0.015
        ok = np.isfinite(alat) & np.isfinite(droll)
        slope = float(np.polyfit(alat[ok], droll[ok] - np.median(droll[ok]), 1)[0]) if ok.sum() > 50 else 0.0
        c0 = float(np.median(droll[straight])) if straight.sum() > 20 else float(np.median(droll))   # the mounting
        c0p = float(np.median(dpitch[straight])) if straight.sum() > 20 else float(np.median(dpitch))
        p99 = float(np.percentile(np.abs(alat[ok]), 99)) if ok.any() else 0.0
        out["heads"][str(did)] = {
            "offset_s": round(float(dt), 3),
            "straight_deg": _p68(droll[straight], c0) if straight.sum() > 20 else None,
            "turning_deg": _p68(droll[turning], c0) if turning.sum() > 20 else None,
            "pitch_deg": _p68(dpitch, c0p),
            "turn_slope_deg_per_ms2": round(slope, 2),
            "worst_turn_deg": round(max(abs(slope) * p99, float(np.percentile(np.abs(droll[turning] - c0), 99))
                                        if turning.sum() > 20 else 0.0), 2),
            "pitch_slope_deg_per_ms2": round(float(np.polyfit(alon[ok], dpitch[ok], 1)[0]), 2)
            if ok.sum() > 50 and float(np.std(alon[ok])) > 0.005 else None,
            "mount_rotation": R.round(6).tolist()}
        rotated[did] = (tt, Ub, droll)
    if len(rotated) >= 2:
        (d1, (t1, U1, _r1)), (d2, (t2, U2, _r2)) = list(rotated.items())[:2]
        U2i = np.column_stack([np.interp(t1, t2, U2[:, j]) for j in range(3)])
        ang = np.degrees(np.arccos(np.clip(np.sum(U1 * U2i, 1), -1, 1)))
        out["heads_deg"] = round(float(np.percentile(ang, 68)), 3)
        out["heads_p95_deg"] = round(float(np.percentile(ang, 95)), 3)
    cog = np.degrees(np.arctan2(ve, vn))
    yaw_p = np.degrees(np.interp(pos["t"], ta, yaw))
    mv = spd > max(1.0, float(cog_min))
    if mv.sum() > 20:
        d = (yaw_p[mv] - cog[mv] + 180.0) % 360.0 - 180.0
        off = float(np.degrees(np.angle(np.mean(np.exp(1j * np.radians(d))))))
        res = (d - off + 180.0) % 360.0 - 180.0
        out["heading_offset_deg"] = round(off, 2)
        out["heading_scatter_deg"] = round(float(np.percentile(np.abs(res), 68)), 2)
    out["sideways_acc_p99_ms2"] = round(float(np.percentile(np.abs(np.interp(pos["t"], ta, yawr) * spd_s), 99)), 3)
    out["autopilot_clock_rms_s"] = V["boot_fit"][2]
    # a time series for the plot: head-against-autopilot roll difference of the first head, and turning or not
    if rotated:
        tt, _Ub, dr = next(iter(rotated.values()))
        n = max(1, len(tt) // 1500)
        al = np.interp(tt, pos["t"], spd_s) * np.interp(tt, ta, yawr)
        out["series"] = {"t": tt[::n].round(2).tolist(), "droll_deg": (dr - np.median(dr))[::n].round(3).tolist(),
                         "turning": (np.abs(al[::n]) > 0.03).astype(int).tolist()}
    return out


def motion_summary(per_rec):
    """The survey's motion check from each recording's: the worst of each figure, and the verdicts against
    MOTION_LIMITS -> {"heads_deg", "worst_turn_deg", "offset_s", "heading_offset_deg", "heading_scatter_deg",
    "straight_deg", "turning_deg", "flags": [...], "recordings": {...}}."""
    per_rec = {k: v for k, v in per_rec.items() if v}
    if not per_rec:
        return {}

    def worst(key, sub=False):
        vals = []
        for m in per_rec.values():
            src = m.get("heads", {}).values() if sub else [m]
            vals += [abs(x[key]) for x in src if x.get(key) is not None]
        return round(max(vals), 3) if vals else None
    s = {"heads_deg": worst("heads_deg"), "straight_deg": worst("straight_deg", True),
         "turning_deg": worst("turning_deg", True), "worst_turn_deg": worst("worst_turn_deg", True),
         "turn_slope_deg_per_ms2": worst("turn_slope_deg_per_ms2", True), "offset_s": worst("offset_s", True),
         "heading_offset_deg": worst("heading_offset_deg"), "heading_scatter_deg": worst("heading_scatter_deg"),
         "recordings": {k: {kk: vv for kk, vv in v.items() if kk != "series"} for k, v in per_rec.items()}}
    L = MOTION_LIMITS
    flags = []
    if s["heads_deg"] is not None and s["heads_deg"] > L["heads_deg"]:
        flags.append("the heads disagree by %.2f deg" % s["heads_deg"])
    if s["worst_turn_deg"] is not None and s["worst_turn_deg"] > L["turn_deg"]:
        flags.append("roll is off by up to %.2f deg in turns" % s["worst_turn_deg"])
    if s["offset_s"] is not None and s["offset_s"] > L["offset_s"]:
        flags.append("the heads' clock is %.2f s off the autopilot's" % s["offset_s"])
    s["flags"] = flags
    return s


def fuse_505(I, tau=0.5):
    """The world-up vector from a head's raw IMU (505): gyro-propagated, pulled towards the accelerometer with
    time constant tau (s). Only a check of the sonar's own attitude: no gyro-bias estimate, so it drifts with a
    longer tau (13 Nov 2026: tau 0.5 s agrees with 504 to ~0.08 deg, 68 %)."""
    t = I["t"]
    a = I["acc"].astype(float)
    g = np.radians(I["gyro"].astype(float))
    out = np.empty((len(t), 3))
    u = a[0] / np.linalg.norm(a[0])
    for i in range(len(t)):
        dt = t[i] - t[i - 1] if i else 0.0
        an = a[i] / max(np.linalg.norm(a[i]), 1e-9)
        if not 0 < dt <= 0.5:
            u = an
        else:
            u = u - np.cross(g[i], u) * dt
            k = dt / (tau + dt)
            u = (1 - k) * u + k * an
            u /= np.linalg.norm(u)
        out[i] = u
    return out


# ----------------------------------------------------------------------------- engine trajectory
GPS_MINUS_UNIX = 315964800.0 - 18.0          # UTC (Unix s) -> GPS seconds since 1980-01-06: t - GPS_MINUS_UNIX
STATUS_OF_Q = {1: 6, 2: 5, 3: 4, 4: 3, 5: 2, 6: 4, 9: 2}      # POS quality -> engine PPST (fix 6, float 5, DGPS 3...)


def _att_at(T, t):
    """Vessel roll, pitch, heading (rad) and velocity N / E at times t (UTC) from the processed trajectory T."""
    roll = np.interp(t, T["t"], T["roll"])
    pitch = np.interp(t, T["t"], T["pitch"])
    hs = np.interp(t, T["t"], np.sin(T["heading"]))
    hc = np.interp(t, T["t"], np.cos(T["heading"]))
    hsd = np.where(np.interp(t, T["t"], T["hsrc"].astype(float)) > 0.5, math.radians(2.0), math.radians(1.0))
    return roll, pitch, np.arctan2(hs, hc), np.interp(t, T["t"], T["vn"]), np.interp(t, T["t"], T["ve"]), hsd


def write_pass(path, P, T, direction=1):
    """A position pass (POS: t UTC, lat / lon deg, h, q, sd) with the vessel attitude as an engine trajectory pass
    (PPTRJ001, the record the engine's combine reads)."""
    from .editor import _DT
    P = np.sort(P, order="t")
    n = len(P)
    out = np.zeros(n, dtype=_DT)
    roll, pitch, hdg, vn, ve, hsd = _att_at(T, P["t"])
    out["t"] = P["t"] - GPS_MINUS_UNIX
    out["lat"], out["lon"], out["h"] = np.radians(P["lat"]), np.radians(P["lon"]), P["h"]
    out["vel"] = np.column_stack([vn, ve, np.zeros(n)]).astype(np.float32)
    out["att"] = np.column_stack([roll, pitch, hdg]).astype(np.float32)
    out["sdp"] = np.column_stack([P["sdn"], P["sde"], P["sdu"]]).astype(np.float32)
    rest = np.zeros((n, 18), np.float32)
    rest[:, 0:3] = 0.05                                        # velocity sigma m/s
    rest[:, 3:6] = np.column_stack([np.full(n, math.radians(0.5)), np.full(n, math.radians(0.5)), hsd])
    out["rest"] = rest
    lut = np.zeros(16, np.uint8)
    for q, st in STATUS_OF_Q.items():
        lut[q] = st
    st = lut[np.clip(P["q"].astype(int), 0, 15)]
    out["status"] = st
    good = (st == 6) | (st == 5)
    last = np.maximum.accumulate(np.where(good, np.arange(n), -1)) if n else np.zeros(0, np.int64)
    out["gap"] = np.where(last >= 0, np.maximum(0.0, out["t"] - out["t"][np.maximum(last, 0)]), 0.0)
    if direction < 0:
        out = out[::-1]                         # backward passes are stored latest first (the engine reverses them)
    with open(path + ".tmp", "wb") as fo:
        fo.write(b"PPTRJ001" + struct.pack("<Q", n) + struct.pack("<3d", 0, 0, 0) + struct.pack("<ii", 0, direction)
                 + b"\0" * 24)
        out.tofile(fo)
    _replace(path + ".tmp", path)
    return n


def prefer_fixed(F, B):
    """Where one pass is fixed and the other is not at the same epoch, leave out the worse one (fixed > float >
    others), so the combination never pulls a fixed position toward a float one; both fixed, or both float, are
    combined by their sigmas (and the separation is measured there)."""
    rank = lambda a: np.where(a["q"] == 1, 0, np.where(a["q"] == 2, 1, 2))      # noqa: E731
    kf, kb = np.round(F["t"] * 20).astype(np.int64), np.round(B["t"] * 20).astype(np.int64)
    _c, i_f, i_b = np.intersect1d(kf, kb, return_indices=True)
    rf, rb = rank(F)[i_f], rank(B)[i_b]
    drop_f = np.zeros(len(F), bool)
    drop_b = np.zeros(len(B), bool)
    drop_f[i_f[rf > rb]] = True
    drop_b[i_b[rb > rf]] = True
    return F[~drop_f], B[~drop_b]


def engine_trajectory(project, T, passes, ctx):
    """results/trajectory.bin + qc.csv from the passes (PPK forward / backward of every recording, or the one
    position source) with the engine's combination, as a mobile mapping GNSS-only run. -> combine results."""
    from . import engine, pipeline
    idir = project.wpath("ins", "x")[:-2]
    rdir = project.wpath("results", "x")[:-2]
    args = ["combine", "--out", os.path.join(rdir, "trajectory.bin"), "--qc", os.path.join(rdir, "qc.csv")]
    keys = ("fwd", "bwd") if passes["both"] and passes["bwd"] else ("fwd",)
    P = {k: np.sort(np.hstack(passes[k]), order="t") for k in keys}
    if len(keys) == 2:
        P["fwd"], P["bwd"] = prefer_fixed(P["fwd"], P["bwd"])
    for k in keys:
        f = os.path.join(idir, "marine_%s.bin" % k)
        write_pass(f, P[k], T, 1 if k == "fwd" else -1)
        args += ["--" + k, f]
    r = pipeline._check(engine.run(args, ctx.progress if ctx else None, ctx.log if ctx else None,
                                   ctx.cancelled if ctx else None), "Combining the passes")
    return r.results


# ----------------------------------------------------------------------------- process and finalize
def process(project, ctx, atx=None):
    """Trajectory of every marine recording of the job -> work/marine/trajectory.npz + summary (cfg results)."""
    from dcs_core.marine import SonarRecording
    opts = options(project)
    recs = recordings(project.dcs)
    if not recs:
        raise MarineError("the job has no sonar recordings (.clraw made by RemixPro)")
    spans = {}
    for paths in recs:
        r = SonarRecording(paths)
        spans[r.name] = r.span()
    ov = overlaps(spans)
    if ov:
        raise MarineError("recordings cover the same time: %s. Untick one of each on the Data page (PosPro would "
                          "put the boat in two places at once)" % "; ".join(
                              "%s and %s (%.0f s)" % (a, b, d) for a, b, d in ov))
    wdir = project.wpath("marine", "x")[:-2]
    parts, summary, heads = [], {"recordings": [], "notes": []}, {}
    motion = {}
    passes = {"fwd": [], "bwd": [], "both": True}
    t_start = time.time()
    from . import pipeline as _pl
    navs_extra = []
    if selected_base(project) is not None:          # the Data page's ephemeris, as for mobile mapping
        try:
            navs_extra = _pl.nav_files(project, ctx or _pl.Ctx(log=lambda *_: None))
        except Exception as e:      # noqa: BLE001 - the recordings' own navigation files are still there
            summary["notes"].append("ephemeris: %s (the recordings' own navigation files are used)" % e)
    crop = None
    rc = project.cfg["rover"]
    if rc.get("time_start") or rc.get("time_end"):  # the Data page's time range (GPS s) -> UTC
        span = project.rover_span()
        if span:
            crop = (span[0] + GPS_MINUS_UNIX, span[1] + GPS_MINUS_UNIX)
    for i, paths in enumerate(recs):
        rec = SonarRecording(paths)
        sub = ctx.sub(i / len(recs), (i + 0.8) / len(recs)) if ctx else None
        if crop and (rec.span()[1] < crop[0] or rec.span()[0] > crop[1]):
            continue                                # outside the Data page's time range
        pos, src, info, notes = choose_positions(rec, os.path.join(wdir, rec.name), opts, sub, atx,
                                                 base=base_for(project, rec), navs_extra=navs_extra,
                                                 proc=ppk_proc(project),
                                                 forward_only=project.cfg["processing"].get("passes") == "forward")
        if src == "ppk" and (info.get("base") or {}).get("approximate"):
            notes.append("PPK against a base position from its RINEX header (approximate): every position is "
                         "off by the same metres - enter the base mark or run PPP on the Base page")
        f_, b_ = info.pop("_fwd", None), info.pop("_bwd", None)
        if crop:
            cut = lambda a: a if a is None else a[(a["t"] >= crop[0]) & (a["t"] <= crop[1])]     # noqa: E731
            pos, f_, b_ = cut(pos), cut(f_), cut(b_)
            if not len(pos):
                continue
        if f_ is not None and b_ is not None and len(f_) and len(b_):
            passes["fwd"].append(f_)
            passes["bwd"].append(b_)
        else:
            passes["fwd"].append(pos)
            passes["both"] = False
        att = rec.vehicle()["att"]
        hd, spd, hsrc, vn, ve = heading_and_speed(pos, att, float(opts["cog_min_speed"]))
        roll = np.interp(pos["t"], att["t"], att["roll"]) if len(att) else np.zeros(len(pos))
        pitch = np.interp(pos["t"], att["t"], att["pitch"]) if len(att) else np.zeros(len(pos))
        parts.append(np.rec.fromarrays([pos["t"], pos["lat"], pos["lon"], pos["h"], pos["q"], pos["sdn"], pos["sde"],
                                        pos["sdu"], vn, ve, roll, pitch, hd, hsrc, spd],
                                       names="t,lat,lon,h,q,sdn,sde,sdu,vn,ve,roll,pitch,heading,hsrc,speed"))
        rec_atts = {}
        for did in rec.heads:
            if str(did) in (opts.get("heads_off") or []):  # switched off on the Installation page
                continue
            A, check = head_attitude(rec, did)
            rec_atts[did] = A
            info_h = dict(rec.heads[did])
            ent = (opts.get("levers") or {}).get(str(did))
            if ent:                                       # typed on the Installation page
                info_h.update(lever_m=[float(v) for v in ent], lever_source="entered in PosPro")
            heads.setdefault(did, {"parts": [], "checks": [], "info": info_h, "mounting": []})
            heads[did]["parts"].append(A)
            heads[did]["checks"].append(check)
            try:
                from dcs_core import marine as dmarine
                m = dmarine.mounting_check(rec, did, every=10, attitude=A)
            except Exception:       # noqa: BLE001 - a check only: never stops the processing
                m = None
            if m:
                heads[did]["mounting"].append(m)
        try:
            motion[rec.name] = motion_check(rec, rec_atts, pos, vn, ve, float(opts["cog_min_speed"]))
        except Exception as e:      # noqa: BLE001 - a check only: never stops the processing
            summary["notes"].append("%s: motion check not made (%s)" % (rec.name, e))
        q = {Q_NAMES.get(int(k), str(int(k))): round(100.0 * float((pos["q"] == k).mean()), 1) for k in np.unique(pos["q"])}
        summary["recordings"].append({"name": rec.name, "source": src, "source_label": SOURCES[src], "epochs": len(pos),
                                      "quality_pct": q, "rate_hz": rate_hz(pos["t"]), "compass_pct": round(100.0 * float(hsrc.mean()), 1),
                                      "span": [float(pos["t"][0]), float(pos["t"][-1])], "info": _jsonable(info),
                                      "notes": notes, "mavlink_boot_fit_rms_s": rec.vehicle()["boot_fit"][2]})
        summary["notes"] += ["%s: %s" % (rec.name, n) for n in notes]
    if not parts:
        raise MarineError("no recording inside the Data page's time range")
    T = np.hstack(parts)
    T.sort(order="t")
    np.save(os.path.join(wdir, "trajectory.npy"), T)
    for did, h in heads.items():
        A = np.hstack(h["parts"])
        A.sort(order="t")
        np.save(os.path.join(wdir, "sonar_%d_attitude.npy" % did), A)
        h["n"] = len(A)
    summary["heads"] = {str(d): {"nickname": h["info"].get("nickname"), "lever_m": h["info"].get("lever_m"),
                                 "lever_source": h["info"].get("lever_source"), "looks": h["info"].get("looks"),
                                 "attitudes": h["n"], "mounting": _mount_of(h["mounting"]),
                                 "check": h["checks"][0] if h["checks"] else {}} for d, h in heads.items()}
    summary["mounting"] = mounting_summary(summary["heads"])
    summary["motion"] = motion_summary(motion)
    series = {k: v.get("series") for k, v in motion.items() if v and v.get("series")}
    if series:
        import json as _json
        with open(os.path.join(wdir, "motion_series.json"), "w") as f:
            _json.dump(series, f)
    summary["position_source"] = sorted({r["source"] for r in summary["recordings"]})
    summary["fixed_pct"] = round(100.0 * float((T["q"] == 1).mean()), 1)
    summary["compass_pct"] = round(100.0 * float(T["hsrc"].mean()), 1)
    summary["cog_min_speed"] = float(opts["cog_min_speed"])
    summary["settings"] = settings_key(opts, project)
    # the same results as a mobile mapping GNSS-only run: results/trajectory.bin + qc.csv from the engine's
    # combination, so Quality, Trajectory Review (and its editor) and Export are the mobile mapping pages
    from . import pipeline
    project.cfg["rover"]["marine_span"] = [float(T["t"][0]) - GPS_MINUS_UNIX, float(T["t"][-1]) - GPS_MINUS_UNIX]
    project.cfg["processing"]["mode"] = "gnss"      # no IMU: the results read as a GNSS-only run
    comb = engine_trajectory(project, T, passes, ctx)
    project.cfg["results"] = {"started": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t_start)),
                              "mode": "gnss", "positioning": "+".join(summary["position_source"]),
                              "passes": "both" if passes["both"] else "forward",
                              "combine": comb, "marine": summary}
    base = next((r["info"].get("base") for r in summary["recordings"] if r["source"] == "ppk"), None)
    fu = {"name": (base or {}).get("frame") or ("NAD83(CSRS)" if base else "as the GNSS receiver gave it"),
          "epoch": (base or {}).get("epoch")}
    project.cfg["results"]["frame_used"] = fu
    project.cfg["results"]["frame"] = ("%s %s" % (fu["name"], fu["epoch"] or "")).strip()
    pipeline._finish_review(project, ctx or pipeline.Ctx(log=lambda *_: None))
    project.cfg["results"]["elapsed_s"] = round(time.time() - t_start, 1)
    project.cfg["results"]["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    project.dirty = True
    if ctx:
        ctx.progress(1.0)
    return summary


def _jsonable(o):
    return json.loads(json.dumps(o, default=lambda x: x.item() if hasattr(x, "item") else str(x)))


def write_sbet(T, path, leap=18):
    """SBET (17 doubles) + SMRMSG at the GNSS antenna; time = GPS seconds of week. -> (sbet, smrmsg, week)."""
    from dcs_core import sbet as dsbet
    gps = T["t"] + leap
    week = int((gps[0] - dsbet.GPS_EPOCH_UNIX) // dsbet.WEEK)
    sow = gps - dsbet.week_start(week)
    S = np.zeros(len(T), dsbet.SBET)
    S["t"] = sow
    S["lat"], S["lon"], S["h"] = np.radians(T["lat"]), np.radians(T["lon"]), T["h"]
    S["vx"], S["vy"] = T["vn"], T["ve"]
    S["roll"], S["pitch"], S["heading"] = T["roll"], T["pitch"], T["heading"]
    S.tofile(path)
    M = np.zeros(len(T), dsbet.SMRMSG)
    M["t"] = sow
    M["sd_n"], M["sd_e"], M["sd_d"] = T["sdn"], T["sde"], T["sdu"]
    hsd = np.where(T["hsrc"] == 1, 2.0, np.degrees(np.arctan2(np.hypot(T["sdn"], T["sde"]) * 2,
                                                              np.maximum(T["speed"] * 2.0, 0.1))))
    M["sd_roll"] = M["sd_pitch"] = 30.0
    M["sd_heading"] = np.minimum(hsd, 30.0) * 60.0
    import re
    smr = os.path.join(os.path.dirname(path), re.sub(r"(?i)sbet", "smrmsg", os.path.basename(path)))
    M.tofile(smr)
    return path, smr, week


def _quality(project, summ):
    """The handover's quality block from the published qc (Trajectory Review's 'poor' raises the sigmas)."""
    from . import results
    comb = (project.cfg.get("results") or {}).get("combine") or {}
    qc = results.load_qc(os.path.join(project.work_dir, "results", "qc.csv"))

    def num(k):
        try:
            return float(comb.get(k))
        except (TypeError, ValueError):
            return None
    return {"mode": "gnss", "positioning": "+".join(summ["position_source"]), "fixed_pct": summ["fixed_pct"],
            "sep_h_p95_m": num("sep_h_p95_m"),
            "max_sd_h_m": round(max(qc["sd_h"]), 4) if qc else None,
            "max_sd_v_m": round(max(qc["sd_d"]), 4) if qc else None}


def publish(project, ctx):
    """Finalize the marine trajectory: SBET + SMRMSG at the GNSS antenna, each head's attitude, trajectory.json
    (dcs.trajectory/1 with the marine and sensors sections)."""
    from dcs_core import handover
    from . import pipeline
    summ = (project.cfg.get("results") or {}).get("marine")
    wdir = os.path.join(project.work_dir, "marine")
    tf = os.path.join(wdir, "trajectory.npy")
    trj = os.path.join(project.work_dir, "results", "trajectory.bin")
    if not summ or not os.path.exists(tf) or not os.path.exists(trj):
        raise MarineError("process the marine trajectory first")
    T = np.load(tf)
    od = project.out_dir
    os.makedirs(od, exist_ok=True)
    try:
        os.remove(os.path.join(od, "trajectory.json"))
    except OSError:
        pass
    tmp = os.path.join(wdir, "handover")
    os.makedirs(tmp, exist_ok=True)
    for f in os.listdir(tmp):
        try:
            os.unlink(os.path.join(tmp, f))
        except OSError:
            pass
    # SBET / SMRMSG from the processed trajectory as Trajectory Review left it (the mobile mapping export)
    sub = ctx.sub(0, 0.8) if ctx else pipeline.Ctx(log=lambda *_: None)
    written = pipeline.export(project, sub, tmp, rate_hz=0, lever=[0.0, 0.0, 0.0], sbet=True)
    files = {"sbet": os.path.join(od, "trajectory_sbet.out"), "smrmsg": os.path.join(od, "trajectory_smrmsg.out"),
             "qc": os.path.join(od, "trajectory_qc.csv")}
    _replace(written[0], files["sbet"])
    _replace(written[1], files["smrmsg"])
    shutil.copyfile(os.path.join(project.work_dir, "results", "qc.csv"), files["qc"] + ".tmp")
    _replace(files["qc"] + ".tmp", files["qc"])
    from dcs_core import sbet as dsbet
    week = int((T["t"][0] + 18 - dsbet.GPS_EPOCH_UNIX) // dsbet.WEEK)
    sensors = {}
    for did in summ.get("heads", {}):
        src = os.path.join(wdir, "sonar_%s_attitude.npy" % did)
        dst = os.path.join(od, "sonar_%s_attitude.npy" % did)
        shutil.copyfile(src, dst + ".tmp")
        _replace(dst + ".tmp", dst)
        files["sonar_%s_attitude" % did] = dst
        h = summ["heads"][did]
        sensors["sonar_%s" % did] = {"attitude": os.path.basename(dst), "time": "UTC seconds",
                                     "format": "numpy .npy: t (UTC s), up[3] (Cerulean's world-up vector in the sonar's "
                                               "frame), roll, pitch (deg)",
                                     "source": "the sonar's own fused attitude (packet 504) at every ping",
                                     "nickname": h.get("nickname"), "lever_m": h.get("lever_m"),
                                     "lever_source": h.get("lever_source"), "looks": h.get("looks"),
                                     "mounting": h.get("mounting"), "check": h.get("check")}
    recs = summ["recordings"]
    base = next((r["info"].get("base") for r in recs if r["source"] == "ppk"), None)
    ep = (base or {}).get("epoch")
    try:
        ep = float(ep) if ep not in (None, "") else None
    except (TypeError, ValueError):
        pass
    frame = {"name": (base or {}).get("frame") or ("NAD83(CSRS)" if base else "as the GNSS receiver gave it"),
             "epoch": ep, "heights": "ellipsoidal"}
    if base and not frame["epoch"] and "NAD83" in (frame["name"] or "").upper():
        frame["epoch"] = 2010.0
    span = (float(T["t"][0]), float(T["t"][-1]))
    rate = recs[0].get("rate_hz") if recs else None
    fields = {
        "time": {"system": "GPS", "week": week, "leap_seconds": 18, "start_gps": span[0] + 18, "end_gps": span[1] + 18,
                 "sbet_time": "GPS seconds of week"},
        "frame": frame,
        "platform": "marine",
        "reference_point": {"name": "GNSS antenna reference point (ARP) of the position receiver",
                            "offset_from_imu_m": [0.0, 0.0, 0.0]},
        "body_frame": "vessel: X forward, Y starboard, Z down; roll + starboard down, pitch + bow up, heading + "
                      "clockwise (roll / pitch from the autopilot; heading per marine.heading)",
        "levers_m": {"gnss1": {"value": [0.0, 0.0, 0.0], "source": "reference point"}},
        "rate_hz": rate,
        "quality": _quality(project, summ),
        "review": pipeline.review_summary(project),
        "recording_holes": pipeline.recording_holes(
            files["sbet"], [(a - week * dsbet.WEEK, b - week * dsbet.WEEK) for a, b, _n in recording_runs(project)],
            week, 18),
        "base_station": "%s (%s)" % ("surveyed base", base.get("source")) if base else None,
        "marine": {"position": [{"recording": r["name"], "source": r["source_label"], "quality_pct": r["quality_pct"]}
                                for r in recs],
                   "motion": {k: v for k, v in (summ.get("motion") or {}).items() if k != "recordings"},
                   "heading": {"course_over_ground_above_m_s": summ["cog_min_speed"],
                               "below": "autopilot compass (MAVLink ATTITUDE yaw)", "compass_pct": summ["compass_pct"]},
                   "vessel_attitude": "autopilot (MAVLink ATTITUDE)", "notes": summ.get("notes", []),
                   "mounting": summ.get("mounting") or {}},
        "sensors": sensors,
        "attitude": "vessel roll / pitch from the autopilot; each sonar's own attitude in sensors",
    }
    doc = handover.publish(project.dcs, "pospro", files, fields, "PosPro", pipeline.plugin_version(), "")
    if ctx:
        ctx.log("info", "marine trajectory finalized for AlignPro and CloudPro: %s" % os.path.join(od, "trajectory.json"))
    return {"id": doc["id"], "path": os.path.join(od, "trajectory.json")}


# ----------------------------------------------------------------------------- SonarView recordings added on the Data page
SONARVIEW = (".svlz", ".svlog")


def convert_sonarview(dcs, paths, ctx):
    """SonarView recordings chosen on the Data page (PosPro 2.11.0) -> their .clraw, made by RemixPro (DCS Core
    registry service "sonar_to_clraw") into the job's day folder. -> {"files", "notes", "failed"}.
    Raises MarineError when RemixPro is not loaded (or is older than 1.4.0)."""
    from dcs_core import registry
    fn = registry.service("sonar_to_clraw") if hasattr(registry, "service") else None
    if fn is None:
        raise MarineError("SonarView recordings are converted by RemixPro 1.4.0 or later: install or turn on "
                          "RemixPro (Plugins > Manage and Install Plugins), then add them again.")
    try:
        out_root = dcs.remix_dir(create=True)
    except Exception:       # noqa: BLE001  (older DCS Core)
        out_root = None
    from . import engine
    try:
        return fn(list(paths), job_folder=dcs.job_folder if getattr(dcs, "layout", 1) >= 2 else None,
                  out_root=out_root, progress=lambda f, t="": ctx.progress(f, t), cancelled=ctx.cancelled)
    except Exception as e:      # noqa: BLE001
        if type(e).__name__ == "Cancelled":
            raise engine.Cancelled() from e
        raise
