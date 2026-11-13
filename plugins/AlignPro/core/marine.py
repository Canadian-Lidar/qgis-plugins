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
            att = None
            p = (doc.get("paths") or {}).get("sonar_%d_attitude" % did)
            if p and os.path.exists(p):
                att = np.load(p)
            s = marine.sample_soundings(rec, did, every=every, power=power, rmax=rmax, attitude=att)
            if s is None:
                continue
            e = heads.setdefault(did, {"parts": [], "lever": h.get("lever_m") or [0.0, 0.0, 0.0],
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
            "between": between,
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


def publish(dcs, result, data, applied=None):
    """Finalize the per-head corrections (dcs.alignment/1, platform marine, heads section) for CloudPro."""
    from dcs_core import handover
    from .project import PLUGIN, VERSION
    heads = applied or {d: {k: v[k] for k in PARAMS} for d, v in result["heads"].items()}
    fields = {"platform": "marine",
              "heads": {d: dict(heads[d], nickname=data["heads"][int(d)].get("nickname"),
                                side=data["heads"][int(d)].get("side"),
                                sd={k: result["heads"][d].get(k + "_sd") for k in PARAMS} if d in result["heads"] else {},
                                determined={k: result["heads"][d].get(k + "_determined") for k in PARAMS}
                                if d in result["heads"] else {})
                        for d in heads},
              "convention": {"name": "marine heads: corrections added to each sonar's own attitude and to the "
                                     "vessel heading; latency added to the ping time"},
              "trajectory": {"handover_id": (data.get("doc") or {}).get("id")},
              "quality": {"before_cm": result.get("before_cm"), "after_cm": result.get("after_cm"),
                          "observations": result.get("observations"), "passes": result.get("passes"),
                          "between_heads_share": result.get("between_heads_share")},
              "method": "per-head patch test: seabed point-to-plane between passes and between heads"}
    used = {"pospro": (data.get("doc") or {}).get("id")} if (data.get("doc") or {}).get("id") else None
    return handover.publish(dcs, "alignpro", {}, fields, PLUGIN, VERSION, "marine patch test", used=used,
                            inputs=sorted(dcs.sonar_clraw_files()))
