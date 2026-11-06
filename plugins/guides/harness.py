"""Run the DCS plugins outside QGIS to take user-guide screenshots of the real windows.

Real Qt widgets (PyQt5, or PyQt6 with DCS_QT=6; see ../qtshim.py), a stand-in for the parts of QGIS the plugins call (no map canvas
drawing), and QGIS background tasks run straight away on the main thread so processing
finishes before the next line. Only for the guides and tests; the plugins do not use it.

    import harness
    app, iface = harness.start("/path/with/plugin/folders", settings_dir)
    plugin = harness.load("pospro", iface)        # classFactory + initGui
    ...
    harness.grab(widget, "fig/name.png")          # 2x pixel ratio PNG
"""
import os
import sys
import types
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_app = None
QT = None               # the Qt namespace from qtshim (QtCore, QtGui, QtWidgets, QtSvg, sip, QT6, binding)
CRS_NAMES = {"EPSG:2953": "NAD83(CSRS) / New Brunswick Stereographic", "EPSG:4617": "NAD83(CSRS)",
             "EPSG:4326": "WGS 84", "EPSG:2036": "NAD83(CSRS98) / New Brunswick Stereo",
             "EPSG:22620": "NAD83(CSRS)v6 / UTM zone 20N", "EPSG:2961": "NAD83(CSRS) / UTM zone 20N",
             "EPSG:6661": "NAD83(CSRS) / New Brunswick Stereographic (2.0)", "EPSG:8254": "NAD83(CSRS)v7"}


