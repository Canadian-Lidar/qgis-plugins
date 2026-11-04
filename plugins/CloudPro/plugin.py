"""QGIS entry: Plugins > Canadian Lidar > Data Systems > CloudPro.

CloudPro is stage 03 of the DCS chain and needs the DCS Core plugin (shared
project, handover files, menu and look). Without it a message bar says so
instead of failing."""
import os

from .qt import QAction, QtGui, QtWidgets, Qt

HERE = os.path.dirname(os.path.abspath(__file__))
CORE_API = 2


def _core():
    try:
        import dcs_core
        dcs_core.require(CORE_API)
        return dcs_core, ""
    except ImportError as e:
        return None, str(e) or "the DCS Core plugin is not installed"


class CloudProPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.win = None
        self.actions = []
        self.maptool = None
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
        self.core, self._why = _core()
        if self.core is None:
            for x in self.actions:
                self.iface.addPluginToMenu("&Canadian Lidar", x)
            return
        from dcs_core import menu, registry
        for x in self.actions:
            menu.add(self.iface, x)
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
            for x in self.actions:
                if self.core is not None:
                    from dcs_core import menu
                    menu.remove(self.iface, x)
                else:
                    self.iface.removePluginMenu("&Canadian Lidar", x)
            if self.core is not None:
                from dcs_core import registry
                registry.unregister("cloudpro")
        if self.win is not None:
            try:
                self.win.shutdown()
            except Exception:       # noqa: BLE001
                pass
            self.win.close()
            self.win.deleteLater()
            self.win = None
        if self.maptool is not None:
            try:
                self.iface.mapCanvas().unsetMapTool(self.maptool)
            except Exception:       # noqa: BLE001
                pass

    def _need_core(self):
        if self.core is not None:
            return True
        from qgis.core import Qgis
        self.iface.messageBar().pushMessage(
            "CloudPro", "CloudPro needs the DCS Core plugin (%s). Install DCS Core first (Plugins > Manage and "
            "Install Plugins > Install from ZIP), then restart QGIS." % self._why, Qgis.MessageLevel.Warning, 15)
        return False

    def show(self):
        if not self._need_core():
            return None
        if self.win is None:
            from .ui.mainwindow import MainWindow
            self.win = MainWindow(self.iface, self.iface.mainWindow())
            self.win.map_pick = self.map_tool
            self.win.setWindowFlag(Qt.WindowType.Window, True)
        self.win.show()
        self.win.raise_()
        self.win.activateWindow()
        return self.win

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
        self.maptool = QgsMapToolEmitPoint(canvas)

        def clicked(pt, _btn):
            src = canvas.mapSettings().destinationCrs()
            dst = QgsCoordinateReferenceSystem(win.project.crs)
            q = QgsCoordinateTransform(src, dst, QgsProject.instance()).transform(pt)
            win.goto_world(q.x(), q.y())
            win.raise_()
        self.maptool.canvasClicked.connect(clicked)
        canvas.setMapTool(self.maptool)
        self.iface.messageBar().pushInfo("CloudPro", "Click on the map to centre the Inspection Area there")
