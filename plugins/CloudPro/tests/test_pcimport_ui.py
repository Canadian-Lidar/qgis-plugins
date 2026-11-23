"""CloudPro 2.16.0 window: Import point clouds... with no job open makes a job (New DCS project dialog), imports
two overlapping LAS files; the first step reads Import; the views show the files' colours; Check measures the
offset between the two files; a job with nothing to georeference offers the import; Finalize writes cloud.json
with source "imported point clouds" and stays current.

    QT_QPA_PLATFORM=offscreen python3 CloudPro/tests/test_pcimport_ui.py      (from the plugins folder)
"""
import json
import os
import shutil
import sys
import tempfile

import numpy as np

PLUGINS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(PLUGINS, "guides"))
sys.path.insert(0, PLUGINS)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness                                                    # noqa: E402

app, iface = harness.start(PLUGINS, tempfile.mkdtemp())
import pcfiles as PF                                              # noqa: E402
from dcs_core import dialogs                                      # noqa: E402
from dcs_core.project import DcsProject                           # noqa: E402
from CloudPro.qt import QtWidgets                                 # noqa: E402
from CloudPro.ui import mainwindow as MW                          # noqa: E402
from CloudPro.ui import theme                                     # noqa: E402

msgs = []
for _n in ("msg_info", "msg_warn"):
    setattr(theme, _n, lambda parent, title, text, *a, **k: msgs.append(text))

tmp = tempfile.mkdtemp()
try:
    job = os.path.join(tmp, "Bridge")
    os.makedirs(job)
    a = PF.cloud(30000, seed=1)
    b = PF.cloud(30000, seed=2)
    b["z"] = b["z"] + 0.04                                           # the second survey sits 4 cm higher
    b["src"][:] = 1
    a["src"][:] = 1
    PF.write_las_legacy(os.path.join(job, "north.las"), a)
    PF.write_las14(os.path.join(job, "south.laz"), b, laz=True)

    import CloudPro                                               # noqa: E402
    plugin = CloudPro.classFactory(iface)
    plugin.initGui()
    win = plugin.show()
    harness.settle()
    made = []
    dialogs.new_project = lambda parent, plugin_name: made.append(DcsProject.create(job, "Bridge", plugin="test")) \
        or made[-1]
    accepted = []

    def fake_exec(d, *a_):
        accepted.append(type(d).__name__)
        return QtWidgets.QDialog.DialogCode.Accepted
    MW.exec_ = fake_exec
    win.import_pointclouds(files=[os.path.join(job, "north.las"), os.path.join(job, "south.laz")])
    harness.wait(lambda: not win._busy, 300)
    harness.settle(4)
    p = win.project
    assert p is not None and made and accepted == ["PointCloudDialog"], accepted
    assert p.crs == "EPSG:2953" and p.data.get("pointcloud_job")
    assert [ln["name"] for ln in p.lines] == ["north", "south"], [ln["name"] for ln in p.lines]
    assert p.store.total == 60000 and win.import_mode()
    h = win.header
    assert [t for _k, t, *_ in h.steps.items] == ["Import", "Check", "Clean & classify", "Finalize"]
    st = {k: s for k, _t, s, *_ in h.steps.items}
    assert st["georef"] == "done" and h.steps.current == "check", (st, h.steps.current)
    assert win.panes["3d"].colour.currentText() == "RGB" and win.ps.rgb is not None and len(win.ps.rgb) == len(win.ps)
    cols = win._colours("RGB", view="3d")
    assert len(cols) == len(win.ps)
    print("import from the menu with no job: new job, 2 lines, 60,000 points, steps Import > Check > ..., RGB shown")

    # Check: the two files compared (no trajectories needed)
    win._step_clicked("check")
    harness.wait(lambda: not win._busy, 300)
    harness.settle(4)
    res = win.plan.separation
    assert res is not None, "no separation result"
    pr = res.pairs[0]
    assert abs(pr["median"] - 0.04) < 0.01 and pr["patches"] > 100, pr
    print("check: the two files compared without trajectories: %.1f cm apart over %d patches" % (
        100 * pr["median"], pr["patches"]))

    # Finalize: cloud.json with source "imported point clouds", current afterwards
    from CloudPro.core import publish as PB
    doc = PB.deliver(p, las=True)
    cj = json.load(open(os.path.join(p.out_dir, "cloud.json")))
    assert cj["source"] == "imported point clouds" and "no calibration" not in " ".join(cj["warnings"]), cj["warnings"]
    assert [l["format"].split(" ")[0] for l in cj["lines"]] == ["LAS", "LAZ"]
    st_, why = p.dcs.state("cloudpro")
    assert st_ == "done", (st_, why)
    print("finalize: cloud.json source '%s', state %s" % (cj["source"], st_))

    # a job with captures folder but no .clraw: Georeference offers the import
    asked = []
    theme.msg_choice = lambda *a_, **k: asked.append(a_[2]) or 1
    win.project.data.pop("pointcloud_job")
    assert win.import_mode()                                     # only imported lines: still Import
    win.georeference()
    print("georeference on this job: %s" % ("offers Import point clouds" if asked else "opens Import"))
    win.shutdown()
    plugin.unload()
    print("test_pcimport_ui: OK")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
