"""Steady relation and calibration findings (CloudPro 2.2.1): a street driven east (Line 1) and back west (Line 2),
like Shirley's Lines 4 / 5. Each pass places its points 4 cm ahead of the vehicle (a timing latency) and has a roll
error of 2 cm per 10 m from its drive line; Line 2 is also 12 cm low for a few seconds. The check must report the
steady part as calibration (along the road, reversing with the driving direction; roll), find exactly one local
problem area (Line 2's few seconds) and write calibration_check.json for AlignPro.

    python3 CloudPro/tests/test_calibration_check.py      (from the plugins folder)
"""
import json
import os
import sys
import tempfile

import numpy as np

PLUGINS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PLUGINS)

from CloudPro.core import separation as S          # noqa: E402
from CloudPro.core.project import Project          # noqa: E402
from CloudPro.core.store import REC                # noqa: E402

T0 = 1_793_741_000.0
SPEED = 8.0
L = 300.0
AHEAD = 0.04                    # m each pass, forward along its travel (latency-like)
ROLL = 0.002                    # m per m from the drive line, rising to the left
BUMP = (120.0, 150.0, -0.12)    # Line 2 low between these x


def street(rng):
    parts = []
    n = 500_000
    gx, gy = rng.uniform(0, L, n), rng.uniform(-14, 14, n)
    parts.append(np.column_stack([gx, gy, 0.003 * gx]))
    for x0 in np.arange(5, L - 25, 22.0):           # buildings both sides, walls in all four directions
        for side in (-1, 1):
            w, d, h = rng.uniform(10, 16), rng.uniform(7, 10), rng.uniform(5, 9)
            x0j = x0 + rng.uniform(0, 4) + 0.37
            y0 = (8.3 if side > 0 else -8.3 - d)
            base = 0.003 * x0j
            m = 9000
            u, v = rng.uniform(0, 1, m), rng.uniform(0, h, m)
            parts += [np.column_stack([x0j + w * u, np.full(m, y0), base + v]),
                      np.column_stack([x0j + w * u, np.full(m, y0 + d), base + v]),
                      np.column_stack([np.full(m, x0j), y0 + d * u, base + v]),
                      np.column_stack([np.full(m, x0j + w), y0 + d * u, base + v]),
                      np.column_stack([rng.uniform(x0j, x0j + w, m), rng.uniform(y0, y0 + d, m), np.full(m, base + h)])]
    return np.concatenate(parts)


def build(tmp):
    rng = np.random.default_rng(3)
    p = Project.create(tmp, "street")
    p.store.origin = np.zeros(3)
    scene = street(rng)                               # the same street for both passes
    for lid, (ylane, east) in enumerate(((-2.0, True), (2.0, False))):
        xyz = scene[rng.random(len(scene)) < 0.8].copy()
        u = np.array([1.0, 0.0]) if east else np.array([-1.0, 0.0])
        left = np.array([-u[1], u[0]])
        x = xyz[:, 0]
        t = T0 + (x / SPEED if east else 100.0 + (L - x) / SPEED)
        c = (xyz[:, :2] - np.array([0.0, ylane])) @ left             # left of this pass's drive line
        xyz[:, :2] += AHEAD * u
        xyz[:, 2] += ROLL * c
        if not east:
            m = (x > BUMP[0]) & (x < BUMP[1])
            xyz[m, 2] += BUMP[2]
        xyz += rng.normal(0, 0.004, xyz.shape)
        r = np.zeros(len(xyz), REC)
        r["x"], r["y"], r["z"] = np.round(xyz * 1000).astype(np.int32).T
        r["line"], r["cls"], r["t"] = lid, 1, t
        p.add_line({"name": "Line %d" % (lid + 4), "line": str(lid + 4), "points": len(r), "t0": float(t.min()),
                    "files": ["/job/lines/L%03d/clraw/L%03d_0001.clraw" % (lid + 4, lid + 4)]})
        p.store.append(r)
        tt = np.arange(0, L / SPEED + 0.01, 0.1)
        tx = SPEED * tt if east else L - SPEED * tt
        p.save_track(lid, (T0 if east else T0 + 100.0) + tt, tx, np.full(len(tt), ylane), np.full(len(tt), 2.0),
                     np.full(len(tt), np.pi / 2 if east else -np.pi / 2))
    p.store.flush()
    p.data["inputs"] = {"alignment": {"source": "alignpro", "handover_id": "sha256:cal1", "calibration": "applied"},
                        "trajectory": {"source": "pospro", "handover_id": "sha256:traj1"}}
    p.save()
    return p


