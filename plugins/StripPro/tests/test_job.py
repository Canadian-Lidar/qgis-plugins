"""StripPro end to end on the simulated patch-test job (/jobs/cloudpro/Job_Oct20, six lines, no recording holes):
read the lines, solve, finalize the corrected trajectory; the trajectory hardly moves (nothing to correct), the
handover is a dcs.trajectory/1 that DCS Core counts as StripPro's stage, CloudPro then georeferences with it, and
the holes PosPro's trajectory would have are filled.

    python3 StripPro/tests/test_job.py      (from the plugins folder)
"""
import json
import os
import shutil
import sys
import tempfile

import numpy as np

PLUGINS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PLUGINS)

RAW = "/jobs/cloudpro/Job_Oct20"


def main():
    if not os.path.isdir(RAW):
        print("SKIP: %s not here" % RAW)
        return
    from dcs_core.project import DcsProject
    from dcs_core import sbet as dsbet
    from StripPro.core import job as J
    tmp = tempfile.mkdtemp(prefix="strippro_")
    root = os.path.join(tmp, "Job_Oct20")
    shutil.copytree(RAW, root, ignore=shutil.ignore_patterns("separation", "*.bak", "store"))
    dcs = DcsProject.open(os.path.join(root, "Processing", "Job_Oct20", "Job_Oct20.dcsproj"))
    assert dcs.state("strippro")[0] == "none"
    # holes in PosPro's trajectory: 0.4 s missing every 3 s (the simulated motion is smooth, so straight lines
    # across them are nearly right: this checks the plumbing, the simulation test checks the solve)
    tj = json.load(open(os.path.join(root, "Processing", "Job_Oct20", "01_PosPro", "out", "trajectory.json")))
    sb = os.path.join(root, "Processing", "Job_Oct20", "01_PosPro", "out", "trajectory_sbet.out")
    a = np.fromfile(sb, dsbet.SBET)
    keep = ((a["t"] - a["t"][0]) % 3.0) < 2.6
    a[keep].tofile(sb)
    n_holes = len(dsbet.holes(a[keep]["t"]))
    inp = J.Inputs(dcs)
    assert len(inp.lines()) == 6, inp.lines()
    seen = []
    summary = J.run(inp, progress=lambda f, m: seen.append(f), log=print)
    assert seen and seen[-1] > 0.9
    for k, v in summary["lines"].items():
        mv = v["moved"]
        print("%s: moved median %.1f cm, 95 %% %.1f cm" % (v["name"], 100 * mv["median_m"], 100 * mv["p95_m"]))
        assert mv["median_m"] < 0.03, v
    doc = J.write_outputs(inp, summary, "test")
    assert doc["schema"] == "dcs.trajectory/1" and doc["strip"]["filled_records"] > 0
    dcs.reload()
    assert dcs.state("strippro")[0] == "done", dcs.state("strippro")
    t_new, _a, _ws = dsbet.read(os.path.join(J.out_dir(dcs), "trajectory_sbet.out"), inp.leap, week=inp.week)
    left = dsbet.holes(t_new)
    print("holes in PosPro's trajectory %d; left after StripPro %d (outside the lines)" % (n_holes, len(left)))
    assert len(left) < n_holes / 2
    # CloudPro: uses StripPro's trajectory now, and its points are out of date
    st, why = dcs.state("cloudpro")
    assert st == "stale" and any("StripPro" in w for w in why), (st, why)
    from CloudPro.core import chain
    ci = chain.resolve(dcs)
    assert ci.traj_info["source"] == "strippro" and ci.traj_info["handover_id"] == doc["id"], ci.traj_info
    assert ci.traj_info.get("pospro_id") == tj["id"]
    print("CloudPro: %s; stale because %s" % (ci.traj_info["description"], why[0]))
    shutil.rmtree(tmp, ignore_errors=True)
    print("OK")


if __name__ == "__main__":
    main()
