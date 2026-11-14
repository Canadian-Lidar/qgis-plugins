"""Marine jobs (sonar recordings from the survey boat): the patch test for each sonar head.

Solves, per head, corrections to roll, pitch and heading (deg) and the latency (s) so the seabed agrees between
different passes of the same head and between the two heads (point-to-plane, as AlignPro and StripPro do for the
Lidar). The soundings are placed with PosPro's finalized boat trajectory, each head's own attitude and its lever
arm (dcs_core.marine.georef_local).

Observations: the soundings of one head on one pass (a straight run: the heading turns less than 45 deg) are cut
into PATCH-metre cells; a cell with a flat seabed (thickness <= MAX_THICKNESS, >= MIN_POINTS) gives a plane; the
soundings of another pass or of the other head in the same cell are compared with it along its normal. Gauss-
Newton with numerical derivatives, robust weights (Huber) and priors (PRIOR_SD) so a correction the lines cannot
see stays near zero; each correction's uncertainty and whether the data determine it are reported.
"""
import math
import os

import numpy as np

PATCH = 2.0
MIN_POINTS = 15
MAX_THICKNESS = 0.20
MAX_SEP = 1.5
SAME_PASS_GAP_S = 30.0
PRIOR_SD = {"roll": 3.0, "pitch": 3.0, "heading": 5.0, "latency_s": 0.10}
STEP = {"roll": 0.02, "pitch": 0.02, "heading": 0.05, "latency_s": 0.002}
PARAMS = ("roll", "pitch", "heading", "latency_s")
MAX_OBS = 400_000
MIN_GAIN_CM = 0.3
RMAX = 40.0          # m: the far range (grazing angles) bends with the sound speed and is left out


class MarineError(Exception):
    pass


def is_marine(dcs):
    try:
        return bool(dcs.sonar_clraw_files()) and not dcs.survey_clraw_files()
    except Exception:       # noqa: BLE001
        return False


def load(dcs, every=2, power="medium", rmax=RMAX):
    """{"traj": LocalTrajectory, "heads": {did: {"S": soundings, "lever", "nickname", "side" (looks to)}}, "doc": the
    trajectory handover} from PosPro's finalized marine trajectory and the job's sonar recordings."""
    from dcs_core import clraw, handover, marine, sbet
    doc = handover.read(dcs, "pospro", required=False)
    if doc is None or doc.get("platform") != "marine":
        raise MarineError("PosPro has not finalized the boat's trajectory (PosPro, Marine page)")
    tm = doc.get("time") or {}
    ws = sbet.week_start(int(tm.get("week") or 0))
    S = np.fromfile(doc["paths"]["sbet"], sbet.SBET)
    t = ws + S["t"] - float(tm.get("leap_seconds", 18))
    traj = marine.LocalTrajectory(t, np.degrees(S["lat"]), np.degrees(S["lon"]), S["h"], S["roll"], S["pitch"],
                                  S["heading"])
    groups = clraw.group_recordings(dcs.sonar_clraw_files(), sonar=True)
    heads = {}
    for _k, paths in groups.items():
        rec = marine.SonarRecording(paths)
        for did, h in rec.heads.items():
            if doc.get("sensors") and "sonar_%d" % did not in doc["sensors"]:
                continue                                  # switched off in PosPro
            att = None
            p = (doc.get("paths") or {}).get("sonar_%d_attitude" % did)
            if p and os.path.exists(p):
                att = np.load(p)
            s = marine.sample_soundings(rec, did, every=every, power=power, rmax=rmax, attitude=att)
            if s is None:
                continue
            sens = (doc.get("sensors") or {}).get("sonar_%d" % did) or {}
            e = heads.setdefault(did, {"parts": [], "lever": sens.get("lever_m") or h.get("lever_m") or [0.0, 0.0, 0.0],
                                       "lever_source": sens.get("lever_source") or h.get("lever_source"),
                                       "nickname": h.get("nickname"), "side": h.get("looks") or h.get("side"),
                                       "recordings": []})
            e["parts"].append(s)
            e["recordings"].append(rec.name)
    for did, e in heads.items():
        e["S"] = {k: np.concatenate([p[k] for p in e["parts"]]) for k in e["parts"][0]}
        del e["parts"]
    if not heads:
        raise MarineError("no soundings in the job's sonar recordings")
    return {"traj": traj, "heads": heads, "doc": doc}


