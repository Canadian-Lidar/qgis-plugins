"""CloudPro's calibration findings in AlignPro (AlignPro 1.8.0, CloudPro 2.2.1), on a copy of the simulated
patch-test job (/jobs/cloudpro/_pristine/Job_Oct20: PosPro -> AlignPro -> CloudPro done).

Every CloudPro point is moved 4 cm forward along its line's direction of travel (what a timing latency does), the
line separation check is run, and AlignPro must: read calibration_check.json, list the pairs driven both ways as
calibration, find its own passes for a pair, and build an area of interest along the stretch the pair shares that
holds the pair's surfaces.

    python3 AlignPro/tests/cloudcheck_test.py      (from the plugins folder; needs /jobs/cloudpro/_pristine)
"""
import os
import shutil
import sys
import tempfile

import numpy as np

PLUGINS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PLUGINS)
SRC = "/jobs/cloudpro/_pristine/Job_Oct20"          # Processing/Job_Oct20 as it was after CloudPro
RAW = "/jobs/cloudpro/Job_Oct20"                     # the job folder (captures)
AHEAD = 0.04


def prepare():
    """A copy of the job with every CloudPro point 4 cm ahead along its line's travel, checked.
    -> (tmp folder, Processing/Job_Oct20 folder, CloudPro's separation result)"""
    tmp = tempfile.mkdtemp(prefix="alignpro_cloud_")
    root = os.path.join(tmp, "Job_Oct20")
    shutil.copytree(RAW, root, ignore=shutil.ignore_patterns("Processing"))
    job = os.path.join(root, "Processing", "Job_Oct20")
    shutil.copytree(SRC, job, ignore=shutil.ignore_patterns("separation", "*.bak"))
    from CloudPro.core import separation as S, pairmodel
    from CloudPro.core.project import Project as CP
    cp = CP.open_any(os.path.join(job, "03_CloudPro", "cloudpro.json"))
    st = cp.store
    mot = {int(k): pairmodel.Motion(v) for k, v in cp.tracks().items()}
    for k in st.keys():
        a = st.tile_array(k, writable=True)
        for lid, m in mot.items():
            sel = np.flatnonzero(a["line"] == lid)
            if not len(sel):
                continue
            u = m.direction(a["t"][sel])
            ok = np.isfinite(u[:, 0])
            a["x"][sel[ok]] += np.round(1000 * AHEAD * u[ok, 0]).astype(np.int32)
            a["y"][sel[ok]] += np.round(1000 * AHEAD * u[ok, 1]).astype(np.int32)
    st.sync()
    r = S.run(cp)
    S.write_feedback(cp, r, 0.03)
    return tmp, job, r


def main():
    if not os.path.isdir(SRC):
        print("SKIP: %s not here" % SRC)
        return
    from CloudPro.core import separation as S
    tmp, job, r = prepare()
    cal = S.calibration_pairs(r, 0.03)
    opp = [q for q in r.pairs if (q.get("model") or {}).get("relation") == "opposite"]
    assert opp and cal, [(q["a"], q["b"], q.get("model")) for q in r.pairs]
    for q in opp:
        al = q["model"]["along"]
        assert al is not None and abs(abs(al) - 2 * AHEAD) < 0.015, q["model"]
    print("CloudPro: %d pairs, %d driven both ways, %d point at the calibration (along %s cm)" % (
        len(r.pairs), len(opp), len(cal), ", ".join("%+.1f" % (100 * q["model"]["along"]) for q in opp)))

    from AlignPro.core import cloudcheck
    from AlignPro.core.project import Project
    p = Project.open_any(os.path.join(job, "Job_Oct20.dcsproj"))
    p.load()
    doc = cloudcheck.load(p)
    assert doc is not None and doc["pairs"], doc
    assert doc["current"], doc["other"]
    k = next(i for i, q in enumerate(doc["pairs"]) if q["calibration"] and q["relation"] == "opposite")
    pair = doc["pairs"][k]
    print("AlignPro reads: %s (current calibration)" % cloudcheck.describe(pair))
    idx = cloudcheck.lines_for(p, pair)
    assert len(idx) >= 2, (pair["files"], [ln.name for ln in p.lines])
    ring = cloudcheck.corridor(p, pair)
    assert ring and len(ring) > 10 and ring[0] == ring[-1]
    for i, ln in enumerate(p.lines):
        ln.use = i in idx
    p.cfg["aoi"] = ring
    p.settings["use_aoi"] = True
    p.analyse()
    inside = p.aoi_func()
    assert inside is not None and len(p.P) >= 6, len(p.P)
    print("AlignPro uses passes %s with an area of interest of %d points: %d patches" % (
        ", ".join(p.lines[i].name for i in idx), len(ring), len(p.P)))
    shutil.rmtree(tmp, ignore_errors=True)
    print("OK")


if __name__ == "__main__":
    main()
