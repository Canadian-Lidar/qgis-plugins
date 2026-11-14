"""Marine filters on a simulated sonar survey: a sloping, rippled seabed 10 m below a boat running two passes,
soundings with 5 cm noise, plus a multipath ghost 12 m under the seabed, water-column returns (fish) and scattered
deep noise. The standard marine group must remove the ghosts, the fish and the noise and keep the seabed.

    python3 CloudPro/tests/test_marine_filters.py      (from the plugins folder)
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from CloudPro.core import filters as F                            # noqa: E402
from CloudPro.core import marinefilters as MF                     # noqa: E402
from CloudPro.core.pointset import PointSet                       # noqa: E402

rng = np.random.default_rng(3)


def seabed(x, y):
    return -10.0 - 0.02 * x + 0.15 * np.sin(x / 4.0)


def survey():
    pts, kind, line, tt = [], [], [], []
    tracks = {}
    for lid, (y0, side) in enumerate(((0.0, 1.0), (30.0, -1.0))):
        t = np.arange(0, 80, 0.2)
        E, N = t * 1.5, np.full_like(t, y0)
        tracks[lid] = {"t": t, "E": E, "N": N, "Z": np.zeros_like(t), "heading": np.full_like(t, np.pi / 2)}
        for k, ti in enumerate(t):
            ang = np.radians(rng.uniform(-10, 70, 60))          # mostly outboard (to starboard: -y with heading east)
            across = 10.0 * np.tan(ang) * side
            x = np.full(60, E[k]) + rng.normal(0, 0.2, 60)
            y = N[k] - across
            z = seabed(x, y) + rng.normal(0, 0.05, 60)
            pts.append(np.column_stack([x, y, z])); kind += [0] * 60; line += [lid] * 60; tt += [ti] * 60
    P = np.vstack(pts)
    kind, line, tt = np.array(kind), np.array(line), np.array(tt)
    sea = np.flatnonzero(kind == 0)
    gh = rng.choice(sea, 1500, replace=False)                     # multipath: 12 m under the seabed
    fish = rng.choice(sea, 800, replace=False)                    # water column: 2-7 m above the seabed
    deep = rng.choice(sea, 300, replace=False)                    # scattered noise
    extra = [P[gh] + [0, 0, -12.0], P[fish] + np.column_stack([np.zeros((800, 2)), rng.uniform(2, 7, 800)]),
             P[deep] + np.column_stack([np.zeros((300, 2)), -rng.uniform(5, 80, 300)])]
    P = np.vstack([P] + extra)
    kind = np.r_[kind, np.full(1500, 1), np.full(800, 2), np.full(300, 3)]
    line = np.r_[line, line[gh], line[fish], line[deep]]
    tt = np.r_[tt, tt[gh], tt[fish], tt[deep]]
    ps = PointSet(len(P))
    ps.x, ps.y, ps.z = (P[:, i].astype(np.float32) for i in range(3))
    ps.line[:] = line
    ps.t[:] = tt
    ps.i[:] = 128
    ps.rng[:] = 1500
    return ps, kind, tracks


def test_tvu():
    assert abs(MF.tvu(0, "Order 1a") - 0.5) < 1e-12
    assert abs(MF.tvu(40, "Order 1a") - np.hypot(0.5, 0.52)) < 1e-12
    assert abs(MF.tvu(10, "Special") - np.hypot(0.25, 0.075)) < 1e-12


def test_standard_group():
    ps, kind, tracks = survey()
    g = [x for x in F.default_groups(marine=True) if x.name.startswith("Marine - standard")][0]
    ctx = F.Ctx(ps, tracks=tracks)
    g.run(ctx)
    rej = ctx.group_rejected
    sea = kind == 0
    print("seabed kept %.1f %%, ghosts removed %.1f %%, fish removed %.1f %%, deep noise removed %.1f %%" % (
        100 * (~rej[sea]).mean(), 100 * rej[kind == 1].mean(), 100 * rej[kind == 2].mean(), 100 * rej[kind == 3].mean()))
    assert (~rej[sea]).mean() > 0.93
    assert rej[kind == 1].mean() > 0.95
    assert rej[kind == 2].mean() > 0.95
    assert rej[kind == 3].mean() > 0.95


def test_angle_gate_and_marine_first():
    ps, kind, tracks = survey()
    ctx = F.Ctx(ps, tracks=tracks)
    sel = np.flatnonzero(kind == 0)
    a = MF.outboard_angle(ctx, sel)
    assert np.nanmedian(a) > 15                                   # the fans look outboard
    r = F.make("m_gates", inboard=5.0).run(ctx, sel)
    assert 0.05 < len(r["reject"]) / len(sel) < 0.3              # roughly the -10..-5 deg part
    assert F.default_groups(marine=True)[0].name.startswith("Marine")
    assert not F.default_groups()[0].name.startswith("Marine")


if __name__ == "__main__":
    test_tvu()
    test_angle_gate_and_marine_first()
    test_standard_group()
    print("test_marine_filters: OK")
