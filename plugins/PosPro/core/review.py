"""Trajectory Review: find the parts of a processed trajectory that do not
meet the quality limits, and fix them the GNSS/INS way.

Edits (project.cfg["review"]["edits"]), each a GPS-time window:
  exclude      GNSS is not used there; the IMU carries the trajectory
               (needs processing again: passed to the engine as gnss.exclude)
  poor         kept, but its standard deviations are raised to at least
               sd_floor_m (SBET/SMRMSG, quality, handover), so later stages
               know not to trust it
  interpolate  positions replaced between the good samples on both sides
               (horizontal and/or vertical), in any processing mode:
                 method "smooth"    a curve that leaves and meets the good
                                    samples at their own speed and direction
                 method "straight"  a straight line (the original behaviour)
               A window at the start or end of the trajectory is carried on
               from its one good side at that sample's velocity.
  reject       marked bad, still to be fixed (listed as open)
  accept       the anomaly is looked at and left as it is

Poor and interpolate are applied to results/trajectory.bin and qc.csv from
untouched copies (trajectory_original.bin, qc_original.csv), so undo and
reset never need processing again. No Qt.
"""
import bisect
import copy
import csv
import math
import os
import shutil
import struct
import time

TRJ_HDR = 72
TRJ = struct.Struct("<dddd30fBBBBf")          # trj_rec_t, 160 bytes
assert TRJ.size == 160
# index of fields in a TRJ tuple
I_T, I_LAT, I_LON, I_H = 0, 1, 2, 3
I_VEL, I_ATT, I_SDP, I_SDV, I_SDA = 4, 7, 10, 13, 16
I_SEP = 31
I_STATUS, I_NSAT, I_RATIO, I_FLAGS, I_GAP = 34, 35, 36, 37, 38
FLAG_INTERP, FLAG_POOR = 0x10, 0x20

SOLUTIONS = {"fixed": (6,), "float": (6, 5, 4), "dgnss": (6, 5, 4, 3), "any": (6, 5, 4, 3, 2)}
DEFAULTS = {
    "min_solution": "fixed", "use_solution": True,
    "sd_h_m": 0.10, "use_sd_h": True,
    "sd_v_m": 0.15, "use_sd_v": True,
    "sep_h_m": 0.10, "use_sep_h": True,
    "imu_only_s": 10.0, "use_imu_only": True,
    "heading_sd_deg": 0.5, "use_heading": False,
    "extend_before_s": 1.0, "extend_after_s": 1.0,
    "poor_sd_m": 0.30,
}
KIND_LABEL = {"solution": "Solution type", "sd_h": "Horizontal σ", "sd_v": "Vertical σ",
              "sep_h": "Fwd/bwd separation", "imu_only": "IMU only", "heading": "Heading σ",
              "manual": "Selected by hand"}
ACTION_LABEL = {"exclude": "GNSS excluded", "poor": "Marked poor", "interpolate": "Interpolated",
                "reject": "Rejected", "accept": "Accepted"}
METHODS = {"smooth": "smooth curve", "straight": "straight line"}


def state(project):
    r = project.cfg.setdefault("review", {})
    t = r.setdefault("thresholds", {})
    for k, v in DEFAULTS.items():
        t.setdefault(k, v)
    r.setdefault("anomalies", [])
    r.setdefault("edits", [])
    r.setdefault("undo", [])
    r.setdefault("log", [])
    r.setdefault("excluded_in_run", [])
    return r


def results_dir(project):
    return os.path.join(project.work_dir, "results")


def _orig(project):
    d = results_dir(project)
    return os.path.join(d, "trajectory_original.bin"), os.path.join(d, "qc_original.csv")


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------
def _runs(t, flags, vals=None):
    """Consecutive flagged samples (gaps up to 1.5 s join) -> [(t0, t1, max)]."""
    out = []
    cur = None
    for i, f in enumerate(flags):
        if not f:
            continue
        v = vals[i] if vals is not None else 0.0
        if cur and t[i] - cur[1] <= 1.5:
            cur[1] = t[i]
            cur[2] = max(cur[2], v)
        else:
            if cur:
                out.append(tuple(cur))
            cur = [t[i], t[i], v]
    if cur:
        out.append(tuple(cur))
    return out


