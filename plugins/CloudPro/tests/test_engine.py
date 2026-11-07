"""The C engine (core/engine.py, bin/*/libcpengine.so / cpengine.dll) gives the same results as the Python code:

- georeferencing: every record of every tile identical, for several import settings (both returns with and
  without duplicates, strongest, last, skip stopped, decimation)
- chunk reading, tile grids, free-space ray counts (Moving objects), clusters (Guardrails, poles, signs) and
  RANSAC scores (Vertical plane) equal to their numpy / scipy forms
- CLOUDPRO_NO_ENGINE=1 turns it off (CloudPro then uses the Python code)

    python3 CloudPro/tests/test_engine.py          (from the plugins folder)
"""
import os
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGINS = os.path.dirname(os.path.dirname(HERE))
if PLUGINS not in sys.path:
    sys.path.insert(0, PLUGINS)

from CloudPro.tests import _job                       # noqa: E402
from CloudPro.core import chain, engine, importer as IM   # noqa: E402


def check_available():
    assert engine.available(), "engine not loaded: %s" % engine.status()
    out = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, %r); from CloudPro.core import "
                          "engine; print(engine.available(), engine.status())" % PLUGINS],
                         env=dict(os.environ, CLOUDPRO_NO_ENGINE="1"), capture_output=True, text=True).stdout
    assert out.startswith("False Python"), out
    print("engine:", engine.status(), "| CLOUDPRO_NO_ENGINE=1 ->", out.strip())


def _tiles(p):
    return {k: np.array(p.store.tile_array(k)) for k in p.store.keys()}


def _dual(P):
    """The simulated single-return packets as dual return (Last + Strongest): blocks 1, 3, 5, 7 become second
    returns of 0, 2, 4, 6, some the same distance or 1 unit off (duplicates), some further (a second target)."""
    P = P.copy()
    P[:, 1062] = 0x39
    k = np.arange(32)
    for b in (0, 2, 4, 6):
        s0, s1 = 12 + 130 * b, 12 + 130 * (b + 1)
        P[:, s1:s1 + 130] = P[:, s0:s0 + 130]
        off = s1 + 2 + 4 * k
        d = P[:, off].astype(np.int64) | (P[:, off + 1].astype(np.int64) << 8)
        d = np.where(d > 0, d + np.where(k % 3 == 0, 0, np.where(k % 3 == 1, 1, 400)), 0)
        d = np.minimum(d, 65535)
        P[:, off], P[:, off + 1] = d & 0xFF, d >> 8
    return P


def check_georef():
    from CloudPro.core import clraw
    orig_read, orig_points = clraw.Recording.read_lidar_chunk, engine.read_points
    for kw, dual in (({}, False), ({"drop_duplicates": False}, True), ({}, True), ({"returns": "strongest"}, True),
                     ({"returns": "last"}, True), ({"min_speed": 10.9}, False),
                     ({"decimate": 3, "rmin": 4.0, "rmax": 30.0}, False)):
        res = []
        if dual:
            clraw.Recording.read_lidar_chunk = lambda self, *a: _dual(orig_read(self, *a))
            engine.read_points = lambda *a, **k: _dual(orig_points(*a, **k))
        try:
            for use in (False, True):
                dcs, p = _job.fresh_job("eng")
                IM.import_chain(p, chain.resolve(dcs), IM.ImportOptions(use_engine=use, threads=2, **kw))
                res.append(_tiles(p))
                p.store.close()
        finally:
            clraw.Recording.read_lidar_chunk, engine.read_points = orig_read, orig_points
        kw = dict(kw, dual=True) if dual else kw
        a, b = res
        assert sorted(a) == sorted(b), "different tiles %s" % kw
        n = 0
        for k in a:
            assert len(a[k]) == len(b[k]) and (a[k] == b[k]).all(), "tile %s differs %s" % (k, kw)
            n += len(a[k])
        assert n > 0
        print("georeferencing %-40s %8d records identical" % (kw or "default", n))


def check_grids():
    dcs, p = _job.fresh_job("engr")
    IM.import_chain(p, chain.resolve(dcs), IM.ImportOptions(threads=2))
    st = p.store
    k0 = max(st.keys(), key=lambda k: st.counts.get(k, 0))
    a = st.tile_array(k0, writable=True)
    a["flag"][::5] |= 0x80                                    # some rejected points
    a.flush()
    ge = st.grid_stats(k0)
    real = engine._lib
    engine._lib, engine._why = None, "test"
    try:
        gp = st.grid_stats(k0)
    finally:
        engine._lib, engine._why = real, ""
    for f in ("count", "rej", "zmean", "zmin", "zmax", "imean"):
        x, y = ge[f].astype(float), gp[f].astype(float)
        with np.errstate(invalid="ignore"):
            ok = (x == y) | (np.isnan(x) & np.isnan(y)) | \
                (np.abs(x - y) <= 1e-6 * np.maximum(1, np.abs(np.nan_to_num(x))))
        assert ok.all(), f
    print("grids: engine = numpy for tile %s (%d points)" % (k0, len(a)))
    st.close()


