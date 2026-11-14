"""Interpolation (fill holes with virtual points, as AutoClean): a sloping surface 100 x 60 m with a round hole
(r 6 m) inside and a bite out of its edge. Each method must fill what it is meant to (holes / the whole area /
the edge) with heights on the surface; points per cell, append, delete, and the virtual points surviving a
rebuild of the store.

    python3 CloudPro/tests/test_interpolate.py      (from the plugins folder)
"""
import os
import shutil
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from CloudPro.core import interpolate as I                     # noqa: E402
from CloudPro.core.project import Project                      # noqa: E402
from CloudPro.core.store import REC                           # noqa: E402


def surface(x, y):
    return 10.0 + 0.05 * x - 0.02 * y


def make():
    rng = np.random.default_rng(4)
    n = 120000
    x, y = rng.uniform(0, 100, n), rng.uniform(0, 60, n)
    hole = np.hypot(x - 50, y - 30) < 6.0                     # inside: a hole surrounded by data
    bite = (x > 85) & (y > 45)                                 # on the edge: open to the outside
    k = ~hole & ~bite
    x, y = x[k], y[k]
    z = surface(x, y) + rng.normal(0, 0.01, len(x))
    d = tempfile.mkdtemp()
    p = Project.create(d, "interp")
    p.store.origin = np.zeros(3)
    r = np.zeros(len(x), REC)
    r["x"], r["y"], r["z"] = (np.round(v * 1000).astype(np.int32) for v in (x, y, z))
    r["cls"] = 2
    p.add_line({"name": "Line 1", "files": ["a.clraw"]})
    p.store.append(r)
    p.store.flush()
    p.store.save_meta()
    return p, d


def check_on_surface(p, lid, tol=0.03):
    a = np.concatenate([np.array(p.store.tile_array(k)) for k in p.store.keys()])
    v = a[a["line"] == lid]
    x, y, z = v["x"] * 1e-3, v["y"] * 1e-3, v["z"] * 1e-3
    err = np.abs(z - surface(x, y))
    assert np.percentile(err, 95) < tol, np.percentile(err, 95)
    return x, y


def test_methods():
    p, d = make()
    try:
        # hole - inverse distance, whole project: the round hole is filled, the bite (open) is not
        r = I.run(p, I.Settings(method="hole_idw", cell=1.0, points_per_cell=4, search=10))
        lid = r["line"]
        x, y = check_on_surface(p, lid)
        assert r["points"] == 4 * r["filled_cells"]
        assert np.all(np.hypot(x - 50, y - 30) < 8.0), "only the round hole is filled"
        assert 90 < r["filled_cells"] < 140, r            # ~ pi 6^2 = 113 cells
        n_idw = r["points"]
        # hole - triangulation replaces them (append off): the same hole, straight lines
        r = I.run(p, I.Settings(method="hole_tri", cell=1.0, points_per_cell=1, search=10))
        x, y = check_on_surface(p, r["line"])
        assert np.all(np.hypot(x - 50, y - 30) < 8.0) and r["total"] == r["points"] != n_idw
        # area methods need an area
        try:
            I.run(p, I.Settings(method="area_tri"))
            raise AssertionError("area_tri without an area")
        except I.InterpolationError:
            pass
        box = dict(cx=92.0, cy=52.0, length=20.0, width=20.0, bearing=0.0)       # over the bite
        r = I.run(p, I.Settings(method="area_extra", cell=1.0, points_per_cell=16, search=20, append=True), box)
        assert r["filled_cells"] > 100 and r["points"] == 16 * r["filled_cells"], r
        assert r["total"] > r["points"]                                           # appended to the hole's
        check_on_surface(p, r["line"], tol=0.6)       # extrapolation holds the neighbours level (no slope)
        r = I.run(p, I.Settings(method="area_tri", cell=1.0, points_per_cell=64, search=30), box)
        check_on_surface(p, r["line"])
        # keep through a rebuild: snapshot, new store, restore
        snap = I.snapshot(p)
        n_before = I.count(p)
        p.begin_rebuild()
        lid2 = p.add_line({"name": "Line 1", "files": ["a.clraw"]})
        assert lid2 == 0
        I.restore(p, snap)
        p.commit_rebuild()
        assert I.count(p) == n_before and I.line_id(p) is not None
        # delete
        n, keys = I.delete(p)
        assert n == n_before and keys and I.count(p) == 0
        print("interpolation: hole idw / hole tri / area extrapolation / area tri on the surface; "
              "append, rebuild, delete OK")
    finally:
        p.store.close()
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    test_methods()
    print("test_interpolate: OK")
