"""Headless tests of dcs_core.header.PluginHeader (real Qt; see test_window.py for the stand-in)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_window import APP, Iface, settle  # noqa: E402,F401 - sets up qgis.PyQt outside QGIS

from qgis.PyQt.QtWidgets import QLabel, QPushButton  # noqa: E402


@pytest.fixture()
def parts():
    from dcs_core import header, window
    iface = Iface()
    yield iface, header, window
    iface.mw.close()
    settle()


def test_layout_and_names(parts):
    """1.11.0: logo and name, breadcrumbs (chain over steps), extras, project box, tools, then "?" at the right; no
    Window button."""
    iface, header, window = parts
    w = window.PluginWindow(iface, "alignpro", "AlignPro", size=(1400, 900))
    opened = []
    h = header.PluginHeader("alignpro", "AlignPro", window=w, guide=lambda: opened.append(1))
    extra = QLabel("note")
    h.add_extra(extra)
    tool = QPushButton("T")
    h.add_tool(tool)
    w.setWidget(h)
    w.show_window()
    w.resize(1400, 300)
    settle()
    assert h.name_label.text() == "AlignPro"                      # the name as written
    assert h.group_label.text() == h.group_label.text().upper()
    xs = [x.geometry().x() for x in (h.chain, extra, h.project_btn, tool, h.help_btn)]
    assert xs == sorted(xs), xs                                   # crumbs, extras, project, tools, guide
    assert h.chain.geometry().x() < h.width() / 3
    assert h.help_btn.geometry().right() > h.width() - 60         # "?" at the right edge
    assert not h.window_btn.isVisible()
    h.help_btn.click()
    assert opened == [1]
    h.set_project_name("PatchTest_Oct20")
    assert h.project_btn.text().startswith("PATCHTEST_OCT20")
    h.set_project_name(None)
    assert h.project_btn.text().startswith("NO PROJECT")
    w.release_window()


def test_step_breadcrumbs(parts):
    """The plugin's steps under the chain: coloured by state, the current one marked, reasons as tooltips, a click
    emits stepClicked."""
    _iface, header, _window = parts
    h = header.PluginHeader("cloudpro", "CloudPro")
    assert not h.steps.isVisibleTo(h)
    got = []
    h.stepClicked.connect(got.append)
    h.set_steps([("georef", "Georeference", "done", "2 lines"), ("clean", "Clean & classify", "todo", "optional"),
                 ("publish", "Finalize", "warn", "out of date")], "clean")
    h.resize(1400, 80)
    h.show()
    settle()
    assert h.steps.isVisible() and h.steps.current == "clean"
    assert h.steps.geometry().top() > h.chain.geometry().top()     # the second row
    assert "2 lines" in h.steps.tip_for(h.steps.items[0]) and "out of date" in h.steps.tip_for(h.steps.items[2])
    h.steps._layout()
    r = h.steps._boxes[2]
    from qgis.PyQt.QtCore import QPointF
    assert h.steps._hit(QPointF(r.center()))[0] == "publish"
    h.steps.clicked.emit("publish")
    assert got == ["publish"]
    h.set_steps([])
    assert not h.steps.isVisibleTo(h)
    h.close()


def test_without_a_window_the_window_button_is_hidden(parts):
    _iface, header, _window = parts
    h = header.PluginHeader("cloudpro", "CloudPro")
    h.add_extra(QLabel("inputs"))
    h.resize(1400, 80)
    h.show()
    settle()
    assert not h.window_btn.isVisible() and h.help_btn.isVisible()
    h.close()
