"""QGIS entry: Plugins > Canadian Lidar > Data Systems > CloudPro.

CloudPro is stage 03 of the DCS chain and needs the DCS Core plugin (shared
project, handover files, menu and look). Without it a message bar says so
instead of failing."""
import os

from .qt import QAction, QtCore, QtGui, QtWidgets, Qt, Signal

HERE = os.path.dirname(os.path.abspath(__file__))
CORE_API = 12


def _core():
    """(dcs_core or None, reason, menu); see corecheck."""
    from . import corecheck
    return corecheck.load(CORE_API)


class _Bridge(QtCore.QObject):
    """Brings DCS Core's publish notices and the background job's progress to QGIS's main thread."""
    published = Signal(str, str)
    progress = Signal(float, str)
    finished = Signal(str, object)


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
        self._bridge = None
        self._bg = None                 # background georeferencing: (thread, dcs path, cancel flag)
        self._bg_bar = None
        self._bg_pending = set()        # projects to look at again when the running job ends
        self._open_after = None

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
        # georeference in the background when PosPro / AlignPro publish (DCS Core API 11)
        self._bridge = _Bridge()
        self._bridge.published.connect(self._on_published)
        self._bridge.progress.connect(self._bg_progress)
        self._bridge.finished.connect(self._bg_finished)
        registry.on_publish("cloudpro", lambda path, stage: self._bridge.published.emit(path, stage))

    # ----------------------------------------------------------- background georeferencing
    def _on_published(self, path, stage):
        """A stage published (main thread): georeference this job again if its upstream is now current."""
        if stage == "cloudpro":
            return
        path = os.path.abspath(path)
        if self._bg is not None:
            self._bg_pending.add(path)          # look again when the running job ends
            return
        from .core import autoupdate
        ok, why = autoupdate.decide(path)
        if not ok:
            return
        w = self.win
        if w is not None and w.project is not None and w.project.dcs is not None and \
                os.path.abspath(w.project.dcs.path) == path:
            w.auto_update(why)                  # the open window does it, with its progress and reload
            return
        self._bg_start(path, why)

    def _bg_start(self, path, why):
        import threading
        from .core import autoupdate
        cancel = [False]
        name = os.path.splitext(os.path.basename(path))[0]

        def work():
            try:
                res = autoupdate.run(path, progress=lambda f, s=None: self._bridge.progress.emit(float(f or 0), str(s or "")),
                                     cancel=lambda: cancel[0])
            except Exception as e:              # noqa: BLE001 - reported in the message bar; old points kept
                res = e
            self._bridge.finished.emit(path, res)
        th = threading.Thread(target=work, name="CloudPro background georeferencing", daemon=True)
        self._bg = (th, path, cancel)
        self._bg_message(name, why)
        th.start()

    def _bg_message(self, name, why):
        try:
            from qgis.core import Qgis
            bar = self.iface.messageBar()
            item = bar.createMessage("CloudPro", "Georeferencing %s in the background (%s)" % (name, why))
            pb = QtWidgets.QProgressBar()
            pb.setRange(0, 100)
            pb.setMaximumWidth(220)
            item.layout().addWidget(pb)
            btn = QtWidgets.QPushButton("Cancel")
            btn.clicked.connect(self.cancel_background)
            item.layout().addWidget(btn)
            bar.pushWidget(item, Qgis.MessageLevel.Info)
            self._bg_bar = (bar, item, pb)
        except Exception:                       # noqa: BLE001 - outside QGIS (tests)
            self._bg_bar = None

    def _bg_progress(self, f, _text):
        if self._bg_bar is not None:
            try:
                self._bg_bar[2].setValue(int(round(100 * max(0.0, min(1.0, f)))))
            except RuntimeError:                # the message was closed
                self._bg_bar = None

    def cancel_background(self):
        if self._bg is not None:
            self._bg[2][0] = True

    def background_busy(self, path):
        """True while the background job works on this project (the window then waits for it)."""
        return self._bg is not None and os.path.abspath(path) == self._bg[1]

    def _bg_finished(self, path, res):
        th, _p, _c = self._bg
        self._bg = None
        if self._bg_bar is not None:
            try:
                self._bg_bar[0].popWidget(self._bg_bar[1])
            except RuntimeError:
                pass
            self._bg_bar = None
        name = os.path.splitext(os.path.basename(path))[0]
        try:
            from qgis.core import Qgis
            bar = self.iface.messageBar()
            if isinstance(res, Exception):
                bar.pushMessage("CloudPro", "Background georeferencing of %s stopped: %s. The previous points and "
                                "edits are kept." % (name, res), Qgis.MessageLevel.Warning, 15)
            else:
                out, rest = res
                bar.pushMessage("CloudPro", "%s georeferenced in the background: %d line(s), %s points%s. Open it "
                                "in CloudPro to clean." % (name, len(out), "{:,}".format(sum(st["points"] for _, st in out)),
                                                          ", %s edits carried over" % "{:,}".format(rest) if rest else ""),
                                Qgis.MessageLevel.Success, 15)
        except Exception:                       # noqa: BLE001
            pass
        self.last_background = (path, res)
        if self._open_after and os.path.abspath(self._open_after) == path:
            self._open_after = None
            self.open_project(path)
        pend, self._bg_pending = self._bg_pending, set()
        for pp in pend:                         # published again while it ran
            self._on_published(pp, "pospro")

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
                if hasattr(registry, "off_publish"):
                    registry.off_publish("cloudpro")
        if self._bg is not None:                # stop the background job (its old points are kept)
            self._bg[2][0] = True
            self._bg[0].join(10)
            self._bg = None
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
            self.win.background_busy = self.background_busy
            self.win.open_after_background = self._set_open_after
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

    def _set_open_after(self, path):
        self._open_after = path

    def map_tool(self):
        from qgis.gui import QgsMapToolEmitPoint
        from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject
        win = self.show()
        if win is None:
            return
        if win.project is None:
            from .ui import theme
            theme.msg_info(self.iface.mainWindow(), "CloudPro", "Open a project first.")
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
