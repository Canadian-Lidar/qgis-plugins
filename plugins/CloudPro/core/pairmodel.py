"""The steady relation between two lines, in the vehicle's own frame (CloudPro 2.2.1).

A calibration error moves each line's points the same way relative to its own direction of travel, so between two
passes driven in opposite directions it shows as an offset that reverses with the driving direction; a roll error
shows as a height difference that grows with the distance from the drive lines. A trajectory error does neither.

For each patch both lines saw (normal n, separation d = n . (c_b - c_a)), with u_a the direction line a was
travelling when its points there were taken, left_a its left, c_a / c_b the patch's distance to the left of each
line's drive line, the model is

    d = n . (along * u_a + cross * left_a + up * z) + n_z * roll * (c_b - c_a)

(along, cross, up = the steady offset of line b against line a in line a's frame; roll = m per m, the slope of a
roll boresight error shared by both passes.) Components the surfaces cannot fix are left out (0, "not seen").

    Motion(track)                          drive line from CloudPro's sensor track (t, E, N)
    fit(n, d, xy, ta, tb, mot_a, mot_b)    -> model dict (json-able) | None
    predict(model, n, xy, ta, tb, mot_a, mot_b, world_shift) -> n-array of modelled separations
    findings(model, tol)                   -> [(kind, text)]  kind: "calibration", "trajectory", "either"
"""
import math

import numpy as np

MIN_INFO = 30.0                   # sum of squared normal components a parameter needs to be solved
MIN_ROLL_SPREAD = 3.0             # m: spread of (c_b - c_a) over flat patches needed for the roll slope
MIN_SPEED = 0.5                   # m/s: slower and the direction of travel is not known
STEEP = 0.5                       # |n_z| below this: a wall-like surface. Only these fix along / sideways (2.2.2):
#                                   on gently sloping ground a height difference looks like an along-road shift
SEGMENTS = 4                      # the along-road offset is also measured in this many parts of the line
MIN_SEGMENT_WALLS = 15


class Motion:
    """Position and direction of travel of one line at given times (UTC s), from its sensor track."""

    def __init__(self, track):
        t = np.asarray(track["t"], float)
        o = np.argsort(t)
        self.t = t[o]
        self.E = np.asarray(track["E"], float)[o]
        self.N = np.asarray(track["N"], float)[o]

    def pos(self, tq):
        return np.stack([np.interp(tq, self.t, self.E), np.interp(tq, self.t, self.N)], 1)

    def direction(self, tq, dt=0.5):
        """Unit direction of travel (from the velocity, so reversing counts); NaN when slower than MIN_SPEED."""
        p0 = self.pos(np.asarray(tq) - dt)
        p1 = self.pos(np.asarray(tq) + dt)
        v = (p1 - p0) / (2 * dt)
        s = np.hypot(v[:, 0], v[:, 1])
        u = v / np.where(s > 0, s, 1.0)[:, None]
        u[s < MIN_SPEED] = np.nan
        out = (tq < self.t[0]) | (tq > self.t[-1])
        u[out] = np.nan
        return u


def _geometry(xy, ta, tb, mot_a, mot_b):
    ua = mot_a.direction(ta)
    ub = mot_b.direction(tb)
    la = np.stack([-ua[:, 1], ua[:, 0]], 1)
    lb = np.stack([-ub[:, 1], ub[:, 0]], 1)
    ca = np.einsum("ij,ij->i", xy - mot_a.pos(ta), la)
    cb = np.einsum("ij,ij->i", xy - mot_b.pos(tb), lb)
    return ua, ub, la, ca, cb


def _rows(n, ua, la, ca, cb):
    A = np.zeros((len(n), 4))
    steep = np.abs(n[:, 2]) < STEEP
    A[:, 0] = np.where(steep, n[:, 0] * ua[:, 0] + n[:, 1] * ua[:, 1], 0.0)
    A[:, 1] = np.where(steep, n[:, 0] * la[:, 0] + n[:, 1] * la[:, 1], 0.0)
    A[:, 2] = n[:, 2]
    A[:, 3] = n[:, 2] * (cb - ca)
    return A


