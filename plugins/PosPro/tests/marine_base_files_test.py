"""Sean's Sissboo job (18 Nov 2026), PosPro 2.10.3 / DCS Core 1.19.1:

- The "Recorded" base holds the recordings' base RINEX with its navigation (.26P) and SBAS (.26B) files: PPP and
  processing merge only the observation files (2.10.2: "list different observation types").
- A recording's span is its first to last sonar packet, not the Reach lines kept 60 s either side: back-to-back
  SonarView recordings no longer read as covering the same 2 minutes.
- A job whose sonar files were read as Certus logs first (Read files: 0 IMU samples) takes its span and position
  from the recordings (2.10.2: "Read the rover files first" on Download, time range 2000-01-01).

    python3 PosPro/tests/marine_base_files_test.py      (from the plugins folder)
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from dcs_core.marine import SonarRecording  # noqa: E402
from dcs_core.project import DcsProject  # noqa: E402
from PosPro.core import marine, pipeline  # noqa: E402
from PosPro.core.project import Project  # noqa: E402
from PosPro.tests.marine_multiday_test import make_recording  # noqa: E402
from RemixPro.tests import test_sonar as TS  # noqa: E402

tmp = tempfile.mkdtemp()
try:
    # two recordings back to back (the second starts 2 s after the first ends), each with its Reach log
    t0 = TS.T0
    a = make_recording(os.path.join(tmp, "r1"), os.path.join(tmp, "out"), t0, name="first")
    b = make_recording(os.path.join(tmp, "r2"), os.path.join(tmp, "out2"), t0 + 22.0, name="second")
    ra, rb = SonarRecording([a]), SonarRecording([b])
    fa = ra.files[0].span()
    sa, sb = ra.span(), rb.span()
    assert fa[0] < sa[0] - 30 and fa[1] > sa[1] + 30, (fa, sa)          # the files reach the Reach lines' margin
    assert 19.0 < sa[1] - sa[0] < 21.0, sa                              # 20 s of sonar
    assert marine.overlaps({"a": sa, "b": sb}) == [], (sa, sb)
    assert marine.overlaps({"a": fa, "b": rb.files[0].span()}), "the old spans overlapped"
    print("span: %.1f s of sonar (file span %.1f s); back-to-back recordings do not overlap"
          % (sa[1] - sa[0], fa[1] - fa[0]))

    # the base files: observations, navigation, SBAS -> only the observations are merged
    obs = os.path.join(tmp, "base_Reach_raw.26O")
    TS.make_rinex(obs, t0 - 60, t0 + 60)
    nav = os.path.join(tmp, "base_Reach_raw.26P")
    with open(nav, "w") as f:
        f.write("     3.03           N: GNSS NAV DATA    M: MIXED            RINEX VERSION / TYPE\n"
                "                                                            END OF HEADER\n")
    sbas = os.path.join(tmp, "base_Reach_raw.26B")
    with open(sbas, "w") as f:
        f.write("     2.11           B                                       RINEX VERSION / TYPE\n"
                "                                                            END OF HEADER\n")
    p = Project(os.path.join(tmp, "job", "x.pospro.json"))
    os.makedirs(p.work_dir, exist_ok=True)
    logs = []
    ctx = pipeline.Ctx(log=lambda lvl, m: logs.append(m))
    out = pipeline.base_obs_file(p, {"id": "b1", "name": "Recorded", "files": [sbas, nav, obs]}, ctx)
    assert pipeline._rinex_kind(out) == "O" and any("left out" in m for m in logs), logs
    try:
        pipeline.base_obs_file(p, {"id": "b2", "name": "Nav only", "files": [nav, sbas]}, ctx)
        raise AssertionError("no observation file was accepted")
    except pipeline.StepError as e:
        assert "no RINEX observation file" in str(e), e
    print("base: observation file kept, navigation and SBAS left out")

    # sonar files read as Certus logs first, then moved into the job: span and position from the recordings
    job = os.path.join(tmp, "Sissboo")
    os.makedirs(job)
    sp = Project.from_dcs(DcsProject.create(job, "20261118"))
    sp.cfg["rover"]["files"] = [a, b]
    sp.cfg["rover"]["extract"] = {"imu_t0": "0", "imu_t1": "0", "approx_xyz": ""}       # what Read files left
    sp.save()
    assert marine.adopt_sonar_files(sp) == 2 and marine.is_marine(sp)
    marine.refresh_extent(sp)
    span = sp.rover_span()
    assert span and abs(span[0] - (sa[0] - 315964800.0 + 18.0)) < 0.01, span
    assert sp.approx_xyz() is not None
    print("job: rover span %.1f s from the recordings, position known" % (span[1] - span[0]))
    print("OK")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
