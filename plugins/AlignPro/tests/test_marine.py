"""The sonar patch test on a simulated survey: two side-looking heads, reciprocal passes over a sloping, bumpy
seabed, known roll errors. The solver must find the rolls and leave pitch / heading alone when they gain nothing.
Also dcs_core.marine.split_passes on a lawnmower track."""
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from dcs_core import marine as DM          # noqa: E402
from AlignPro.core import marine as M      # noqa: E402


def seabed(e, n):
    return -10.0 + 0.03 * e + 0.6 * np.sin(e / 9.0) * np.cos(n / 7.0)


def lawnmower(lines=4, length=240.0, spacing=24.0, speed=1.5, rate=10.0):
    """t, E, N, heading (rad) of a boat running alternate north / south lines joined by half-circle turns."""
    pts = []
    t = 0.0
    dt = 1.0 / rate
    for k in range(lines):
        e = k * spacing
        north = k % 2 == 0
        n0, n1 = (0.0, length) if north else (length, 0.0)
        steps = int(length / (speed * dt))
        for i in range(steps):
            n = n0 + (n1 - n0) * i / steps
            pts.append((t, e, n, 0.0 if north else math.pi))
            t += dt
        if k < lines - 1:                       # turn: a half circle to the next line
            r = spacing / 2
            cx, cy = e + r, n1
            steps = int(math.pi * r / (speed * dt))
            for i in range(steps):
                a = math.pi * i / steps
                if north:
                    x, y, hd = cx - r * math.cos(a), cy + r * math.sin(a), a
                else:
                    x, y, hd = cx - r * math.cos(a), cy - r * math.sin(a), math.pi - a
                pts.append((t, x, y, hd))
                t += dt
    a = np.array(pts)
    return a[:, 0], a[:, 1], a[:, 2], np.unwrap(a[:, 3])


def simulate(roll_err, pitch_err=(0.0, 0.0), ping_rate=5.0, beams=np.radians(np.arange(-40, 41, 4.0))):
    t, E, N, hd = lawnmower()
    lat0, lon0 = 45.0, -64.0
    kn = 111132.954 - 559.822 * math.cos(2 * math.radians(lat0))
    ke = 111412.84 * math.cos(math.radians(lat0))
    traj = DM.LocalTrajectory(t, lat0 + N / kn, lon0 + E / ke, np.zeros_like(t), np.zeros_like(t),
                              np.zeros_like(t), hd, origin=(lat0, lon0, 0.0))
    heads = {}
    sos = 1500.0
    for i, (did, lever, tilt) in enumerate(((1, [0.0, -0.06, 0.5], 45.0), (2, [0.0, 0.06, 0.5], -45.0))):
        tp = np.arange(t[0] + 1, t[-1] - 1, 1.0 / ping_rate)
        ang = np.tile(beams + math.radians(tilt), len(tp))
        T = np.repeat(tp, len(beams))
        S = {"t": T, "angle": ang, "tof": np.full(len(T), 2 * 10.0 / sos), "sos": np.full(len(T), sos),
             "power": np.ones(len(T)), "up": np.tile([0.0, 0.0, 1.0], (len(T), 1)), "ping": np.zeros(len(T))}
        true = {"roll": roll_err[i], "pitch": pitch_err[i], "heading": 0.0, "latency_s": 0.0}
        r = np.full(len(T), 10.0)
        for _it in range(8):                    # range to the seabed along each true ray
            S["tof"] = 2 * r / sos
            P1 = DM.georef_local(S, traj, lever, true)
            S["tof"] = 2 * (r + 1.0) / sos
            P2 = DM.georef_local(S, traj, lever, true)
            d = P2 - P1
            f = P1[:, 2] - seabed(P1[:, 0], P1[:, 1])
            g = d[:, 2] - (seabed(P1[:, 0] + d[:, 0], P1[:, 1] + d[:, 1]) - seabed(P1[:, 0], P1[:, 1]))
            r = r - f / g
        S["tof"] = 2 * r / sos + np.random.default_rng(i).normal(0, 0.02, len(r)) * 2 / sos
        heads[did] = {"S": S, "lever": lever, "nickname": str(did), "side": "port" if lever[1] < 0 else "starboard"}
    return {"traj": traj, "heads": heads, "doc": {}}


