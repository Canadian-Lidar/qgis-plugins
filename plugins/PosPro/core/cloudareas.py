"""Problem areas found in the point cloud (PosPro 2.2.0): where CloudPro's line separation check saw one line's
points disagree with the other lines' (03_CloudPro/out/problem_areas.json, dcs.problem_areas/1).

They are listed in Trajectory Review with the anomalies (id C1, C2 ..., found by "Point cloud") and drawn in the
Trajectory editor, so the stretch can be bridged with the IMU, marked poor, interpolated or accepted like any other
window. They are not stored in the review: they are read from CloudPro's file each time, and they belong to the
trajectory the points were georeferenced with ("current" False once PosPro has finalized another one).

    load(project) -> {"areas": [...], "current": bool, "summary", "measured_utc", "tolerance_m"} | None
    anomalies(project) -> [anomaly dicts like review.detect()'s, kind "cloud"]
"""
import json
import os

SCHEMA = "dcs.problem_areas/1"
NAME = "problem_areas.json"
KIND = "cloud"
_CACHE = {}


def path(project):
    dcs = getattr(project, "dcs", None)
    if dcs is None:
        return None
    try:
        return os.path.join(dcs.stage_dir("cloudpro", "out", create=False), NAME)
    except Exception:                           # noqa: BLE001
        return None


def _pospro_id(project):
    try:
        return project.dcs.stage("pospro").get("handover_id")
    except Exception:                           # noqa: BLE001
        return None


def load(project):
    """CloudPro's problem areas for this job, or None (no DCS project, not checked, unreadable)."""
    p = path(project)
    if not p or not os.path.exists(p):
        return None
    try:
        st = os.stat(p)
        key = (p, st.st_size, st.st_mtime_ns)
        doc = _CACHE.get(key)
        if doc is None:
            with open(p, encoding="utf-8") as f:
                doc = json.load(f)
            _CACHE.clear()
            _CACHE[key] = doc
    except (OSError, ValueError):
        return None
    if str(doc.get("schema", "")).split("/")[0] != SCHEMA.split("/")[0]:
        return None
    hid = (doc.get("trajectory") or {}).get("handover_id")
    current = bool(hid) and hid == _pospro_id(project)
    return {"areas": doc.get("areas", []), "current": current, "summary": doc.get("summary", ""),
            "measured_utc": doc.get("measured_utc"), "tolerance_m": doc.get("tolerance_m"),
            "trajectory": doc.get("trajectory") or {}, "by": doc.get("by", "")}


def gps_span(t):
    """(t0, t1) GPS seconds (continuous, as PosPro's trajectory time) of a time entry, or None."""
    w, s = t.get("gps_week"), t.get("gps_sow")
    if w is None or not s:
        return None
    return w * 604800.0 + float(s[0]), w * 604800.0 + float(s[1])


def anomalies(project, extend=(0.0, 0.0)):
    """The problem areas as review anomalies: id "C<n>" (C<n>a, C<n>b ... when the line at fault is not known
    and each line of the pair gets its own time window), kind "cloud", t0 / t1 (GPS s, widened by `extend`
    seconds before / after), max (m), line (the line whose time this is), lines, e, n, current."""
    doc = load(project)
    if doc is None:
        return []
    out = []
    for a in doc["areas"]:
        times = a.get("times") or ([a] if a.get("gps_week") is not None else [])
        n_t = len(times)
        for k, t in enumerate(times):
            sp = gps_span(t)
            if sp is None:
                continue
            aid = "C%d" % a.get("id", len(out) + 1) + ("abcdefghij"[k] if n_t > 1 and k < 10 else "")
            out.append({"id": aid, "kind": KIND, "t0": sp[0] - extend[0], "t1": sp[1] + extend[1],
                        "max": float(a.get("max_m") or 0.0), "median": float(a.get("median_m") or 0.0),
                        "line": t.get("line") or a.get("line"), "at_fault": a.get("line") is not None,
                        "lines": a.get("lines", []), "e": a.get("e"), "n": a.get("n"),
                        "area_m2": a.get("area_m2"), "current": doc["current"]})
    return out


def describe(a):
    """'17 cm, Line 3' (+ ', earlier trajectory' when it was measured on another trajectory)."""
    who = a.get("line") or " / ".join(a.get("lines") or [])
    if not a.get("at_fault", True):
        who += " (or %s)" % " / ".join(x for x in a.get("lines", []) if x != a.get("line"))
    s = "%.0f cm, %s" % (100 * a.get("max", 0.0), who)
    if not a.get("current", True):
        s += ", earlier trajectory"
    return s
