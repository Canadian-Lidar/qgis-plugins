"""Processing steps. Every function takes the project and a context with
progress(frac, text), log(level, text) and cancelled(); none touches the GUI,
so the same code runs in a QGIS background task and in the tests.
"""
import glob
import json
import math
import os
import shutil
import time

from . import engine, geo, products, rinex


class Ctx:
    """Context passed to the steps (console version used by the tests)."""

    def __init__(self, progress=None, log=None, cancelled=None):
        self._p = progress
        self._l = log
        self._c = cancelled
        self.lo, self.hi = 0.0, 1.0

    def sub(self, lo, hi):
        c = Ctx(self._p, self._l, self._c)
        c.lo = self.lo + (self.hi - self.lo) * lo
        c.hi = self.lo + (self.hi - self.lo) * hi
        return c

    def progress(self, f, text=""):
        if self._p:
            self._p(self.lo + (self.hi - self.lo) * max(0.0, min(1.0, f)), text)

    def log(self, level, text):
        if self._l:
            self._l(level, text)

    def cancelled(self):
        return bool(self._c and self._c())


class StepError(Exception):
    pass


def _check(res, what):
    if not res.ok():
        raise StepError("%s failed: %s" % (what, res.message()))
    return res


def _sig(paths):
    out = []
    for p in paths:
        try:
            st = os.stat(p)
            out.append([os.path.abspath(p), st.st_size, int(st.st_mtime)])
        except OSError:
            out.append([os.path.abspath(p), -1, 0])
    return out


# ---------------------------------------------------------------------------
# 1. rover data
# ---------------------------------------------------------------------------
def extract(project, ctx, force=False):
    files = [f for f in project.cfg["rover"]["files"] if f]
    if not files:
        raise StepError("add the rover files (.anpp or .clraw) first")
    for f in files:
        if not os.path.exists(f):
            raise StepError("rover file not found: %s" % f)
    sig = _sig(files)
    out = os.path.join(project.work_dir, "extract")
    okf = os.path.join(out, "ok.json")
    if not force and project.cfg["rover"].get("extract_files") == sig and _extract_ok(okf, sig):
        return project.cfg["rover"]["extract"]
    os.makedirs(out, exist_ok=True)
    for old in glob.glob(os.path.join(out, "*")):
        try:
            os.remove(old)
        except OSError:
            pass
    with open(os.path.join(out, ".reading"), "w") as fh:   # marks an unfinished read
        fh.write("1")
    ctx.log("info", "reading %d rover file(s)" % len(files))
    res = _check(engine.run(["extract", "--out", out] + sorted(files, key=_in_time_order),
                            ctx.progress, ctx.log, ctx.cancelled), "Reading the rover files")
    project.cfg["rover"]["extract"] = res.results
    project.cfg["rover"]["extract_files"] = sig
    with open(okf, "w", encoding="utf-8") as fh:      # written last: a cancelled or failed read has none
        json.dump({"sig": sig}, fh)
    os.remove(os.path.join(out, ".reading"))
    hw = os.path.join(out, "clraw_hardware.json")
    if os.path.exists(hw):
        try:
            with open(hw, "r", encoding="utf-8") as fh:
                project.cfg["rover"]["hardware"] = json.load(fh)
        except (OSError, ValueError):
            pass
    _complete_hardware(project, files)
    project.dirty = True
    return res.results


def _extract_ok(okf, sig):
    try:
        with open(okf, "r", encoding="utf-8") as fh:
            return json.load(fh).get("sig") == json.loads(json.dumps(sig))
    except (OSError, ValueError, AttributeError):
        # extracts from before 2.0.1 have no marker: trusted when imu.bin is there (as before)
        return not os.path.exists(okf) and os.path.exists(os.path.join(os.path.dirname(okf), "imu.bin")) \
            and not os.path.exists(os.path.join(os.path.dirname(okf), ".reading"))


LINE_ONLY_KEYS = ("vehicle", "ins_to_rear_axle_m", "coordinate_system")


def _complete_hardware(project, files):
    """Mapper 2.10 navigation recordings carry a shorter hardware.json (no vehicle, rear axle or coordinate
    system). When the rover files are navigation recordings, take those from a survey line of the same job."""
    try:
        from dcs_core import clraw
        if not any(f.lower().endswith(".clraw") and clraw.is_navigation(f) for f in files):
            return
        hw = project.cfg["rover"].get("hardware") or {}
        if all(k in hw for k in LINE_ONLY_KEYS):
            return
        lines = []
        if project.dcs is not None:
            lines = project.dcs.survey_clraw_files()
        for f in lines[:5]:
            lhw = clraw.ClrawFile(f).hardware
            if lhw:
                for k in LINE_ONLY_KEYS:
                    if k not in hw and k in lhw:
                        hw[k] = lhw[k]
                hw.setdefault("_completed_from", os.path.basename(f))
                project.cfg["rover"]["hardware"] = hw
                return
    except Exception:           # noqa: BLE001 - optional: the offsets PosPro needs are in the navigation file
        return


def _natural(p):
    import re
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", os.path.basename(p))]


def _in_time_order(p):
    """Rover files in recording order: .clraw by the time of their first data (names alone no longer sort in
    time since mapper 2.10, where old NNN_date_time and new <Project>_L001_... names can mix), then by name."""
    t = None
    if p.lower().endswith(".clraw"):
        try:
            from dcs_core import clraw
            t = clraw.file_info(p).get("start")
        except Exception:       # noqa: BLE001 - unreadable header: name order
            t = None
    return (t if t is not None else float("inf"), _natural(p))


def hardware_levers(hw):
    """Look for antenna / Lidar offsets in a mapper hardware.json."""
    found = {}

    def walk(o, path):
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, path + [str(k).lower()])
        elif isinstance(o, (list, tuple)) and len(o) == 3 and all(isinstance(x, (int, float)) for x in o):
            key = "/".join(path)
            if "antenna" in key and ("secondary" in key or "gps2" in key):
                found.setdefault("antenna2", (key, list(o)))
            elif "antenna" in key and ("primary" in key or "gps1" in key or "offset" in key or "lever" in key):
                found.setdefault("antenna", (key, list(o)))
            elif "lidar" in key and ("offset" in key or "lever" in key or "_m" in key):
                found.setdefault("lidar", (key, list(o)))
    walk(hw, [])
    return found


def _vec3(v):
    if isinstance(v, str):
        v = v.replace(";", ",").split(",")
    try:
        out = [float(x) for x in v]
    except (TypeError, ValueError):
        return None
    return out if len(out) == 3 else None


