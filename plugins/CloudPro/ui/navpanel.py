"""Exact-value spin box used by the slice bar and the properties windows."""
from ..qt import QtWidgets


def dspin(lo, hi, val, step, dec=3, suffix=""):
    w = QtWidgets.QDoubleSpinBox()
    w.setRange(lo, hi)
    w.setDecimals(dec)
    w.setSingleStep(step)
    w.setValue(val)
    w.setSuffix(suffix)
    w.setKeyboardTracking(False)
    return w


