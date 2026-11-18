"""Opening PosPro and moving between plugins (PosPro 2.10.1, DCS Core 1.19.0; Sean, 18 Nov 2026).

- A mobile mapping job opened after a marine job (or the reverse) never leaves the step index past the steps:
  2.10.0 raised IndexError in update_nav (the marine Data page stayed on show with the previous job's recordings).
- Opening PosPro from the menu starts with no project, also after a job was open in a window that was closed.
- Moving on to another plugin (registry.leave, used by Continue and the chain breadcrumbs) closes PosPro.

    QT_QPA_PLATFORM=offscreen python3 PosPro/tests/open_handover_ui_test.py
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
from PosPro.core import marine  # noqa: E402
from PosPro.core.project import Project  # noqa: E402
from PosPro.tests.marine_multiday_test import make_recording  # noqa: E402
from RemixPro.tests import test_sonar as TS  # noqa: E402

tmp = tempfile.mkdtemp()
try:
    plugin = harness.load("PosPro", iface)
    plugin.from_menu(True)
    dock = plugin.dock
    harness.settle()
    assert dock.project is None, "PosPro opened a project by itself"

    # a marine job (sonar recordings made by RemixPro), Data page on show
    job = os.path.join(tmp, "Greenstone")
    make_recording(os.path.join(tmp, "rec"), os.path.join(job, "Sonar"), TS.T0)
    mp = Project.from_dcs(DcsProject.create(job, "Greenstone"))
    assert marine.is_marine(mp)
    dock.set_project(mp)
    harness.settle()
    assert dock._marine and dock.stack.currentWidget() in dock._mscroll
    dock.goto_page("install")                   # a marine page of its own (not one shared with mobile mapping)
    harness.settle()
    assert dock.stack.currentWidget() in dock._mscroll

    # then a mobile mapping job: no IndexError, the mobile mapping page of the same step is shown
    lp = Project(os.path.join(tmp, "Sissboo", "Sissboo.pospro.json"))
    lp.cfg["name"] = "Sissboo"
    os.makedirs(lp.work_dir, exist_ok=True)
    lp.save()
    dock.set_project(lp)
    dock._refresh_chain()
    dock.update_nav()
    harness.settle()
    k = dock.current_index()
    assert not dock._marine and 0 <= k < len(dock.keys), k
    assert dock.stack.currentWidget() not in dock._mscroll, "the marine page stayed on show"
    assert dock.stack.currentIndex() == k
    print("marine -> mobile mapping: step %d (%s), no marine page left" % (k, dock.keys[k]))

    # and back: the marine Data page of the new job (its recordings, not another job's)
    dock.set_project(mp)
    dock.update_nav()
    assert dock._marine and dock.current_index() == 0
    print("mobile mapping -> marine: Data page, %d recording(s)" % len(dock.marine_recordings_all()))

    # closed, then opened again from the menu: no project
    dock.close()
    harness.settle()
    plugin.from_menu(True)
    harness.settle()
    assert plugin.dock is not dock and plugin.dock.project is None, "the menu reopened the previous job"
    assert plugin.dock.isVisible()
    print("menu after closing: new window, no project")

    # opened on a job by another plugin (registry.open), then moving on closes PosPro
    registry.open("pospro", mp.dcs.path)
    harness.settle()
    assert plugin.dock.project is not None and plugin.dock.isVisible()
    assert registry.leave(plugin.dock.nav)
    harness.settle()
    assert not plugin.dock.isVisible(), "PosPro stayed open after moving on"
    print("registry.open shows the job; registry.leave closes PosPro")
    plugin.unload()
    print("OK")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
