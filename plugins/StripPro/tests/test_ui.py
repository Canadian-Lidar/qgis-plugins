"""StripPro's window on the simulated patch-test job: the inputs are listed, Align strips runs the job on a worker
thread and shows the before / after, Finalize publishes the corrected trajectory and the chain shows StripPro done.

    QT_QPA_PLATFORM=offscreen python3 StripPro/tests/test_ui.py [figure folder]      (from the plugins folder)
"""
import os
import shutil
import sys
import tempfile

PLUGINS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(PLUGINS, "guides"))
sys.path.insert(0, PLUGINS)
import harness  # noqa: E402

RAW = "/jobs/cloudpro/Job_Oct20"
FIG = sys.argv[1] if len(sys.argv) > 1 else None
if not os.path.isdir(RAW):
    print("SKIP: %s not here" % RAW)
    sys.exit(0)
app, iface = harness.start(PLUGINS, tempfile.mkdtemp())
tmp = tempfile.mkdtemp(prefix="strippro_ui_")
root = os.path.join(tmp, "Job_Oct20")
shutil.copytree(RAW, root, ignore=shutil.ignore_patterns("separation", "*.bak", "store"))
import StripPro  # noqa: E402
from dcs_core import registry  # noqa: E402

plugin = StripPro.classFactory(iface)
plugin.initGui()
assert registry.has("strippro")
win = plugin.show()
win.resize(1250, 720)
win.open_project(os.path.join(root, "Processing", "Job_Oct20", "Job_Oct20.dcsproj"))
harness.settle(4)
assert win.inputs is not None and "Line 1" in win.kv.rows["Lines"].text(), win.kv.rows["Lines"].text()
assert win.b_align.isEnabled() and not win.b_final.isEnabled()
win.align()
assert win.worker is not None
harness.wait(lambda: win.worker is None, 1500)
harness.settle(4)
assert win.summary is not None, win.status.text()
assert "Between lines" in win.lbl_result.text() and win.table.rowCount() == 6
assert win.b_final.isEnabled()
print("aligned: %s" % win.lbl_result.text().replace("<b>", "").replace("</b>", ""))
if FIG:
    harness.grab(win, os.path.join(FIG, "strippro_window.png"))
win.finalize()
harness.settle(2)
assert win.dcs.state("strippro")[0] == "done" and "Finalized" in win.status.text(), win.status.text()
print("finalized: %s" % win.status.text())
plugin.unload()
assert not registry.has("strippro")
shutil.rmtree(tmp, ignore_errors=True)
print("OK")
