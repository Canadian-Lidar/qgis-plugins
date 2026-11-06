"""AlignPro QGIS plugin entry: Plugins > Canadian Lidar > Data Systems > AlignPro.

AlignPro is stage 02 of the DCS chain and needs the DCS Core plugin (shared project, handover
files, menu and look). Without it a message bar says so instead of failing."""
import os

from qgis.PyQt.QtGui import QIcon

try:
    from qgis.PyQt.QtGui import QAction
except ImportError:          # QGIS 3 / Qt5
    from qgis.PyQt.QtWidgets import QAction

HERE = os.path.dirname(os.path.abspath(__file__))
CORE_API = 8


def _core():
    """(dcs_core or None, reason, menu); see corecheck."""
    from . import corecheck
    return corecheck.load(CORE_API)


class AlignProPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.dock = None
        self.action = None
        self.core = None
        self.help_action = None

    def initGui(self):  # noqa: N802
        self.action = QAction(QIcon(os.path.join(HERE, "icons", "alignpro.svg")), "AlignPro", self.iface.mainWindow())
        self.action.setObjectName("mAlignProAction")
        self.action.setToolTip("AlignPro: Lidar boresight calibration (patch test)")
        self.action.setCheckable(True)
        self.action.triggered.connect(self.toggle)
        self.iface.addToolBarIcon(self.action)
        from .guide import MENU_TEXT
        self.help_action = QAction(QIcon(os.path.join(HERE, "icons", "alignpro.svg")), MENU_TEXT,
                                   self.iface.mainWindow())
        self.help_action.setObjectName("mAlignProGuideAction")
        self.help_action.triggered.connect(self.open_guide)
        self.iface.pluginHelpMenu().addAction(self.help_action)
        from . import corecheck
        self.core, why, self._menu = _core()
        corecheck.add_menu(self.iface, self.action, self._menu)
        if self.core is None:
            self._why = why
            return
        from dcs_core import registry
        registry.register("alignpro", self.open_project)

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
                registry.unregister("alignpro")
        if self.dock is not None:
            from .ui import docksafe
            if docksafe.alive(self.dock):
                try:
                    self.dock.visibilityChanged.disconnect(self.action.setChecked)
                except (TypeError, RuntimeError):
                    pass
                self.dock.cleanup()
                self.dock.release_window()  # remembers the window, removes it safely
            self.dock = None

    def toggle(self, on=True):
        if self.core is None:
            from qgis.core import Qgis
            self.action.setChecked(False)
            from . import corecheck
            self.iface.messageBar().pushMessage(
                "AlignPro", "AlignPro can't start: %s. %s" % (self._why, corecheck.advice(self._why)),
                Qgis.MessageLevel.Warning, 15)
            return
        if self.dock is None:
            from .ui.dock import AlignProDock
            self.dock = AlignProDock(self.iface, self.iface.mainWindow())
            self.dock.visibilityChanged.connect(self.action.setChecked)
        if bool(on) or not self.dock.isVisible():
            self.dock.show_window()     # where it was left; floating the first time
        else:
            self.dock.hide()

    def open_guide(self, *_):
        """The user guide PDF for the installed version (Help > Plugins > AlignPro user guide)."""
        from .guide import open_guide
        return open_guide(self.iface, self.iface.mainWindow())

    def open_project(self, path):
        """Show AlignPro on a DCS project (called from another plugin's chain bar)."""
        self.toggle(True)
        if self.dock is not None:
            self.dock.open_project(path)
