"""QGIS entry: Plugins > Canadian Lidar > Data Systems > CloudPro.

CloudPro is stage 03 of the DCS chain and needs the DCS Core plugin (shared
project, handover files, menu and look). Without it a message bar says so
instead of failing."""
import os

from .qt import QAction, QtGui, QtWidgets, Qt

HERE = os.path.dirname(os.path.abspath(__file__))
CORE_API = 7


def _core():
    """(dcs_core or None, reason, menu); see corecheck."""
    from . import corecheck
    return corecheck.load(CORE_API)


class CloudProPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.win = None
        self.panel = None
        self.actions = []
        self.maptool = None
        self._prev_tool = None
        self.core = None
        self._why = ""
        self.help_action = None

    def initGui(self):  # noqa: N802
        a = QAction(QtGui.QIcon(os.path.join(HERE, "icons", "cloudpro.svg")), "CloudPro", self.iface.mainWindow())
        a.setObjectName("mCloudProAction")
        a.setToolTip("CloudPro: georeference, clean, classify and deliver the point cloud (stage 03)")
        a.triggered.connect(self.show)
        self.actions = [a]
        self.iface.addToolBarIcon(a)
        # QGIS Help > Plugins > CloudPro user guide
        self.help_action = QAction("CloudPro user guide", self.iface.mainWindow())
        self.help_action.setObjectName("mCloudProGuideAction")
        self.help_action.triggered.connect(self.open_guide)
        try:
            self.iface.pluginHelpMenu().addAction(self.help_action)
        except Exception:       # noqa: BLE001
            pass
        from . import corecheck
        self.core, self._why, self._menu = _core()
        for x in self.actions:
            corecheck.add_menu(self.iface, x, self._menu)
        if self.core is None:
            return
        from dcs_core import registry
        registry.register("cloudpro", self.open_project)

    def open_guide(self):
        from . import guide
        return guide.open_guide(self.win if self.win is not None else self.iface.mainWindow())

    def unload(self):
        if getattr(self, "help_action", None) is not None:
            try:
                self.iface.pluginHelpMenu().removeAction(self.help_action)
            except Exception:       # noqa: BLE001
                pass
            self.help_action.deleteLater()
            self.help_action = None
        if self.actions:
            self.iface.removeToolBarIcon(self.actions[0])
            from . import corecheck
            for x in self.actions:
                corecheck.remove_menu(self.iface, x, getattr(self, "_menu", None))
            if self.core is not None:
                from dcs_core import registry
                registry.unregister("cloudpro")
        if self.win is not None:
            try:
                self.win.shutdown()
            except Exception:       # noqa: BLE001
                pass
            self.win = None
        if self.panel is not None:
            self.panel.release_window()     # remembers the window; deletes it and CloudPro's view with it
            self.panel = None
        if self.maptool is not None:
            try:
                canvas = self.iface.mapCanvas()
                if canvas.mapTool() is self.maptool:
                    canvas.unsetMapTool(self.maptool)
                    if self._prev_tool is not None:
                        canvas.setMapTool(self._prev_tool)
            except Exception:       # noqa: BLE001  (the previous tool may be gone)
                pass
            self.maptool = None
            self._prev_tool = None

    def _need_core(self):
        if self.core is not None:
            return True
        from qgis.core import Qgis
        from . import corecheck
        self.iface.messageBar().pushMessage(
            "CloudPro", "CloudPro can't start: %s. %s" % (self._why, corecheck.advice(self._why)),
            Qgis.MessageLevel.Warning, 15)
        return False

    def show(self):
        if not self._need_core():
            return None
        if self.win is None:
            from dcs_core import theme as dtheme
            from dcs_core.window import PluginWindow
            from .ui.mainwindow import MainWindow
            self.panel = PluginWindow(self.iface, "cloudpro", "CloudPro", size=(1680, 1000), dock_size=900,
                                      object_name="CloudProWindow", min_size=(900, 600))
            self.win = MainWindow(self.iface, self.panel)
            self.win.setWindowFlags(Qt.WindowType.Widget)   # CloudPro's view inside the standard window
            self.win.map_pick = self.map_tool
            self.panel.setWidget(self.win)
            self.panel.setWindowTitle(self.win.windowTitle())
            self.win.windowTitleChanged.connect(self.panel.setWindowTitle)
            self.panel.closed.connect(self._closed)
            self.win.attach_window(self.panel)
            dtheme.apply_dock(self.panel)
        else:
            self.win.ensure_lock()          # closing the window released the job's edit lock
        self.win.show()
        self.panel.show_window()        # where it was left; floating the first time
        return self.win

    def _closed(self):
        """Closing the window saves the project and frees the edit lock, as closing CloudPro always did."""
        if self.win is not None:
            try:
                self.win.shutdown()
            except Exception:       # noqa: BLE001
                pass

    def open_project(self, path):
        """Show CloudPro on a DCS project (called from another plugin's chain bar)."""
        win = self.show()
        if win is not None:
            win.open_project(path)

    def map_tool(self):
        from qgis.gui import QgsMapToolEmitPoint
        from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject
        win = self.show()
        if win is None:
            return
        if win.project is None:
            QtWidgets.QMessageBox.information(self.iface.mainWindow(), "CloudPro", "Open a project first.")
            return
        canvas = self.iface.mapCanvas()
        if self.maptool is None:
            # one tool, reused; it centres the Inspection Area of whatever project is open when clicked
            self.maptool = QgsMapToolEmitPoint(canvas)

            def clicked(pt, _btn):
                w = self.win
                if w is None or w.project is None:
                    return
                src = canvas.mapSettings().destinationCrs()
                dst = QgsCoordinateReferenceSystem(w.project.crs)
                q = QgsCoordinateTransform(src, dst, QgsProject.instance()).transform(pt)
                w.goto_world(q.x(), q.y())
                w.window().raise_()
                if self._prev_tool is not None:          # one click, then the previous tool is back
                    try:
                        canvas.setMapTool(self._prev_tool)
                    except Exception:       # noqa: BLE001
                        canvas.unsetMapTool(self.maptool)
            self.maptool.canvasClicked.connect(clicked)
        if canvas.mapTool() is not self.maptool:
            self._prev_tool = canvas.mapTool()
        canvas.setMapTool(self.maptool)
        self.iface.messageBar().pushInfo("CloudPro", "Click on the map to centre the Inspection Area there")
