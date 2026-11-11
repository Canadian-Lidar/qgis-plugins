"""StripPro QGIS plugin entry: Plugins > Canadian Lidar > Data Systems > StripPro.

The optional stage between AlignPro and CloudPro: it reads PosPro's trajectory, AlignPro's calibration and the
survey lines, corrects the trajectory from the point cloud and finalizes it for CloudPro. Needs DCS Core."""
import os

from qgis.PyQt.QtGui import QIcon

try:
    from qgis.PyQt.QtGui import QAction
except ImportError:          # QGIS 3 / Qt5
    from qgis.PyQt.QtWidgets import QAction

HERE = os.path.dirname(os.path.abspath(__file__))
CORE_API = 14
ICON = os.path.join(HERE, "icons", "strippro.svg")


def _core():
    from . import corecheck
    return corecheck.load(CORE_API)


class StripProPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.win = None
        self.action = None
        self.help_action = None
        self.core = None
        self._why = ""

    def initGui(self):  # noqa: N802
        self.action = QAction(QIcon(ICON), "StripPro", self.iface.mainWindow())
        self.action.setObjectName("mStripProAction")
        self.action.setToolTip("StripPro: the trajectory corrected from the point cloud")
        self.action.setCheckable(True)
        self.action.triggered.connect(self.toggle)
        self.iface.addToolBarIcon(self.action)
        from .guide import MENU_TEXT
        self.help_action = QAction(QIcon(ICON), MENU_TEXT, self.iface.mainWindow())
        self.help_action.triggered.connect(self.open_guide)
        self.iface.pluginHelpMenu().addAction(self.help_action)
        from . import corecheck
        self.core, self._why, self._menu = _core()
        corecheck.add_menu(self.iface, self.action, self._menu)
        if self.core is None:
            return
        from dcs_core import registry
        registry.register("strippro", self.open_project)

    def unload(self):
        if self.help_action is not None:
            try:
                self.iface.pluginHelpMenu().removeAction(self.help_action)
                self.help_action.deleteLater()
            except RuntimeError:
                pass
            self.help_action = None
        if self.action:
            self.iface.removeToolBarIcon(self.action)
            from . import corecheck
            corecheck.remove_menu(self.iface, self.action, getattr(self, "_menu", None))
            if self.core is not None:
                from dcs_core import registry
                registry.unregister("strippro")
        if self.win is not None:
            from dcs_core.window import alive
            if alive(self.win):
                try:
                    self.win.visibilityChanged.disconnect(self.action.setChecked)
                except (TypeError, RuntimeError):
                    pass
                self.win.cleanup()
                self.win.release_window()
            self.win = None

    def toggle(self, on=True):
        if self.core is None:
            from qgis.core import Qgis
            self.action.setChecked(False)
            from . import corecheck
            self.iface.messageBar().pushMessage(
                "StripPro", "StripPro can't start: %s. %s" % (self._why, corecheck.advice(self._why)),
                Qgis.MessageLevel.Warning, 15)
            return
        if self.win is None:
            from .ui.window import StripProWindow
            self.win = StripProWindow(self.iface, self.iface.mainWindow(), guide=self.open_guide)
            self.win.visibilityChanged.connect(self.action.setChecked)
        if bool(on) or not self.win.isVisible():
            self.win.show_window()
        else:
            self.win.hide()

    def open_guide(self, *_):
        from .guide import open_guide
        return open_guide(self.iface, self.iface.mainWindow())

    def open_project(self, path):
        """Show StripPro on a DCS project (from another plugin's chain bar)."""
        self.toggle(True)
        if self.win is not None:
            self.win.open_project(path)

    def show(self):
        self.toggle(True)
        return self.win