def passes(traj, t):
    """Pass number of each time t: the boat's straight runs (dcs_core.marine.split_passes, as CloudPro's lines)."""
    from dcs_core import marine as dm
    spans = dm.split_passes(traj.t, traj.heading, float(np.min(t)), float(np.max(t)) + 1e-3)
    return dm.pass_of(spans, t).astype(np.int32)


def _cal_vector(cal, heads):
    return np.array([float((cal.get(str(d)) or {}).get(k, 0.0)) for d in heads for k in PARAMS])


def _cal_dict(x, heads):
    return {str(d): {k: float(x[i * len(PARAMS) + j]) for j, k in enumerate(PARAMS)} for i, d in enumerate(heads)}


def _georef_all(data, x, heads):
    from dcs_core import marine
    out = []
    cal = _cal_dict(x, heads)
    for d in heads:
        h = data["heads"][d]
        out.append(marine.georef_local(h["S"], data["traj"], h["lever"], cal[str(d)]))
    return out


def build_observations(data, x, heads, rng=None):
    """Planes (one group's soundings in a cell) and the other groups' soundings in the same cell. -> dict of
    index arrays into the concatenated soundings: obs (point), plane members (for its centroid), normals."""
    rng = rng or np.random.default_rng(1)
    P = _georef_all(data, x, heads)
    allp = np.vstack(P)
    head_of = np.concatenate([np.full(len(p), i) for i, p in enumerate(P)])
    t = np.concatenate([data["heads"][d]["S"]["t"] for d in heads])
    pas = passes(data["traj"], t)
    group = head_of.astype(np.int64) * 10000 + pas
    cx = np.floor(allp[:, 0] / PATCH).astype(np.int64)
    cy = np.floor(allp[:, 1] / PATCH).astype(np.int64)
    cell = (cx << 32) ^ (cy & 0xFFFFFFFF)
    key = np.lexsort((group, cell))
    c_s, g_s = cell[key], group[key]
    brk = np.flatnonzero((np.diff(c_s) != 0) | (np.diff(g_s) != 0)) + 1
    starts, ends = np.r_[0, brk], np.r_[brk, len(key)]
    planes = []                                   # (cell, group, member idx, normal)
    for a, b in zip(starts, ends):
        if b - a < MIN_POINTS:
            continue
        idx = key[a:b]
        q = allp[idx]
        c = q.mean(0)
        w, v = np.linalg.eigh(np.cov((q - c).T))
        if math.sqrt(max(w[0], 0)) > MAX_THICKNESS:
            continue
        n = v[:, 0] * (1 if v[2, 0] >= 0 else -1)
        if abs(n[2]) < 0.7:                       # a seabed, not a wall or a fish
            continue
        planes.append((c_s[a], g_s[a], idx, n))
    by_cell = {}
    for k, pl in enumerate(planes):
        by_cell.setdefault(pl[0], []).append(k)
    obs_pt, obs_plane = [], []
    for cel, ks in by_cell.items():
        if len(ks) < 2:
            continue
        for i in ks:
            for j in ks:
                if i == j:
                    continue
                gi, gj = planes[i][1], planes[j][1]
                if gi // 10000 == gj // 10000 and abs(gi - gj) == 0:
                    continue
                pts = planes[j][2]
                if len(pts) > 12:
                    pts = rng.choice(pts, 12, replace=False)
                obs_pt.append(pts)
                obs_plane.append(np.full(len(pts), i))
    if not obs_pt:
        raise MarineError("no seabed is seen twice (by two passes or by both heads): the patch test needs lines "
                          "that overlap")
    op = np.concatenate(obs_pt)
    opl = np.concatenate(obs_plane)
    if len(op) > MAX_OBS:
        s = rng.choice(len(op), MAX_OBS, replace=False)
        op, opl = op[s], opl[s]
    members = [pl[2] for pl in planes]
    normals = np.array([pl[3] for pl in planes])
    pl_head = np.array([pl[1] // 10000 for pl in planes])
    between = head_of[op] != pl_head[opl]
    return {"op": op, "opl": opl, "members": members, "normals": normals, "n_planes": len(planes),
            "between": between, "group": group, "plane_group": np.array([pl[1] for pl in planes]),
            "between_heads_share": float(between.mean()), "passes": int(pas.max() + 1)}


def compact(data, heads, obs, members_per_plane=24, rng=None):
    """Only the soundings the observations use (the compared points and up to members_per_plane of each plane),
    so each evaluation georeferences tens of thousands of soundings, not millions."""
    rng = rng or np.random.default_rng(2)
    used = np.unique(obs["opl"])
    remap = -np.ones(len(obs["members"]), np.int64)
    remap[used] = np.arange(len(used))
    mem, mpl = [], []
    for k in used:
        m = obs["members"][k]
        if len(m) > members_per_plane:
            m = rng.choice(m, members_per_plane, replace=False)
        mem.append(m)
        mpl.append(np.full(len(m), remap[k]))
    mem, mpl = np.concatenate(mem), np.concatenate(mpl)
    U, inv = np.unique(np.r_[obs["op"], mem], return_inverse=True)
    sizes = [len(data["heads"][d]["S"]["t"]) for d in heads]
    off = np.r_[0, np.cumsum(sizes)]
    sub = []
    for i, d in enumerate(heads):
        sel = U[(U >= off[i]) & (U < off[i + 1])] - off[i]
        S = data["heads"][d]["S"]
        sub.append({k: v[sel] for k, v in S.items()})
    n_op = len(obs["op"])
    return {"sub": sub, "op": inv[:n_op], "opl": remap[obs["opl"]], "mem": inv[n_op:], "mpl": mpl,
            "normals": obs["normals"][used], "count": np.bincount(mpl, minlength=len(used)).astype(float),
            "between": obs["between"], "balance": _balance(obs["between"])}


def _balance(between):
    """Weights so the comparisons between the heads count as much as those between passes of one head (they are
    far fewer, and only they tie the heads to each other)."""
    nb, nw = int(between.sum()), int((~between).sum())
    w = np.ones(len(between))
    if nb and nw:
        w[between] = min(20.0, nw / nb)
    return w


def score(r, C, keep=None):
    """cm: the mean of the median disagreement between passes of a head and between the heads."""
    keep = np.abs(r) <= MAX_SEP if keep is None else keep
    out = []
    for m in (~C["between"], C["between"]):
        mm = m & keep
        if mm.any():
            out.append(100.0 * float(np.median(np.abs(r[mm]))))
    return float(np.mean(out)) if out else 0.0, out


def residuals(data, x, heads, C):
    cal = _cal_dict(x, heads)
    from dcs_core import marine
    P = np.vstack([marine.georef_local(C["sub"][i], data["traj"], data["heads"][d]["lever"], cal[str(d)])
                   for i, d in enumerate(heads)])
    M = P[C["mem"]]
    cent = np.column_stack([np.bincount(C["mpl"], M[:, k], len(C["count"])) for k in range(3)]) / C["count"][:, None]
    return ((P[C["op"]] - cent[C["opl"]]) * C["normals"][C["opl"]]).sum(1)


def _gauss_newton(data, heads, C, x, free, iterations=6, cancelled=None):
    prior = np.array([PRIOR_SD[k] for _d in heads for k in PARAMS])
    steps = np.array([STEP[k] for _d in heads for k in PARAMS])
    x0 = np.zeros(len(x))
    x = x.copy()
    r = residuals(data, x, heads, C)
    keep = np.abs(r) <= MAX_SEP                    # a sounding off by more is not the same seabed
    J = None
    for _it in range(iterations if free.any() else 0):
        if cancelled and cancelled():
            break
        J = np.zeros((len(r), len(x)))
        for k in np.flatnonzero(free):
            xk = x.copy()
            xk[k] += steps[k]
            J[:, k] = (residuals(data, xk, heads, C) - r) / steps[k]
        s = 1.4826 * np.median(np.abs(r[keep] - np.median(r[keep]))) or 0.01
        w = np.minimum(1.0, 1.5 * s / np.maximum(np.abs(r), 1e-9)) * keep * C["balance"]       # Huber
        sw = np.sqrt(w)
        Jf, rf = J[:, free] * sw[:, None], r * sw
        Pd = np.diag((s / prior[free]) ** 2)
        dx = -np.linalg.solve(Jf.T @ Jf + Pd, Jf.T @ rf + Pd @ (x[free] - x0[free]))
        x[free] += dx
        r = residuals(data, x, heads, C)
        if np.max(np.abs(dx / steps[free])) < 0.5:
            break
    sd = np.full(len(x), np.nan)
    corr = None
    if J is not None:
        s = 1.4826 * np.median(np.abs(r[keep] - np.median(r[keep]))) or 0.01
        w = np.minimum(1.0, 1.5 * s / np.maximum(np.abs(r), 1e-9)) * keep * C["balance"]
        Jf = J[:, free] * np.sqrt(w)[:, None]
        try:
            # the soundings of one plane are not independent: count each compared plane pair once
            eff = max(1.0, float((keep * C["balance"]).sum()) / max(1, len(np.unique(C["opl"][keep]))))
            Cv = np.linalg.inv(Jf.T @ Jf + np.diag((s / prior[free]) ** 2)) * s * s * eff
            sd[free] = np.sqrt(np.diag(Cv))
            dd = np.sqrt(np.diag(Cv))
            corr = Cv / np.outer(dd, dd)
        except np.linalg.LinAlgError:
            pass
    return x, np.where(keep, r, np.inf), sd, corr


def solve(data, init=None, latency=False, progress=None, cancelled=None, log=None):
    """The patch test, one step at a time, so a correction is kept only when the seabed agrees better with it:
    roll for every head (seen as the seabed tilting the other way on the reciprocal pass); then, per head, pitch
    and / or heading (both move the seabed along the track, so they are kept only when they gain at least
    MIN_GAIN_CM and one of them is preferred when they cannot be told apart); then the latency when asked (it
    also moves the seabed along the track, as much as the pitch at the boat's speed and depth).
    -> {"heads": {did: {param, param_sd, param_determined}}, "before_cm", "after_cm", "steps", ...}."""
    say = log or (lambda *_: None)
    heads = sorted(data["heads"])
    x = _cal_vector(init or {}, heads)
    nP = len(PARAMS)

    def mask(sets):
        return np.array([k in sets.get(d, ()) for d in heads for k in PARAMS])

    def cm(r):
        return score(r, C)[0]

    obs = build_observations(data, x, heads)
    C = compact(data, heads, obs)
    r0 = residuals(data, x, heads, C)
    before = cm(r0)
    parts0 = score(r0, C)[1]
    steps_log = [{"step": "as recorded", "cm": before}]
    say("As recorded: the seabed agrees to %.1f cm (median)" % before)
    sets = {d: {"roll"} for d in heads}
    plan = 3 + 3 * len(heads) + (1 if latency else 0)
    done = [0]

    def tick():
        done[0] += 1
        if progress:
            progress(min(1.0, done[0] / plan))

    x, r, sd, corr = _gauss_newton(data, heads, C, x, mask(sets), cancelled=cancelled)
    tick()
    # cut the planes again near the solution (the roll moves the outer beams most)
    obs = build_observations(data, x, heads)
    C = compact(data, heads, obs)
    x, r, sd, corr = _gauss_newton(data, heads, C, x, mask(sets), cancelled=cancelled)
    tick()
    base = cm(r)
    steps_log.append({"step": "roll", "cm": base})
    say("Roll: %.1f cm" % base)
    for d in heads:
        best = None
        for trial in (("pitch",), ("heading",), ("pitch", "heading")):
            if cancelled and cancelled():
                break
            s2 = {k: set(v) for k, v in sets.items()}
            s2[d] |= set(trial)
            x2, r2, sd2, c2 = _gauss_newton(data, heads, C, x, mask(s2), cancelled=cancelled)
            tick()
            g = base - cm(r2)
            say("  head %s + %s: %.1f cm (gain %.2f)" % (d, " + ".join(trial), cm(r2), g))
            # two parameters only when the pair gains clearly more than the better single one
            if best is None or g > best[0] + (MIN_GAIN_CM if len(trial) > 1 else 0.0):
                best = (g, trial, x2, r2, sd2, c2)
        if best and best[0] >= MIN_GAIN_CM:
            sets[d] |= set(best[1])
            x, r, sd, corr = best[2], best[3], best[4], best[5]
            base = cm(r)
            steps_log.append({"step": "head %s %s" % (d, " + ".join(best[1])), "cm": base})
    if latency and not (cancelled and cancelled()):
        s2 = {k: set(v) | {"latency_s"} for k, v in sets.items()}
        x2, r2, sd2, c2 = _gauss_newton(data, heads, C, x, mask(s2), cancelled=cancelled)
        tick()
        g = base - cm(r2)
        say("Latency: %.1f cm (gain %.2f)" % (cm(r2), g))
        if g >= MIN_GAIN_CM:
            sets, x, r, sd, corr, base = s2, x2, r2, sd2, c2, cm(r2)
            steps_log.append({"step": "latency", "cm": base})
    # final cut and uncertainty
    x, r, sd, corr = _gauss_newton(data, heads, C, x, mask(sets), iterations=2, cancelled=cancelled)
    tick()
    free_names = [(d, k) for d in heads for k in PARAMS if k in sets[d]]
    out = {}
    for i, d in enumerate(heads):
        e = {}
        for j, k in enumerate(PARAMS):
            v, u = float(x[i * nP + j]), float(sd[i * nP + j])
            e[k] = v
            e[k + "_sd"] = None if np.isnan(u) else u
            e[k + "_determined"] = bool(k in sets[d] and not np.isnan(u) and u < 0.5 * PRIOR_SD[k])
        out[str(d)] = e
    high = []
    if corr is not None:
        for a in range(len(free_names)):
            for b in range(a + 1, len(free_names)):
                if abs(corr[a, b]) > 0.9:
                    high.append(["%s %s" % free_names[a], "%s %s" % free_names[b], float(corr[a, b])])
    parts1 = score(r, C)[1]
    return {"heads": out, "before_cm": before, "after_cm": cm(r), "steps": steps_log,
            "before_parts_cm": parts0, "after_parts_cm": parts1,
            "estimated": {str(d): sorted(sets[d]) for d in heads}, "correlated": high,
            "observations": int(len(C["op"])), "planes": int(len(C["count"])), "passes": obs["passes"],
            "between_heads_share": obs["between_heads_share"], "latency_tried": bool(latency)}


def publish(dcs, result, data, applied=None, accepted=None):
    """Finalize the per-head values in use (dcs.alignment/1, platform marine, heads section) for CloudPro. applied:
    {d: {roll, pitch, heading, latency_s, lever_m (optional)}} - the values in use (default: the solution);
    accepted: {d: {param: bool}} which solved values the user accepted."""
    from dcs_core import handover
    from .project import PLUGIN, VERSION
    result = result or {"heads": {}}
    rh = result.get("heads") or {}
    heads = applied or {d: {k: v[k] for k in PARAMS} for d, v in rh.items()}
    out = {}
    for d, vals in heads.items():
        h = data["heads"].get(int(d)) or data["heads"].get(d) or {}
        e = dict(vals, nickname=h.get("nickname"), side=h.get("side"))
        if d in rh:
            e["solved"] = {k: rh[d].get(k) for k in PARAMS if k in (result.get("estimated") or {}).get(d, [])}
            e["sd"] = {k: rh[d].get(k + "_sd") for k in PARAMS}
            e["determined"] = {k: rh[d].get(k + "_determined") for k in PARAMS}
        e["accepted"] = {k: bool(((accepted or {}).get(d) or {}).get(k)) for k in PARAMS}
        if vals.get("lever_m"):
            e["lever_source"] = "adjusted in AlignPro (PosPro: %s)" % ", ".join("%+.3f" % v for v in h.get("lever") or [])
        out[d] = e
    fields = {"platform": "marine", "heads": out,
              "convention": {"name": "marine heads: corrections added to each sonar's own attitude and to the "
                                     "vessel heading; latency added to the ping time; lever_m replaces PosPro's offset"},
              "trajectory": {"handover_id": (data.get("doc") or {}).get("id")},
              "quality": {"before_cm": result.get("before_cm"), "after_cm": result.get("after_cm"),
                          "observations": result.get("observations"), "passes": result.get("passes"),
                          "between_heads_share": result.get("between_heads_share")},
              "method": "per-head patch test: seabed point-to-plane between passes and between heads; values "
                        "reviewed and accepted in AlignPro"}
    used = {"pospro": (data.get("doc") or {}).get("id")} if (data.get("doc") or {}).get("id") else None
    return handover.publish(dcs, "alignpro", {}, fields, PLUGIN, VERSION, "marine patch test", used=used,
                            inputs=sorted(dcs.sonar_clraw_files()))


# ============================================================================= manual review (AlignPro 1.11.0)
# values = {"<device id>": {"roll", "pitch", "heading" (deg), "latency_s", "lever_m" [F, S, D] (optional)}}: the
# corrections being looked at. Absent keys are 0 (lever: PosPro's).

def with_levers(data, values):
    """data with each head's lever replaced by values[d]["lever_m"] when given (a shallow copy)."""
    out = dict(data)
    out["heads"] = {}
    for d, h in data["heads"].items():
        v = (values or {}).get(str(d)) or {}
        out["heads"][d] = dict(h, lever=list(v["lever_m"])) if v.get("lever_m") else h
    return out


def georef_head(data, d, values, idx=None):
    """Soundings of head d (all, or idx) placed with values -> (n, 3) local east / north / up."""
    from dcs_core import marine
    h = data["heads"][d]
    v = (values or {}).get(str(d)) or {}
    S = h["S"] if idx is None else {k: a[idx] for k, a in h["S"].items()}
    return marine.georef_local(S, data["traj"], v.get("lever_m") or h["lever"], {k: v.get(k, 0.0) for k in PARAMS})


def pass_ids(data, d):
    h = data["heads"][d]
    if "pass" not in h:
        h["pass"] = passes(data["traj"], h["S"]["t"])
    return h["pass"]


def pass_spans(data):
    """[(start, end, heading deg, length m, speed m/s)] of the boat's passes over the soundings' time."""
    from dcs_core import marine as dm
    tr = data["traj"]
    t = np.concatenate([h["S"]["t"] for h in data["heads"].values()])
    out = []
    for a, b in dm.split_passes(tr.t, tr.heading, float(t.min()), float(t.max()) + 1e-3):
        m = (tr.t >= a) & (tr.t < b)
        if m.sum() < 2:
            out.append((a, b, float("nan"), 0.0, 0.0))
            continue
        L = float(np.sum(np.hypot(np.diff(tr.E[m]), np.diff(tr.N[m]))))
        out.append((a, b, float(np.degrees(np.median(tr.heading[m])) % 360), L, L / max(b - a, 1e-3)))
    return out


def view_sample(data, per_head=300000, seed=4):
    """Indices of a fixed random sample of each head's soundings, for the plan view."""
    rng = np.random.default_rng(seed)
    out = {}
    for d, h in data["heads"].items():
        n = len(h["S"]["t"])
        out[d] = np.sort(rng.choice(n, min(n, per_head), replace=False))
    return out


def plan(data, values, sample):
    """x, y, z, head, pass of the sampled soundings placed with values."""
    xs, ys, zs, hs, ps = [], [], [], [], []
    for d, idx in sample.items():
        P = georef_head(data, d, values, idx)
        xs.append(P[:, 0]); ys.append(P[:, 1]); zs.append(P[:, 2])
        hs.append(np.full(len(idx), d)); ps.append(pass_ids(data, d)[idx])
    out = [np.concatenate(v) for v in (xs, ys, zs, hs, ps)]
    ok = np.isfinite(out[0]) & np.isfinite(out[1]) & np.isfinite(out[2])
    return tuple(v[ok] for v in out)


class Section:
    """The soundings within `width` of the line p0-p1 (picked with the values when the line was drawn); place()
    re-places only those with other values, so nudging a correction redraws at once."""

    def __init__(self, data, values, p0, p1, width=2.0, cap=150000):
        self.data, self.p0, self.p1, self.width = data, np.asarray(p0, float), np.asarray(p1, float), float(width)
        u = self.p1 - self.p0
        self.L = float(np.hypot(*u)) or 1.0
        self.u = u / self.L
        self.idx = {}
        for d in data["heads"]:
            P = georef_head(data, d, values)
            a, c = self._ac(P)
            m = np.flatnonzero((a >= -self.width) & (a <= self.L + self.width) & (np.abs(c) <= self.width * 3))
            if len(m) > cap:
                m = np.sort(np.random.default_rng(1).choice(m, cap, replace=False))
            self.idx[d] = m

    def _ac(self, P):
        r = P[:, :2] - self.p0
        return r @ self.u, r @ np.array([-self.u[1], self.u[0]])

    def place(self, values):
        """-> along, height, head, pass, across of the section's soundings with values (|across| <= width)."""
        out = [[], [], [], [], []]
        for d, idx in self.idx.items():
            if not len(idx):
                continue
            P = georef_head(self.data, d, values, idx)
            a, c = self._ac(P)
            m = (np.abs(c) <= self.width) & np.isfinite(a) & np.isfinite(P[:, 2])
            out[0].append(a[m]); out[1].append(P[m, 2]); out[2].append(np.full(int(m.sum()), d))
            out[3].append(pass_ids(self.data, d)[idx][m]); out[4].append(c[m])
        if not out[0]:
            return tuple(np.zeros(0) for _ in range(5))
        return tuple(np.concatenate(o) for o in out)


def _x_of(values, heads):
    return _cal_vector({str(d): {k: (values.get(str(d)) or {}).get(k, 0.0) for k in PARAMS} for d in heads}, heads)


def pair_table(data, values):
    """Overlapping pairs of (head, pass): [{a, b, kind, n, median_cm, signed_cm}] at values; signed = how far b's
    seabed lies above a's. kind: 'between heads' / 'reciprocal' (passes > 120 deg apart) / 'same direction'."""
    heads = sorted(data["heads"])
    dv = with_levers(data, values)
    x = _x_of(values, heads)
    obs = build_observations(dv, x, heads)
    C = compact(dv, heads, obs)
    r = residuals(dv, x, heads, C)
    grp_pt = obs["group"][obs["op"]]
    grp_pl = obs["plane_group"][obs["opl"]]
    spans = pass_spans(data)
    hdg = {i: s[2] for i, s in enumerate(spans)}
    rows = []
    key = grp_pl * 100000 + grp_pt
    for k in np.unique(key):
        m = (key == k) & (np.abs(r) <= MAX_SEP)
        if m.sum() < 30:
            continue
        ga, gb = int(k // 100000), int(k % 100000)
        ha, pa, hb, pb = heads[ga // 10000], ga % 10000, heads[gb // 10000], gb % 10000
        if ha != hb:
            kind = "between heads"
        else:
            dh = abs((hdg.get(pa, 0) - hdg.get(pb, 0) + 180) % 360 - 180)
            kind = "reciprocal" if dh > 120 else "same direction" if dh < 60 else "crossing"
        n_ = obs["normals"][obs["opl"]][m][:, 2]
        rows.append({"a": (ha, pa), "b": (hb, pb), "kind": kind, "n": int(m.sum()),
                     "median_cm": 100 * float(np.median(np.abs(r[m]))),
                     "signed_cm": 100 * float(np.median(r[m] * np.sign(n_)))})
    # each unordered pair once (merge a/b and b/a, flipping the sign)
    merged = {}
    for row in rows:
        k = tuple(sorted([row["a"], row["b"]]))
        s = 1 if (row["a"], row["b"]) == k else -1
        e = merged.setdefault(k, {"a": k[0], "b": k[1], "kind": row["kind"], "n": 0, "w_med": 0.0, "w_sig": 0.0})
        e["n"] += row["n"]
        e["w_med"] += row["n"] * row["median_cm"]
        e["w_sig"] += row["n"] * row["signed_cm"] * s
    out = [{"a": e["a"], "b": e["b"], "kind": e["kind"], "n": e["n"], "median_cm": e["w_med"] / e["n"],
            "signed_cm": e["w_sig"] / e["n"]} for e in merged.values()]
    return sorted(out, key=lambda e: (-e["n"]))


def residual_profiles(data, values, angle_bin=5.0, time_bin=30.0):
    """Per head: the median disagreement against the beam's angle from the vertical (+ outboard: roll and sound
    speed show here) and against time (pitch / latency / heading show as steps between passes).
    -> {d: {"angle": (centres, median_cm, count), "time": (centres, median_cm, count)}}"""
    from dcs_core import marine
    heads = sorted(data["heads"])
    dv = with_levers(data, values)
    x = _x_of(values, heads)
    obs = build_observations(dv, x, heads)
    C = compact(dv, heads, obs)
    r = residuals(dv, x, heads, C)
    sizes = [len(dv["heads"][d]["S"]["t"]) for d in heads]
    off = np.r_[0, np.cumsum(sizes)]
    sgn = np.sign(obs["normals"][obs["opl"]][:, 2])
    out = {}
    for i, d in enumerate(heads):
        m = (obs["op"] >= off[i]) & (obs["op"] < off[i + 1]) & (np.abs(r) <= MAX_SEP)
        j = obs["op"][m] - off[i]
        S = dv["heads"][d]["S"]
        v = marine.level_vectors(marine.rays(S["angle"][j], S["tof"][j], S["sos"][j]), S["up"][j])
        side = 1.0 if np.median(v[:, 1]) >= 0 else -1.0
        ang = np.degrees(np.arctan2(v[:, 1] * side, -v[:, 2]))
        rr = 100 * r[m] * sgn[m]
        prof = {}
        for name, val, w in (("angle", ang, angle_bin), ("time", S["t"][j] - S["t"].min(), time_bin)):
            b = np.floor(val / w).astype(int)
            cen, med, cnt = [], [], []
            for k in np.unique(b):
                mm = b == k
                if mm.sum() >= 20:
                    cen.append((k + 0.5) * w); med.append(float(np.median(rr[mm]))); cnt.append(int(mm.sum()))
            prof[name] = (np.array(cen), np.array(med), np.array(cnt))
        out[d] = prof
    return out


def difference_grid(data, values, cell=2.0, sample=None):
    """Plan grid of how far the seabed seen by different passes / heads disagrees: per cell the range (max - min)
    of the (head, pass) medians where at least two of them have >= 5 soundings. -> (x0, y0, cell, grid) with NaN
    where nothing overlaps."""
    xs, ys, zs, hs, ps = plan(data, values, sample or view_sample(data, 400000))
    ok = np.isfinite(xs) & np.isfinite(ys) & np.isfinite(zs)
    if ok.any():                          # the extent from the bulk of the soundings (a stray one is left out)
        x_lo, x_hi = np.percentile(xs[ok], [0.1, 99.9])
        y_lo, y_hi = np.percentile(ys[ok], [0.1, 99.9])
        ok &= (xs >= x_lo) & (xs <= x_hi) & (ys >= y_lo) & (ys <= y_hi)
    xs, ys, zs, hs, ps = xs[ok], ys[ok], zs[ok], hs[ok], ps[ok]
    if not len(xs):
        return 0.0, 0.0, float(cell), np.full((1, 1), np.nan)
    while ((xs.max() - xs.min()) / cell + 1) * ((ys.max() - ys.min()) / cell + 1) > 25e6:
        cell *= 2.0
    g = hs.astype(np.int64) * 10000 + ps
    ix = np.floor((xs - xs.min()) / cell).astype(np.int64)
    iy = np.floor((ys - ys.min()) / cell).astype(np.int64)
    nx = int(ix.max()) + 1
    key = (iy * nx + ix) * 1000000 + g
    o = np.lexsort((zs, key))
    ks, zz = key[o], zs[o]
    brk = np.flatnonzero(np.diff(ks)) + 1
    st, en = np.r_[0, brk], np.r_[brk, len(ks)]
    cnt = en - st
    med = 0.5 * (zz[st + (cnt - 1) // 2] + zz[st + cnt // 2])
    cellk = ks[st] // 1000000
    ok = cnt >= 5
    cellk, med = cellk[ok], med[ok]
    G = np.full((int(iy.max()) + 1, nx), np.nan)
    if len(cellk):
        o2 = np.argsort(cellk, kind="stable")
        ck, mv = cellk[o2], med[o2]
        b2 = np.flatnonzero(np.diff(ck)) + 1
        s2, e2 = np.r_[0, b2], np.r_[b2, len(ck)]
        multi = (e2 - s2) >= 2
        rng_ = np.maximum.reduceat(mv, s2) - np.minimum.reduceat(mv, s2)
        c = ck[s2][multi]
        G[c // nx, c % nx] = rng_[multi]
    return float(xs.min()), float(ys.min()), float(cell), G
