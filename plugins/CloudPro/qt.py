"""Qt imports for QGIS 3 (PyQt5) and QGIS 4 (PyQt6), and plain PyQt for the
stand-alone test harness."""
try:
    from qgis.PyQt import QtCore, QtGui, QtWidgets          # noqa: F401
    IN_QGIS = True
except Exception:                                            # pragma: no cover
    try:
        from PyQt5 import QtCore, QtGui, QtWidgets          # noqa: F401
    except ImportError:
        from PyQt6 import QtCore, QtGui, QtWidgets          # noqa: F401
    IN_QGIS = False

Qt = QtCore.Qt
Signal = QtCore.pyqtSignal
QAction = getattr(QtGui, "QAction", None) or QtWidgets.QAction
QActionGroup = getattr(QtGui, "QActionGroup", None) or QtWidgets.QActionGroup
QShortcut = getattr(QtGui, "QShortcut", None) or QtWidgets.QShortcut
QT6 = QtCore.QT_VERSION_STR.startswith("6")


def evpos(e):
    """Mouse/wheel event position as QPointF in widget coordinates."""
    if hasattr(e, "position"):
        return e.position()
    return e.localPos() if hasattr(e, "localPos") else QtCore.QPointF(e.pos())


def exec_(obj, *a):
    f = getattr(obj, "exec", None) or getattr(obj, "exec_")
    return f(*a)


def mods(e):
    m = e.modifiers()
    return (bool(m & Qt.KeyboardModifier.ShiftModifier), bool(m & Qt.KeyboardModifier.ControlModifier),
            bool(m & Qt.KeyboardModifier.AltModifier))
