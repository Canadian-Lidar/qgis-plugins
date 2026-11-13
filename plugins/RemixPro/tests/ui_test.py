"""The RemixPro window with real Qt (PyQt5) and the stand-in QGIS of guides/harness.py.

    python tests/ui_test.py [SHOTS_DIR]

Opens the window floating, checks the header (no stage boxes), opens the simulated mapper 2.10 project, makes
every output, checks progress, states and the log, cancels a second run, opens the same recordings through a DCS
project, unloads and reloads. With SHOTS_DIR, saves screenshots there."""
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGINS = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(PLUGINS, "guides"))

import harness      # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("  %s  %s%s" % ("PASS" if ok else "FAIL", name, ("  [%s]" % (detail,)) if detail != "" else ""))
    if not ok:
        failures.append(name)


def main():
    shots = sys.argv[1] if len(sys.argv) > 1 else None
    tmp = tempfile.mkdtemp()
    root = os.path.join(tmp, "plugins")             # folders named as QGIS installs them
    os.makedirs(root)
    os.symlink(os.path.join(PLUGINS, "RemixPro"), os.path.join(root, "remixpro"))
    os.symlink(os.path.join(PLUGINS, "dcs_core"), os.path.join(root, "dcs_core"))
    app, iface = harness.start(root, os.path.join(tmp, "settings"))
    sys.path.insert(0, PLUGINS)
    import sim210
    sim = sim210.make_project(os.path.join(tmp, "jobs"), seconds=(30.0, 12.0, 6.0), split_bytes=6 << 20)
    plugin = harness.load("remixpro", iface)
    from dcs_core import menu
    sub = menu.find(menu.find(iface.pluginMenu(), menu.TOP), menu.GROUP) if False else None  # noqa: F841
    plugin.toggle(True)
    harness.settle()
    w = plugin.win
    check("window opens floating", w.isVisible() and w.window_mode() == "float", w.window_mode())
    check("no stage boxes in the header", not w.header.chain.isVisible())
    check("header names RemixPro", w.header.name_label.text() == "RemixPro")
    check("empty list explains what to open", w.empty.isVisible() and not w.tree.isVisible())
    check("Make is off with nothing open", not w.go.isEnabled())
    w.resize(1240, 780)
    if shots:
        harness.grab(w, os.path.join(shots, "empty.png"))
    w.open_paths([sim["project"]])
    harness.settle()
    n = w.tree.topLevelItemCount()
    check("four recordings found", n == 4, n)
    kinds = [w.tree.topLevelItem(i).text(1) for i in range(n)]
    check("kinds in recording order", kinds == ["Survey line", "Navigation", "Survey line", "Survey line"], kinds)
    check("output folder in the mapper project's Processing folder",
          w.out.text() == os.path.join(sim["project"], "Processing", "RemixPro"), w.out.text())
    nav_row = w.tree.topLevelItem(1)
    check("navigation: BeamworX not applicable", nav_row.text(5) == "n/a", nav_row.text(5))
    w.tree.setCurrentItem(w.tree.topLevelItem(2))
    harness.settle()
    check("details show the lever arm", "+0.082, +0.495, +0.112" in w.kv.rows["Primary antenna"].text())
    check("Make is on", w.go.isEnabled() and "4 RECORDINGS" in w.go.text(), w.go.text())
    if shots:
        harness.grab(w, os.path.join(shots, "found.png"))
    seen = []
    w.start()
    t0 = time.time()
    while w.busy or w.timer.isActive():
        harness.settle(2)
        seen.append(w.bar.value())
        time.sleep(0.02)
        if time.time() - t0 > 300:
            break
    harness.settle()
    log = w.log.toPlainText()
    check("all made", log.count(": made,") == 3 * 3 + 2, log.count(": made,"))
    check("status says done", w.status.text().startswith("Done: 4"), w.status.text())
    states = [[w.tree.topLevelItem(i).text(c) for c in (5, 6, 7)] for i in range(n)]
    check("every output shown as made", all(s in ("✓ made", "n/a") for row in states for s in row), states)
    check("progress went up to the end", w.bar.value() == 1000 and max(seen or [0]) > 0, (w.bar.value(), len(seen)))
    if shots:
        harness.grab(w, os.path.join(shots, "made.png"))
    # a second run with Make again: cancel at once
    w.force.setChecked(True)
    w.start()
    w.request_cancel()
    while w.busy or w.timer.isActive():
        harness.settle(2)
        time.sleep(0.02)
    harness.settle()
    check("cancel stops", w.status.text() == "Cancelled.", w.status.text())
    out = w.out.text()
    left = [f for r, _d, fs in os.walk(out) for f in fs if ".making" in r]
    check("cancel leaves no work folders", not left, left[:3])
    check("earlier results kept after the cancel", all(
        w.tree.topLevelItem(i).text(6) == "✓ made" for i in range(n)))
    w.force.setChecked(False)
    # through a DCS project
    from dcs_core.project import DcsProject
    p = DcsProject.create(sim["project"], "Deck_Test")
    w.open_dcs_project(p.path)
    harness.settle()
    check("DCS project: outputs in Processing/<Name>/RemixPro",
          w.out.text() == os.path.join(p.folder, "RemixPro"), w.out.text())
    check("DCS project: the job's four recordings", w.tree.topLevelItemCount() == 4, w.tree.topLevelItemCount())
    check("DCS project name in the header", "DECK_TEST" in w.header.project_btn.text(), w.header.project_btn.text())
    # 1.1.0: always a normal window, never docked (asking for a dock keeps it a window)
    w.dock_at("right")
    harness.settle()
    check("never docks", w.window_mode() == "float", w.window_mode())
    w.float_window()
    harness.settle()
    plugin.unload()
    harness.settle()
    plugin = harness.load("remixpro", iface)
    plugin.toggle(True)
    harness.settle()
    check("reloads and reopens", plugin.win is not None and plugin.win.isVisible())
    plugin.unload()
    shutil.rmtree(tmp, ignore_errors=True)
    print("ui_test: %s" % ("OK" if not failures else "%d FAILED: %s" % (len(failures), ", ".join(failures))))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
