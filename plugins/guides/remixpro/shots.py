"""Recreates every figure of the RemixPro user guide from the real window (PyQt5 + guides/harness.py).

    python guides/remixpro/shots.py

Needs PyQt5, numpy and zstandard. The recordings are a simulated mapper 2.10 project made on the spot with
the mapper's own .clraw writer (plugins/RemixPro/tests/sim210.py), so no test job is needed."""
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GUIDES = os.path.dirname(HERE)
PLUGINS = os.path.dirname(GUIDES)
FIG = os.path.join(HERE, "fig")
sys.path.insert(0, GUIDES)
sys.path.insert(0, os.path.join(PLUGINS, "RemixPro", "tests"))
import harness  # noqa: E402


def main():
    tmp = tempfile.mkdtemp()
    root = os.path.join(tmp, "plugins")
    os.makedirs(root)
    os.symlink(os.path.join(PLUGINS, "RemixPro"), os.path.join(root, "remixpro"))
    os.symlink(os.path.join(PLUGINS, "dcs_core"), os.path.join(root, "dcs_core"))
    app, iface = harness.start(root, os.path.join(tmp, "settings"))
    sys.path.insert(0, PLUGINS)
    import sim210
    jobs = os.path.join(tmp, "D", "Jobs")
    sim = sim210.make_project(jobs, seconds=(30.0, 12.0, 6.0), split_bytes=6 << 20)
    # a line still being copied, as on a USB drive during the capture
    plugin = harness.load("remixpro", iface)
    plugin.toggle(True)
    w = plugin.win
    w.resize(1240, 780)
    harness.settle()
    harness.grab(w, os.path.join(FIG, "empty.png"))
    harness.grab(w.header, os.path.join(FIG, "header.png"))
    harness.grab_menu(w.header.project_btn.menu(), os.path.join(FIG, "project_menu.png"))
    w.open_paths([sim["project"]])
    w.tree.setCurrentItem(w.tree.topLevelItem(2))
    harness.settle()
    harness.grab(w, os.path.join(FIG, "found.png"))
    w.start()
    while w.busy or w.timer.isActive():
        harness.settle(2)
        time.sleep(0.02)
    harness.settle()
    harness.grab(w, os.path.join(FIG, "made.png"))
    w.tree.setCurrentItem(w.tree.topLevelItem(1))
    harness.settle()
    harness.grab(w.kv.parentWidget(), os.path.join(FIG, "details_nav.png"))
    harness.grab_menu(w.b_all.menu(), os.path.join(FIG, "select_menu.png"))
    harness.grab(w.right, os.path.join(FIG, "make_panel.png"))
    plugin.unload()
    shutil.rmtree(tmp, ignore_errors=True)
    print("figures in", FIG)


if __name__ == "__main__":
    main()