def load_original_qc(project):
    from .results import load_qc
    _, q = _orig(project)
    if not os.path.exists(q):
        q = os.path.join(results_dir(project), "qc.csv")
    return load_qc(q)


def detect(project, qc=None):
    """Anomalies against the thresholds, on the trajectory as processed
    (before any review edit). Returns and stores the list."""
    r = state(project)
    th = r["thresholds"]
    qc = qc or load_original_qc(project)
    if not qc:
        r["anomalies"] = []
        return []
    t = qc["gps_time"]
    st = qc["status"]
    found = []
    if th["use_solution"]:
        ok = SOLUTIONS.get(th["min_solution"], SOLUTIONS["fixed"])
        found += [("solution", a, b, m) for a, b, m in
                  _runs(t, [s not in ok and s != 1 for s in st], [float(6 - s) for s in st])]
    if th["use_sd_h"]:
        found += [("sd_h", a, b, m) for a, b, m in _runs(t, [v > th["sd_h_m"] for v in qc["sd_h"]], qc["sd_h"])]
    if th["use_sd_v"]:
        found += [("sd_v", a, b, m) for a, b, m in _runs(t, [v > th["sd_v_m"] for v in qc["sd_d"]], qc["sd_d"])]
    if th["use_sep_h"] and any(qc["sep_h"]):
        found += [("sep_h", a, b, m) for a, b, m in
                  _runs(t, [v > th["sep_h_m"] for v in qc["sep_h"]], qc["sep_h"])]
    if th["use_imu_only"]:
        found += [("imu_only", a, b, m) for a, b, m in
                  _runs(t, [g > th["imu_only_s"] for g in qc["gap_s"]], qc["gap_s"])]
    if th["use_heading"]:
        found += [("heading", a, b, m) for a, b, m in
                  _runs(t, [v > th["heading_sd_deg"] for v in qc["sd_heading_deg"]], qc["sd_heading_deg"])]
    eb, ea = float(th["extend_before_s"]), float(th["extend_after_s"])
    t_lo, t_hi = t[0], t[-1]
    out = []
    for k, (kind, a, b, m) in enumerate(sorted(found, key=lambda x: x[1])):
        out.append({"id": "A%d" % (k + 1), "kind": kind, "t0": max(t_lo, a - eb), "t1": min(t_hi, b + ea),
                    "max": round(m, 4)})
    r["anomalies"] = out + [a for a in r["anomalies"] if a.get("kind") == "manual"]
    r["anomalies"].sort(key=lambda x: x["t0"])
    log(project, "detected %d anomalies" % len(out))
    return out


def unit(kind):
    return {"solution": "", "sd_h": "m", "sd_v": "m", "sep_h": "m", "imu_only": "s", "heading": "°"}.get(kind, "")


def describe_max(a):
    if a["kind"] == "solution":
        return {0: "fixed", 1: "float", 2: "PPP", 3: "DGNSS", 4: "single", 6: "none"}.get(int(round(a["max"])), "")
    if a["kind"] == "manual":
        return ""
    return "%.3f %s" % (a["max"], unit(a["kind"]))


