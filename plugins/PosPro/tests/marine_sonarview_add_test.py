"""PosPro 2.11.0 (RemixPro 1.4.0, DCS Core 1.20.0 API 21): SonarView recordings (.svlz) chosen with Add recordings...
on the marine Data page are converted by RemixPro (registry service "sonar_to_clraw") into the job's day folder
<job>/<YYYYMMDD>/CLRAW and become the job's recordings. Without RemixPro the reason is given.

    QT_QPA_PLATFORM=offscreen python3 PosPro/tests/marine_sonarview_add_test.py
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGINS = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(PLUGINS, "guides"))
import harness  # noqa: E402

app, iface = harness.start(PLUGINS, tempfile.mkdtemp())
from dcs_core import registry  # noqa: E402
from dcs_core.project import DcsProject  # noqa: E402
from PosPro.core import marine, pipeline  # noqa: E402
from PosPro.core.project import Project  # noqa: E402
from PosPro.tests.marine_multiday_test import make_recording  # noqa: E402
from RemixPro.tests import test_sonar as TS  # noqa: E402

tmp = tempfile.mkdtemp()
try:
    # a marine job with one day's recording; the next day's SonarView recording kept in its own folder
    job = os.path.join(tmp, "Greenstone")
    make_recording(os.path.join(tmp, "rec"), os.path.join(job, "Sonar"), TS.T0)
    dcs = DcsProject.create(job, "Greenstone")
    mp = Project.from_dcs(dcs)
    day2 = os.path.join(tmp, "SonarView", "day2")
    os.makedirs(day2)
    TS.T0 += 86400.0
    sv = os.path.join(day2, "2026-09-22-16-30.svlz")
    TS.make_svlz(sv, seconds=10.0)
    TS.make_llh(os.path.join(day2, "reach.LLH"), TS.T0 - 600, TS.T0 + 600)
    TS.T0 -= 86400.0

    # no RemixPro: the reason
    registry.withdraw("sonar_to_clraw")
    try:
        marine.convert_sonarview(dcs, [sv], pipeline.Ctx())
        raise AssertionError("converted without RemixPro")
    except marine.MarineError as e:
        assert "RemixPro" in str(e), e
        print("no RemixPro: %s" % e)

    remix = harness.load("RemixPro", iface)
    assert registry.service("sonar_to_clraw") is not None
    plugin = harness.load("PosPro", iface)
    plugin.from_menu(True)
    dock = plugin.dock
    dock.set_project(mp)
    harness.settle()
    assert dock._marine and len(dock.marine_recordings_all()) == 1
    from PosPro.ui import marinepages
    page = dock.findChildren(marinepages.MDataPage)[0]
    from qgis.PyQt.QtWidgets import QFileDialog
    QFileDialog.getOpenFileNames = staticmethod(lambda *a, **k: ([sv], ""))
    page._add_recordings()
    import time
    for _ in range(600):
        harness.settle()
        if dock.task is None:
            break
        time.sleep(0.1)
    assert dock.task is None
    recs = dock.marine_recordings_all()
    assert len(recs) == 2, recs
    made = [f for f in dcs.sonar_clraw_files() if os.sep + "CLRAW" + os.sep in f]
    assert made and all(os.path.basename(os.path.dirname(os.path.dirname(f))) == "20260922" for f in made), made
    assert all(f.startswith(dcs.job_folder) for f in made)
    print("SonarView .svlz added: RemixPro made %s; the job has %d recordings" % (
        ", ".join(os.path.relpath(f, job) for f in made), len(recs)))
    # added again: nothing made again (RemixPro's up-to-date check), still two recordings
    res = marine.convert_sonarview(dcs, [sv], pipeline.Ctx())
    assert res["files"] and not res["failed"]
    assert len(DcsProject.open(dcs.path).sonar_clraw_files()) == len(dcs.sonar_clraw_files())
    plugin.unload()
    remix.unload()
    assert registry.service("sonar_to_clraw") is None
    print("OK")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
