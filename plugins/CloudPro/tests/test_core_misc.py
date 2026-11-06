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


def main():
    shift_clip()
    poles()
    tiled_order()
    print("test_core_misc: OK")


if __name__ == "__main__":
    main()