# ---------------------------------------------------------------------------
# edits, undo, log
# ---------------------------------------------------------------------------
def log(project, text):
    r = state(project)
    r["log"].append("%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
    r["log"] = r["log"][-500:]


def _push_undo(r):
    r["undo"].append(copy.deepcopy(r["edits"]))
    r["undo"] = r["undo"][-50:]


def add_edit(project, t0, t1, action, source="manual", horizontal=True, vertical=True, sd_floor_m=None,
             method="smooth"):
    r = state(project)
    if action not in ACTION_LABEL:
        raise ValueError(action)
    if t1 <= t0:
        t1 = t0 + 1.0
    _push_undo(r)
    # a new edit replaces older ones of the same window and kind of action
    keep = [e for e in r["edits"] if not (abs(e["t0"] - t0) < 1e-3 and abs(e["t1"] - t1) < 1e-3)]
    e = {"id": "E%d" % (max([int(x["id"][1:]) for x in r["edits"]] + [0]) + 1), "t0": float(t0), "t1": float(t1),
         "action": action, "source": source, "horizontal": bool(horizontal), "vertical": bool(vertical),
         "sd_floor_m": float(sd_floor_m if sd_floor_m is not None else r["thresholds"]["poor_sd_m"]),
         "created": time.strftime("%Y-%m-%d %H:%M:%S")}
    if action == "interpolate":
        e["method"] = method if method in METHODS else "smooth"
    keep.append(e)
    r["edits"] = sorted(keep, key=lambda x: x["t0"])
    how = (", %s%s" % (METHODS[e["method"]], "" if (horizontal and vertical) else
                       (", horizontal only" if horizontal else ", vertical only"))) if action == "interpolate" else ""
    log(project, "%s %s (%.1f s, %s%s)" % (ACTION_LABEL[action], _span(t0, t1), t1 - t0, source, how))
    return e


def add_selection(project, t0, t1):
    """A part chosen by hand (map shape or plot range): listed like an
    anomaly, still to be decided. The same window twice is listed once."""
    r = state(project)
    t1 = max(t1, t0 + 1.0)
    for a in r["anomalies"]:
        if a.get("kind") == "manual" and abs(a["t0"] - t0) < 0.5 and abs(a["t1"] - t1) < 0.5:
            return a
    k = 1 + max([int(a["id"][1:]) for a in r["anomalies"] if a["id"].startswith("S")] + [0])
    a = {"id": "S%d" % k, "kind": "manual", "t0": float(t0), "t1": float(t1), "max": 0.0}
    r["anomalies"].append(a)
    r["anomalies"].sort(key=lambda x: x["t0"])
    log(project, "selected by hand %s" % _span(a["t0"], a["t1"]))
    return a


def merge_windows(runs, join_s=30.0, max_rows=20):
    """One drawing on the map -> few rows: pieces closer than join_s are one
    window, and if there are still more than max_rows (a scattered
    trajectory), the drawing becomes one window from the first to the last
    piece. runs: [(t0, t1, ...)]."""
    w = []
    for r in sorted(runs, key=lambda x: x[0]):
        if w and r[0] - w[-1][1] <= join_s:
            w[-1][1] = max(w[-1][1], r[1])
        else:
            w.append([r[0], r[1]])
    if len(w) > max_rows:
        w = [[w[0][0], max(x[1] for x in w)]]
    return [tuple(x) for x in w]


def remove_selections(project, ids=None):
    """Take hand selections off the list (all of them when ids is None).
    Decisions already made on them stay as edits. Returns how many went."""
    r = state(project)
    keep, gone = [], []
    for a in r["anomalies"]:
        (gone if a.get("kind") == "manual" and (ids is None or a["id"] in ids) else keep).append(a)
    r["anomalies"] = keep
    if gone:
        log(project, "removed %d hand selection%s" % (len(gone), "" if len(gone) == 1 else "s"))
    return len(gone)


def remove_edit(project, eid):
    r = state(project)
    _push_undo(r)
    gone = [e for e in r["edits"] if e["id"] == eid]
    r["edits"] = [e for e in r["edits"] if e["id"] != eid]
    for e in gone:
        log(project, "removed: %s %s" % (ACTION_LABEL[e["action"]], _span(e["t0"], e["t1"])))


def undo(project):
    r = state(project)
    if not r["undo"]:
        return False
    r["edits"] = r["undo"].pop()
    log(project, "undo")
    return True


def reset(project):
    r = state(project)
    _push_undo(r)
    r["edits"] = []
    log(project, "reset to the trajectory as processed")


def _span(t0, t1):
    try:
        from .geo import gps_to_utc
        return "%s–%s UTC" % (gps_to_utc(t0).strftime("%H:%M:%S"), gps_to_utc(t1).strftime("%H:%M:%S"))
    except Exception:       # noqa: BLE001
        return "%.1f–%.1f" % (t0, t1)


def edit_for(project, t0, t1):
    """The edit covering most of [t0, t1], or None."""
    best, cover = None, 0.0
    for e in state(project)["edits"]:
        c = min(t1, e["t1"]) - max(t0, e["t0"])
        if c > cover:
            best, cover = e, c
    return best if best and cover >= 0.5 * max(t1 - t0, 1e-6) else None


def is_open(edit):
    """True when a window still needs a decision: no edit, or only rejected."""
    return edit is None or edit.get("action") == "reject"


def exclusions(project):
    return [(e["t0"], e["t1"]) for e in state(project)["edits"] if e["action"] == "exclude"]


def exclusions_pending(project):
    """True when the GNSS exclusions differ from those of the last processing."""
    r = state(project)
    now = sorted([round(a, 3), round(b, 3)] for a, b in exclusions(project))
    return now != sorted([round(a, 3), round(b, 3)] for a, b in r.get("excluded_in_run") or [])


ENGINE_MAX_EXCLUDE = 256      # windows the engine takes (pp_ins.c); it would drop the rest unseen


def fit_windows(wins, limit=ENGINE_MAX_EXCLUDE):
    """At most limit windows: the two closest neighbours are joined until they fit (a little more is
    excluded, nothing is lost)."""
    w = sorted([list(x) for x in wins])
    while len(w) > limit:
        k = min(range(len(w) - 1), key=lambda i: w[i + 1][0] - w[i][1])
        w[k][1] = max(w[k][1], w[k + 1][1])
        del w[k + 1]
    return [tuple(x) for x in w]


def engine_exclude_value(project):
    return ";".join("%.3f-%.3f" % w for w in fit_windows(exclusions(project)))


# ---------------------------------------------------------------------------
# applying poor / interpolate to the results
# ---------------------------------------------------------------------------
def snapshot_original(project):
    """After processing: keep untouched copies, record which exclusions this
    run used."""
    d = results_dir(project)
    tb, qb = _orig(project)
    for src, dst in ((os.path.join(d, "trajectory.bin"), tb), (os.path.join(d, "qc.csv"), qb)):
        if os.path.exists(src):
            shutil.copyfile(src, dst + ".tmp")
            os.replace(dst + ".tmp", dst)
    r = state(project)
    r["excluded_in_run"] = [list(w) for w in exclusions(project)]


def _read_times(fh, n):
    """GPS time of record i (file stays open)."""
    def at(i):
        fh.seek(TRJ_HDR + i * TRJ.size)
        return struct.unpack("<d", fh.read(8))[0]
    return at


def _index(at, n, t):
    lo, hi = 0, n
    while lo < hi:
        mid = (lo + hi) // 2
        if at(mid) < t:
            lo = mid + 1
        else:
            hi = mid
    return lo


def apply(project):
    """Rebuild results/trajectory.bin and qc.csv from the originals with the
    poor / interpolate edits. Returns the number of records changed."""
    d = results_dir(project)
    tb, qb = _orig(project)
    if not os.path.exists(tb):
        return 0
    out, outq = os.path.join(d, "trajectory.bin"), os.path.join(d, "qc.csv")
    edits = [e for e in state(project)["edits"] if e["action"] in ("poor", "interpolate")]
    shutil.copyfile(tb, out + ".tmp")
    changed = {}
    with open(out + ".tmp", "r+b") as fh:
        fh.seek(8)
        n = struct.unpack("<Q", fh.read(8))[0]
        at = _read_times(fh, n)
        for e in sorted(edits, key=lambda x: (x["action"] != "interpolate", x["t0"])):
            i0, i1 = _index(at, n, e["t0"]), _index(at, n, e["t1"] + 1e-6)
            if i1 <= i0:
                continue
            fh.seek(TRJ_HDR + i0 * TRJ.size)
            recs = [list(x) for x in TRJ.iter_unpack(fh.read((i1 - i0) * TRJ.size))]
            if e["action"] == "interpolate":
                a = b = None
                if i0 > 0:
                    fh.seek(TRJ_HDR + (i0 - 1) * TRJ.size)
                    a = TRJ.unpack(fh.read(TRJ.size))
                if i1 < n:
                    fh.seek(TRJ_HDR + i1 * TRJ.size)
                    b = TRJ.unpack(fh.read(TRJ.size))
                if a is None and b is None:
                    continue            # the whole trajectory: nothing good to start from
                interpolate_records(recs, a, b, e.get("horizontal", True), e.get("vertical", True),
                                    e.get("method", "straight"))
            else:
                fl = float(e.get("sd_floor_m") or 0.3)
                for r_ in recs:
                    for k in range(3):
                        r_[I_SDP + k] = max(r_[I_SDP + k], fl)
                    r_[I_FLAGS] |= FLAG_POOR
            fh.seek(TRJ_HDR + i0 * TRJ.size)
            fh.write(b"".join(TRJ.pack(*r_) for r_ in recs))
            for r_ in recs:
                changed[round(r_[I_T], 3)] = r_
    os.replace(out + ".tmp", out)
    _rewrite_qc(qb, outq, changed)
    return len(changed)



# ---------------------------------------------------------------------------
# interpolation
# ---------------------------------------------------------------------------
_A, _F = 6378137.0, 1 / 298.257222101           # GRS80


def _radii(lat):
    e2 = _F * (2 - _F)
    w = 1 - e2 * math.sin(lat) ** 2
    return _A * (1 - e2) / w ** 1.5, _A / math.sqrt(w)     # meridian, prime vertical


def _hermite(s):
    s2, s3 = s * s, s * s * s
    return ((2 * s3 - 3 * s2 + 1, s3 - 2 * s2 + s, -2 * s3 + 3 * s2, s3 - s2),
            (6 * s2 - 6 * s, 3 * s2 - 4 * s + 1, -6 * s2 + 6 * s, 3 * s2 - 2 * s))


def interpolate_records(recs, a, b, horizontal=True, vertical=True, method="smooth"):
    """Replace the positions of recs (lists, TRJ fields) between the good records a (before) and b (after).
    "smooth": cubic curve leaving a and meeting b at their own velocities; "straight": a straight line.
    With only one good side (a window at the start or end) the trajectory is carried on from it at its
    velocity. Velocities follow the new path; standard deviations grow away from the good samples."""
    ref = a if a is not None else b
    lat0, lon0, h0 = ref[I_LAT], ref[I_LON], ref[I_H]
    M, N = _radii(lat0)
    cl = math.cos(lat0)

    def local(r):           # north, east, up (m) of record r from ref
        return ((r[I_LAT] - lat0) * M, (r[I_LON] - lon0) * N * cl, r[I_H] - h0)

    def vel(r):             # north, east, up (m/s)
        return (r[I_VEL], r[I_VEL + 1], -r[I_VEL + 2])

    pa, pb = (local(a) if a is not None else None), (local(b) if b is not None else None)
    va, vb = (vel(a) if a is not None else None), (vel(b) if b is not None else None)
    span = (b[I_T] - a[I_T]) if (a is not None and b is not None) else 0.0
    for r_ in recs:
        t = r_[I_T]
        if a is not None and b is not None and span > 0:
            s = min(1.0, max(0.0, (t - a[I_T]) / span))
            if method == "straight":
                p = [pa[k] + s * (pb[k] - pa[k]) for k in range(3)]
                v = [(pb[k] - pa[k]) / span for k in range(3)]
            else:
                (h00, h10, h01, h11), (d00, d10, d01, d11) = _hermite(s)
                p = [h00 * pa[k] + h10 * span * va[k] + h01 * pb[k] + h11 * span * vb[k] for k in range(3)]
                v = [(d00 * pa[k] + d10 * span * va[k] + d01 * pb[k] + d11 * span * vb[k]) / span for k in range(3)]
            dmin = min(t - a[I_T], b[I_T] - t)
        else:
            g, gp, gv = (a, pa, va) if a is not None else (b, pb, vb)
            dt = t - g[I_T]
            p = [gp[k] + gv[k] * dt for k in range(3)]
            v = list(gv)
            dmin = abs(dt)
        if horizontal:
            r_[I_LAT] = lat0 + p[0] / M
            r_[I_LON] = lon0 + p[1] / (N * cl)
            r_[I_VEL], r_[I_VEL + 1] = v[0], v[1]
        if vertical:
            r_[I_H] = h0 + p[2]
            r_[I_VEL + 2] = -v[2]
        grow = 0.05 + 0.05 * dmin
        for k in range(3):
            if (k < 2 and horizontal) or (k == 2 and vertical):
                good = [x[I_SDP + k] for x in (a, b) if x is not None]
                r_[I_SDP + k] = max(max(good), grow)
        r_[I_FLAGS] |= FLAG_INTERP


def _rewrite_qc(src, dst, changed):
    if not os.path.exists(src):
        return
    times = sorted(changed)
    with open(src, newline="") as fi, open(dst + ".tmp", "w", newline="") as fo:
        rd = csv.reader(fi)
        wr = csv.writer(fo, lineterminator="\n")
        head = next(rd)
        wr.writerow(head)
        ix = {k: i for i, k in enumerate(head)}
        for row in rd:
            if times:
                t = float(row[ix["gps_time"]])
                j = bisect.bisect_left(times, t - 0.01)
                if j < len(times) and abs(times[j] - t) < 0.011:
                    r_ = changed[times[j]]
                    row[ix["lat_deg"]] = "%.9f" % math.degrees(r_[I_LAT])
                    row[ix["lon_deg"]] = "%.9f" % math.degrees(r_[I_LON])
                    row[ix["h_m"]] = "%.4f" % r_[I_H]
                    row[ix["sd_n"]] = "%.4f" % r_[I_SDP]
                    row[ix["sd_e"]] = "%.4f" % r_[I_SDP + 1]
                    row[ix["sd_d"]] = "%.4f" % r_[I_SDP + 2]
            wr.writerow(row)
    os.replace(dst + ".tmp", dst)


def summary(project):
    """For the handover file: what the review did."""
    r = state(project)
    pick = lambda a: [{"t0": e["t0"], "t1": e["t1"], **({"sd_floor_m": e["sd_floor_m"]} if a == "poor" else {})}
                      for e in r["edits"] if e["action"] == a]
    interp = [{"t0": e["t0"], "t1": e["t1"], "method": e.get("method", "straight"),
               "horizontal": e.get("horizontal", True), "vertical": e.get("vertical", True)}
              for e in r["edits"] if e["action"] == "interpolate"]
    return {"gnss_excluded": pick("exclude"), "reduced_quality": pick("poor"), "interpolated": interp,
            "accepted": pick("accept"), "thresholds": dict(r["thresholds"])}


# ---------------------------------------------------------------------------
# real-time trajectory (no post-processing)
# ---------------------------------------------------------------------------
CERTUS_FIX = {7: 6, 6: 5, 5: 4, 4: 3, 3: 3, 2: 2, 1: 2, 0: 0}


def rt_to_pass(rt_path, out_path, span=None):
    """The Certus real-time solution (rt.bin, packet 20) as a trajectory pass
    for the engine's combine step, inside span (t0, t1 GPS s) when given (the time crop).
    Returns the record count."""
    t_lo, t_hi = (span[0], span[1]) if span else (-1e300, 1e300)
    from .results import RT_FMT
    with open(rt_path, "rb") as fi, open(out_path + ".tmp", "wb") as fo:
        hdr = fi.read(64)
        if hdr[:8] != b"PPRT0001":
            raise ValueError("%s is not a real-time track" % rt_path)
        fo.write(b"PPTRJ001" + struct.pack("<Q", 0) + struct.pack("<3d", 0, 0, 0) + struct.pack("<ii", 0, 1) +
                 b"\0" * 24)
        n, tlast_fix = 0, None
        while True:
            b = fi.read(RT_FMT.size * 8192)
            if not b:
                break
            buf = []
            for (t, lat, lon, h, vn, ve, vd, ro, pi, he, sdlat, sdlon, sdh, gx, gy, gz, sysst, filt) in \
                    RT_FMT.iter_unpack(b[:len(b) - len(b) % RT_FMT.size]):
                if t < t_lo or t > t_hi:
                    continue
                fix = (filt >> 4) & 7
                # filter status: bit 1 navigation initialised, bits 4-6 GNSS fix type (0 = none)
                st = CERTUS_FIX.get(fix, 0) if fix else (1 if filt & 0x02 else 0)
                if st in (6, 5, 4):
                    tlast_fix = t
                gap = 0.0 if tlast_fix is None else max(0.0, t - tlast_fix)
                vals = [t, lat, lon, h, vn, ve, vd, ro, pi, he, sdlat, sdlon, sdh,
                        0.05, 0.05, 0.05, math.radians(0.1), math.radians(0.1), math.radians(0.3),
                        0, 0, 0, gx, gy, gz, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                        st, 0, 0, 0, gap]
                buf.append(TRJ.pack(*vals))
                n += 1
            fo.write(b"".join(buf))
        fo.seek(8)
        fo.write(struct.pack("<Q", n))
    os.replace(out_path + ".tmp", out_path)
    return n
