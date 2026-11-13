"""CloudPro on a marine job: a simulated SonarView recording (RemixPro), PosPro's marine trajectory, AlignPro's
corrections per head; every head of every pass becomes a sonar line and the corrections are used."""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)


class TestMarineImport(unittest.TestCase):
    def test_chain(self):
        from dcs_core import handover
        from dcs_core.project import DcsProject
        from PosPro.core import marine as PM, pipeline
        from PosPro.core.project import Project as PProject
        from AlignPro.core import marine as AM
        from CloudPro.core import chain, importer as IM
        from CloudPro.core.project import Project
        from RemixPro.core import make, sources
        from RemixPro.tests import test_sonar as TS
        tmp = tempfile.mkdtemp()
        try:
            rec = os.path.join(tmp, "rec")
            os.makedirs(rec)
            TS.make_svlz(os.path.join(rec, "2026-09-21_survey.svlz"), seconds=20.0)
            TS.make_llh(os.path.join(rec, "reach.LLH"), TS.T0 - 600, TS.T0 + 600)
            with open(os.path.join(rec, "sonar_offsets.json"), "w") as f:
                json.dump({"1": [-0.40, -0.31, 0.65], "2": [-0.40, 0.31, 0.65]}, f)
            job = os.path.join(tmp, "job")
            os.makedirs(os.path.join(job, "Sonar"))
            make.run(sources.scan([rec])[0], [sources.CLRAW], os.path.join(job, "Sonar"))
            dcs = DcsProject.create(job, "MarineTest")
            pp = PProject.from_dcs(dcs)
            with dcs.run("pospro", "PosPro", "test"):
                PM.process(pp, pipeline.Ctx(log=lambda *_: None))
                PM.publish(pp, None)
            dcs.reload()
            doc = handover.read(dcs, "pospro")
            res = {"heads": {d: {"roll": 0.0, "pitch": 0.0, "heading": 0.0, "latency_s": 0.0} for d in ("1", "2")},
                   "before_cm": 0.0, "after_cm": 0.0, "observations": 0, "passes": 1, "between_heads_share": 0.0}
            applied = {"1": {"roll": 0.5, "pitch": 0.0, "heading": 0.0, "latency_s": 0.0},
                       "2": {"roll": -0.25, "pitch": 0.0, "heading": 0.0, "latency_s": 0.0}}
            with dcs.run("alignpro", "AlignPro", "test"):
                AM.publish(dcs, res, {"heads": {1: {"nickname": "1"}, 2: {"nickname": "2"}}, "doc": doc},
                           applied=applied)
            dcs.reload()
            inp = chain.resolve(dcs)
            self.assertTrue(inp.marine)
            self.assertEqual(inp.heads_cal["1"]["roll"], 0.5)
            p = Project.from_dcs(dcs)
            out, _ = IM.import_chain(p, inp, IM.ImportOptions(), progress=None)
            lines = [p.lines[lid] for lid, _st in out]
            self.assertEqual(sorted(l["head"] for l in lines), [1, 2])
            self.assertTrue(all(l["kind"] == "sonar" for l in lines))
            byh = {l["head"]: l for l in lines}
            self.assertEqual(byh[1]["calibration_deg"]["roll"], 0.5)
            self.assertEqual(byh[2]["calibration_deg"]["roll"], -0.25)
            self.assertGreater(sum(st["points"] for _l, st in out), 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