def test_split_passes_lawnmower():
    t, E, N, hd = lawnmower()
    spans = DM.split_passes(t, hd)
    assert len(spans) == 4
    assert abs(spans[0][0] - t[0]) < 1e-9 and abs(spans[-1][1] - t[-1]) < 1e-9
    for (a, b), (c, _d) in zip(spans[:-1], spans[1:]):
        assert b == c                           # contiguous
    p = DM.pass_of(spans, t)
    assert p[0] == 0 and p[-1] == 3


def test_patch_test_finds_roll():
    data = simulate(roll_err=(0.5, -0.3))
    res = M.solve(data)
    h1, h2 = res["heads"]["1"], res["heads"]["2"]
    assert abs(h1["roll"] - 0.5) < 0.05, h1
    assert abs(h2["roll"] + 0.3) < 0.05, h2
    assert res["after_cm"] < res["before_cm"]
    assert h1["roll_determined"] and h2["roll_determined"]


def test_patch_test_no_error_stays_zero():
    data = simulate(roll_err=(0.0, 0.0))
    res = M.solve(data)
    for d in ("1", "2"):
        e = res["heads"][d]
        assert abs(e["roll"]) < 0.06, e         # 2 x its uncertainty on this bumpy seabed
        assert res["estimated"][d] == ["roll"]   # nothing to gain from pitch / heading


def test_georef_local_matches_rays():
    """georef_local with a level boat heading north puts a starboard-looking beam to the east."""
    t = np.array([0.0, 10.0])
    traj = DM.LocalTrajectory(t, [45.0, 45.0], [-64.0, -64.0], [0.0, 0.0], [0, 0], [0, 0], [0, 0],
                              origin=(45.0, -64.0, 0.0))
    S = {"t": np.array([5.0]), "angle": np.array([math.radians(30.0) * DM.SIDE]), "tof": np.array([2 * 10.0 / 1500]),
         "sos": np.array([1500.0]), "up": np.array([[0.0, 0.0, 1.0]])}
    P = DM.georef_local(S, traj, [0, 0, 0], {})
    # 30 deg off the vertical: 5 m across the track, 8.66 m down
    assert abs(abs(P[0, 0]) - 10 * math.sin(math.radians(30))) < 1e-6
    assert abs(P[0, 2] + 10 * math.cos(math.radians(30))) < 1e-6


def test_review_tools():
    """Pairs, residual profiles, the disagreement map and live sections see a roll error and its correction."""
    data = simulate(roll_err=(0.6, -0.4))
    wrong = {}
    right = {"1": {"roll": 0.6}, "2": {"roll": -0.4}}
    pt0, pt1 = M.pair_table(data, wrong), M.pair_table(data, right)
    assert pt0 and any(e["kind"] == "reciprocal" for e in pt0)
    med = lambda pt: np.average([e["median_cm"] for e in pt], weights=[e["n"] for e in pt])
    assert med(pt1) < med(pt0)
    g0, g1 = M.difference_grid(data, wrong)[3], M.difference_grid(data, right)[3]
    assert np.nanmedian(g1) < np.nanmedian(g0)
    pr = M.residual_profiles(data, right)
    assert set(pr) == {1, 2} and len(pr[1]["angle"][0]) >= 3
    xs, ys = M.plan(data, right, M.view_sample(data, 20000))[:2]
    c = np.array([np.median(xs), np.median(ys)])
    sec = M.Section(data, wrong, c - [0, 60], c + [0, 60], 2.0)
    a0 = sec.place(wrong)
    a1 = sec.place(dict(right, **{"1": {"roll": 0.6, "lever_m": [0.0, -0.06, 0.6]}}))
    assert len(a0[0]) > 100 and len(a1[0]) > 100
    pub = M.with_levers(data, {"1": {"lever_m": [1.0, 2.0, 3.0]}})
    assert pub["heads"][1]["lever"] == [1.0, 2.0, 3.0] and data["heads"][1]["lever"] != [1.0, 2.0, 3.0]


if __name__ == "__main__":
    test_review_tools()
    test_split_passes_lawnmower()
    test_georef_local_matches_rays()
    test_patch_test_no_error_stays_zero()
    test_patch_test_finds_roll()
    print("ok")
