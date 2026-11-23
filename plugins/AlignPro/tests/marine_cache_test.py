"""AlignPro 1.14.0: the soundings read from a marine job's sonar recordings are kept in 02_AlignPro/WORK/soundings
(compact: float32 but the time) and read again only when a recording, PosPro's attitude or a read setting changed.

    python3 AlignPro/tests/marine_cache_test.py      (from the plugins folder)
"""
import os
import shutil
import sys
import tempfile
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from dcs_core.project import DcsProject                     # noqa: E402
from AlignPro.core import marine as M                       # noqa: E402
from PosPro.core import marine as PM, pipeline              # noqa: E402
from PosPro.core.project import Project                     # noqa: E402
from PosPro.tests.marine_multiday_test import make_recording  # noqa: E402
from RemixPro.tests import test_sonar as TS                 # noqa: E402


def test_cache():
    tmp = tempfile.mkdtemp()
    try:
        job = os.path.join(tmp, "job")
        make_recording(os.path.join(tmp, "rec"), os.path.join(job, "Sonar"), TS.T0)
        dcs = DcsProject.create(job, "Cache")
        p = Project.from_dcs(dcs)
        ctx = pipeline.Ctx(log=lambda lvl, m: None)
        PM.process(p, ctx)
        PM.publish(p, ctx)
        t = time.perf_counter()
        a = M.load(dcs, every=1)
        t1 = time.perf_counter() - t
        d = M.cache_dir(dcs, create=False)
        files = sorted(os.listdir(d))
        assert files and all(f.endswith(".npz") for f in files), files
        t = time.perf_counter()
        b = M.load(dcs, every=1)
        t2 = time.perf_counter() - t
        for did, h in a["heads"].items():
            S, S2 = h["S"], b["heads"][did]["S"]
            assert S["t"].dtype == np.float64 and S["angle"].dtype == np.float32 and S["up"].dtype == np.float32
            for k in S:
                assert np.array_equal(S[k], S2[k]), k
        assert sorted(os.listdir(d)) == files                 # read from the cache, nothing written
        n = sum(len(h["S"]["t"]) for h in a["heads"].values())
        # another read setting: read again, the old cache of that recording and head replaced
        c = M.load(dcs, every=2)
        assert sorted(os.listdir(d)) != files and len(os.listdir(d)) == len(files)
        assert sum(len(h["S"]["t"]) for h in c["heads"].values()) < n
        # a recording changed: read again
        rec = dcs.sonar_clraw_files()[0]
        os.utime(rec, ns=(time.time_ns(), time.time_ns() + 10**9))
        M.load(dcs, every=2)
        assert len(os.listdir(d)) == len(files)
        print("soundings cache: %s soundings, first read %.2f s, again %.3f s, %d file(s)" % ("{:,}".format(n), t1, t2,
                                                                                           len(files)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_compact_solve():
    """The patch test on compact soundings finds the same corrections."""
    sys.path.insert(0, HERE)
    import test_marine as TM
    data = TM.simulate(roll_err=(0.5, -0.3))
    r64 = M.solve(data)
    for h in data["heads"].values():
        h["S"] = M.compact_soundings(h["S"])
    r32 = M.solve(data)
    for d in ("1", "2"):
        assert abs(r64["heads"][d]["roll"] - r32["heads"][d]["roll"]) < 0.005, (r64["heads"][d], r32["heads"][d])
    print("compact soundings: rolls %.4f / %.4f (float64 %.4f / %.4f)" % (
        r32["heads"]["1"]["roll"], r32["heads"]["2"]["roll"], r64["heads"]["1"]["roll"], r64["heads"]["2"]["roll"]))


if __name__ == "__main__":
    test_cache()
    test_compact_solve()
    print("marine_cache_test: OK")