def check_read_points():
    import glob
    from CloudPro.core import clraw
    files = sorted(glob.glob(os.path.join(_job.simjob(), "Shirley_Ave", "lines", "*", "clraw", "*.clraw")))
    groups = {}
    for f in files:
        groups.setdefault(os.path.basename(os.path.dirname(os.path.dirname(f))), []).append(f)
    n = 0
    for fs in groups.values():
        rec = clraw.Recording(fs)
        c1, c2 = {}, {}
        for fi, ci in rec.chunk_list():
            x, y = rec.read_lidar_chunk(c1, fi, ci), engine.read_points(rec, c2, fi, ci)
            assert x.shape == y.shape and (x == y).all()
            n += len(x)
        for c in (c1, c2):
            for fh in c.values():
                fh.close()
    assert n > 0
    print("chunk reading: %d packets identical" % n)


def _rays_python(o, u, Lmax, rl, step, lo, vs, dims, lid_bits):
    """filters.DynamicObjects' numpy loop (the reference)."""
    def vkey(p):
        v = np.floor((p - lo) / vs).astype(np.int64)
        ok = np.all((v >= 0) & (v < dims), axis=1)
        return v[:, 0] * dims[1] * dims[2] + v[:, 1] * dims[2] + v[:, 2], ok
    ns = np.ceil(np.maximum(Lmax, 0) / step).astype(np.int64)
    owner = np.repeat(np.arange(len(ns)), ns)
    k = np.arange(int(ns.sum())) - np.repeat(np.cumsum(ns) - ns, ns)
    dist = Lmax[owner] - k * step
    q = o[owner] + u[owner] * dist[:, None]
    kk, okk = vkey(q)
    fk = (kk[okk] << lid_bits) | rl[owner][okk]
    ray = owner[okk]
    keep = np.ones(len(fk), bool)
    keep[1:] = (fk[1:] != fk[:-1]) | (ray[1:] != ray[:-1])
    return np.unique(fk[keep], return_counts=True)


def check_rays():
    rng = np.random.default_rng(3)
    n = 20000
    o = np.column_stack([rng.uniform(0, 60, n), rng.uniform(-1, 1, n) + 20, np.full(n, 2.2)])
    p = np.column_stack([rng.uniform(-10, 70, n), rng.uniform(0, 40, n), rng.uniform(-1, 8, n)]).astype(np.float32)
    d = p - o
    L = np.linalg.norm(d, axis=1)
    u = d / np.maximum(L, 1e-6)[:, None]
    Lmax = np.minimum(L - 0.6, 40.0)
    rl = rng.integers(0, 3, n).astype(np.int64)
    vs = 0.25
    lo = p.min(0) - vs
    dims = np.ceil((p.max(0) + vs - lo) / vs).astype(np.int64) + 1
    ka, ca = _rays_python(o, u, Lmax, rl, vs * 0.5, lo, vs, dims, 8)
    kb, cb = engine.ray_count(o, u, Lmax, rl, vs * 0.5, lo, vs, dims, 8, nthreads=3)
    assert np.array_equal(ka, kb) and np.array_equal(ca, cb)
    print("free-space rays: %d voxel keys, counts identical" % len(ka))


def check_cluster():
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    from CloudPro.core import spatial as sp
    rng = np.random.default_rng(5)
    xyz = np.concatenate([rng.normal(c, 0.4, (3000, 3)) for c in rng.uniform(0, 30, (12, 3))] +
                         [rng.uniform(0, 30, (5000, 3))]).astype(np.float32)
    for r in (0.05, 0.2, 0.6):
        pairs = sp.tree(xyz).query_pairs(r, output_type="ndarray")
        g = coo_matrix((np.ones(len(pairs), np.int8), (pairs[:, 0], pairs[:, 1])), shape=(len(xyz),) * 2)
        _, la = connected_components(g, directed=False)
        lb, sb = engine.cluster(xyz, r)
        assert np.array_equal(la, lb) and np.array_equal(np.bincount(la), sb), r
    print("clusters: labels identical to scipy for 3 radii")


def check_line_counts():
    rng = np.random.default_rng(7)
    P2 = (rng.random((50000, 2)) * [80, 40] + [1000, 2000]).astype(np.float32)
    idx = rng.integers(0, len(P2), 64)
    ang = rng.uniform(0, np.pi, 64)
    nrm = np.column_stack([np.cos(ang), np.sin(ang)])
    ref = [int((np.abs((P2 - P2[a]) @ n) < 0.05).sum()) for a, n in zip(idx, nrm)]
    got = engine.line_counts(P2, P2[idx], nrm, 0.05, nthreads=3)
    assert list(got) == ref
    print("RANSAC scores: 64 candidates identical")


def main():
    check_available()
    check_read_points()
    check_georef()
    check_grids()
    check_rays()
    check_cluster()
    check_line_counts()


if __name__ == "__main__":
    main()
    print("test_engine: OK")