def capture_offsets(project):
    """Offsets recorded with the capture: the mapper's hardware.json first
    (same frame as PosPro: Certus reference point, X forward, Y starboard,
    Z down, metres), then what the Certus itself reported (packet 185).

    Returns {"antenna", "antenna2", "lidar", "axle": [x,y,z] or None,
    "alignment": [roll, pitch, heading] or None, "vehicle": {...} or None,
    "certus_antenna": [x,y,z] or None, "source": text}."""
    hw = project.cfg["rover"].get("hardware") or {}
    ex = project.cfg["rover"].get("extract") or {}
    out = {"antenna": _vec3(hw.get("ins_to_primary_antenna_m")),
           "antenna2": _vec3(hw.get("ins_to_secondary_antenna_m")),
           "lidar": _vec3(hw.get("ins_to_lidar_m")),
           "axle": _vec3(hw.get("ins_to_rear_axle_m")),
           "alignment": None, "vehicle": hw.get("vehicle") if isinstance(hw.get("vehicle"), dict) else None,
           "certus_antenna": _vec3(ex.get("certus_antenna_offset")), "certus_antenna2": None,
           "certus_dual": ex.get("certus_dual_mode") or "", "source": ""}
    man = _vec3(ex.get("certus_dual_offset"))
    if out["certus_antenna"] is not None and man is not None and ex.get("certus_dual_mode") == "manual":
        out["certus_antenna2"] = [round(a + b, 4) for a, b in zip(out["certus_antenna"], man)]
    al = hw.get("certus_alignment_deg")
    if isinstance(al, dict):
        out["alignment"] = [float(al.get(k, 0) or 0) for k in ("roll", "pitch", "heading")]
    elif _vec3(al):
        out["alignment"] = _vec3(al)
    elif _vec3(ex.get("certus_alignment_rph")):
        out["alignment"] = _vec3(ex.get("certus_alignment_rph"))
    if hw and any(out[k] is None for k in ("antenna", "lidar")):
        # older captures name the offsets differently: look for anything offset-like, then the Certus
        found = hardware_levers(hw)
        for k in ("antenna", "antenna2", "lidar"):
            if out[k] is None and k in found:
                out[k] = found[k][1]
    srcs = []
    if out["antenna"] is not None:
        srcs.append("mapper hardware.json")
    elif out["certus_antenna"] is not None:
        out["antenna"] = list(out["certus_antenna"])
        srcs.append("Certus installation (packet 185)")
        if out["antenna2"] is None and out["certus_antenna2"] is not None:
            out["antenna2"] = list(out["certus_antenna2"])
            srcs.append("Certus dual antenna (packet 196)")
    out["source"] = " + ".join(srcs)
    return out


def _rt_attitude(path):
    """(t, roll, pitch, heading) radians from rt.bin (the Certus's own
    attitude, vehicle frame)."""
    from .results import read_rt_array
    rt = read_rt_array(path)
    if rt is None or not len(rt):
        return []
    a = rt["att"].astype(float)
    return list(zip(rt["t"].tolist(), a[:, 0].tolist(), a[:, 1].tolist(), a[:, 2].tolist()))


def baseline_solutions(project, ctx):
    """GPS1 -> GPS2 carrier-phase baseline from the recorded data (RTKLIB moving base, no base station
    needed): [(gps time, east, north, up, quality)], quality 1 = fixed. Also the paths of the inputs."""
    ex = os.path.join(project.work_dir, "extract")
    r1, r2, rt = (os.path.join(ex, f) for f in ("rover.obs", "rover_rx2.obs", "rt.bin"))
    for f, what in ((r1, "GPS1 observations"), (r2, "GPS2 observations (Certus receiver 1)"),
                    (rt, "Certus attitude (packet 20)")):
        if not os.path.exists(f) or os.path.getsize(f) < 2000:
            raise StepError("the capture has no %s; read the rover files first" % what)
    navs = nav_files(project, ctx.sub(0, 0.1))
    gdir = project.wpath("gnss", "x")[:-2]
    conf = os.path.join(gdir, "baseline.conf")
    proc = project.cfg["processing"]
    with open(conf, "w", encoding="utf-8") as fh:
        fh.write("\n".join([
            "pos1-posmode=movingbase", "pos1-frequency=l1+l2", "pos1-elmask=15", "pos1-dynamics=off",
            "pos1-ionoopt=brdc", "pos1-tropopt=saas", "pos1-sateph=brdc",
            "pos1-navsys=%d" % navsys(proc.get("systems", "GREJ")),
            "pos2-armode=continuous", "pos2-arthres=3", "pos2-arlockcnt=10", "pos2-minfixsats=4",
            "pos2-slipthres=0.05", "stats-eratio1=300", "stats-eratio2=300", "stats-errphase=0.003",
            "misc-timeinterp=off", "out-outvel=off"]) + "\n")
    out = os.path.join(gdir, "baseline.pos")
    span = project.rover_span()
    args = ["gnss", "-k", conf, "-o", out, "-d", "fwd", "--enu"]
    if span:
        args += ["--ts", "%.3f" % span[0], "--te", "%.3f" % span[1]]
    _check(engine.run(args + [r2, r1] + navs, ctx.sub(0.1, 0.85).progress, ctx.log, ctx.cancelled),
           "GPS1-GPS2 baseline")
    sols = []
    with open(out) as fh:
        for line in fh:
            if line.startswith("%"):
                continue
            v = line.split()
            try:
                t = int(v[0]) * 604800.0 + float(v[1])
                e, n, u, q = float(v[2]), float(v[3]), float(v[4]), int(v[5])
            except (ValueError, IndexError):
                continue
            sols.append((t, e, n, u, q))
    if not sols:
        raise StepError("no GPS1-GPS2 baseline solutions (are both antennas tracking the same satellites?)")
    return sols, rt


