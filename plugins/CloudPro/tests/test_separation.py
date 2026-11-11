"""Line separation (CloudPro 2.2.0): a simulated street (sloped ground, buildings with walls in two directions and
roofs, trees) seen by three lines - line 0 true, line 1 with a constant shift, line 2 with a height error that grows
along the street. The check must find the shift of line 1, see that line 2's error is not constant, and leave the
points alone.

    python3 CloudPro/tests/test_separation.py      (from the plugins folder)
"""
import os
import sys
import tempfile

import numpy as np

PLUGINS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PLUGINS)

from CloudPro.core import separation as S          # noqa: E402
from CloudPro.core.project import Project          # noqa: E402
from CloudPro.core.store import REC, REJECTED      # noqa: E402

SHIFT1 = np.array([0.02, -0.03, 0.05])
BUMP = 0.12                    # m: line 0 low in 20-30 m east, 20-35 m north
T0 = 1_792_508_400.0


def scene(rng, n=400_000):
    """Points (x, y, z) of the street, without noise."""
    parts = []
    gx, gy = rng.uniform(0, 100, n), rng.uniform(0, 60, n)
    parts.append(np.column_stack([gx, gy, 0.02 * gx + 0.01 * gy]))
    for (x0, y0, w, d, h) in ((10.37, 5.61, 14.3, 9.2, 7), (40.72, 38.18, 10.4, 16.3, 9), (70.15, 8.44, 18.1, 10.5, 6),
                                  (55, 20, 6, 6, 4)):            # the last on whole metres: cut by cube faces
        base = 0.02 * x0 + 0.01 * y0
        m = 40_000
        for side in range(4):                         # walls facing E/W and N/S
            u, v = rng.uniform(0, 1, m), rng.uniform(0, h, m)
            if side == 0:
                p = np.column_stack([x0 + w * u, np.full(m, y0), base + v])
            elif side == 1:
                p = np.column_stack([x0 + w * u, np.full(m, y0 + d), base + v])
            elif side == 2:
                p = np.column_stack([np.full(m, x0), y0 + d * u, base + v])
            else:
                p = np.column_stack([np.full(m, x0 + w), y0 + d * u, base + v])
            parts.append(p)
        parts.append(np.column_stack([rng.uniform(x0, x0 + w, m), rng.uniform(y0, y0 + d, m), np.full(m, base + h)]))
    for _ in range(25):                                # trees: not flat, must not count
        cx, cy = rng.uniform(0, 100), rng.uniform(0, 60)
        parts.append(np.column_stack([cx + rng.normal(0, 1.2, 6000), cy + rng.normal(0, 1.2, 6000),
                                      0.02 * cx + rng.uniform(2, 8, 6000)]))
    return np.concatenate(parts)


def build(tmp):
    rng = np.random.default_rng(7)
    p = Project.create(tmp, "sep")
    p.store.origin = np.zeros(3)
    for lid in range(3):
        xyz = scene(rng)
        if lid == 1:
            xyz = xyz + SHIFT1
        if lid == 0:                                         # a local trajectory error on line 0 only
            m = (xyz[:, 0] > 20) & (xyz[:, 0] < 30) & (xyz[:, 1] > 20) & (xyz[:, 1] < 35)
            xyz[m, 2] -= BUMP
        if lid == 2:
            xyz[:, 2] += 0.10 * xyz[:, 0] / 100.0          # 0 cm at x = 0 to 10 cm at x = 100 m
        xyz += rng.normal(0, 0.005, xyz.shape)
        r = np.zeros(len(xyz), REC)
        r["x"], r["y"], r["z"] = np.round(xyz * 1000).astype(np.int32).T
        r["line"] = lid
        r["t"] = T0 + 100 * lid + xyz[:, 0] / 10.0               # driving east at 10 m/s
        r["cls"] = 1
        p.add_line({"name": "Line %d" % (lid + 1), "points": len(r), "mount": [90, 45, 0], "t0": T0 + 100 * lid})
        p.store.append(r)
    # rejected points far off must be left out
    bad = np.zeros(5000, REC)
    bad["x"], bad["y"] = rng.integers(0, 100_000, 5000), rng.integers(0, 60_000, 5000)
    bad["z"] = 3000
    bad["flag"] = REJECTED
    bad["line"] = 1
    p.store.append(bad)
    p.store.flush()
    p.save()
    return p


