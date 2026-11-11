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
    A[:, 0] = n[:, 0] * ua[:, 0] + n[:, 1] * ua[:, 1]
    A[:, 1] = n[:, 0] * la[:, 0] + n[:, 1] * la[:, 1]
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
    r = d - A @ p
    return {"relation": relation, "direction_dot": round(med, 3), "patches": int(len(d)),
            "along": float(p[0]) if seen[0] else None, "cross": float(p[1]) if seen[1] else None,
            "up": float(p[2]) if seen[2] else None, "roll": float(p[3]) if seen[3] else None,
            "rest": float(np.median(np.abs(r))), "spread_m": float(np.std((cb - ca)[flat])) if flat.any() else 0.0}


def predict(model, n, xy, ta, tb, mot_a, mot_b, world_shift=None):
    """Modelled separation for patches (or cells): the vehicle-frame model where the drive lines are known, else
    the world shift (E, N, Up; None components = 0), else 0."""
    n = np.asarray(n, float)
    out = np.zeros(len(n))
    ws = np.array([0.0 if (v is None or not np.isfinite(v)) else v for v in (world_shift or (0, 0, 0))])
    out[:] = n @ ws
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
    if rel == "opposite":
        if al is not None and abs(al) > tol:
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
