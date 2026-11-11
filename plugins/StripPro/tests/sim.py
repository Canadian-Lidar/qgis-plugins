"""A simulated survey for StripPro: a street with buildings on both sides, driven east (line 0) and back west
(line 1) at 3 m/s on a trailer that rolls and pitches. The INS record has holes like mapper 2.9.0's (0.8 s missing
every 2.4 s, and one 3.1 s hole over a bump); through a hole the trajectory is a straight line between the
records each side, as PosPro and CloudPro have it. Points are made from the true motion and placed with the
holed trajectory, so their errors are exactly what the holes do.
"""
import numpy as np

SPEED = 3.0
L = 240.0
RANGE = 35.0
T_GAP = 200.0                 # s between the start of line 0 and of line 1


def scene(rng, n_ground=900_000):
    parts = []
    gx, gy = rng.uniform(-10, L + 10, n_ground), rng.uniform(-14, 14, n_ground)
    parts.append(np.column_stack([gx, gy, 0.02 * gx + 0.01 * gy + 0.05 * np.sin(gx / 7.0)]))
    for x0 in np.arange(0, L - 10, 19.0):
        for side in (-1, 1):
            w, d, h = rng.uniform(9, 14), rng.uniform(7, 10), rng.uniform(5, 9)
            x0j = x0 + rng.uniform(0, 4) + 0.37
            y0 = 8.3 if side > 0 else -8.3 - d
            base = 0.02 * x0j
            m = 14000
            u, v = rng.uniform(0, 1, m), rng.uniform(0, h, m)
            parts += [np.column_stack([x0j + w * u, np.full(m, y0 if side > 0 else y0 + d), base + v]),
                      np.column_stack([np.full(m, x0j), y0 + d * u, base + v]),
                      np.column_stack([np.full(m, x0j + w), y0 + d * u, base + v])]
    return np.concatenate(parts)


def true_motion(t, east, t0):
    """Sensor-independent true pose: s (n, 3), roll, pitch, yaw (rad) of the vehicle in the ENU frame."""
    tt = t - t0
    x = SPEED * tt if east else L - SPEED * tt
    y = (-2.0 if east else 2.0) + 0.15 * np.sin(tt / 9.0)
    z = 0.02 * x + 0.05 * np.sin(x / 7.0) + 1.8
    roll = np.radians(1.2) * np.sin(2 * np.pi * 0.6 * tt) + np.radians(0.6) * np.sin(2 * np.pi * 1.7 * tt + 1)
    pitch = np.radians(0.8) * np.sin(2 * np.pi * 0.45 * tt + 0.5)
    yaw = (0.0 if east else np.pi) + 0.02 * np.cos(tt / 9.0)
    return np.column_stack([x, y, z]), roll, pitch, yaw


def rot(roll, pitch, yaw):
    """Vehicle (x forward, y left, z up) -> ENU: Rz(yaw) Ry(-pitch) Rx(roll), (n, 3, 3)."""
    cr, sr, cp, sp, cy, sy = np.cos(roll), np.sin(roll), np.cos(-pitch), np.sin(-pitch), np.cos(yaw), np.sin(yaw)
    return np.stack([np.stack([cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr], -1),
                     np.stack([sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr], -1),
                     np.stack([-sp, cp * sr, cp * cr], -1)], -2)


def holes_for(t0, t1, bump_t, rng):
    h = []
    a = t0 + 1.0
    while a < t1 - 1:
        g = rng.uniform(0.6, 1.0)
        if not (bump_t - 2 < a < bump_t + 4):
            h.append([a, a + g])
        a += rng.uniform(2.0, 2.8)
    h.append([bump_t, bump_t + 3.1])
    h.sort()
    return np.array(h)


def holed(t, rec_t, values):
    """The recorded trajectory as used downstream: values at the record times, straight lines across holes."""
    return np.interp(t, rec_t, values)


def make(seed=5, rate=50.0):
    rng = np.random.default_rng(seed)
    S = scene(rng)
    out = {"t": [], "p": [], "s": [], "line": [], "p_true": []}
    holes_all = []
    dur = L / SPEED
    for lid, east in ((0, True), (1, False)):
        t0 = 1000.0 + lid * T_GAP
        bump = t0 + (0.55 if east else 0.45) * dur
        hl = holes_for(t0, t0 + dur, bump, rng)
        holes_all.append(hl)
        rec = np.arange(t0 - 2, t0 + dur + 2, 1.0 / rate)
        inh = np.zeros(len(rec), bool)
        for a, b in hl:
            inh |= (rec > a) & (rec < b)
        rec = rec[~inh]
        s_r, r_r, p_r, y_r = true_motion(rec, east, t0)
        # points: each scene point within range gets one firing time while the vehicle passes
        pts = S[rng.random(len(S)) < 0.8]
        xv = pts[:, 0]
        dy = pts[:, 1] - (-2.0 if east else 2.0)
        half = np.sqrt(np.maximum(RANGE ** 2 - dy ** 2 - (pts[:, 2] - 2) ** 2, 0))
        x0 = np.clip(xv - half, 0, L)
        x1 = np.clip(xv + half, 0, L)
        ok = x1 - x0 > 1
        pts, x0, x1 = pts[ok], x0[ok], x1[ok]
        xs = rng.uniform(x0, x1)
        t = t0 + (xs / SPEED if east else (L - xs) / SPEED)
        sT, rT, pT, yT = true_motion(t, east, t0)
        CT = rot(rT, pT, yT)
        v = np.einsum("nji,nj->ni", CT, pts - sT)                  # vehicle frame vectors (true)
        v += rng.normal(0, 0.004, v.shape)
        sM = np.column_stack([holed(t, rec, s_r[:, k]) for k in range(3)])
        CM = rot(holed(t, rec, r_r), holed(t, rec, p_r), holed(t, rec, y_r))
        pM = sM + np.einsum("nij,nj->ni", CM, v)
        out["t"].append(t)
        out["p"].append(pM)
        out["s"].append(sM)
        out["line"].append(np.full(len(t), lid))
        out["p_true"].append(pts)
    d = {k: np.concatenate(v) for k, v in out.items()}
    d["holes"] = np.concatenate(holes_all)
    d["spans"] = {0: (1000.0, 1000.0 + dur), 1: (1000.0 + T_GAP, 1000.0 + T_GAP + dur)}
    return d
