"""What CloudPro's line separation check says about the calibration (AlignPro 1.8.0).

CloudPro compares overlapping lines on the finished points and writes 03_CloudPro/out/calibration_check.json
(dcs.calibration_check/1): per line pair the steady offset in the vehicle frame, whether the two lines were driven
in opposite directions, the roll slope, its findings, the lines' recordings and when each line was over the shared
surfaces. A steady offset that reverses with the driving direction, or a height difference that grows with the
distance from the drive line, is a calibration error: AlignPro shows it and can use that pair as patch-test
material (its lines, and an area of interest along the stretch they share).

    load(project) -> {"pairs", "summary", "measured_utc", "current", "other"} | None
    lines_for(project, pair) -> AlignPro line indexes recorded as the pair's two lines
    corridor(project, pair, half_width) -> AOI ring [[lon, lat], ...] (degrees) along the shared stretch
"""
import json
import os

import numpy as np

SCHEMA = "dcs.calibration_check/1"
NAME = "calibration_check.json"
HALF_WIDTH = 25.0                 # m each side of the drive line
STEP_S = 0.5


def path(project):
    dcs = getattr(project, "dcs", None)
    if dcs is None:
        return None
    try:
        return os.path.join(dcs.stage_dir("cloudpro", "out", create=False), NAME)
    except Exception:                           # noqa: BLE001
        return None


def _alignpro_id(project):
    try:
        return project.dcs.stage("alignpro").get("handover_id")
    except Exception:                           # noqa: BLE001
        return None


def load(project):
    p = path(project)
    if not p or not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    if str(doc.get("schema", "")).split("/")[0] != SCHEMA.split("/")[0]:
        return None
    al = doc.get("alignment") or {}
    hid = al.get("handover_id")
    current = bool(hid) and hid == _alignpro_id(project)
    other = "" if current else ("an earlier calibration" if al.get("source") == "alignpro" and hid else
                                "no AlignPro calibration")
    return {"pairs": doc.get("pairs", []), "summary": doc.get("summary", ""), "measured_utc": doc.get("measured_utc"),
            "current": current, "other": other, "tolerance_m": doc.get("tolerance_m")}


def _number(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def lines_for(project, pair):
    """Indexes of AlignPro's lines (passes) recorded as the pair's two lines: by recording file name, else by line
    number."""
    files = [set(f) for f in pair.get("files", [[], []])]
    nums = [_number(v) for v in pair.get("line_numbers", [None, None])]
    out = []
    for i, ln in enumerate(getattr(project, "lines", []) or []):
        d = ln.data if hasattr(ln, "data") else {}
        info = d.get("info", {}) if isinstance(d, dict) else {}
        base = set(info.get("files") or [os.path.basename(p) for p in info.get("paths") or [] if p])
        hit = any(base & f for f in files if f)
        if not hit:
            hit = _number(info.get("line")) in [n for n in nums if n is not None]
        if hit:
            out.append(i)
    return out


def windows(pair, k=0):
    """[(t0, t1)] UTC when the pair's line k was over the shared surfaces."""
    return [tuple(w["utc"]) for w in (pair.get("windows") or [[], []])[k]]


def corridor(project, pair, half_width=HALF_WIDTH):
    """AOI ring (lon, lat degrees) half_width each side of the first line's drive line over the shared stretch
    (from the first to the last window), from AlignPro's own trajectory."""
    ws = windows(pair, 0) or windows(pair, 1)
    traj, frame = getattr(project, "traj", None), getattr(project, "frame", None)
    if not ws or traj is None or frame is None:
        return None
    t0, t1 = min(w[0] for w in ws), max(w[1] for w in ws)
    t0, t1 = max(t0, traj.t[0]), min(t1, traj.t[-1])
    if t1 - t0 < 2 * STEP_S:
        return None
    t = np.arange(t0, t1 + 1e-9, STEP_S)
    lat, lon, h = traj.interp(t)[:3]
    enu = frame.from_geodetic(lat, lon, h)
    xy = enu[:, :2]
    d = np.gradient(xy, axis=0)
    s = np.hypot(d[:, 0], d[:, 1])
    keep = s > 1e-3                              # standing still: no direction
    if keep.sum() < 2:
        return None
    xy, d, s, z = xy[keep], d[keep], s[keep], enu[keep, 2]
    left = np.stack([-d[:, 1], d[:, 0]], 1) / s[:, None]
    ring = np.concatenate([xy + half_width * left, (xy - half_width * left)[::-1]])
    zz = np.concatenate([z, z[::-1]])
    lat_r, lon_r = frame.to_geodetic(np.column_stack([ring, zz]))[:2]
    pts = [[float(np.degrees(lo)), float(np.degrees(la))] for la, lo in zip(lat_r, lon_r)]
    pts.append(pts[0])
    return pts


def describe(pair):
    """One line for a pair: 'Line 4 / Line 5, both ways: 4.2 cm along the road each pass ...'."""
    st = pair.get("steady_m") or {}
    drv = {"opposite": "both ways", "same": "the same way", "crossing": "crossing"}.get(pair.get("relation"), "")
    parts = []
    for k, lab in (("along", "along"), ("cross", "sideways"), ("up", "up")):
        if st.get(k) is not None:
            parts.append("%s %+.1f cm" % (lab, 100 * st[k]))
    if pair.get("roll_m_per_m") is not None:
        parts.append("roll %.1f cm per 10 m" % (1000 * pair["roll_m_per_m"]))
    return "%s, %s: %s" % (" / ".join(pair.get("lines", [])), drv, ", ".join(parts))
