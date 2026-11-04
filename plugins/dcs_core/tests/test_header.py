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
    iface, header, window = parts
    w = window.PluginWindow(iface, "alignpro", "AlignPro", size=(1400, 900))
    opened = []
    h = header.PluginHeader("alignpro", "AlignPro", window=w, guide=lambda: opened.append(1),
                            window_extras=[("Open the Data View", lambda: None)])
    extra = QPushButton("MENU")
    h.add_extra(extra)
    w.setWidget(h)
    w.show_window()
    w.resize(1400, 300)
    settle()
    assert h.name_label.text() == "AlignPro"                      # the name as written
    assert h.group_label.text() == h.group_label.text().upper()
    xs = [x.geometry().x() for x in (h.chain, extra, h.project_btn, h.window_btn, h.help_btn)]
    assert xs == sorted(xs), xs                                   # boxes, extras, project, window, guide
    assert h.chain.geometry().x() < h.width() / 2                 # workflow boxes beside the name
    assert h.chain.width() == header.CHAIN_WIDTH
    h.help_btn.click()
    assert opened == [1]
    h.window_btn.menu().aboutToShow.emit()
    labels = [a.text() for a in h.window_btn.menu().actions() if a.text()]
    assert "Reset window" in labels and labels[-1] == "Open the Data View"
    h.set_project_name("PatchTest_Oct20")
    assert h.project_btn.text().startswith("PATCHTEST_OCT20")
    h.set_project_name(None)
    assert h.project_btn.text().startswith("NO PROJECT")
    w.release_window()


def test_narrow_window_moves_boxes_to_a_second_row(parts):
    iface, header, window = parts
    w = window.PluginWindow(iface, "pospro", "PosPro", size=(1400, 900), min_size=(300, 200))
    h = header.PluginHeader("pospro", "PosPro", window=w)
    w.setWidget(h)
    w.show_window()
    w.resize(760, 300)
    settle()
    assert h._wrapped and h.chain.geometry().y() > h.project_btn.geometry().bottom()
    w.resize(1400, 300)
    settle()
    assert not h._wrapped and abs(h.chain.geometry().center().y() - h.project_btn.geometry().center().y()) < 6
    w.release_window()


def test_without_a_window_the_window_button_is_hidden(parts):
    _iface, header, _window = parts
    h = header.PluginHeader("cloudpro", "CloudPro")
    h.add_extra(QLabel("inputs"))
    h.resize(1400, 80)
    h.show()
    settle()
    assert not h.window_btn.isVisible() and h.help_btn.isVisible()
    h.close()
