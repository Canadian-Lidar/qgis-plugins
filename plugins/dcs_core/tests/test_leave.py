"""Moving on to another plugin closes the one being left (DCS Core 1.19.0, API 20; Sean, 18 Nov 2026):
registry.leave(widget) closes the PluginWindow holding widget; StepNav's Continue and the chain breadcrumbs open the
next plugin and then close their own window; nothing is closed when the next plugin is not installed.

    QT_QPA_PLATFORM=offscreen python3 -m pytest dcs_core/tests/test_leave.py      (from the plugins folder)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_window import APP, env, make  # noqa: E402,F401


def test_leave_closes_the_window(env):
    iface, window = env
    from qgis.PyQt.QtWidgets import QLabel, QVBoxLayout, QWidget
    from dcs_core import registry
    w = make(window, iface)
    inner = QWidget()
    QVBoxLayout(inner).addWidget(QLabel("deep"))
    w.setWidget(inner)
    w.show_window()
    APP.processEvents()
    assert w.isVisible()
    assert registry.leave(inner.layout().itemAt(0).widget())
    APP.processEvents()
    assert not w.isVisible()
    assert not registry.leave(QLabel("no window"))


def test_continue_and_breadcrumbs(env):
    iface, window = env
    from dcs_core import registry, stepnav
    from dcs_core.stepbar import StepBar
    opened = []
    w = make(window, iface)
    nav = stepnav.StepNav(["Data", "Report"], lambda i: None, stage="pospro")
    w.setWidget(nav)
    w.show_window()
    nav.update(1, "done", "")
    nav.set_continue("alignpro", True, "", "/job/A.dcsproj")
    nav._next()                                         # AlignPro not installed: PosPro stays open
    APP.processEvents()
    assert w.isVisible() and opened == []
    registry.register("alignpro", lambda p: opened.append(p))
    nav._next()
    APP.processEvents()
    assert opened == ["/job/A.dcsproj"] and not w.isVisible()
    registry.unregister("alignpro")

    # the chain breadcrumbs: a click on CloudPro opens it on the project and closes this window
    w2 = make(window, iface, "alignpro", "AlignPro")
    bar = StepBar()
    w2.setWidget(bar)
    w2.show_window()

    class P:
        path = "/job/A.dcsproj"
    bar.project = P()
    bar.current = "alignpro"
    registry.register("cloudpro", lambda p: opened.append(("cloudpro", p)))
    bar._open("cloudpro")
    APP.processEvents()
    assert opened[-1] == ("cloudpro", "/job/A.dcsproj") and not w2.isVisible()
    registry.unregister("cloudpro")
