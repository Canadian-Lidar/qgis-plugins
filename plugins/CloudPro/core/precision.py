"""Sensor precision: what the data can resolve, and filter settings derived from it.

Two sources, the larger one wins:

1. The error budget of the hardware (Hesai XT-32 and Certus Evo specifications)
   and of the trajectory actually used (PosPro's SMRMSG standard deviations,
   else the Certus real-time / RTK figures), at the ranges of the points:

       vertical noise within a pass   s_pass = sqrt(s_range^2 + (r * s_tilt)^2)
       horizontal noise               s_horiz = sqrt(s_range^2 + (r * s_heading)^2)
       pass-to-pass (vertical)        s_between = sqrt(2) * s_pos_v (independent epochs)

2. Measurements on the points of the Inspection Area:
   - within a pass: residuals of a plane fitted to each line's ground points
     per 1 m cell (robust SD), which also holds the road's micro-roughness;
   - between passes: median-height differences of the same cells seen by two
     lines (robust SD / sqrt 2).

``tune(groups, prec)`` then sets the height and distance tolerances of the
filters as multiples of these (with sensible floors), and the intensity
thresholds of the feature filters from the intensities actually recorded.
"""
import math

import numpy as np

from . import spatial as sp

# Manufacturer figures (1 sigma unless stated)
XT32 = {"range_precision_m": 0.005, "range_accuracy_m": 0.01, "az_step_deg": 0.18, "el_step_deg": 1.0,
        "source": "Hesai XT32: range precision 0.5 cm (1 sigma), accuracy +/-1 cm, 0.18 deg x 1 deg"}
CERTUS = {"realtime": {"pos_h": 0.01, "pos_v": 0.015, "tilt_deg": 0.03, "heading_deg": 0.05,
                       "source": "Certus Evo, RTK: 0.01 m H, 0.015 m V, roll/pitch 0.03 deg, heading 0.05 deg "
                                 "(dual antenna, 1 m)"},
          "post": {"pos_h": 0.01, "pos_v": 0.015, "tilt_deg": 0.01, "heading_deg": 0.01,
                   "source": "Certus Evo, post-processed: roll/pitch 0.01 deg, heading 0.01 deg"}}


def trajectory_sd(traj_info, smrmsg_path=None):
    """Median standard deviations of the trajectory used: from PosPro's SMRMSG when there is one."""
    post = (traj_info or {}).get("source") == "pospro"
    out = dict(CERTUS["post" if post else "realtime"])
    if smrmsg_path:
        try:
            from .trajectory import SMRMSG
            a = np.fromfile(smrmsg_path, SMRMSG)
            if len(a) > 10:
                med = lambda k: float(np.nanmedian(a[k]))          # noqa: E731
                out["pos_h"] = math.hypot(med("sd_n"), med("sd_e"))
                out["pos_v"] = med("sd_d")
                # SMRMSG attitude SDs are arc-minutes
                out["tilt_deg"] = max(med("sd_roll"), med("sd_pitch")) / 60.0
                out["heading_deg"] = med("sd_heading") / 60.0
                out["source"] = "PosPro SMRMSG (median of the run)"
        except Exception:       # noqa: BLE001
            pass
    return out


def budget(ranges_m, traj):
    """Expected noise at the points' ranges (median and 90th percentile range)."""
    r = np.asarray(ranges_m, float)
    r = r[np.isfinite(r) & (r > 0)]
    r50 = float(np.median(r)) if len(r) else 15.0
    r90 = float(np.percentile(r, 90)) if len(r) else 30.0
    sr = XT32["range_precision_m"]
    tilt = math.radians(traj["tilt_deg"])
    hdg = math.radians(traj["heading_deg"])
    return {"range_median_m": r50, "range_p90_m": r90,
            "pass_v": math.hypot(sr, r50 * tilt), "pass_v_p90": math.hypot(sr, r90 * tilt),
            "horiz": math.hypot(sr, r50 * hdg), "horiz_p90": math.hypot(sr, r90 * hdg),
            "between_v": math.sqrt(2.0) * traj["pos_v"]}


