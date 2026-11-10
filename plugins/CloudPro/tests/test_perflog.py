"""Performance log and report (CloudPro 1.9.8): loads, jobs, 3D drawing and frame times go to
03_CloudPro/log/performance.log with the computer at the top of each session; MENU > Performance report writes one
text file with the computer, the job, the log, recent History and run logs.

    QT_QPA_PLATFORM=offscreen python3 CloudPro/tests/test_perflog.py      (from the plugins folder)
"""
import os
import sys
import tempfile

PLUGINS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(PLUGINS, "guides"))
sys.path.insert(0, PLUGINS)
import harness                                                    # noqa: E402

app, iface = harness.start(PLUGINS, tempfile.mkdtemp())
from CloudPro.tests import _job                                    # noqa: E402
from CloudPro.core import chain, importer as IM, perflog          # noqa: E402
from CloudPro.qt import QtWidgets                                  # noqa: E402

dcs, p = _job.fresh_job("perf")
_job.publish_sbet(dcs, _job.realtime_track(dcs.survey_clraw_files()))
p.data["override"] = {"source": "recorded"}
IM.import_chain(p, chain.resolve(dcs, override=p.data["override"]), IM.ImportOptions(min_speed=0.3))
p.run_log("georeference", ["test run log line"])
p.save()
p.store.close()

import CloudPro                                                    # noqa: E402
plugin = CloudPro.classFactory(iface)
plugin.initGui()
win = plugin.show()
win.open_project(dcs.path)
harness.settle(3)
win.v3d.show()
win.v3d.repaint()
harness.settle(1)
win.load_box()                                                     # logs the previous area's 3D frames too
harness.settle(1)
win._run("test", lambda progress, cancel: 42, "Test job")
for _ in range(20):
    harness.settle(1)
    if not win._busy:
        break
log = open(perflog.path(win.project), encoding="utf-8").read()
for want in ("session", "Processor:", "Memory:", "Engine:", " load ", "Loaded ", " 3d ", "drawn by the",
             "3D frames,", " job ", "Test job (test):"):
    assert want in log, (want, log)
assert log.count("===== ") == 1, "one session header per job and QGIS session"
print("performance.log: session header, loads, 3D drawing and frames, jobs")

out = os.path.join(tempfile.mkdtemp(), "report.txt")
QtWidgets.QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (out, ""))
import CloudPro.ui.theme as theme                                  # noqa: E402
theme.msg_info = lambda *a, **k: None
win.performance_report()
rep = open(out, encoding="utf-8").read()
for want in ("== Computer", "== Job", "Points: ", "== Performance log", "Loaded ", "== History", "Imported",
             "== Recent run logs", "test run log line", "Inspection Area:"):
    assert want in rep, (want, rep[:2000])
assert "performance.log" not in rep.split("== Recent run logs")[1].split("\n")[1]
print("report: computer, job, log, History, run logs (%d lines)" % rep.count("\n"))
win.shutdown()
plugin.unload()
print("test_perflog: OK")
