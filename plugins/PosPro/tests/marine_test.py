"""PosPro on a marine job: a simulated SonarView recording with a Reach log, converted by RemixPro, processed and
finalized (Marine page's work, without the window)."""
import json
import os
import shutil
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from dcs_core import sbet                        # noqa: E402
from dcs_core.project import DcsProject          # noqa: E402
from PosPro.core import marine, pipeline         # noqa: E402
from PosPro.core.project import Project          # noqa: E402
from RemixPro.core import make, sources          # noqa: E402
from RemixPro.tests import test_sonar as TS      # noqa: E402


def test_marine_llh_process_and_finalize():
    tmp = tempfile.mkdtemp()
    try:
        rec = os.path.join(tmp, "rec")
        os.makedirs(rec)
        TS.make_svlz(os.path.join(rec, "2026-09-21_survey.svlz"), seconds=20.0)
        TS.make_llh(os.path.join(rec, "reach.LLH"), TS.T0 - 600, TS.T0 + 600)
        with open(os.path.join(rec, "sonar_offsets.json"), "w") as f:
            json.dump({"1": [-0.40, -0.31, 0.65], "2": [-0.40, 0.31, 0.65]}, f)
        items = sources.scan([rec])
        job = os.path.join(tmp, "job")
        os.makedirs(os.path.join(job, "Sonar"))
        make.run(items[0], [sources.CLRAW], os.path.join(job, "Sonar"))
        dcs = DcsProject.create(job, "MarineTest")
        p = Project.from_dcs(dcs)
        assert marine.is_marine(p)
        assert len(marine.recordings(dcs)) == 1
        log = []
        ctx = pipeline.Ctx(log=lambda lvl, m: log.append((lvl, m)))
        with dcs.run("pospro", "PosPro", "test"):
            s = marine.process(p, ctx)
            h = marine.publish(p, ctx)
        r0 = s["recordings"][0]
        assert r0["source"] == "llh", r0
        assert sorted(s["heads"]) in (["1", "2"], [1, 2])
        dcs.reload()
        assert dcs.state("pospro")[0] == "done"
        doc = json.load(open(h["path"]))
        assert doc["platform"] == "marine"
        assert sorted(doc["sensors"]) == ["sonar_1", "sonar_2"]
        assert doc["sensors"]["sonar_1"]["lever_m"] == [-0.40, -0.31, 0.65]
        S = np.fromfile(os.path.join(p.out_dir, "trajectory_sbet.out"), sbet.SBET)
        assert len(S) > 10
        att = np.load(os.path.join(p.out_dir, "sonar_1_attitude.npy"))
        assert len(att) > 0 and att["up"].shape[1] == 3
        # a head switched off on the Installation page is left out of the trajectory's sensors
        p.cfg.setdefault("marine", {})["heads_off"] = ["2"]
        with dcs.run("pospro", "PosPro", "test"):
            s2 = marine.process(p, ctx)
            h2 = marine.publish(p, ctx)
        assert sorted(str(k) for k in s2["heads"]) == ["1"]
        assert sorted(json.load(open(h2["path"]))["sensors"]) == ["sonar_1"]
        assert s2["settings"]["heads_off"] == ["2"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_marine_llh_process_and_finalize()
    print("ok")
