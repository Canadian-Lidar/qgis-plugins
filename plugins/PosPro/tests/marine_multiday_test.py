"""Marine jobs over several days (PosPro 2.8.0, Sean 16 Nov, Greenstone): recordings added from another day's
folder and left out (kept by DCS Core for every plugin), one coverage bar per recording (no data over the night),
two recordings at the same time refused, each recording processed with the base that covers it, and a recording
holding its Reach log twice (a session file and the day file made from it) processed without dividing by zero."""
import os
import shutil
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from dcs_core.project import DcsProject          # noqa: E402
from dcs_core.marine import SonarRecording      # noqa: E402
from PosPro.core import marine, pipeline         # noqa: E402
from PosPro.core.project import Project          # noqa: E402
from RemixPro.core import make, sources          # noqa: E402
from RemixPro.tests import test_sonar as TS      # noqa: E402

DAY = 86400.0
G = 315964800.0 - 18.0


def make_recording(folder, out, t0, twice=False, name="survey"):
    """A simulated SonarView recording at t0 with its Reach log (twice: also a 'day file' copy) -> .clraw path."""
    TS.T0 = t0
    os.makedirs(folder)
    TS.make_svlz(os.path.join(folder, name + ".svlz"), seconds=20.0)
    TS.make_llh(os.path.join(folder, "reach.LLH"), t0 - 600, t0 + 600)
    if twice:
        shutil.copy(os.path.join(folder, "reach.LLH"), os.path.join(folder, "reach_day.LLH"))
    items = sources.scan([folder])
    os.makedirs(out, exist_ok=True)
    res = make.run(items[0], [sources.CLRAW], out)
    fs = [f for f in res[sources.CLRAW]["files"] if str(f).endswith(".clraw")]
    f = fs[0]
    return f if os.path.isabs(f) else os.path.join(res[sources.CLRAW]["folder"], f)


def test_multiday():
    t_orig = TS.T0
    tmp = tempfile.mkdtemp()
    try:
        job = os.path.join(tmp, "job")
        a = make_recording(os.path.join(tmp, "d1"), os.path.join(job, "Sonar"), t_orig, twice=True)
        b = make_recording(os.path.join(tmp, "d2"), os.path.join(tmp, "day2", "Sonar"), t_orig + DAY, name="day2")
        dcs = DcsProject.create(job, "MultiDay")
        p = Project.from_dcs(dcs)
        assert len(marine.recordings(dcs)) == 1
        # a recording of another day added from its own folder; kept in the .dcsproj for every plugin
        dcs.set_sonar_selection(added=[b])
        assert len(marine.recordings(dcs)) == 2
        assert len(DcsProject.open(dcs.path).sonar_clraw_files()) == 2
        runs = marine.recording_runs(p)
        assert len(runs) == 2 and runs[1][0] - runs[0][1] > DAY - 1000, runs     # the night is a gap
        # left out: gone for every plugin, still listed with include_excluded
        dcs.set_sonar_selection(excluded=[b])
        assert len(marine.recordings(dcs)) == 1 and len(marine.recordings(dcs, include_excluded=True)) == 2
        dcs.set_sonar_selection(excluded=[])
        # two recordings at the same time (a salvaged copy beside the original): refused, named
        c = make_recording(os.path.join(tmp, "d1s"), os.path.join(job, "Salvaged"), t_orig, name="survey_SALVAGED_VALID")
        ctx = pipeline.Ctx(log=lambda lvl, m: None)
        try:
            marine.process(p, ctx)
            raise AssertionError("overlapping recordings were processed")
        except marine.MarineError as e:
            assert "same time" in str(e), e
        dcs.set_sonar_selection(excluded=[c])
        # each recording with the base covering it; none covering -> the reason, not a silent wrong base
        ra, rb = [SonarRecording(ps) for ps in marine.recordings(dcs)]
        xyz = list(marine.llh_to_ecef(45.96, -66.64, 20.0))
        for k, day in enumerate((0, 1)):
            f = os.path.join(tmp, "base%d.26O" % k)
            TS.make_rinex(f, t_orig + day * DAY - 60, t_orig + day * DAY + 120)
            p.add_base({"name": "Day%d" % (k + 1), "files": [f], "xyz": xyz, "pos_source": "manual",
                        "coverage": [[t_orig + day * DAY - 60 - G, t_orig + day * DAY + 120 - G]]})
        assert marine.choose_base(p, ra)[0]["name"] == "Day1" and marine.choose_base(p, rb)[0]["name"] == "Day2"
        assert marine.base_for(p, rb)["obs"].endswith("base1.26O")
        p.remove_base([x for x in p.cfg["bases"] if x["name"] == "Day2"][0]["id"])
        none = marine.base_for(p, rb)
        assert "none" in none and "covers" in none["none"], none
        pos, why = marine.positions_ppk(rb, os.path.join(tmp, "w"), none, None)
        assert pos is None and "Add that day's base" in why, why
        # the Reach log held twice in recording A: each epoch once, a rate, no division by zero
        P = marine.positions_llh(ra)
        assert np.all(np.diff(P["t"]) > 0) and marine.rate_hz(P["t"]) > 0
        assert marine.rate_hz([5.0, 5.0, 5.0]) is None
        assert marine.is_sbas_log("Reach_20261014.26B") and not marine.is_sbas_log("Reach_20261014.26P")
        p.cfg["bases"] = []
        s = marine.process(p, ctx)                       # Reach log for both (no base): runs through
        assert [r["name"] for r in s["recordings"]] == [ra.name, rb.name]
        assert all(r["rate_hz"] for r in s["recordings"]), s["recordings"]
        print("multi-day: added / left out, coverage gap %.0f h, overlap refused, base per day, rates %s"
              % ((runs[1][0] - runs[0][1]) / 3600, [r["rate_hz"] for r in s["recordings"]]))
    finally:
        TS.T0 = t_orig
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_multiday()
    print("ok")