def fit(n, d, xy, ta, tb, mot_a, mot_b):
    """The steady relation of line b to line a; None when the drive lines are not known there."""
    if mot_a is None or mot_b is None:
        return None
    n = np.asarray(n, float)
    d = np.asarray(d, float)
    ua, ub, la, ca, cb = _geometry(np.asarray(xy, float), ta, tb, mot_a, mot_b)
    ok = np.isfinite(ua).all(1) & np.isfinite(ub).all(1)
    if ok.sum() < 20:
        return None
    n, d, ua, ub, la, ca, cb = n[ok], d[ok], ua[ok], ub[ok], la[ok], ca[ok], cb[ok]
    dot = np.einsum("ij,ij->i", ua, ub)
    med = float(np.median(dot))
    relation = "opposite" if med < -0.5 else ("same" if med > 0.5 else "crossing")
    A = _rows(n, ua, la, ca, cb)
    flat = np.abs(n[:, 2]) > 0.9
    seen = [float((A[:, 0] ** 2).sum()) >= MIN_INFO, float((A[:, 1] ** 2).sum()) >= MIN_INFO, bool(flat.sum() >= 20),
            bool(flat.sum() >= 50 and np.std((cb - ca)[flat]) >= MIN_ROLL_SPREAD)]
    cols = [k for k in range(4) if seen[k]]
    p = np.zeros(4)
    keep = np.ones(len(d), bool)
    for _ in range(5):
        Ak, dk = A[keep][:, cols], d[keep]
        if len(dk) < len(cols) + 3:
            break
        sol = np.linalg.lstsq(Ak, dk, rcond=None)[0]
        p[:] = 0
        p[cols] = sol
        r = d - A @ p
        mad = 1.4826 * np.median(np.abs(r[keep] - np.median(r[keep])))
        keep = np.abs(r) <= max(3 * mad, 0.01)
    # along: the median of the walls facing along the road (the direct measurement; the least-squares value is
    # pulled by oblique walls and sideways terms), then the rest solved again with it fixed
    wa0 = np.abs(A[:, 0]) > 0.4
    along_se, along_n = None, int(wa0.sum())
    if seen[0] and wa0.sum() >= 2 * MIN_SEGMENT_WALLS:
        e0 = (d - A[:, 1:] @ p[1:])[wa0] / A[wa0, 0]
        p[0] = float(np.median(e0))
        along_se = _se_median(e0)
        rest_cols = [k for k in cols if k != 0]
        if rest_cols:
            keep = np.ones(len(d), bool)
            for _ in range(5):
                dk = d[keep] - A[keep, 0] * p[0]
                sol = np.linalg.lstsq(A[keep][:, rest_cols], dk, rcond=None)[0]
                p[rest_cols] = sol
                r = d - A @ p
                mad = 1.4826 * np.median(np.abs(r[keep] - np.median(r[keep])))
                keep = np.abs(r) <= max(3 * mad, 0.01)
    r = d - A @ p
    # the along-road offset part by part (walls facing along the road): a calibration error is the same everywhere
    segs = []
    if seen[0] and along_se is not None:
        wa = np.abs(A[:, 0]) > 0.4
        if wa.sum() >= SEGMENTS * MIN_SEGMENT_WALLS:
            t_a = np.asarray(ta, float)[ok]
            q = np.quantile(t_a[wa], np.linspace(0, 1, SEGMENTS + 1))
            est = (d - A[:, 1:] @ p[1:]) / np.where(wa, A[:, 0], 1.0)
            for k in range(SEGMENTS):
                m = wa & (t_a >= q[k]) & (t_a <= q[k + 1])
                if m.sum() >= MIN_SEGMENT_WALLS:
                    segs.append([float(np.median(est[m])), _se_median(est[m]), int(m.sum())])
    return {"relation": relation, "direction_dot": round(med, 3), "patches": int(len(d)),
            "walls": int((np.abs(n[:, 2]) < STEEP).sum()), "along_parts": segs,
            "along_se": along_se, "along_walls": along_n,
            "along": float(p[0]) if seen[0] else None, "cross": float(p[1]) if seen[1] else None,
            "up": float(p[2]) if seen[2] else None, "roll": float(p[3]) if seen[3] else None,
            "rest": float(np.median(np.abs(r))), "spread_m": float(np.std((cb - ca)[flat])) if flat.any() else 0.0}


def _se_median(v):
    """Standard error of a median (robust: from the MAD)."""
    v = np.asarray(v, float)
    if len(v) < 2:
        return float("inf")
    mad = 1.4826 * np.median(np.abs(v - np.median(v)))
    return float(1.2533 * mad / math.sqrt(len(v)))


