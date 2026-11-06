"""qgis.PyQt for tests and the guide harness outside QGIS, on PyQt5 or PyQt6.

    import qtshim
    Qt = qtshim.install()          # -> namespace: QtCore, QtGui, QtWidgets, QtSvg, sip, QT6, binding

The binding is chosen by the environment variable DCS_QT ("5" or "6"). Without DCS_QT the real qgis.PyQt is
used when QGIS's Python is running, else PyQt5, else PyQt6. With DCS_QT set the stand-in is always installed
(also under QGIS's Python), so the same tests can be run on both Qt versions:

    DCS_QT=6 QT_QPA_PLATFORM=offscreen python3 -m pytest dcs_core/tests

The stand-in is strict on purpose: like QGIS 4's own qgis.PyQt it re-exports QAction, QActionGroup and
QShortcut from QtWidgets (set on PyQt6.QtWidgets itself) and QSvgWidget from QtSvg, but it adds no exec_() and
no short enum names, so Qt5-only code fails here first.
Only the qgis.PyQt modules are installed; qgis.core / qgis.gui stand-ins are the caller's business.
"""
import importlib
import os
import sys
import types

_done = None
MODULES = ("QtCore", "QtGui", "QtWidgets", "QtSvg", "QtPrintSupport", "sip")


def binding():
    """'PyQt5' or 'PyQt6' as DCS_QT asks, or None when it is not set."""
    v = os.environ.get("DCS_QT", "").strip()
    if not v:
        return None
    if v not in ("5", "6"):
        raise RuntimeError("DCS_QT must be 5 or 6, not %r" % v)
    return "PyQt" + v


def _module(name, sources, extra=None):
    m = types.ModuleType(name)
    for src in sources:
        m.__dict__.update({k: v for k, v in vars(src).items() if not k.startswith("__")})
    m.__dict__.update(extra or {})
    return m


def _real():
    try:
        qp = importlib.import_module("qgis.PyQt")
        QtCore = importlib.import_module("qgis.PyQt.QtCore")
    except ImportError:
        return None
    if getattr(qp, "__dcs_shim__", False):
        return None
    ns = types.SimpleNamespace(binding="qgis", QT6=QtCore.QT_VERSION_STR.startswith("6"))
    for n in MODULES:
        try:
            setattr(ns, n, importlib.import_module("qgis.PyQt." + n))
        except ImportError:
            setattr(ns, n, None)
    return ns


def install():
    """Install (once) and return the Qt namespace."""
    global _done
    if _done is not None:
        return _done
    want = binding()
    if want is None:
        real = _real()
        if real is not None:
            _done = real
            return real
        try:
            importlib.import_module("PyQt5.QtCore")
            want = "PyQt5"
        except ImportError:
            want = "PyQt6"
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtCore = importlib.import_module(want + ".QtCore")
    QtGui = importlib.import_module(want + ".QtGui")
    QtWidgets = importlib.import_module(want + ".QtWidgets")
    sip = importlib.import_module(want + ".sip")
    qt6 = want == "PyQt6"
    svg = []
    for n in ("QtSvg", "QtSvgWidgets"):           # Qt 6 moved QSvgWidget to QtSvgWidgets
        try:
            svg.append(importlib.import_module("%s.%s" % (want, n)))
        except ImportError:
            pass
    try:
        prt = [importlib.import_module(want + ".QtPrintSupport")]
    except ImportError:
        prt = []
    widgets_extra = {}
    if qt6:                                         # as QGIS 4's qgis/PyQt/QtWidgets.py does
        for n in ("QAction", "QActionGroup", "QShortcut", "QFileSystemModel", "QUndoCommand"):
            if hasattr(QtGui, n):
                widgets_extra[n] = getattr(QtGui, n)
    # the binding's own module objects (as QGIS does): wrapping them in copies changed the order Qt objects are
    # torn down at exit and made the guide harness crash there. Qt 6 additions are set on the modules.
    for n, v in widgets_extra.items():
        setattr(QtWidgets, n, v)
    svg_mod = svg[0] if svg else _module("qgis.PyQt.QtSvg", [])
    for extra in svg[1:]:
        for k, v in vars(extra).items():
            if not k.startswith("__") and not hasattr(svg_mod, k):
                setattr(svg_mod, k, v)
    mods = {"QtCore": QtCore, "QtGui": QtGui, "QtWidgets": QtWidgets, "QtSvg": svg_mod, "sip": sip}
    if prt:
        mods["QtPrintSupport"] = prt[0]
    q = sys.modules.get("qgis")
    if q is None or getattr(q, "__file__", None):   # no qgis, or the real one: replace with a bare package
        q = types.ModuleType("qgis")
        q.__path__ = []
        sys.modules["qgis"] = q
    qp = types.ModuleType("qgis.PyQt")
    qp.__path__ = []
    qp.__dcs_shim__ = True
    q.PyQt = qp
    sys.modules["qgis.PyQt"] = qp
    for n, m in mods.items():
        setattr(qp, n, m)
        sys.modules["qgis.PyQt." + n] = m
    for k in [k for k in sys.modules if k.startswith("qgis.") and k.split(".")[1] in ("core", "gui", "utils")]:
        if getattr(sys.modules[k], "__file__", None):     # the real QGIS modules do not go with a stand-in Qt
            del sys.modules[k]
    _done = types.SimpleNamespace(binding=want, QT6=qt6, sip=sip, **{k: v for k, v in mods.items() if k != "sip"})
    return _done
