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


def test_fan_edge():
    ps, kind, tracks = survey()
    a = rng.uniform(-45, 45, len(ps.x))                           # beam angle from the head's beam centre
    ps.az[:] = np.mod(np.round(a * 100), 36000).astype(np.uint16)
    assert np.allclose(MF.beam_angle(ps.az), np.round(a * 100) / 100)
    ctx = F.Ctx(ps, tracks=tracks)
    sel = np.arange(len(ps.x))
    r = F.make("m_gates", edge=40.0, rmax=500.0, outboard=90.0).run(ctx, sel)
    assert set(r["reject"]) == set(np.flatnonzero(np.abs(np.round(a * 100) / 100) > 40.0))
    assert len(F.make("m_gates", rmax=500.0, outboard=90.0).run(ctx, sel)["reject"]) == 0      # 90 = off
    std = [x for x in F.default_groups(marine=True) if x.name.startswith("Marine - standard")][0]
    gate = [f for f in std.filters if f.key == "m_gates"][0]
    assert gate.v("edge") == 40.0 and gate.v("outboard") == 85.0      # 2.15.0 (was 65)
    light = [x for x in F.default_groups(marine=True) if x.name.startswith("Marine - light")][0]
    assert [f for f in light.filters if f.key == "m_gates"][0].v("edge") == 90.0


def channel_pings(n_pings=60, per_ping=200, multipath=0.25, fish=0.02):
    """Pings of one head looking to starboard across a river channel (12 m deep, a bank rising to 1 m at 35 m out):
    beam centre 40 deg from the vertical, fan +-45 deg. -> (ps, kind) kind 0 seabed, 1 multipath beyond the
    bottom (under the seabed and the bank), 2 fish in front of it."""
    def bottom(y):
        return np.where(y < 20, 12.0, np.maximum(1.0, 12.0 - (y - 20) * 11.0 / 15.0))
    r_all, a_all, t_all, kind = [], [], [], []
    for k in range(n_pings):
        th = np.radians(rng.uniform(-5, 85, per_ping))                 # from the vertical
        R = np.linspace(0.5, 120, 2400)
        y = np.outer(np.sin(th), R)
        z = np.outer(np.cos(th), R)
        hit = z >= bottom(y)
        first = np.where(hit.any(1), hit.argmax(1), -1)
        m = first >= 0
        rb = np.where(m, R[np.clip(first, 0, None)], np.nan)
        rr = rb + rng.normal(0, 0.05, per_ping)
        typ = np.zeros(per_ping, int)
        u = rng.random(per_ping)
        mp = m & (u < multipath)
        rr[mp] = rb[mp] * rng.uniform(1.3, 2.5, mp.sum())                # multipath, side lobes: past the bottom
        typ[mp] = 1
        fs = m & (u > 1 - fish)
        rr[fs] = rb[fs] * rng.uniform(0.3, 0.8, fs.sum())
        typ[fs] = 2
        keep = m
        r_all.append(rr[keep]); a_all.append(np.degrees(th[keep]) - 40.0); t_all.append(np.full(keep.sum(), k * 0.1))
        kind.append(typ[keep])
    r, a, t, kind = (np.concatenate(v) for v in (r_all, a_all, t_all, kind))
    ps = PointSet(len(r))
    ps.line[:] = 7
    ps.t[:] = 1790000000.0 + t
    ps.az[:] = np.mod(np.round(a * 100), 36000).astype(np.uint16)
    ps.rng[:] = np.clip(np.round(r * 100), 0, 65535).astype(np.uint16)
    return ps, kind


def test_first_bottom():
    """Beyond the first bottom (CloudPro 2.14.0): what comes back from past the seabed a ping already reached is
    rejected at any depth (under the channel and under the bank); the seabed and the water column are left."""
    ps, kind = channel_pings()
    ctx = F.Ctx(ps)
    sel = np.arange(len(ps.x))
    rej = np.zeros(len(sel), bool)
    rej[F.make("m_first_bottom").run(ctx, sel)["reject"]] = True
    print("first bottom: seabed kept %.1f %%, multipath removed %.1f %%, fish kept %.1f %%" % (
        100 * (~rej[kind == 0]).mean(), 100 * rej[kind == 1].mean(), 100 * (~rej[kind == 2]).mean()))
    assert (~rej[kind == 0]).mean() > 0.99
    assert rej[kind == 1].mean() > 0.95
    assert (~rej[kind == 2]).all()                                     # in front of the bottom: other filters' job
    # the marine groups run it after the depth hypotheses
    for name in ("Marine - standard", "Marine - strict", "Marine - water column"):
        g = [x for x in F.default_groups(marine=True) if x.name.startswith(name)][0]
        keys = [f.key for f in g.filters]
        assert keys.index("m_first_bottom") == keys.index("m_cube") + 1, (name, keys)


def test_update_group():
    """2.15.0: a ready-made group saved in a job before 2.14.0 (no Beyond the first bottom, outboard 65 deg) gains
    the new filter at its place and the new outboard default; a setting the user changed is kept."""
    old = {"name": "Marine - standard clean (IHO Order 1a)", "mode": "Combined", "user": "",
           "filters": [{"key": "reset", "enabled": True, "values": {}},
                       {"key": "m_gates", "enabled": True, "values": {"inboard": 20.0, "outboard": 65.0, "edge": 40.0,
                                                                      "dmax": 35.0}},
                       {"key": "isolated", "enabled": True, "values": {"interval": 1.0, "min_pts": 10}},
                       {"key": "m_cube", "enabled": True, "values": {"nmin": 2}},
                       {"key": "m_outliers", "enabled": True, "values": {}},
                       {"key": "m_spline", "enabled": True, "values": {}}]}
    g0 = F.FilterGroup.from_dict(old)
    g, changes = F.update_group(g0, marine=True)
    keys = [f.key for f in g.filters]
    assert keys == ["reset", "m_gates", "isolated", "m_cube", "m_first_bottom", "m_outliers", "m_spline"], keys
    gates = g.filters[1]
    assert gates.values["outboard"] == 85.0 and gates.values.get("dmax", 35.0) == 35.0
    assert g.filters[3].values["nmin"] == 2                         # the user's setting kept
    assert g.version == 2 and any("Beyond the first bottom" in c for c in changes), changes
    g2, changes2 = F.update_group(g, marine=True)                    # up to date now
    assert not changes2, changes2
    # a user-changed outboard angle is kept
    old["filters"][1]["values"]["outboard"] = 78.0
    g3, _ = F.update_group(F.FilterGroup.from_dict(old), marine=True)
    assert g3.filters[1].values["outboard"] == 78.0
    # Beyond the first bottom added by hand and moved up (as Sean did): not added twice
    moved = dict(old)
    moved["filters"] = old["filters"][:2] + [{"key": "m_first_bottom", "enabled": True, "values": {}}] + old["filters"][2:]
    g4, _ = F.update_group(F.FilterGroup.from_dict(moved), marine=True)
    assert [f.key for f in g4.filters].count("m_first_bottom") == 1
    # round trip keeps base / version
    assert F.FilterGroup.from_dict(g.to_dict()).version == 2
    print("group update: %s" % "; ".join(changes))


if __name__ == "__main__":
    test_update_group()
    test_first_bottom()
    test_tvu()
    test_fan_edge()
    test_angle_gate_and_marine_first()
    test_standard_group()
    print("test_marine_filters: OK")