def measure_antenna2(project, ctx):
    """GPS2's offset from the recorded data: the carrier-phase baseline from
    GPS1 to GPS2 (RTKLIB moving base, fixed epochs only), turned into the
    vehicle frame with the Certus's attitude and added to GPS1's offset.
    Works parked as well as driving. Returns {"value", "sd", "length",
    "epochs", "fixed_pct"}."""
    sols, rt = baseline_solutions(project, ctx)
    fixed = [s for s in sols if s[4] == 1]
    if len(fixed) < 20:
        raise StepError("the GPS1-GPS2 baseline did not fix (%d of %d epochs); try a longer recording in the "
                        "open" % (len(fixed), len(sols)))
    att = _rt_attitude(rt)
    if not att:
        raise StepError("the rover files hold no Certus attitude (packet 20) to turn the baseline with")
    import bisect
    ts = [a[0] for a in att]
    vecs = []
    for t, e, n, u, _q in fixed:
        k = bisect.bisect_left(ts, t)
        k = min(max(k, 0), len(att) - 1)
        if k > 0 and abs(ts[k - 1] - t) < abs(ts[k] - t):
            k -= 1
        if abs(ts[k] - t) > 0.1:
            continue
        _t, rl, pt, hd = att[k]
        ned = (n, e, -u)
        # body = C_bn * ned, C_nb = Rz(h) Ry(p) Rx(r)
        cr, sr, cp, sp, ch, sh = math.cos(rl), math.sin(rl), math.cos(pt), math.sin(pt), math.cos(hd), math.sin(hd)
        cnb = [[cp * ch, sr * sp * ch - cr * sh, cr * sp * ch + sr * sh],
               [cp * sh, sr * sp * sh + cr * ch, cr * sp * sh - sr * ch],
               [-sp, sr * cp, cr * cp]]
        vecs.append([sum(cnb[i][j] * ned[i] for i in range(3)) for j in range(3)])
    if len(vecs) < 20:
        raise StepError("the baseline epochs do not line up with the Certus attitude")
    m = len(vecs)
    mean = [sorted(v[j] for v in vecs)[m // 2] for j in range(3)]          # median, robust
    sd = [math.sqrt(sum((v[j] - mean[j]) ** 2 for v in vecs) / max(1, m - 1)) for j in range(3)]
    gps1 = project.cfg["installation"]["lever_antenna"]
    return {"value": [round(gps1[j] + mean[j], 4) for j in range(3)], "baseline": [round(x, 4) for x in mean],
            "sd": [round(x, 4) for x in sd], "length": round(math.sqrt(sum(x * x for x in mean)), 4),
            "epochs": m, "fixed_pct": round(100.0 * len(fixed) / len(sols), 1)}


# ---------------------------------------------------------------------------
# 2. ephemeris
# ---------------------------------------------------------------------------
def nav_files(project, ctx, need_precise=False):
    """Navigation files for processing (worked out once per job: every step asks). The INS's own ephemeris (rover.nav)
    and the IGS broadcast product are trusted; every other file (next to the
    base observations, or added on the Data page) is checked orbit by orbit
    against them first (engine navcheck) and only the orbits that agree are
    used."""
    key = json.dumps([project.rover_span(), project.cfg.get("nav_files"), project.cfg.get("auto_ephemeris", True),
                      (project.base() or {}).get("files"), project.cfg["rover"].get("extract_files")], default=str)
    memo = getattr(project, "_navs", None)
    if memo and memo[0] == key and all(os.path.exists(f) for f in memo[1]):
        return list(memo[1])
    out = _nav_files(project, ctx)
    project._navs = (key, list(out))
    return out


def _nav_files(project, ctx):
    trusted, screened = [], []
    ex = os.path.join(project.work_dir, "extract", "rover.nav")
    navdir = project.wpath("nav", "x")[:-2]
    for f in project.cfg.get("nav_files") or []:
        if os.path.exists(f):
            screened.append(rinex.decompress_to(f, navdir))
    span = project.rover_span()
    if project.cfg.get("auto_ephemeris", True) and span:
        for day in products.days_between(span[0] - 60, span[1] + 60):
            try:
                trusted.append(products.brdc(day, lambda m: ctx.log("info", m)))
            except products.DownloadError as e:
                ctx.log("warning", "broadcast ephemeris for %s not downloaded (%s); using the files "
                                   "you supplied and the rover's own GPS ephemeris" % (day, str(e).splitlines()[0]))
    # navigation files next to the base observations
    b = project.base()
    if b:
        for f in b.get("files", []):
            d = os.path.dirname(f)
            stem = os.path.basename(f)[:4].lower()
            for cand in glob.glob(os.path.join(d, "*")):
                lc = os.path.basename(cand).lower()
                if cand == f or not lc.startswith(stem):
                    continue
                if lc.endswith(("n", "g", "l", "p")) and lc[-3:-1].isdigit() or "_mn." in lc or lc.endswith("nav"):
                    screened.append(rinex.decompress_to(cand, navdir))
    if os.path.exists(ex):
        trusted.insert(0, ex)
    screened = _unique([f for f in screened if f not in trusted])
    if screened and trusted:
        screened = check_nav_files(project, screened, trusted, ctx)
    out = _unique(trusted + screened)
    if not out:
        raise StepError("no navigation (ephemeris) data: connect to the internet or add a RINEX "
                        "navigation file on the Data page")
    return out


def _unique(files):
    seen, out = set(), []
    for f in files:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def check_nav_files(project, files, refs, ctx):
    """Engine navcheck: each file's orbits against the trusted ones. Returns
    the checked copies to use (a file that cannot be read is left out)."""
    odir = project.wpath("nav", "checked", "x")[:-2]
    os.makedirs(odir, exist_ok=True)
    args = ["navcheck", "--out", odir]
    for r in refs:
        args += ["--ref", r]
    res = engine.run(args + files, None, ctx.log, ctx.cancelled)
    if not res.ok():
        ctx.log("warning", "navigation files could not be checked (%s); they are used as they are"
                % (res.message() or "engine error"))
        return files
    out = []
    for k, f in enumerate(files):
        o = res.results.get("nav%d_out" % k)
        name = os.path.basename(f)
        if o is None:
            out.append(f)
            continue
        if not o:
            ctx.log("warning", "navigation file %s could not be read and is not used" % name)
            continue
        out.append(o)
        nd = int(res.results.get("nav%d_dropped" % k, "0") or 0)
        nc = int(res.results.get("nav%d_checked" % k, "0") or 0)
        if nd:
            ctx.log("warning", "navigation file %s: %d orbit%s disagree%s with the INS's own and the IGS "
                               "broadcast orbits (by up to %s m) and %s left out (%s). If this keeps happening, "
                               "remove the file from the base folder." %
                    (name, nd, "" if nd == 1 else "s", "s" if nd == 1 else "", res.results.get("nav%d_worst_m" % k, "?"),
                     "is" if nd == 1 else "are", res.results.get("nav%d_dropped_sats" % k, "")))
        else:
            ctx.log("info", "navigation file %s checked: %d orbit%s agree with the reference orbits"
                    % (name, nc, "" if nc == 1 else "s"))
    return out


# ---------------------------------------------------------------------------
# 3. base station observations and position
# ---------------------------------------------------------------------------
import re as _re
_R2_EPOCH = _re.compile(r"^ [ \d]\d [ \d]\d [ \d]\d [ \d]\d [ \d]\d [ \d]\d\.\d{7}  [0-6][ \d]{3}")


def base_obs_file(project, base, ctx):
    """One plain RINEX observation file covering the rover span (merged if
    the base came as several files)."""
    files = [f for f in base.get("files", []) if os.path.exists(f)]
    if not files:
        raise StepError("the base station %s has no observation file" % base.get("name", ""))
    plain = []
    for f in files:
        wdir = project.wpath("base", base["id"], "x")[:-2]
        p = rinex.decompress_to(f, wdir)
        if rinex.is_hatanaka(p):
            if os.path.dirname(os.path.abspath(p)) != os.path.abspath(wdir):
                q = os.path.join(wdir, os.path.basename(p))
                shutil.copyfile(p, q)
                p = q
            p = products.crx2rnx(p)
        plain.append(p)
    if len(plain) == 1:
        return plain[0]
    # concatenate bodies of RINEX files that have the same observation types;
    # epochs already written (overlap between files) are skipped
    def types(path):
        out = []
        with open(path, "r", encoding="ascii", errors="replace") as fh:
            for line in fh:
                if "TYPES OF OBS" in line[60:] or "OBS TYPES" in line[60:] or "RINEX VERSION" in line[60:]:
                    out.append(line[:60].rstrip() if "RINEX VERSION" not in line[60:] else line[:9].strip())
                if "END OF HEADER" in line:
                    break
        return out
    def first_epoch(path):
        try:
            f = rinex.header_info(path, scan_body=False).get("first")
            return f.isoformat() if f else ""
        except Exception:       # noqa: BLE001
            return ""
    # in time order (by the first observation, not the path: files from different folders)
    plain = sorted(plain, key=lambda f: (first_epoch(f), f))
    t0 = types(plain[0])
    for p in plain[1:]:
        if types(p) != t0:
            raise StepError("the base files %s and %s list different observation types; "
                            "add them as separate base stations" % (os.path.basename(plain[0]), os.path.basename(p)))
    v3 = t0 and t0[0].startswith("3")
    out = project.wpath("base", base["id"], "merged.obs")
    last = ""
    with open(out, "w", encoding="ascii", errors="replace") as fo:
        for i, p in enumerate(plain):
            with open(p, "r", encoding="ascii", errors="replace") as fi:
                in_header = True
                skip = False
                for line in fi:
                    if in_header:
                        if i == 0:
                            fo.write(line)
                        if "END OF HEADER" in line:
                            in_header = False
                        continue
                    stamp = None
                    if v3 and line.startswith(">"):
                        stamp = line[2:29]
                    elif not v3 and _R2_EPOCH.match(line):
                        yy = int(line[1:3])
                        stamp = "%04d%s" % (yy + (1900 if yy >= 80 else 2000), line[3:26])
                    if stamp is not None:
                        skip = stamp <= last        # epochs already written (files that overlap)
                        if not skip:
                            last = stamp
                    if not skip:
                        fo.write(line)
    return out


def base_position(base):
    """Marker position (ECEF) used for processing, or None."""
    xyz = base.get("xyz")
    if xyz and len(xyz) == 3 and sum(v * v for v in xyz) > 1e12:
        return tuple(float(v) for v in xyz)
    return None


def ppp_base(project, base, ctx, frame_epoch=None):
    """Static PPP of a base station with downloaded precise products."""
    obs = base_obs_file(project, base, ctx)
    info = rinex.header_info(obs)
    if not info.get("first"):
        raise StepError("cannot read the time of the base station file")
    first_gps = (info["first"] - geo.GPS_EPOCH).total_seconds()     # RINEX times are GPS time
    last_gps = (info["last"] - geo.GPS_EPOCH).total_seconds() if info.get("last") else first_gps + 3600
    files = []
    sp3s, clks, acs = [], [], []
    for day in products.days_between(first_gps, last_gps):
        sp3, clk, ac = products.precise(day, lambda m: ctx.log("info", m))
        sp3s.append(sp3); clks.append(clk); acs.append(ac)
        try:
            files.append(products.brdc(day, lambda m: ctx.log("info", m)))
        except products.DownloadError:
            pass
    atx = products.antex(lambda m: ctx.log("info", m))
    conf = project.wpath("base", base["id"], "ppp.conf")
    with open(conf, "w", encoding="utf-8") as fh:
        fh.write(rtklib_conf_ppp(static=True, atx=atx, sp3_ok=True))
    out = project.wpath("base", base["id"], "ppp.pos")
    res = _check(engine.run(["gnss", "-k", conf, "-o", out, "-d", "fwd", obs] + sp3s + clks + files,
                            ctx.progress, ctx.log, ctx.cancelled), "PPP of the base station")
    try:
        xyz = [float(v) for v in res.results["last_xyz"].split(",")]
        sd = [float(v) for v in res.results["last_sd"].split(",")]
    except (KeyError, ValueError):
        raise StepError("PPP produced no solution")
    dur = last_gps - first_gps
    mid = geo.gps_to_utc((first_gps + last_gps) / 2)
    epoch = geo.decimal_year(mid)
    result = {"itrf_xyz": xyz, "sd": sd, "epoch": round(epoch, 4), "products": sorted(set(acs)),
              "duration_h": round(dur / 3600.0, 2), "when": time.strftime("%Y-%m-%d %H:%M")}
    if dur < 2 * 3600:
        ctx.log("warning", "the base file is only %.1f h long: PPP is usually good to a few cm after 2 h "
                           "and around 1 cm after 24 h" % (dur / 3600.0))
    return result


# ---------------------------------------------------------------------------
# 4. GNSS-only processing (RTKLIB)
# ---------------------------------------------------------------------------
SYS_MASK = {"G": 1, "R": 4, "E": 8, "J": 16, "C": 32}


def navsys(systems):
    return sum(SYS_MASK.get(c, 0) for c in set(systems))


def rtklib_conf_ppk(proc, atx=None, delta_hen=None, antenna=""):
    nf = int(proc.get("frequencies", 2))
    lines = [
        "pos1-posmode=kinematic",
        "pos1-frequency=%s" % ("l1+l2" if nf >= 2 else "l1"),
        "pos1-elmask=%g" % float(proc.get("elmask", 12)),
        "pos1-dynamics=on",
        "pos1-tidecorr=off",
        "pos1-ionoopt=brdc",
        "pos1-tropopt=saas",
        "pos1-sateph=brdc",
        "pos1-navsys=%d" % navsys(proc.get("systems", "GREJ")),
        "pos2-armode=%s" % ("continuous" if proc.get("ar", True) else "off"),
        "pos2-gloarmode=%s" % ("autocal" if proc.get("glonass_ar") else "off"),
        "pos2-arthres=%g" % float(proc.get("ar_ratio", 3.0)),
        "pos2-arlockcnt=20",
        "pos2-arelmask=15",
        "pos2-minfixsats=4",
        "pos2-arfilter=on",
        "pos2-slipthres=0.05",
        "stats-eratio1=300",
        "stats-eratio2=300",
        "stats-errphase=0.003",
        "stats-errphaseel=0.003",
        "misc-timeinterp=on",
        "out-outvel=on",
    ]
    # the base antenna height and type as kept in the project (the user may
    # have corrected the RINEX header), the same values tight coupling uses
    if atx and antenna:
        lines += ["file-rcvantfile=%s" % atx, "ant2-anttype=%s" % antenna, "pos1-posopt2=on"]
    if delta_hen:
        lines += ["ant2-antdele=%.4f" % delta_hen[1], "ant2-antdeln=%.4f" % delta_hen[2],
                  "ant2-antdelu=%.4f" % delta_hen[0]]
    lines += ["%s=%s" % (k, v) for k, v in (proc.get("advanced") or {}).items() if k.startswith(("pos", "stats", "misc", "ant"))]
    return "\n".join(lines) + "\n"


def rtklib_conf_ppp(static, atx=None, sp3_ok=True, proc=None):
    proc = proc or {}
    lines = [
        "pos1-posmode=%s" % ("ppp-static" if static else "ppp-kine"),
        "pos1-frequency=l1+l2",
        "pos1-elmask=%g" % float(proc.get("elmask", 10)),
        "pos1-tidecorr=on",
        "pos1-ionoopt=dual-freq",
        "pos1-tropopt=est-ztdgrad",
        "pos1-sateph=precise",
        "pos1-posopt1=on",
        "pos1-posopt2=on",
        "pos1-posopt3=on",
        "pos1-posopt4=on",
        "pos1-posopt5=on",
        "pos1-navsys=%d" % navsys(proc.get("systems", "GRE")),
        "pos2-armode=off",
        "stats-eratio1=100",
        "stats-eratio2=100",
        "out-outvel=on",
    ]
    if atx:
        lines += ["file-satantfile=%s" % atx, "file-rcvantfile=%s" % atx, "ant1-anttype=*"]
    return "\n".join(lines) + "\n"


def gnss(project, ctx):
    """RTKLIB forward and backward GNSS solutions (used by loose coupling)."""
    proc = project.cfg["processing"]
    rover = os.path.join(project.work_dir, "extract", "rover.obs")
    if not os.path.exists(rover):
        raise StepError("no rover observations were found in the rover files (packet 60)")
    navs = nav_files(project, ctx.sub(0, 0.05))
    span = project.rover_span()
    gdir = project.wpath("gnss", "x")[:-2]
    atx = None
    try:
        atx = products.antex(lambda m: ctx.log("info", m))
    except products.DownloadError:
        ctx.log("warning", "antenna calibrations (igs20.atx) not available; base antenna phase "
                           "centre variations are not applied")
    args_common = []
    if span:
        args_common += ["--ts", "%.3f" % (span[0] - 5), "--te", "%.3f" % (span[1] + 5)]
    if proc.get("positioning") == "ppp":
        sp3s, clks = [], []
        for day in products.days_between(span[0], span[1]):
            sp3, clk, _ = products.precise(day, lambda m: ctx.log("info", m))
            sp3s.append(sp3); clks.append(clk)
        conf = os.path.join(gdir, "ppp.conf")
        with open(conf, "w", encoding="utf-8") as fh:
            fh.write(rtklib_conf_ppp(False, atx, proc=proc))
        inputs = [rover] + sp3s + clks + navs
    else:
        b = project.base()
        if not b:
            raise StepError("choose a base station (or select PPP on the Processing page)")
        bxyz = base_position(b)
        if not bxyz:
            raise StepError("the base station %s has no position yet" % b.get("name", ""))
        bobs = base_obs_file(project, b, ctx)
        conf = os.path.join(gdir, "ppk.conf")
        with open(conf, "w", encoding="utf-8") as fh:
            fh.write(rtklib_conf_ppk(proc, atx, b.get("delta_hen") or
                                     rinex.header_info(bobs, scan_body=False).get("delta_hen"),
                                     (b.get("antenna") or "").strip()))
        args_common += ["--base"] + ["%.4f" % v for v in bxyz]
        inputs = [rover, bobs] + navs
    jobs = [("GNSS forward", ["gnss", "-k", conf, "-o", os.path.join(gdir, "fwd.pos"), "-d", "fwd"] + args_common + inputs)]
    if proc.get("passes", "both") == "both":
        jobs.append(("GNSS backward", ["gnss", "-k", conf, "-o", os.path.join(gdir, "bwd.pos"), "-d", "bwd"] + args_common + inputs))
    sigf = os.path.join(gdir, "inputs.json")
    if os.path.exists(sigf):
        os.remove(sigf)
    res = engine.run_parallel(jobs, ctx.sub(0.05, 1).progress, ctx.log, ctx.cancelled)
    summary = {}
    for label, r in res.items():
        _check(r, label)
        summary[label] = r.results
    with open(sigf, "w", encoding="utf-8") as fh:
        json.dump({"sig": _gnss_sig(project)}, fh)
    return summary


def _gnss_sig(project):
    """What the GNSS-only solutions depend on, so a later tight-coupled run uses them only when they still
    match (base, its position, positioning, time crop, rover data and the GNSS settings)."""
    c = project.cfg
    b = project.base() or {}
    proc = {k: v for k, v in c["processing"].items() if k not in ("mode", "passes")}
    return json.dumps([proc.get("positioning"), b.get("id"), base_position(b) if b else None, b.get("files"),
                       project.rover_span(), c["rover"].get("extract_files"), proc], sort_keys=True, default=str)


def _gnss_current(project):
    f = os.path.join(project.work_dir, "gnss", "inputs.json")
    try:
        with open(f, "r", encoding="utf-8") as fh:
            return json.load(fh).get("sig") == _gnss_sig(project)
    except (OSError, ValueError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# 5. inertial passes and combination
# ---------------------------------------------------------------------------
def antenna2_obs(project):
    """The secondary antenna's observations when they were recorded and its
    offset is known (tight coupling uses both antennas)."""
    inst = project.cfg["installation"]
    f = os.path.join(project.work_dir, "extract", "rover_rx2.obs")
    if inst.get("use_antenna2", True) and any(abs(v) > 1e-6 for v in inst.get("lever_antenna2") or [0, 0, 0]) \
            and os.path.exists(f) and os.path.getsize(f) > 2000:
        return f
    return None


def ins_conf(project, direction, navs, bobs=None, bxyz=None, base=None, atx=None, solve_levers=False):
    p = project.cfg["processing"]
    inst = project.cfg["installation"]
    veh = project.vehicle_profile()
    imu = project.imu_params()
    ex = os.path.join(project.work_dir, "extract")
    gdir = os.path.join(project.work_dir, "gnss")
    idir = project.wpath("ins", "x")[:-2]
    span = project.rover_span()
    mode = "tc" if p.get("mode") == "tc" else "lc"
    L = []
    w = L.append
    w("mode = %s" % mode)
    w("direction = %d" % direction)
    w("file.imu = %s" % os.path.join(ex, "imu.bin"))
    w("file.rt = %s" % os.path.join(ex, "rt.bin"))
    if solve_levers:
        w("file.out = %s" % os.path.join(idir, "lever_fwd.bin" if direction > 0 else "lever_bwd.bin"))
    else:
        w("file.out = %s" % os.path.join(idir, "fwd.bin" if direction > 0 else "bwd.bin"))
    if mode == "lc" or _gnss_current(project):
        w("file.gnss_fwd = %s" % os.path.join(gdir, "fwd.pos"))
        w("file.gnss_bwd = %s" % os.path.join(gdir, "bwd.pos" if p.get("passes", "both") == "both" else "fwd.pos"))
    # tight coupling with older GNSS-only solutions (another base or base position, crop or settings) would
    # seed the alignment from wrong positions at a 5 cm sigma: they are left out and the filter starts from SPP
    w("file.rover_obs = %s" % os.path.join(ex, "rover.obs"))
    if bobs:
        w("file.base_obs = %s" % bobs)
    for n in navs:
        w("file.nav = %s" % n)
    if atx:
        w("file.atx = %s" % atx)
    if bxyz:
        w("base.xyz = %.4f, %.4f, %.4f" % tuple(bxyz))
    if base is not None:
        w("base.anttype = %s" % (base.get("antenna") or ""))
        dh = base.get("delta_hen") or [0, 0, 0]
        w("base.antdel_enu = %.4f, %.4f, %.4f" % (dh[1], dh[2], dh[0]))
    if span:
        w("time.start = %.3f" % span[0])
        w("time.end = %.3f" % span[1])
    from . import review as _review
    if _review.exclusions(project):
        w("gnss.exclude = %s" % _review.engine_exclude_value(project))
    for k, v in imu.items():
        w("imu.%s = %s" % (k, v))
    w("imu.time_offset_s = %.6f" % (float(inst.get("time_offset_ms", 0)) / 1000.0))
    w("lever.antenna = %.4f, %.4f, %.4f" % tuple(inst["lever_antenna"]))
    # the trajectory is always made at the IMU centre; Export moves it to the chosen point
    w("lever.output = 0, 0, 0")
    w("lever.nhc = %.4f, %.4f, %.4f" % tuple(inst["lever_nhc"]))
    if mode == "tc":
        a2 = antenna2_obs(project)
        if a2:
            w("file.rover2_obs = %s" % a2)
            w("lever.antenna2 = %.4f, %.4f, %.4f" % tuple(inst["lever_antenna2"]))
        atm = p.get("atmosphere", "auto")
        w("tc.iono = %s" % atm)
        w("tc.tropo = %s" % atm)
        if not p.get("snr_weighting", True):
            w("tc.snr_err_m = 0")
        if solve_levers:
            w("lever.estimate = on")
    w("nhc.enable = %d" % (1 if (p.get("nhc", True) and veh["nhc"]) else 0))
    w("nhc.sigma_m_s = %g" % float(p.get("nhc_sigma") or veh["nhc_sigma"]))
    md = inst.get("mount_deg") or [0.0, 0.0]
    w("nhc.mount_deg = %.4f, %.4f" % (float(md[0]), float(md[1])))
    if inst.get("mount_mode") == "fixed":
        w("nhc.estimate_mount = 0")
    elif abs(float(md[0])) + abs(float(md[1])) > 0:
        w("nhc.mount_sd_deg = 1")        # starting from a known value: it may still adjust
    w("zupt.enable = %d" % (1 if (p.get("zupt", True) and veh["zupt"]) else 0))
    w("gnss.systems = %s" % p.get("systems", "GREJ"))
    w("gnss.frequencies = %d" % int(p.get("frequencies", 2)))
    w("gnss.elmask_deg = %g" % float(p.get("elmask", 12)))
    w("tc.ar = %d" % (1 if p.get("ar", True) else 0))
    w("tc.ar_ratio = %g" % float(p.get("ar_ratio", 3.0)))
    w("tc.glonass_ar = %d" % (1 if p.get("glonass_ar") else 0))
    w("tc.max_base_interval_s = %g" % float(p.get("base_max_dt", 60)))
    w("lc.use_float = %d" % (1 if p.get("lc_use_float", True) else 0))
    w("lc.use_dgps = %d" % (1 if p.get("lc_use_dgps", True) else 0))
    w("gate.chi2 = %g" % float(p.get("gate_chi2", 9.0)))
    w("align.mode = %d" % int(p.get("align_mode", 0)))
    w("align.speed_m_s = %g" % float(p.get("align_speed", 4.0)))
    rate = float(p.get("output_rate_hz", 0) or 0)
    try:
        imu_rate = float(project.cfg["rover"]["extract"].get("imu_rate_hz", 200))
    except (TypeError, ValueError):
        imu_rate = 200.0
    w("output.decimation = %d" % (max(1, int(round(imu_rate / rate))) if rate > 0 else 1))
    for k, v in (p.get("advanced") or {}).items():
        if not k.startswith(("pos", "stats", "misc", "ant")):
            w("%s = %s" % (k, v))
    path = os.path.join(idir, "ins_fwd.conf" if direction > 0 else "ins_bwd.conf")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    return path


def ins(project, ctx):
    p = project.cfg["processing"]
    navs = nav_files(project, ctx.sub(0, 0.02))
    bobs = bxyz = b = atx = None
    if p.get("mode") == "tc":
        b = project.base()
        if not b:
            raise StepError("tight coupling needs a base station; add one or choose loose coupling with PPP")
        bxyz = base_position(b)
        if not bxyz:
            raise StepError("the base station %s has no position yet" % b.get("name", ""))
        bobs = base_obs_file(project, b, ctx)
        try:
            atx = products.antex()
        except products.DownloadError:
            atx = None
    if p.get("mode") == "tc" and p.get("positioning") == "ppp":
        raise StepError("tight coupling needs a base station: choose loose coupling for PPP")
    jobs = [("Forward pass", ["ins", "-k", ins_conf(project, 1, navs, bobs, bxyz, b, atx)])]
    if p.get("passes", "both") == "both":
        jobs.append(("Backward pass", ["ins", "-k", ins_conf(project, -1, navs, bobs, bxyz, b, atx)]))
    res = engine.run_parallel(jobs, ctx.sub(0.02, 1).progress, ctx.log, ctx.cancelled)
    out = {}
    for label, r in res.items():
        _check(r, label)
        out[label] = r.results
    _report_mount(out, ctx)
    return out


def mount_offset(results):
    """Vehicle frame against the IMU's aligned frame, from the passes:
    (pitch, yaw, pitch_sd, yaw_sd) in degrees, variance-weighted, or None."""
    vals = []
    for r in (results or {}).values():
        try:
            p_, y_, sp, sy = [float(v) for v in str(r.get("vehicle_mount_offset_deg", "")).split(",")]
        except (TypeError, ValueError, AttributeError):
            continue
        vals.append((p_, y_, max(sp, 1e-3), max(sy, 1e-3)))
    if not vals:
        return None
    out = []
    for k in (0, 1):
        w = [1.0 / v[2 + k] ** 2 for v in vals]
        out.append(sum(wi * v[k] for wi, v in zip(w, vals)) / sum(w))
    for k in (0, 1):
        out.append((1.0 / sum(1.0 / v[2 + k] ** 2 for v in vals)) ** 0.5)
    return tuple(out)


def _report_mount(results, ctx):
    m = mount_offset(results)
    if not m:
        return
    pitch, yaw, sp, sy = m
    ctx.log("info", "vehicle frame against the IMU: pitch %.2f deg (+/- %.2f), yaw %.2f deg (+/- %.2f), "
                    "estimated while driving" % (pitch, sp, yaw, sy))
    if abs(pitch) > 1.5 or (abs(yaw) > 1.5 and sy < 0.3):
        ctx.log("warning", "the IMU's aligned frame is %.1f deg (pitch) / %.1f deg (yaw) off the vehicle: the "
                           "INS alignment set in the Certus does not match how it is mounted. PosPro allows for it; "
                           "correcting the alignment in the Certus improves its real-time solution too." % (pitch, yaw))



def solve_levers(project, ctx):
    """Antenna lever arms estimated by the tightly coupled filter, forward and
    backward, combined by their variances."""
    p = project.cfg["processing"]
    if p.get("mode") != "tc":
        raise StepError("the lever-arm solver needs tight coupling (Processing)")
    navs = nav_files(project, ctx.sub(0, 0.02))
    b = project.base()
    if not b:
        raise StepError("the lever-arm solver needs a base station")
    bxyz = base_position(b)
    if not bxyz:
        raise StepError("the base station %s has no position yet" % b.get("name", ""))
    bobs = base_obs_file(project, b, ctx)
    try:
        atx = products.antex()
    except products.DownloadError:
        atx = None
    jobs = [("Forward pass", ["ins", "-k", ins_conf(project, 1, navs, bobs, bxyz, b, atx, solve_levers=True)]),
            ("Backward pass", ["ins", "-k", ins_conf(project, -1, navs, bobs, bxyz, b, atx, solve_levers=True)])]
    res = engine.run_parallel(jobs, ctx.sub(0.02, 1).progress, ctx.log, ctx.cancelled)
    out = {"passes": {}}
    for label, r in res.items():
        _check(r, label)
        out["passes"][label] = r.results

    def vec(s):
        try:
            return [float(x) for x in str(s).split(",")]
        except ValueError:
            return None
    for key in ("lever_antenna", "lever_antenna2"):
        vals, sds = [], []
        prior = None
        for label, rr in out["passes"].items():
            v, sd = vec(rr.get(key)), vec(rr.get(key + "_sd"))
            prior = prior or vec(rr.get(key + "_prior"))
            if v and sd and len(v) == 3 and len(sd) == 3:
                vals.append(v)
                sds.append(sd)
        if not vals:
            continue
        est, sd = [], []
        for i in range(3):
            w_ = [1.0 / max(s[i], 1e-4) ** 2 for s in sds]
            est.append(sum(wi * v[i] for wi, v in zip(w_, vals)) / sum(w_))
            # the two passes use the same data: do not shrink the uncertainty
            sd.append(min(s[i] for s in sds))
        spread = [abs(vals[0][i] - vals[-1][i]) for i in range(3)] if len(vals) > 1 else [0, 0, 0]
        out[key] = {"value": est, "sd": sd, "prior": prior, "fwd_bwd_diff": spread}
    if "lever_antenna" not in out:
        raise StepError("the filter did not report lever arms (no tightly coupled updates?)")
    out["finished"] = time.strftime("%Y-%m-%d %H:%M")
    return out


def gnss_trajectory(project, ctx):
    """GNSS-only: the forward/backward RTKLIB solutions as trajectory passes,
    then the usual combination (trajectory.bin + qc.csv)."""
    gdir = os.path.join(project.work_dir, "gnss")
    idir = project.wpath("ins", "x")[:-2]
    rdir = project.wpath("results", "x")[:-2]
    args = ["combine", "--out", os.path.join(rdir, "trajectory.bin"), "--qc", os.path.join(rdir, "qc.csv")]
    for d in ("fwd", "bwd"):
        pos = os.path.join(gdir, d + ".pos")
        if d == "bwd" and project.cfg["processing"].get("passes", "both") != "both":
            continue
        if not os.path.exists(pos):
            continue
        out = os.path.join(idir, "gnss_%s.bin" % d)
        _check(engine.run(["pos2trj", "--pos", pos, "--out", out, "--dir", d], None, ctx.log, ctx.cancelled),
               "GNSS %s solution" % d)
        args += ["--" + d, out]
    if len(args) < 6:
        raise StepError("the GNSS processing produced no solutions")
    r = _check(engine.run(args, ctx.progress, ctx.log, ctx.cancelled), "Combining the passes")
    return r.results


def combine(project, ctx):
    idir = os.path.join(project.work_dir, "ins")
    rdir = project.wpath("results", "x")[:-2]
    args = ["combine", "--out", os.path.join(rdir, "trajectory.bin"), "--qc", os.path.join(rdir, "qc.csv"),
            "--fwd", os.path.join(idir, "fwd.bin")]
    if project.cfg["processing"].get("passes", "both") == "both":
        args += ["--bwd", os.path.join(idir, "bwd.bin")]
    r = _check(engine.run(args, ctx.progress, ctx.log, ctx.cancelled), "Combining the passes")
    check_runaway(r.results, ctx)
    return r.results


RUNAWAY_SD_M = 1000.0       # any σ above this: the solution diverged, stop
LARGE_SD_M = 10.0           # above this: warn


def check_runaway(results, ctx):
    """Stop with a clear error instead of handing over a trajectory whose
    solution ran away (field test 2026-11-05: σ 540 km from a bad
    navigation file)."""
    try:
        sd = max(float(results.get("max_sd_h_m") or 0), float(results.get("max_sd_v_m") or 0))
    except ValueError:
        return
    if sd > RUNAWAY_SD_M:
        raise StepError(
            "the solution ran away: its σ reached %.0f km, so this trajectory cannot be used. The usual "
            "causes are a bad navigation file next to the base observations (see the warnings above), a "
            "wrong base position, or IMU data that did not arrive in full. Fix the cause and process "
            "again; Trajectory Review cannot repair this." % (sd / 1000.0))
    if sd > LARGE_SD_M:
        ctx.log("warning", "the largest σ is %.1f m: look at the quality plots before using this "
                           "trajectory" % sd)


def rt_trajectory(project, ctx):
    """Real-time mode: the Certus's own solution (packet 20) as the
    trajectory, cleaned in Trajectory Review."""
    from . import review
    rt = os.path.join(project.work_dir, "extract", "rt.bin")
    if not os.path.exists(rt):
        raise StepError("the rover files hold no real-time solution (packet 20)")
    idir = project.wpath("ins", "x")[:-2]
    rdir = project.wpath("results", "x")[:-2]
    fwd = os.path.join(idir, "rt_fwd.bin")
    n = review.rt_to_pass(rt, fwd, project.rover_span())
    if not n:
        raise StepError("the real-time solution has no records inside the time range")
    ctx.log("info", "real-time solution: %d records" % n)
    r = _check(engine.run(["combine", "--out", os.path.join(rdir, "trajectory.bin"), "--qc",
                           os.path.join(rdir, "qc.csv"), "--fwd", fwd], ctx.progress, ctx.log, ctx.cancelled),
               "Real-time trajectory")
    return r.results


def _finish_review(project, ctx):
    """Keep the processed trajectory untouched for Trajectory Review and
    re-apply its edits (poor, interpolate)."""
    from . import review
    review.snapshot_original(project)
    # hand selections were drawn on the trajectory before this run: they no longer
    # match it. Decisions made on them stay (they are edits). The list is found
    # again on the new trajectory with the same limits.
    gone = review.remove_selections(project)
    if gone:
        ctx.log("info", "Trajectory Review: %d hand selection%s from the previous run taken off the list"
                % (gone, "" if gone == 1 else "s"))
    try:
        found = review.detect(project)
        ctx.log("info", "Trajectory Review: %d anomal%s against your limits"
                % (len(found), "y" if len(found) == 1 else "ies"))
    except Exception as e:      # noqa: BLE001
        ctx.log("warning", "Trajectory Review: could not look for anomalies (%s)" % e)
    n = review.apply(project)
    if n:
        ctx.log("info", "Trajectory Review edits applied to %d records" % n)


def lever_bug_job(project):
    """True for a job processed before 2.0.1 with an output point set: its published trajectory is at that
    point (labelled IMU centre), so downstream adds the Lidar offset twice. 2.0.1+ results carry settings_sig;
    real-time and GNSS-only runs ignored the output lever as well (written at the IMU then too)."""
    r = project.cfg.get("results") or {}
    if not r or r.get("settings_sig"):
        return False
    if r.get("mode") in ("rt", "gnss"):
        return False
    if not (r.get("combine") or r.get("ins")):
        return False
    lo = project.cfg["installation"].get("lever_output") or [0, 0, 0]
    return any(abs(float(x)) > 1e-6 for x in lo)


def settings_sig(project):
    """What the processed trajectory depends on: when it changes the trajectory is out of date."""
    c = project.cfg
    rt = c["processing"].get("mode") == "rt"
    b = {} if rt or c["processing"].get("positioning") == "ppp" else (project.base() or {})
    inst = {k: v for k, v in c["installation"].items() if k not in ("lever_output", "output_name", "source")}
    data = [c["processing"], inst, c.get("vehicle"), project.rover_span(), c["rover"].get("extract_files"),
            c.get("nav_files"), b.get("id"), base_position(b) if b else None, b.get("delta_hen"), b.get("antenna")]
    import hashlib
    return hashlib.sha1(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:16]


def run_all(project, ctx):
    """The whole processing chain. Returns the results summary."""
    t0 = time.time()
    p = project.cfg["processing"]
    summary = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "mode": p.get("mode"),
               "positioning": p.get("positioning"), "passes": p.get("passes"),
               "settings_sig": settings_sig(project)}
    extract(project, ctx.sub(0.0, 0.10))
    if ctx.cancelled():
        raise engine.Cancelled()
    if p.get("mode") == "rt":
        summary["combine"] = rt_trajectory(project, ctx.sub(0.10, 1.0))
        summary["frame"] = "as the Certus real-time solution (RTK corrections' frame)"
        summary["frame_used"] = project.run_frame()
        _finish_review(project, ctx)
        summary["elapsed_s"] = round(time.time() - t0, 1)
        summary["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        project.cfg["results"] = summary
        project.dirty = True
        return summary
    if p.get("positioning") == "ppp" and p.get("mode") == "tc":
        raise StepError("tight coupling needs a base station; for PPP choose \"Loosely coupled\"")
    if p.get("mode") == "lc" or p.get("mode") == "gnss" or p.get("positioning") == "ppp":
        summary["gnss"] = gnss(project, ctx.sub(0.10, 0.45))
    if p.get("positioning") == "ppp":
        ctx.log("warning", "PPP: the trajectory is in ITRF2020 at the observation date, not NAD83(CSRS)")
    if p.get("mode") == "gnss":
        # the GNSS solutions become the trajectory (no attitude), so Quality
        # and Export work the same as after inertial processing
        summary["combine"] = gnss_trajectory(project, ctx.sub(0.45, 1.0))
        fu = summary["frame_used"] = project.run_frame()
        summary["frame"] = "%s %s" % (fu["name"], fu["epoch"])
        _finish_review(project, ctx)
        summary["elapsed_s"] = round(time.time() - t0, 1)
        summary["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        project.cfg["results"] = summary
        project.dirty = True
        return summary
    if p.get("positioning") == "ppp":
        # the trajectory is then in the frame of the precise orbits
        summary["frame"] = "ITRF2020 (IGS20) at the observation date"
    summary["ins"] = ins(project, ctx.sub(0.45, 0.92))
    summary["combine"] = combine(project, ctx.sub(0.92, 1.0))
    _finish_review(project, ctx)
    fu = summary["frame_used"] = project.run_frame()
    summary["frame"] = summary.get("frame") or "%s %s" % (fu["name"], fu["epoch"])
    summary["elapsed_s"] = round(time.time() - t0, 1)
    summary["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    project.cfg["results"] = summary
    project.dirty = True
    return summary


def _sbet_rate(path):
    """Sample rate (Hz) of an SBET from the median interval of its first records, or None."""
    import statistics
    import struct
    try:
        with open(path, "rb") as fh:
            b = fh.read(136 * 400)
        t = [struct.unpack_from("<d", b, i * 136)[0] for i in range(len(b) // 136)]
        d = [y - x for x, y in zip(t, t[1:]) if y > x]
        if not d:
            return None
        r = 1.0 / statistics.median(d)
        return round(r) if abs(r - round(r)) < 0.05 else round(r, 2)
    except (OSError, struct.error, statistics.StatisticsError, ZeroDivisionError):
        return None


def export(project, ctx, out_dir, rate_hz=0, lever=None, sbet=True, ascii_cols=None, ascii_sep=",",
           proj=None, ts=None, te=None):
    traj = os.path.join(project.work_dir, "results", "trajectory.bin")
    if not os.path.exists(traj):
        raise StepError("process the project first")
    os.makedirs(out_dir, exist_ok=True)
    span = project.rover_span()
    day = geo.gps_to_utc(span[0] if span else 0).strftime("%Y%m%d")
    common = []
    if rate_hz:
        common += ["--rate", "%g" % rate_hz]
    if lever is not None:
        common += ["--lever"] + ["%.4f" % v for v in lever]
    if ts:
        common += ["--ts", "%.3f" % ts]
    if te:
        common += ["--te", "%.3f" % te]
    leap = geo.leap_seconds(geo.gps_to_utc(span[0])) if span else 18
    written = []
    if sbet:
        f1 = os.path.join(out_dir, "sbet_%s.out" % day)
        f2 = os.path.join(out_dir, "smrmsg_%s.out" % day)
        _check(engine.run(["export", "--in", traj, "--out", f1, "--format", "sbet"] + common,
                          ctx.sub(0, 0.5).progress, ctx.log, ctx.cancelled), "SBET export")
        _check(engine.run(["export", "--in", traj, "--out", f2, "--format", "smrmsg"] + common,
                          ctx.sub(0.5, 0.7).progress, ctx.log, ctx.cancelled), "SMRMSG export")
        written += [f1, f2]
    if ascii_cols:
        f3 = os.path.join(out_dir, "%s_%s.txt" % (project.name, day))
        args = ["export", "--in", traj, "--out", f3, "--format", "csv", "--cols", ascii_cols,
                "--sep", {"\t": "tab", " ": "space"}.get(ascii_sep, ascii_sep), "--leap", str(leap)] + common
        if proj:
            args += ["--proj", proj]
        _check(engine.run(args, ctx.sub(0.7, 1).progress, ctx.log, ctx.cancelled), "Text export")
        written.append(f3)
    return written


# ---------------------------------------------------------------------------
# handover to AlignPro / CloudPro (DCS project)
# ---------------------------------------------------------------------------
def plugin_version():
    try:
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "metadata.txt"),
                  encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("version="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def review_summary(project):
    from . import review
    return review.summary(project)


def publish_handover(project, ctx):
    """Publish the trajectory for the next stages: SBET + SMRMSG at the
    Certus reference point (IMU centre), every sample, the 1 Hz quality table
    and trajectory.json (DCS spec, dcs.trajectory/1). Returns the handover."""
    if not project.dcs:
        return None
    from dcs_core import handover
    od = project.out_dir
    tmp = project.wpath("handover", "x")[:-2]
    for f in os.listdir(tmp):
        try:
            os.unlink(os.path.join(tmp, f))
        except OSError:
            pass
    written = export(project, ctx.sub(0, 0.9), tmp, rate_hz=0, lever=[0.0, 0.0, 0.0], sbet=True)
    sbet = os.path.join(od, "trajectory_sbet.out")
    smr = os.path.join(od, "trajectory_smrmsg.out")
    qcf = os.path.join(od, "trajectory_qc.csv")
    # the old trajectory.json must never describe the new files: take it away before they are replaced,
    # so a failure below leaves "not published" rather than a handover that looks current
    try:
        os.remove(os.path.join(od, "trajectory.json"))
    except OSError:
        pass
    os.replace(written[0], sbet)
    os.replace(written[1], smr)
    shutil.copyfile(os.path.join(project.work_dir, "results", "qc.csv"), qcf + ".tmp")
    os.replace(qcf + ".tmp", qcf)
    c = project.cfg
    span = project.rover_span() or (0, 0)
    leap = geo.leap_seconds(geo.gps_to_utc(span[0])) if span[0] else 18
    week = int(span[0] // 604800) if span[0] else 0
    res = c.get("results") or {}
    comb = res.get("combine") or {}
    inst = c["installation"]
    hw = c["rover"].get("hardware") or {}

    def num(k):
        try:
            return float(comb.get(k))
        except (TypeError, ValueError):
            return None
    # the largest sigmas as published (review "poor" raises them after processing)
    from . import results as _results
    qc = _results.load_qc(qcf)
    max_h = round(max(qc["sd_h"]), 4) if qc else num("max_sd_h_m")
    max_v = round(max(qc["sd_d"]), 4) if qc else num("max_sd_v_m")
    src = inst.get("source") or "entered"
    fu = project.trajectory_frame()     # as the trajectory was processed (PPP: ITRF2020; rt: the Certus's)
    frame = {"name": fu.get("name"), "epoch": fu.get("epoch"), "heights": "ellipsoidal"}
    rate = _sbet_rate(sbet) or (c["processing"].get("output_rate_hz") or 200)
    fields = {
        "time": {"system": "GPS", "week": week, "leap_seconds": leap, "start_gps": span[0], "end_gps": span[1],
                 "sbet_time": "GPS seconds of week"},
        "frame": frame,
        "reference_point": {"name": "Certus reference point (IMU centre)", "offset_from_imu_m": [0.0, 0.0, 0.0]},
        "body_frame": "Certus aligned vehicle frame: X forward, Y starboard, Z down; roll + starboard down, "
                      "pitch + bow up, heading + clockwise",
        "levers_m": {"gnss1": {"value": inst.get("lever_antenna"), "source": src},
                     "gnss2": {"value": inst.get("lever_antenna2"), "source": src,
                               # GPS2 is processed only by tight coupling
                               "used": bool(inst.get("use_antenna2")) and c["processing"].get("mode") == "tc"},
                     "lidar": {"value": hw.get("ins_to_lidar_m"), "source": "capture (hardware.json)"}
                     if hw.get("ins_to_lidar_m") else None},
        "rate_hz": rate,
        "quality": {"mode": c["processing"].get("mode"), "positioning": c["processing"].get("positioning"),
                    "fixed_pct": num("fixed_pct"), "imu_only_pct": num("ins_only_pct"),
                    "sep_h_p95_m": num("sep_h_p95_m"), "max_sd_h_m": max_h,
                    "max_sd_v_m": max_v},
        "base_station": (project.base() or {}).get("name"),
        "review": review_summary(project),
    }
    if c["processing"].get("mode") == "gnss":
        fields["attitude"] = "not observed (GNSS only): roll and pitch 0, heading from the direction of travel"
    v_engine = ""
    try:
        v_engine = (engine.version() or ("",))[0] or ""
    except Exception:       # noqa: BLE001
        pass
    doc = handover.publish(project.dcs, "pospro", {"sbet": sbet, "smrmsg": smr, "qc": qcf}, fields, "PosPro",
                           plugin_version(), v_engine)
    ctx.log("info", "trajectory published for AlignPro and CloudPro: %s" % os.path.join(od, "trajectory.json"))
    return {"id": doc["id"], "path": os.path.join(od, "trajectory.json")}