def _install_qgis():
    global QT
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))      # plugins/ (qtshim)
    import qtshim
    if os.environ.get("DCS_QT") is None:
        os.environ["DCS_QT"] = "5"      # always the stand-in here (the harness replaces qgis.core / gui too)
    QT = qtshim.install()
    QtCore, QtWidgets = QT.QtCore, QT.QtWidgets
    q = sys.modules["qgis"]

    class Auto(types.ModuleType):
        def __getattr__(self, n):
            if n.startswith("__"):
                raise AttributeError(n)
            v = MagicMock(name=n)
            setattr(self, n, v)
            return v

    class FakeW(QtWidgets.QWidget):
        def __init__(self, *a, **k):
            super().__init__()

        def __getattr__(self, n):
            if n.startswith("__"):
                raise AttributeError(n)
            return MagicMock(name=n)

    core, gui, utils = Auto("qgis.core"), Auto("qgis.gui"), Auto("qgis.utils")

    # ---------------------------------------------------------------- tasks: run at once
    class QgsTask(QtCore.QObject):
        progressChanged = QtCore.pyqtSignal(float)
        taskCompleted = QtCore.pyqtSignal()
        taskTerminated = QtCore.pyqtSignal()
        statusChanged = QtCore.pyqtSignal(int)

        class Flag:
            CanCancel = 1
            Silent = 2
        CanCancel = 1

        def __init__(self, description="", flags=0):
            super().__init__()
            self._desc = description
            self._cancel = False
            self._progress = 0.0

        def description(self):
            return self._desc

        def setProgress(self, v):             # noqa: N802
            self._progress = v
            self.progressChanged.emit(float(v))
            QtWidgets.QApplication.processEvents()

        def progress(self):
            return self._progress

        def isCanceled(self):                 # noqa: N802
            return self._cancel

        def cancel(self):
            self._cancel = True

        def run(self):
            return True

        def finished(self, ok):
            pass

    class TaskManager:
        def addTask(self, t):                 # noqa: N802
            ok = t.run()
            t.finished(ok)
            (t.taskCompleted if ok else t.taskTerminated).emit()
            return 1

        def activeTasks(self):                # noqa: N802
            return []

        def count(self):
            return 0

    _tm = TaskManager()

    class QgsApplication:
        @staticmethod
        def taskManager():                    # noqa: N802
            return _tm

        @staticmethod
        def instance():
            return QtWidgets.QApplication.instance()

        @staticmethod
        def qgisSettingsDirPath():            # noqa: N802
            return os.environ.get("DCS_HARNESS_SETTINGS", "/tmp")

        def __getattr__(self, n):
            return MagicMock(name=n)

    # ---------------------------------------------------------------- CRS that print sensibly
    class QgsCoordinateReferenceSystem:
        def __init__(self, s="", *a):
            s = s if isinstance(s, str) else ""
            self._id = s if s.upper().startswith("EPSG:") else ("EPSG:%s" % s if str(s).isdigit() else s)

        @staticmethod
        def fromEpsgId(n):                    # noqa: N802
            return QgsCoordinateReferenceSystem("EPSG:%d" % n)

        @staticmethod
        def fromOgcWmsCrs(s):                 # noqa: N802
            return QgsCoordinateReferenceSystem(s)

        def isValid(self):                    # noqa: N802
            return bool(self._id)

        def authid(self):
            return self._id

        def description(self):
            return CRS_NAMES.get(self._id, self._id)

        def userFriendlyIdentifier(self, *a):  # noqa: N802
            return "%s - %s" % (self._id, self.description())

        def toWkt(self, *a):                  # noqa: N802
            return 'PROJCS["%s"]' % self.description()

        def isGeographic(self):               # noqa: N802
            return self._id in ("EPSG:4326", "EPSG:4617", "EPSG:8254")

        def postgisSrid(self):                # noqa: N802
            try:
                return int(self._id.split(":")[1])
            except (IndexError, ValueError):
                return 0

        def __eq__(self, o):
            return isinstance(o, QgsCoordinateReferenceSystem) and o._id == self._id

        def __hash__(self):
            return hash(self._id)

    class QgsProjectionSelectionWidget(QtWidgets.QLineEdit):
        crsChanged = QtCore.pyqtSignal(object)

        def __init__(self, *a, **k):
            super().__init__()
            self._crs = QgsCoordinateReferenceSystem("EPSG:2953")
            self.setReadOnly(True)
            self._show()

        def _show(self):
            self.setText(self._crs.userFriendlyIdentifier())

        def setCrs(self, c):                  # noqa: N802
            self._crs = c if isinstance(c, QgsCoordinateReferenceSystem) else QgsCoordinateReferenceSystem(str(c))
            self._show()

        def crs(self):
            return self._crs

        def setOptionVisible(self, *a):       # noqa: N802
            pass

        def setNotSetText(self, *a):          # noqa: N802
            pass

    class QgsPointXY:
        def __init__(self, x=0.0, y=0.0):
            self._x, self._y = float(x), float(y)

        def x(self):
            return self._x

        def y(self):
            return self._y

    class QgsCoordinateTransform:
        def __init__(self, *a):
            pass

        def transform(self, p, *a):
            return p

    class QgsProject:
        _inst = None

        @classmethod
        def instance(cls):
            if cls._inst is None:
                cls._inst = MagicMock(name="QgsProject")
                cls._inst.mapLayers.return_value = {}
                cls._inst.fileName.return_value = ""
            return cls._inst

    class Qgis:
        class MessageLevel:
            Info, Warning, Critical, Success = 0, 1, 2, 3
        Info, Warning, Critical, Success = 0, 1, 2, 3

        class GeometryType:
            Point, Line, Polygon = 0, 1, 2

        def __getattr__(self, n):
            return MagicMock(name=n)

    core.QgsTask = QgsTask
    core.QgsApplication = QgsApplication
    core.QgsCoordinateReferenceSystem = QgsCoordinateReferenceSystem
    core.QgsCoordinateTransform = QgsCoordinateTransform
    core.QgsPointXY = QgsPointXY
    core.QgsProject = QgsProject
    core.Qgis = Qgis
    gui.QgsProjectionSelectionWidget = QgsProjectionSelectionWidget

    class _MapTool(QtCore.QObject):
        canvasClicked = QtCore.pyqtSignal(object, object)

        def __init__(self, *a, **k):
            super().__init__()

        def __getattr__(self, n):
            return MagicMock(name=n)
    for n in ("QgsMapTool", "QgsMapToolEmitPoint", "QgsMapToolPan"):
        setattr(gui, n, _MapTool)

    class QgsMapCanvas(FakeW):
        def __init__(self, *a, **k):
            super().__init__()
            self.setMinimumHeight(120)
            self._c = None

        def paintEvent(self, e):               # noqa: N802
            QColor, QPainter = QT.QtGui.QColor, QT.QtGui.QPainter
            p = QPainter(self)
            p.fillRect(self.rect(), self._c or QColor("#2a2d31"))
            p.end()

        def setCanvasColor(self, c):          # noqa: N802
            self._c = c
    gui.QgsMapCanvas = QgsMapCanvas
    for n in ("QgsMessageBar",):
        setattr(gui, n, type(n, (FakeW,), {}))
    for name, mod in (("core", core), ("gui", gui), ("utils", utils)):
        setattr(q, name, mod)
        sys.modules["qgis." + name] = mod


