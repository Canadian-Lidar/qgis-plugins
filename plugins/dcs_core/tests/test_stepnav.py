"""Back / Next under the step pages (stepnav) and the version in the header."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import qtshim  # noqa: E402

qtshim.install()

from qgis.PyQt.QtWidgets import QApplication  # noqa: E402

APP = QApplication.instance() or QApplication([])

from dcs_core import header, registry, stepnav  # noqa: E402

TITLES = ["Data", "Base", "Processing", "Review", "Export"]


def test_advice_wording_and_whether_next_is_allowed():
    a = stepnav.advice
    assert a("Data", "done", "15 min")[1:] == ("ok", True)
    assert a("Base", "warn", "approximate position")[2] is True
    assert a("Processing", "todo", "press PROCESS") == ("○ Processing is not done yet: press PROCESS.", "", False)
    assert a("Pairs", "error", "too few")[2] is False
    assert a("Review", "todo", "find anomalies", optional=True)[2] is True
    assert "You can skip it" not in a("Review", "todo", "or skip", optional=True)[0]
    assert a("Processing", "running", "")[2] is False


def test_bar_follows_the_steps_and_continues_in_the_next_plugin(tmp_path):
    went = []
    nav = stepnav.StepNav(TITLES, went.append, optional={"Review"}, stage="pospro")
    nav.update(0, "todo", "not read", todo="press READ FILES")
    assert not nav.back.isVisible() and not nav.next.isEnabled()
    assert "press READ FILES" in nav.msg.text()
    nav.update(1, "warn", "approximate position")
    assert nav.next.isEnabled() and nav.next.text().startswith("NEXT: PROCESSING")
    nav.back.click()
    nav.next.click()
    assert went == [0, 2]
    nav.update(3, "todo", "find anomalies")
    assert nav.next.text().startswith("SKIP TO EXPORT") and nav.next.isEnabled()
    # last step: continue in AlignPro once published and AlignPro is loaded
    nav.update(4, "todo", "optional")
    nav.set_continue("alignpro", False, "Process first.")
    assert nav.next.text().startswith("CONTINUE IN ALIGNPRO") and not nav.next.isEnabled()
    opened = []
    registry.register("alignpro", opened.append)
    try:
        nav.set_continue("alignpro", True, "", str(tmp_path / "job.dcsproj"))
        assert nav.next.isEnabled()
        assert nav.msg.text().count("continue in AlignPro") == 1
        nav.next.click()
        assert opened == [str(tmp_path / "job.dcsproj")]
    finally:
        registry.unregister("alignpro")


def test_header_shows_the_plugin_version(tmp_path):
    plug = tmp_path / "MyPlugin"
    (plug / "ui").mkdir(parents=True)
    (plug / "metadata.txt").write_text("[general]\nname=MyPlugin\nversion=2.3.4\n", encoding="utf-8")
    assert header.plugin_version(str(plug / "ui" / "dock.py")) == "2.3.4"
    assert header.plugin_version(str(tmp_path / "nowhere.py")) == ""
    h = header.PluginHeader("pospro", "PosPro", version="1.6.0")
    assert h.version_label.text() == "v1.6.0"
    # without version=, the caller's own metadata.txt (here: none) decides
    assert header.PluginHeader("pospro", "PosPro").version_label.text() in ("", "v" + header.plugin_version(__file__))


def test_header_crumbs_group_the_pages():
    groups = [("prep", "Prepare", [0, 1]), ("proc", "Process", [2, 3]), ("fin", "Finalize", [4])]
    titles = ["Data", "Base", "Processing", "Review", "Export"]
    items, cur = stepnav.crumbs(groups, titles, [("done", "3 files"), ("warn", "approximate"), ("done", ""),
                                                 ("todo", ""), ("todo", "")], 2, optional={"Review"})
    assert cur == "proc"
    assert [i[2] for i in items] == ["warn", "done", "todo"]       # a warning shows; optional Review may stay todo
    assert "Base: approximate" in items[0][3] and "Review (optional)" in items[1][3]
    items, _ = stepnav.crumbs(groups, titles, [("done", ""), ("done", ""), ("running", ""), ("todo", ""),
                                               ("error", "bad")], 4)
    assert [i[2] for i in items] == ["done", "running", "error"]
