"""Every filter on degenerate pieces of a job: no points, one, two, a row of points one cell wide (a sliver at a
tile edge), a column, a vertical stack, two far-apart points. A project-wide run splits the job into many pieces,
and one piece that crashes stops the whole run (Shirley, 12 Nov 2026: SMRF on a one-row piece, after 49 min).

    python3 CloudPro/tests/test_filter_edges.py      (from the plugins folder)
"""
import os
import sys
import traceback

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from CloudPro.core import filters as F                            # noqa: E402
from CloudPro.core.pointset import PointSet                       # noqa: E402

rng = np.random.default_rng(1)


def shape(name):
    if name == "empty":
        return np.zeros((0, 3))
    if name == "one":
        return np.array([[1.0, 1.0, 0.0]])
    if name == "two":
        return np.array([[1.0, 1.0, 0.0], [1.1, 1.0, 0.05]])
    if name == "row":           # 40 m long, 5 cm wide: one row of 0.5 m cells
        n = 3000
        return np.column_stack([rng.uniform(0, 40, n), rng.uniform(0, 0.05, n), rng.normal(0, 0.02, n)])
    if name == "column":
        n = 3000
        return np.column_stack([rng.uniform(0, 0.05, n), rng.uniform(0, 40, n), rng.normal(0, 0.02, n)])
    if name == "stack":         # a pole: every point in one cell
        n = 2000
        return np.column_stack([rng.normal(0, 0.01, n), rng.normal(0, 0.01, n), rng.uniform(0, 6, n)])
    if name == "far":
        return np.array([[0.0, 0.0, 0.0], [300.0, 250.0, 2.0]])
    if name == "same":          # identical points
        return np.tile([[2.0, 3.0, 1.0]], (50, 1))
    raise KeyError(name)


SHAPES = ["empty", "one", "two", "row", "column", "stack", "far", "same"]
fails = []
for name in SHAPES:
    P = shape(name)
    for key in sorted(F.REGISTRY):
        ps = PointSet(len(P))
        ps.x, ps.y, ps.z = P[:, 0].copy(), P[:, 1].copy(), P[:, 2].copy()
        ps.cls[:] = 1
        ps.code = np.arange(len(P))
        if hasattr(ps, "t"):
            ps.t = np.linspace(0, 10, len(P))
        ctx = F.Ctx(ps)
        sel = np.flatnonzero(ctx.accepted)
        if not len(sel) and key not in ("reset", "edit_class"):
            continue                            # the group never runs a filter on nothing (FilterGroup._apply_one)
        try:
            F.apply_result(ctx, F.make(key).run(ctx, sel))
        except Exception as e:      # noqa: BLE001
            fails.append((name, key, "%s: %s" % (type(e).__name__, e), traceback.format_exc().splitlines()[-3]))
for f in fails:
    print("FAIL %-7s %-14s %s\n      %s" % f)
assert not fails, "%d filter / shape combinations fail" % len(fails)
print("%d filters x %d degenerate pieces: no failures" % (len(F.REGISTRY), len(SHAPES)))
print("test_filter_edges: OK")
