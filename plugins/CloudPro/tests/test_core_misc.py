"""Smaller audit fixes in the core (no QGIS needed):

- tiled filter runs: every tile computed on the points as they were before the run (the result does not depend
  on the tile order), grids of the changed tiles rebuilt by the run, one undo step
- height shifts: z moves by exactly what dz keeps (int16 limit)
- Poles: the faster crown test gives the same poles as before (14 of 40 stacks on the synthetic scene)

    python3 -m CloudPro.tests.test_core_misc          (from the plugins folder)
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import numpy as np                                                  # noqa: E402

from CloudPro.tests import _job                                     # noqa: E402


def tiled_order():
    from CloudPro.core import chain, filters as F, importer as IM
    from CloudPro.core.edits import EditManager
    from CloudPro.core.project import Project
    from CloudPro.core.runner import run_group_tiled
    dcs, p = _job.fresh_job("tiled")
    IM.import_chain(p, chain.resolve(dcs), IM.ImportOptions(decimate=2))
    work2 = p.work + "_copy"
    shutil.copytree(p.work, work2)
    g = F.FilterGroup("sor", [F.make("sor")])          # a neighbourhood filter: sees the tile buffers
    results = []
    for order in (1, -1):
        q = Project.from_dcs(dcs)
        if order < 0:
            shutil.rmtree(q.work)
            shutil.copytree(work2, q.work)
            q = Project.from_dcs(dcs)
            keys = q.store.keys
            q.store.keys = lambda keys=keys: list(reversed(keys()))
        ed = EditManager(q)
        n = run_group_tiled(q, g, ed, buffer=5.0)
        assert not getattr(q.store, "dirty_tiles", set()), "grids must be rebuilt by the run"
        flags = {}
        for k in sorted(q.store.keys()):
            a = q.store.tile_array(k)
            flags[k] = np.asarray(a["flag"]).copy()
        results.append((n, flags, len(ed.undo_stack)))
        q.store.close()
    (n1, f1, u1), (n2, f2, u2) = results
    assert n1 == n2 and n1 > 0, (n1, n2)
    assert all(np.array_equal(f1[k], f2[k]) for k in f1)
    assert u1 == u2 == 1
    print("tiled run: %d points changed, the same in either tile order, one undo step, grids rebuilt" % n1)


def tiled_parallel():
    """1.9.9: parts in parallel threads give the same edits as one thread; dense tiles are split into parts that
    cover every point once; cancelling writes nothing."""
    from CloudPro.core import chain, filters as F, importer as IM, runner
    from CloudPro.core.edits import EditManager
    from CloudPro.core.project import Project
    dcs, p = _job.fresh_job("par")
    IM.import_chain(p, chain.resolve(dcs), IM.ImportOptions(decimate=2))
    work2 = p.work + "_copy"
    shutil.copytree(p.work, work2)
    g = F.FilterGroup("sor", [F.make("sor"), F.make("ror")])
    flags = []
    for nw in (1, 3):
        shutil.rmtree(p.work)
        shutil.copytree(work2, p.work)
        q = Project.from_dcs(dcs)
        st = {}
        n = runner.run_group_tiled(q, g, EditManager(q), buffer=5.0, workers=nw, stats=st)
        assert st["workers"] == min(nw, st["units"]) and n > 0, st
        flags.append((n, {k: np.asarray(q.store.tile_array(k)["flag"]).copy() for k in q.store.keys()}))
        q.store.close()
    assert flags[0][0] == flags[1][0] and all((flags[0][1][k] == flags[1][1][k]).all() for k in flags[0][1])
    # dense tiles split: every point in exactly one part
    shutil.rmtree(p.work)
    shutil.copytree(work2, p.work)
    q = Project.from_dcs(dcs)
    big = max(q.store.counts.values())
    units = runner.plan_units(q.store, q.store.keys(), 5.0, unit_points=max(1000, big // 5))
    assert len(units) > len(q.store.keys()) and sum(u.n for u in units) == q.store.total
    # cancel: nothing written, Cancelled raised
    f0 = {k: np.asarray(q.store.tile_array(k)["flag"]).copy() for k in q.store.keys()}
    calls = [0]

    def cancel():
        calls[0] += 1
        return calls[0] > 3
    try:
        runner.run_group_tiled(q, g, EditManager(q), buffer=5.0, cancel=cancel)
        raise AssertionError("not cancelled")
    except IM.Cancelled:
        pass
    assert all((np.asarray(q.store.tile_array(k)["flag"]) == f0[k]).all() for k in f0)
    q.store.close()
    print("tiled run in parallel: same edits with 1 and 3 threads (%d), dense tiles split (%d parts), cancel writes "
          "nothing" % (flags[0][0], len(units)))


def shift_clip():
    from CloudPro.core.edits import EditManager
    from CloudPro.core.pointset import PointSet
    from CloudPro.core.project import Project
    from CloudPro.core.store import REC
    import tempfile
    p = Project.create(tempfile.mkdtemp(), "shift")
    r = np.zeros(10, REC)
    r["x"] = np.arange(10) * 100
    r["z"] = 1000
    r["dz"] = 32000
    p.store.origin = np.zeros(3)
    p.store.append(r)
    p.store.flush()
    ps = PointSet.from_store(p.store, dict(cx=0.5, cy=0, length=10, width=10, bearing=0))
    ed = EditManager(p)
    ed.ps = ps
    ed.apply(np.arange(len(ps)), "shift", 5.0)            # +5 m, but dz can only take 0.767 m more
    a = p.store.tile_array(p.store.keys()[0])
    assert np.all(a["dz"] == 32767) and np.all(a["z"] == 1000 + 767), (a["dz"], a["z"])
    print("height shift: z and dz stay consistent at the int16 limit")


def poles():
    from CloudPro.core import filters as F
    from CloudPro.core.pointset import PointSet
    rng = np.random.default_rng(1)
    pts = [np.column_stack([rng.uniform(0, 60, 200000), rng.uniform(0, 60, 200000), rng.normal(0, 0.02, 200000)])]
    for k in range(40):
        cx, cy = rng.uniform(5, 55, 2)
        pts.append(np.column_stack([cx + rng.normal(0, 0.08, 3000), cy + rng.normal(0, 0.08, 3000),
                                    rng.uniform(0, 6, 3000)]))
        if k % 2:
            pts.append(np.column_stack([cx + rng.normal(0, 1.5, 8000), cy + rng.normal(0, 1.5, 8000),
                                        rng.uniform(6.2, 9, 8000)]))
    P = np.vstack(pts)
    ps = PointSet(len(P))
    ps.x, ps.y, ps.z = P[:, 0], P[:, 1], P[:, 2]
    ps.cls[:] = 1
    ps.code = np.arange(len(P))
    ctx = F.Ctx(ps)
    F.make("f_poles").run(ctx, np.flatnonzero(ctx.accepted))
    assert ctx.messages[-1] == "Poles: 14 found, 39334 points", ctx.messages      # = CloudPro 1.6.1
    print("poles: %s (as before the speed-up)" % ctx.messages[-1])


def tiled_failure():
    """2.5.1: a filter failing on one part leaves that part as it was, writes the others and reports the part
    (Shirley, 12 Nov 2026: SMRF failed on one sliver after 49 min and the whole run was thrown away)."""
    from CloudPro.core import chain, filters as F, importer as IM, runner
    from CloudPro.core.edits import EditManager
    from CloudPro.core.project import Project
    dcs, p = _job.fresh_job("fail")
    IM.import_chain(p, chain.resolve(dcs), IM.ImportOptions(decimate=2))
    q = Project.from_dcs(dcs)
    units = runner.plan_units(q.store, q.store.keys(), 5.0)
    bad = units[len(units) // 2]

    class Boom(F.Filter):
        key, name, group, mod = "boom", "Boom", "Edit", "R"
        params = []

        def run(self, ctx, sel):
            xs, ys = ctx.x[ctx.scope], ctx.y[ctx.scope]
            if len(xs) and bad.x0 <= xs.min() < bad.x1 and bad.y0 <= ys.min() < bad.y1:     # the chosen part
                raise ValueError("Shape of array too small to calculate a numerical gradient")
            return {"reject": sel[::5]}
    f0 = {k: np.asarray(q.store.tile_array(k)["flag"]).copy() for k in q.store.keys()}
    st = {}
    n = runner.run_group_tiled(q, F.FilterGroup("boom", [Boom()]), EditManager(q), buffer=5.0, stats=st)
    assert len(st["failed"]) == 1 and st["failed"][0]["box"] == (bad.x0, bad.y0, bad.x1, bad.y1), st["failed"]
    assert "numerical gradient" in st["failed"][0]["error"] and n > 0
    changed_outside = unchanged_inside = 0
    for k in q.store.keys():
        a = q.store.tile_array(k)
        x, y = a["x"] * 1e-3, a["y"] * 1e-3
        inb = (x >= bad.x0) & (x < bad.x1) & (y >= bad.y0) & (y < bad.y1)
        ch = np.asarray(a["flag"]) != f0[k]
        assert not ch[inb].any()
        changed_outside += int(ch[~inb].sum())
        unchanged_inside += int(inb.sum())
    assert changed_outside == n and unchanged_inside == bad.n
    q.store.close()
    print("tiled run with one failing part: %d of %d parts written (%d points changed), the failed part (%d points) "
          "left as it was and reported" % (st["units"] - 1, st["units"], n, bad.n))


def main():
    shift_clip()
    poles()
    tiled_order()
    tiled_parallel()
    tiled_failure()
    print("test_core_misc: OK")


if __name__ == "__main__":
    main()