def main():
    tmp = tempfile.mkdtemp()
    p = build(tmp)
    before = {k: p.store.tile_array(k).copy() for k in p.store.keys()}

    # cancel: raises, writes nothing
    try:
        S.run(p, cancelled=lambda: True)
        raise AssertionError("not cancelled")
    except S.Cancelled:
        pass
    assert S.load(p) is None and not os.path.exists(os.path.join(S.folder(p), "separation.json"))
    print("cancel: nothing written")

    seen = []
    r = S.run(p, progress=lambda f, m: seen.append(f))
    assert seen and abs(seen[-1] - 1.0) < 1e-9
    pairs = {(q["a"], q["b"]): q for q in r.pairs}
    assert set(pairs) == {(0, 1), (0, 2), (1, 2)}, pairs.keys()

    q = pairs[(0, 1)]
    t = np.array(q["shift"], float)
    assert q["horizontal"], q
    assert np.all(np.abs(t - SHIFT1) < 0.004), (t, SHIFT1)
    assert q["rest"] < 0.008, q["rest"]
    print("line 1 against line 0: shift %s cm (true %s), rest %.1f cm, %d patches (%d walls)" % (
        np.round(100 * t, 1), np.round(100 * SHIFT1, 1), 100 * q["rest"], q["patches"], q["walls"]))

    q = pairs[(0, 2)]
    t = np.array(q["shift"], float)
    assert abs(t[2] - 0.05) < 0.01, t                    # the mean of the ramp
    assert q["rest"] > 0.015, q["rest"]                  # not constant
    # without drive lines (no tracks here) the steady relation is the world shift; the ramp stays in the rest
    print("line 2 against line 0: up %+.1f cm, rest %.1f cm (changes along the street)" % (100 * t[2], 100 * q["rest"]))

    # the grid follows the ramp: small at the west end, large at the east end
    T = p.store.tile
    west, east = [], []
    for k, g in r.grids.items():
        i, _ = map(int, k.split("_"))
        x = i * T + (np.arange(g.shape[1]) + 0.5) * r.patch
        cols = np.broadcast_to(x, g.shape)
        ok = np.isfinite(g)
        west.append(g[ok & (cols < 30)])
        east.append(g[ok & (cols >= 70)])
    w, e = np.median(np.concatenate(west)), np.median(np.concatenate(east))
    assert e > w + 0.01, (w, e)
    print("grid: median %.1f cm at the west end, %.1f cm at the east end" % (100 * w, 100 * e))

    # the local error: one problem area, line 0 at fault, where and when it happened
    areas = r.problem_areas(0.03)
    hit = [a for a in areas if a["line"] == 0]
    assert len(hit) == 1, [(a["line"], a["lines"], a["x"], a["y"], a["m2"]) for a in areas]
    a = hit[0]
    assert 20 <= a["x"] <= 30 and 20 <= a["y"] <= 35 and a["bbox"][0] >= 18 and a["bbox"][2] <= 32, a
    assert set(a["pairs"]) == {(0, 1), (0, 2)} and 0.10 < a["max"] < 0.25, a
    assert T0 + 1.8 <= a["t0"] < a["t1"] <= T0 + 3.2, (a["t0"] - T0, a["t1"] - T0)
    assert "local problem area" in S.verdict(r, 0.03) and "Line 1" in S.verdict(r, 0.03)
    print("local error: area #%d, %s at E %.1f N %.1f, %.0f m2, max %.1f cm, %s" % (
        a["id"], S.area_lines_text(a, r.data["names"]), a["e"], a["n"], a["m2"], 100 * a["max"],
        S.time_text(a["t0"], a["t1"])))
    # the pair that is off everywhere (line 1, constant) is not a local area
    assert not any(a["pairs"] == [(0, 1)] for a in areas)

    # saved and loaded; nothing in the points changed
    r2 = S.load(p)
    assert r2 is not None and r2.is_current(p) and set(r2.grids) == set(r.grids)
    for k, a in before.items():
        assert np.array_equal(a, p.store.tile_array(k)), k
    txt = "\n".join(S.report_lines(r2, 0.03, "sep"))
    assert "Line 1 / Line 2" in txt and "Per line" in txt and "Problem areas" in txt
    assert r2.problem_areas(0.03)[0]["t0"] == r.problem_areas(0.03)[0]["t0"]
    print("saved, loaded, points unchanged; report %d lines" % len(txt.splitlines()))

    # the file PosPro reads
    import json
    p.data["inputs"] = {"trajectory": {"source": "pospro", "handover_id": "sha256:abc", "description": "test"}}
    fp = S.write_feedback(p, r2, 0.03)
    fb = json.load(open(fp))
    assert fb["schema"] == "dcs.problem_areas/1" and fb["trajectory"]["handover_id"] == "sha256:abc"
    one = [x for x in fb["areas"] if x["line"] == "Line 1"][0]
    w, s0 = S.gps_week_sow(T0 + 2.0)
    assert one["gps_week"] == w and s0 - 0.05 <= one["gps_sow"][0] < one["gps_sow"][1] <= s0 + 1.1, one
    print("problem_areas.json: %d areas; Line 1 at GPS week %d, %.1f-%.1f s" % (
        len(fb["areas"]), one["gps_week"], one["gps_sow"][0], one["gps_sow"][1]))

    # out of date when the points are georeferenced again
    p.lines[1]["mount"] = [90, 45, 0.1]
    assert not r2.is_current(p)
    print("out of date after a georeferencing change")
    print("OK")


if __name__ == "__main__":
    main()