def measure(ps, cls=None, cell=1.0, min_pts=12):
    """Noise measured on the ground of the loaded points: (within a pass, between passes) in metres, or None."""
    n = len(ps)
    if n < 1000:
        return None, None, 0
    cls = ps.cls if cls is None else cls
    g = np.flatnonzero((cls == 2) & ~ps.rejected)
    if len(g) < 1000:                       # no ground yet: the lowest 30 % of each cell
        ix, iy, _, _ = sp.grid_index(ps.x, ps.y, cell)
        cid = iy.astype(np.int64) * (int(ix.max()) + 2) + ix
        o = np.lexsort((ps.z, cid))
        cs = cid[o]
        first = np.r_[0, np.flatnonzero(np.diff(cs)) + 1]
        rank = np.arange(n) - np.repeat(first, np.diff(np.r_[first, n]))
        cnt = np.repeat(np.diff(np.r_[first, n]), np.diff(np.r_[first, n]))
        g = np.sort(o[rank < 0.3 * cnt])
    x, y, z, ln = ps.x[g], ps.y[g], ps.z[g], ps.line[g].astype(np.int64)
    ix, iy, _, _ = sp.grid_index(x, y, cell)
    nx = int(ix.max()) + 2
    key = (iy.astype(np.int64) * nx + ix) * 1024 + ln
    order = np.argsort(key, kind="stable")
    key, x, y, z, ln = key[order], x[order], y[order], z[order], ln[order]
    start = np.r_[0, np.flatnonzero(np.diff(key)) + 1]
    stop = np.r_[start[1:], len(key)]
    res = []
    meds = {}
    for a, b in zip(start, stop):
        if b - a < min_pts:
            continue
        X = np.column_stack([np.ones(b - a), x[a:b] - x[a:b].mean(), y[a:b] - y[a:b].mean()])
        coef, *_ = np.linalg.lstsq(X, z[a:b], rcond=None)
        rr = z[a:b] - X @ coef
        mad = np.median(np.abs(rr - np.median(rr))) * 1.4826
        if np.isfinite(mad):
            res.append(mad)
            meds[int(key[a])] = float(coef[0])
        if len(res) > 4000:
            break
    within = float(np.median(res)) if res else None
    # between passes: same cell, two lines
    cells = {}
    for k, v in meds.items():
        cells.setdefault(k // 1024, []).append(v)
    d = [vs[i] - vs[j] for vs in cells.values() if len(vs) > 1 for i in range(len(vs)) for j in range(i + 1, len(vs))]
    between = float(np.median(np.abs(np.asarray(d) - np.median(d))) * 1.4826 * math.sqrt(2)) if len(d) > 20 else None
    if d and len(d) > 20:
        between = max(between, abs(float(np.median(d))) / 2)        # a systematic offset between passes
    return within, between, len(res)


def assess(ps, traj_info=None, smrmsg_path=None, cls=None):
    """Everything the tuner needs, with the source of each number."""
    traj = trajectory_sd(traj_info, smrmsg_path)
    b = budget(ps.rng * 0.01 if len(ps) else [], traj)
    within, between, ncells = measure(ps, cls)
    s_pass = max(b["pass_v"], within or 0.0)
    # passes minutes apart share most of the GNSS error, so a measured pass-to-pass spread is better than the
    # specification; the specification (half of it) stays as a floor
    s_between = max(between, 0.5 * b["between_v"]) if between is not None else b["between_v"]
    ground = (ps.cls == 2) if cls is None else (cls == 2)
    I = ps.i[ground] if ground.sum() > 500 else ps.i
    I = I.astype(float)
    med = float(np.median(I)) if len(I) else 30.0
    sd = float(np.median(np.abs(I - med)) * 1.4826) if len(I) else 10.0
    return {"traj": traj, "budget": b, "measured_pass": within, "measured_between": between, "cells": ncells,
            "s_pass": s_pass, "s_between": s_between, "s_all": math.hypot(s_pass, s_between),
            "s_horiz": max(b["horiz"], 0.005), "s_horiz_p90": b["horiz_p90"],
            "road_intensity_median": med, "road_intensity_sd": max(sd, 1.0),
            "intensity_p99": float(np.percentile(ps.i, 99.5)) if len(ps) else 200.0,
            "lidar": XT32}


def _clamp(v, lo, hi):
    return float(min(hi, max(lo, v)))


def rules(pr):
    """(filter key, parameter, new value, why) for every tolerance that follows the precision."""
    sp_, sb, sa, sh = pr["s_pass"], pr["s_between"], pr["s_all"], pr["s_horiz_p90"]
    rm, rsd = pr["road_intensity_median"], pr["road_intensity_sd"]
    r = lambda v, d=3: round(float(v), d)        # noqa: E731
    return [
        ("conf95", "limit", r(_clamp(2.0 * sa, 0.02, 0.15)), "2 x combined height noise (95 %)"),
        ("surface", "gate", r(_clamp(4.0 * sa, 0.03, 0.25)), "4 x combined height noise"),
        ("slope", "dh", r(_clamp(4.0 * sp_, 0.02, 0.2)), "4 x within-pass noise"),
        ("clip_mean", "gate", r(_clamp(6.0 * sa, 0.05, 0.5)), "6 x combined height noise"),
        ("clip_reference", "above", r(_clamp(5.0 * sa, 0.05, 1.0)), "5 x combined height noise"),
        ("clip_reference", "below", r(_clamp(5.0 * sa, 0.05, 1.0)), "5 x combined height noise"),
        ("hag", "below", r(_clamp(6.0 * sa, 0.08, 0.5)), "6 x combined height noise (low points)"),
        ("smrf", "thr", r(_clamp(10.0 * sa, 0.15, 0.5)), "10 x combined noise, at least 0.15 m (kerbs, cars)"),
        ("pmf", "dh0", r(_clamp(6.0 * sa, 0.10, 0.4)), "6 x combined noise"),
        ("pmf", "thr", r(_clamp(8.0 * sa, 0.15, 0.5)), "8 x combined noise"),
        ("csf", "hcc", r(_clamp(8.0 * sa, 0.10, 0.5)), "8 x combined noise"),
        ("vplane", "thick", r(_clamp(4.0 * math.hypot(sh, sb), 0.05, 0.3)), "4 x horizontal noise at the far range"),
        ("ror", "r", r(_clamp(10 * sh, 0.15, 0.6)), "10 x horizontal noise"),
        ("dynamic", "margin", r(_clamp(0.5 + 6 * sh, 0.4, 1.0)), "free-space margin over the horizontal noise"),
        # feature filters: their defaults describe the objects (paint contrast, curb height, rail shape), so the
        # precision only ever raises them above the noise, never below the default
        ("f_curbs", "hmin", r(5.0 * sp_), "5 x within-pass noise, not below the default", "floor"),
        ("f_markings", "contrast", r(4.0 * rsd, 0), "4 x SD of the road's intensity, not below the default", "floor"),
        ("f_manholes", "contrast", r(3.0 * rsd, 0), "3 x SD of the road's intensity, not below the default", "floor"),
        ("f_signs", "imin", int(_clamp(max(pr["intensity_p99"] + 15, rm + 12 * rsd), 0, 245)),
         "above the 99.5th percentile of intensity, not below the default", "floor"),
        ("f_guardrails", "rms", r(4.0 * math.hypot(sh, sb)), "4 x horizontal noise, not below the default", "floor"),
        ("f_wires", "rms", r(8.0 * sa), "8 x combined noise, not below the default", "floor"),
    ]


def tune(groups, pr):
    """Apply rules() to every matching filter of the groups. Returns [(group, filter name, param, old, new, why)]."""
    from .filters import REGISTRY
    changes = []
    rl = [x if len(x) == 5 else x + ("",) for x in rules(pr)]
    for g in groups:
        for f in g.filters:
            for key, prm, val, why, kind in rl:
                if f.key == key and prm in f.values:
                    old = f.values[prm]
                    if kind == "floor":
                        d = next(p.default for p in REGISTRY[key].params if p.key == prm)
                        val = max(val, d)
                    new = type(old)(val) if isinstance(old, (int, float)) and not isinstance(old, bool) else val
                    if old != new:
                        f.values[prm] = new
                        changes.append((g.name, f.name, prm, old, new, why))
    return changes


def report_html(pr, changes=None):
    b, t = pr["budget"], pr["traj"]
    mm = lambda v: "-" if v is None else "%.1f mm" % (1000 * v)      # noqa: E731
    rows = [("Lidar", XT32["source"]), ("Trajectory", "%s<br>pos H %s, V %s; roll/pitch %.3f deg, heading %.3f deg" % (
        t["source"], mm(t["pos_h"]), mm(t["pos_v"]), t["tilt_deg"], t["heading_deg"])),
        ("Ranges", "median %.1f m, 90 %% %.1f m" % (b["range_median_m"], b["range_p90_m"])),
        ("Expected, within a pass", "%s vertical (%s at the 90 %% range), %s horizontal (%s)" % (
            mm(b["pass_v"]), mm(b["pass_v_p90"]), mm(b["horiz"]), mm(b["horiz_p90"]))),
        ("Expected, between passes", mm(b["between_v"])),
        ("Measured on the ground", "within a pass %s, between passes %s (%d cells)" % (
            mm(pr["measured_pass"]), mm(pr["measured_between"]), pr["cells"])),
        ("Used", "within %s, between %s, combined %s" % (mm(pr["s_pass"]), mm(pr["s_between"]), mm(pr["s_all"]))),
        ("Road intensity", "median %.0f, SD %.1f; 99.5th percentile of all %.0f" % (
            pr["road_intensity_median"], pr["road_intensity_sd"], pr["intensity_p99"]))]
    h = "<table>" + "".join("<tr><td style='padding-right:12px'><b>%s</b></td><td>%s</td></tr>" % r for r in rows) + \
        "</table>"
    if changes is not None:
        if changes:
            h += "<p><b>Settings that change</b></p><table>" + "".join(
                "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s &rarr; <b>%s</b></td><td style='color:#7f9b94'>%s</td></tr>"
                % c for c in changes) + "</table>"
        else:
            h += "<p>All settings already match the precision.</p>"
    return h