def main():
    p = build(tempfile.mkdtemp())
    r = S.run(p)
    assert len(r.pairs) == 1
    q = r.pairs[0]
    m = q["model"]
    assert m and m["relation"] == "opposite", m
    assert abs(m["along"] - (-2 * AHEAD)) < 0.01, m           # line b against a, in a's frame
    assert abs(m["cross"] or 0) < 0.01 and abs(m["up"] or 0) < 0.01, m
    assert m["roll"] is not None and abs(m["roll"] - ROLL) < 0.0006, m
    print("steady relation: along %+.1f cm (true %+.1f), sideways %+.1f, up %+.1f, roll %.2f cm per 10 m (true %.2f),"
          " rest %.1f cm" % (100 * m["along"], -200 * AHEAD, 100 * (m["cross"] or 0), 100 * (m["up"] or 0),
                             1000 * m["roll"], 1000 * ROLL, 100 * m["rest"]))
    f = S.pair_findings(q, 0.03)
    kinds = [k for k, _ in f]
    assert kinds.count("calibration") == 2, f
    assert any("4.0 cm along the road on each pass" in t and "ahead of" in t for _, t in f), f
    assert any("roll" in t for _, t in f), f
    v = S.verdict(r, 0.03)
    assert "Calibration (AlignPro), Line 4 / Line 5" in v, v
    print("verdict:", v)

    areas = r.problem_areas(0.03)
    assert len(areas) == 1, [(a["x"], a["y"], a["m2"], a["median"]) for a in areas]
    a = areas[0]
    assert BUMP[0] - 5 <= a["bbox"][0] and a["bbox"][2] <= BUMP[1] + 5, a["bbox"]
    sp = a["spans"]
    t5 = sp[1]
    exp0, exp1 = T0 + 100 + (L - BUMP[1]) / SPEED, T0 + 100 + (L - BUMP[0]) / SPEED
    assert exp0 - 1.5 <= t5[0] and t5[1] <= exp1 + 1.5, (t5[0] - T0, t5[1] - T0, exp0 - T0, exp1 - T0)
    print("problem areas: 1 (x %.0f-%.0f m, %.0f m2, median %.1f cm); Line 5 %.1f-%.1f s (true %.1f-%.1f)" % (
        a["bbox"][0], a["bbox"][2], a["m2"], 100 * a["median"], t5[0] - T0, t5[1] - T0, exp0 - T0, exp1 - T0))
    # without taking the steady relation off, the same data gives many areas (Shirley's 102)
    g = r.grid(r.tiles()[0])
    g2 = r.grid(r.tiles()[0], steady_off=True)
    assert np.nanmedian(g2) < np.nanmedian(g), (np.nanmedian(g), np.nanmedian(g2))

    S.write_feedback(p, r, 0.03)
    cc = json.load(open(os.path.join(p.out_dir, "calibration_check.json")))
    assert cc["schema"] == "dcs.calibration_check/1" and cc["alignment"]["handover_id"] == "sha256:cal1"
    pc = cc["pairs"][0]
    assert pc["calibration"] and pc["relation"] == "opposite" and pc["line_numbers"] == ["4", "5"]
    assert pc["files"] == [["L004_0001.clraw"], ["L005_0001.clraw"]]
    w4, w5 = pc["windows"]
    assert len(w4) == 1 and w4[0]["utc"][0] < T0 + 5 and w4[0]["utc"][1] > T0 + L / SPEED - 5, w4
    assert len(w5) == 1 and w5[0]["gps_week"] > 2000
    print("calibration_check.json: Lines %s, %s, windows %.0f-%.0f s and %.0f-%.0f s" % (
        pc["line_numbers"], pc["relation"], w4[0]["utc"][0] - T0, w4[0]["utc"][1] - T0,
        w5[0]["utc"][0] - T0, w5[0]["utc"][1] - T0))
    txt = "\n".join(S.report_lines(r, 0.03, "street"))
    assert "driven in opposite directions" in txt and "Calibration (AlignPro):" in txt
    print("OK")


if __name__ == "__main__":
    main()
