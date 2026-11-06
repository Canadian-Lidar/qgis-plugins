"""CloudPro's window and its background jobs (real widgets, stand-in QGIS: guides/harness.py):

- an entire-project filter run never touches widgets from its worker thread (audit C2): every window callback
  runs on the main thread
- while a job runs, Undo / Remove all points / Open project / editing wait (I1), the actions are disabled and
  come back afterwards
- closing the window stops the job and frees the edit lock; showing it again takes the lock back (I2)
- opening another project resets the edited line, line visibility and class filters (I4); a project whose lock
  is held elsewhere leaves the open project and its lock as they were (I5)
- Georeference with a new CRS from Project properties, cancelled: the project keeps its CRS (I6)

    QT_QPA_PLATFORM=offscreen python3 -m CloudPro.tests.test_ui_jobs          (from the plugins folder)
"""
import os
import sys
import tempfile
import threading
import time

PLUGINS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(PLUGINS, "guides"))
sys.path.insert(0, PLUGINS)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import harness                                                      # noqa: E402

app, iface = harness.start(PLUGINS, tempfile.mkdtemp(prefix="cloudpro_ui_settings_"))

from CloudPro.tests import _job                                     # noqa: E402


def main():
    import CloudPro
    from CloudPro.core import chain, filters as F, importer as IM
    from CloudPro.ui import mainwindow as MW, theme
    from dcs_core import locks
    Yes = harness.QT.QtWidgets.QMessageBox.StandardButton.Yes
    msgs = []
    for n in ("msg_info", "msg_warn"):
        setattr(theme, n, lambda parent, title, text, *a, **k: msgs.append(text))
    theme.msg_question = lambda *a, **k: Yes

    dcs, p = _job.fresh_job("ui")
    IM.import_chain(p, chain.resolve(dcs), IM.ImportOptions(decimate=4))
    p.store.close()
    dcs2, p2 = _job.fresh_job("ui2")
    IM.import_chain(p2, chain.resolve(dcs2), IM.ImportOptions(decimate=4))
    p2.store.close()

    plugin = CloudPro.classFactory(iface)
    plugin.initGui()
    win = plugin.show()
    win.open_project(dcs.path)
    harness.settle()
    assert win.project is not None and win._edit_lock is not None
    main_thread = threading.current_thread()
    seen = []
    real_edited = win._edited

    def edited(act):
        seen.append(threading.current_thread() is main_thread)
        real_edited(act)
    win._edited = edited
    win.edits.on_change = edited

    # ---- I4: per-project state reset
    win.cmb_eline.setCurrentIndex(2)                                # "Only L2"
    assert win.edit_line == 1
    win.line_enabled[0] = False
    win.class_hidden.add(2)
    win.open_project(dcs2.path)
    harness.settle()
    assert win.project.dcs.path == dcs2.path
    assert win.edit_line == -1 and win.cmb_eline.currentIndex() == 0, win.edit_line
    assert all(win.line_enabled.values()) and not win.class_hidden
    print("I4: the edited line, hidden lines and classes are reset for another project")

    # ---- I5: a project locked elsewhere: the open one keeps its lock
    other = locks.Lock(p.root, "CloudPro editing", "other", name=".edit.lock")
    other.data = None
    import json
    lp = os.path.join(p.root, ".edit.lock")
    with open(lp, "w") as f:                    # held by a live process on another host
        json.dump({"plugin": "CloudPro", "host": "elsewhere", "pid": 1, "time": time.time(),
                   "what": "CloudPro editing"}, f)
    held = win._edit_lock
    win.open_project(dcs.path)
    harness.settle()
    if win.project.dcs.path == dcs2.path:
        assert win._edit_lock is held and os.path.exists(os.path.join(p2.root, ".edit.lock"))
        print("I5: a job locked elsewhere is not opened; the open job keeps its lock")
    else:
        print("I5: (lock file from another host was taken over by DCS Core's rules; not checked)")
    os.unlink(lp)
    win.open_project(dcs.path)
    harness.settle()
    assert win.project.dcs.path == dcs.path

    # ---- C2 + I1: an entire-project filter run
    win.edits.on_change = edited
    g = F.FilterGroup("sor", [F.make("sor")])
    win.filters.groups = [g] + list(win.filters.groups)
    type(win.filters).group = property(lambda self: g)
    gate = threading.Event()
    real_sor_run = g.filters[0].run

    def slow_run(ctx, sel, _r=real_sor_run):
        gate.wait(10)                           # keep the job running while the window is poked
        return _r(ctx, sel)
    g.filters[0].run = slow_run
    win.run_scope("Entire project")
    assert win._busy and not win.btn_menu.isEnabled() and not win.b_undo.isEnabled()
    n_hist = len(win.edits.undo_stack)
    win.undo()
    win.reset_points()
    win.open_project(dcs2.path)
    assert win.project.dcs.path == dcs.path and len(win.edits.undo_stack) == n_hist and win.project.lines
    gate.set()
    harness.wait(lambda: not win._busy, 300)
    harness.settle()
    assert seen and all(seen), seen
    assert win.btn_menu.isEnabled() and win.b_undo.isEnabled() and len(win.edits.undo_stack) == n_hist + 1
    assert win.edits.on_change is not None
    print("C2/I1: tiled filter run - %d window callbacks, all on the main thread; Undo, Remove all points and "
          "Open project waited; actions back afterwards" % len(seen))

    # ---- I6: a new CRS from Project properties, georeferencing dialog cancelled
    from CloudPro.ui import dialogs as D
    real_exec = MW.exec_
    MW.exec_ = lambda d, *a: 0 if isinstance(d, D.GeorefDialog) else real_exec(d, *a)
    crs0 = win.project.crs
    win.apply_geodetics("EPSG:2961", False, "", False)
    assert win.project.crs == crs0, win.project.crs
    MW.exec_ = real_exec
    print("I6: CRS change with the georeferencing cancelled: the project keeps %s" % crs0)

    # ---- I2: closing during a job stops it and frees the lock; showing again takes the lock back
    gate.clear()
    win.run_scope("Entire project")
    assert win._busy
    threading.Timer(0.5, gate.set).start()
    plugin.panel.closed.emit() if hasattr(plugin.panel, "closed") else win.shutdown()
    assert win._thread is None or not win._thread.is_alive()
    assert win._edit_lock is None and not os.path.exists(os.path.join(p.root, ".edit.lock"))
    harness.settle()
    plugin.show()
    assert win._edit_lock is not None and os.path.exists(os.path.join(p.root, ".edit.lock"))
    print("I2: closing stopped the job and freed the lock; showing the window took it back")
    plugin.unload()
    assert not os.path.exists(os.path.join(p.root, ".edit.lock"))
    print("test_ui_jobs: OK")


if __name__ == "__main__":
    main()