def along_state(model, tol):
    """("measured" | "not measurable" | None, parts that differ beyond their noise?)"""
    if model is None or model.get("along") is None:
        return None, False
    se = model.get("along_se")
    if se is None or not np.isfinite(se) or se > tol / 2:
        return "not measurable", False
    parts = [q for q in (model.get("along_parts") or []) if isinstance(q, (list, tuple)) and np.isfinite(q[1])]
    varies = False
    if len(parts) >= 2:
        hi = max(parts, key=lambda q: q[0])
        lo = min(parts, key=lambda q: q[0])
        gap = hi[0] - lo[0]
        varies = gap > tol and gap > 3 * math.hypot(hi[1], lo[1])
    return "measured", varies


def predict(model, n, xy, ta, tb, mot_a, mot_b, world_shift=None):
    """Modelled separation for patches (or cells): the vehicle-frame model where the drive lines are known, else
    the world shift (E, N, Up; None components = 0), else 0."""
    n = np.asarray(n, float)
    out = np.zeros(len(n))
    ws = np.array([0.0 if (v is None or not np.isfinite(v)) else v for v in (world_shift or (0, 0, 0))])
    nh = n.copy()
    nh[np.abs(nh[:, 2]) >= STEEP, :2] = 0.0
    out[:] = nh @ ws
    if model is None or mot_a is None or mot_b is None:
        return out
    ua, ub, la, ca, cb = _geometry(np.asarray(xy, float), ta, tb, mot_a, mot_b)
    ok = np.isfinite(ua).all(1) & np.isfinite(ub).all(1)
    p = np.array([model.get(k) or 0.0 for k in ("along", "cross", "up", "roll")])
    if ok.any():
        out[ok] = _rows(n[ok], ua[ok], la[ok], ca[ok], cb[ok]) @ p
    return out


def findings(model, tol, names=("line a", "line b")):
    """What the steady relation says, as (kind, sentence)."""
    if model is None:
        return []
    f = []
    cm = lambda v: 100 * abs(v)                               # noqa: E731
    rel = model["relation"]
    al, cr, up, roll = model.get("along"), model.get("cross"), model.get("up"), model.get("roll")
    state, varies = along_state(model, tol)
    se = model.get("along_se")
    if state == "not measurable":
        f.append(("note", "Along the road: not measurable here (%d walls facing along the road; ±%.1f cm)."
                  % (model.get("along_walls") or 0, 100 * se if se is not None and np.isfinite(se) else float("nan"))))
    parts = [q for q in (model.get("along_parts") or []) if isinstance(q, (list, tuple))]
    if varies:
        f.append(("trajectory", "The along-road offset changes along the line (%s cm in its %d parts, each ± about "
                  "%.1f): not a calibration error, which would be the same everywhere." % (
                      " / ".join("%+.1f" % (100 * q[0]) for q in parts), len(parts),
                      100 * max(q[1] for q in parts))))
    if rel == "opposite":
        if (al is not None and state == "measured" and abs(al) > tol and abs(al) > 3 * (se or 0)
                and not varies):
            f.append(("calibration", "%.1f cm along the road on each pass, reversing with the driving direction "
                      "(points placed %s the vehicle): timing latency, an along-track lever arm or pitch."
                      % (cm(al) / 2, "ahead of" if -al > 0 else "behind")))
        if cr is not None and abs(cr) > tol:
            f.append(("calibration", "%.1f cm sideways on each pass, reversing with the driving direction: a "
                      "cross-track lever arm or heading." % (cm(cr) / 2)))
        if up is not None and abs(up) > tol:
            f.append(("either", "%.1f cm height difference between the two directions: pitch or the lever arm "
                      "height, or a GNSS height difference between the two times." % cm(up)))
    elif rel == "same":
        steady = [v for v in (al, cr, up) if v is not None and abs(v) > tol]
        if steady:
            f.append(("trajectory", "Driven the same way with a steady offset (%.1f cm): a calibration error would "
                      "cancel here, so the two trajectories disagree." % (100 * max(abs(v) for v in steady))))
    else:
        steady = [v for v in (al, cr, up) if v is not None and abs(v) > tol]
        if steady:
            f.append(("either", "Lines crossing with a steady offset (%.1f cm)." % (100 * max(abs(v) for v in steady))))
    if roll is not None and abs(roll) * 10 > tol / 2:
        f.append(("calibration", "The height difference grows by %.1f cm per 10 m from the drive lines (roll "
                  "about %.3f°): roll boresight." % (100 * abs(roll) * 10, math.degrees(math.atan(abs(roll))))))
    if model.get("rest", 0) > tol:
        f.append(("trajectory", "%.1f cm left after this steady relation: the error changes along the line "
                  "(trajectory)." % (100 * model["rest"])))
    return f