class Iface:
    """The bits of QgisInterface the plugins call; the main window is a plain QMainWindow."""

    def __init__(self):
        QtWidgets = QT.QtWidgets
        self.mw = QtWidgets.QMainWindow()
        self.mw.setCentralWidget(QtWidgets.QLabel(""))
        self.mw.resize(1600, 1000)
        self.mw.show()
        self._canvas = MagicMock(name="canvas")
        self._bar = MagicMock(name="messageBar")
        self.messages = []
        self._bar.pushMessage.side_effect = lambda *a, **k: self.messages.append(a)

    def mainWindow(self):                     # noqa: N802
        return self.mw

    def addDockWidget(self, a, d):            # noqa: N802
        self.mw.addDockWidget(a, d)

    def removeDockWidget(self, d):            # noqa: N802
        self.mw.removeDockWidget(d)

    def mapCanvas(self):                      # noqa: N802
        return self._canvas

    def messageBar(self):                     # noqa: N802
        return self._bar

    def __getattr__(self, n):
        return MagicMock(name=n)


def start(plugins_root, settings_dir):
    """QApplication + Iface with the stand-in QGIS; plugins_root holds pospro/, alignpro/, cloudpro/, dcs_core/."""
    global _app
    _install_qgis()
    sys.path.insert(0, plugins_root)
    os.environ["DCS_HARNESS_SETTINGS"] = settings_dir
    QtCore, QtWidgets = QT.QtCore, QT.QtWidgets
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.Format.IniFormat)
    QtCore.QSettings.setPath(QtCore.QSettings.Format.IniFormat, QtCore.QSettings.Scope.UserScope, settings_dir)
    if not QT.QT6:          # Qt 6 always scales for high DPI (the attribute is deprecated there)
        QtCore.QCoreApplication.setAttribute(QtCore.Qt.ApplicationAttribute.AA_EnableHighDpiScaling, False)
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _app.setOrganizationName("dcs-guide")
    _app.setApplicationName("dcs-guide")
    return _app, Iface()


def load(key, iface):
    mod = __import__(key)
    p = mod.classFactory(iface)
    p.initGui()
    return p


def settle(n=6):
    for _ in range(n):
        QT.QtWidgets.QApplication.processEvents()


def wait(cond, timeout=600):
    """Process events until cond() is true (for CloudPro's worker threads)."""
    import time
    t0 = time.time()
    while not cond():
        settle(2)
        time.sleep(0.05)
        if time.time() - t0 > timeout:
            raise TimeoutError("harness.wait timed out")


def grab(widget, path, scale=2):
    """PNG of a widget (and its children) at `scale` x pixel ratio."""
    QtCore, QtGui, QtWidgets = QT.QtCore, QT.QtGui, QT.QtWidgets
    settle()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    sz = widget.size() * scale
    img = QtGui.QImage(sz, QtGui.QImage.Format.Format_ARGB32)
    img.setDevicePixelRatio(scale)
    img.fill(QtCore.Qt.GlobalColor.transparent)
    p = QtGui.QPainter(img)
    rf = QtWidgets.QWidget.RenderFlag
    widget.render(p, QtCore.QPoint(0, 0), QtGui.QRegion(), rf.DrawChildren | rf.DrawWindowBackground)
    p.end()
    img.save(path)
    return path


def grab_menu(menu, path, scale=2):
    """PNG of a QMenu as it looks when open."""
    menu.adjustSize()
    menu.popup(menu.parentWidget().mapToGlobal(menu.parentWidget().rect().bottomLeft()) if menu.parentWidget() else
               QT.QtCore.QPoint(100, 100))
    settle()
    out = grab(menu, path, scale)
    menu.hide()
    return out
