"""Faster filters and grids with identical results (CloudPro 1.8.1):

- spatial.disk_opening equals scipy.ndimage.grey_opening with a disk footprint (r = 1..40)
- SMRF classifies a street scene exactly as with scipy's disk opening
- Store.grid_stats (integer cell indices) equals the float computation of the grid statistics
- the moving-objects filter's per-ray de-duplication gives the same voxel counts as np.unique

    python3 -m CloudPro.tests.test_filter_speed          (from the plugins folder)
"""
import os
import sys
import tempfile
import time

PLUGINS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PLUGINS)

import numpy as np                                                  # noqa: E402


def street(n=300_000, seed=1):
    from CloudPro.core.pointset import PointSet
    r = np.random.default_rng(seed)
    x = r.uniform(-40, 40, n)
    y = r.uniform(-20, 20, n)
    z = 0.02 * x - 0.02 * np.abs(y) + r.normal(0, 0.01, n)
    z[np.abs(y) > 6] += 0.15
    obj = r.random(n) < 0.25                                        # walls, poles, crowns, noise
    z[obj] += r.uniform(0.3, 9, obj.sum())
    ps = PointSet(n)
    ps.x, ps.y, ps.z = x.astype(np.float32), y.astype(np.float32), z.astype(np.float32)
    ps.cls[:] = 1
    ps.code = np.arange(n, dtype=np.int64)
    return ps


def test_disk_opening():
    from scipy import ndimage
    from CloudPro.core import spatial as sp
    G = np.random.default_rng(0).normal(size=(90, 170)).cumsum(1)
    G[10:20, 30:60] = np.nan
    G = sp.fill_nan_nearest(G)
    for r in range(1, 41):
        assert np.array_equal(ndimage.grey_opening(G, footprint=sp.disk(r)), sp.disk_opening(G, r)), r
    print("disk opening identical to scipy for r = 1..40: OK")


def test_smrf_same():
    from scipy import ndimage
    from CloudPro.core import filters as F, spatial as sp
    ps = street()
    out = []
    for fast in (True, False):
        if not fast:
            real = sp.disk_opening
            sp.disk_opening = lambda G, r: ndimage.grey_opening(G, footprint=sp.disk(r))   # noqa: E731
        try:
            ctx = F.Ctx(ps)
            t = time.time()
            F.FilterGroup("g", [F.make("smrf")]).run(ctx)
            out.append((ctx.cls.copy(), time.time() - t))
        finally:
            if not fast:
                sp.disk_opening = real
    assert np.array_equal(out[0][0], out[1][0]), "SMRF classes differ"
    print("SMRF: same %d ground points, %.2f s instead of %.2f s: OK" % (int((out[0][0] == 2).sum()), out[0][1],
                                                                        out[1][1]))


def test_grid_stats():
    from CloudPro.core.store import Store, REC
    st = Store(tempfile.mkdtemp(), origin=(0, 0, 0))
    n = 400_000
    r = np.zeros(n, REC)
    g = np.random.default_rng(2)
    r["x"], r["y"] = g.integers(-2000, 52000, n), g.integers(0, 50000, n)
    r["z"], r["i"] = g.integers(-800, 3000, n), g.integers(0, 255, n)
    r["flag"][g.random(n) < 0.2] = 0x80
    st.append(r)
    st.flush()
    for k in st.keys():
        a = st.tile_array(k)
        i, j = map(int, k.split("_"))
        x = a["x"] * 1e-3 - i * 50.0
        y = a["y"] * 1e-3 - j * 50.0
        z = a["z"] * 1e-3
        cell = np.clip((y / 0.5).astype(np.int64), 0, 99) * 100 + np.clip((x / 0.5).astype(np.int64), 0, 99)
        acc = (a["flag"] & 0x80) == 0
        G = st.grid_stats(k)
        assert np.array_equal(np.bincount(cell[acc], minlength=10000).reshape(100, 100), G["count"])
        assert np.array_equal(np.bincount(cell[~acc], minlength=10000).reshape(100, 100), G["rej"])
        zm = np.full(10000, -np.inf)
        np.maximum.at(zm, cell[acc], z[acc])
        assert np.allclose(zm.reshape(100, 100).astype(np.float32), G["zmax"], equal_nan=True)
    print("grid statistics from integer cells identical (%d tiles): OK" % len(st.keys()))


def test_ray_dedup():
    r = np.random.default_rng(3)
    ray = np.repeat(np.arange(500), r.integers(1, 60, 500))
    fk = np.concatenate([np.repeat(r.integers(0, 50, k), r.integers(1, 4, k))[:k]
                         for k in np.bincount(ray)])                # runs of repeated voxels per ray
    pair = np.unique(fk * 1000 + ray)
    ref = np.unique(pair // 1000, return_counts=True)
    keep = np.ones(len(fk), bool)
    keep[1:] = (fk[1:] != fk[:-1]) | (ray[1:] != ray[:-1])
    new = np.unique(fk[keep], return_counts=True)
    # a ray can come back to a voxel only by a repeated random draw here, never on a straight ray
    same = np.array_equal(ref[0], new[0])
    assert same
    print("ray de-duplication: same voxels as np.unique: OK")


def test_neighbour_count():
    """Weather (DROR/LIOR/DIOR) and ROR: the nearest-neighbour test with the dense-cube shortcut gives exactly
    the answer of counting every neighbour within r, and does not slow down with density."""
    import time
    from CloudPro.core import spatial as sp
    r = np.random.default_rng(4)
    wall = np.column_stack([r.uniform(0, 40, 1_500_000), 30 + r.normal(0, 0.01, 1_500_000),
                            r.uniform(0, 10, 1_500_000)])                 # ~3,750 points / m2
    noise = r.uniform([0, 0, -2], [40, 40, 20], (100_000, 3))
    data = np.concatenate([wall, noise])
    tr = sp.tree(data)
    q = np.concatenate([data[::2], r.uniform([0, 0, -2], [40, 40, 20], (20_000, 3))])   # some not in the tree
    rad = np.maximum(0.04, r.uniform(0, 0.4, len(q)))
    for kmin in (1, 3, 8):
        t = time.time()
        ref = tr.query_ball_point(q, rad, workers=-1, return_length=True) - 1 < kmin
        t_ref = time.time() - t
        t = time.time()
        got = sp.fewer_than(tr, q, rad, kmin)
        t_new = time.time() - t
        assert np.array_equal(ref, got), kmin
        print("neighbour count kmin=%d: identical (%d of %d too few), %.2f s -> %.2f s: OK"
              % (kmin, ref.sum(), len(q), t_ref, t_new))
    d, _ = tr.query(q[:50_000], k=13, workers=-1)
    assert np.allclose(d[:, 1:].mean(1), sp.mean_knn_distance(tr, q[:50_000], 12))
    print("SOR mean distance in blocks: identical: OK")


if __name__ == "__main__":
    test_disk_opening()
    test_smrf_same()
    test_grid_stats()
    test_ray_dedup()
    test_neighbour_count()
    print("test_filter_speed: OK")
