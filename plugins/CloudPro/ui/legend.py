"""Colour scale bar (legend) drawn on top of a view.

A legend is a dict made by the main window from the view's colour scheme:
  {"title": "Height", "kind": "ramp", "lut": "Rainbow", "lo": 20.1, "hi": 31.6, "fmt": callable(v) -> str}
  {"title": "Classification", "kind": "swatches", "items": [(rgb tuple, label), ...]}
plus "extra": [(rgb, label), ...] for rejected / preview / selected points.
"""
import numpy as np

from ..qt import QtCore, QtGui, Qt
from . import render, theme


def paint(p, view, info, x=None, y=10):
    if not info:
        return
    p.save()
    p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)
    f = theme.mono(10)
    p.setFont(f)
    fm = p.fontMetrics()
    x = 56 if x is None else x
    title = info.get("title", "")
    rows = []
    if info.get("kind") == "ramp":
        lo, hi = float(info["lo"]), float(info["hi"])
        fmt = info.get("fmt") or (lambda v: "%.2f" % v)
        ticks = [hi, (lo + hi) / 2, lo]
        labels = [fmt(v) for v in ticks]
        bar_h = 120
        w = max(fm.horizontalAdvance(title), 18 + max(fm.horizontalAdvance(t) for t in labels)) + 16
    else:
        items = list(info.get("items", []))[:14]
        more = len(info.get("items", [])) - len(items)
        rows = items + ([((0, 0, 0), "+%d more" % more)] if more > 0 else [])
        w = max([fm.horizontalAdvance(title)] + [18 + fm.horizontalAdvance(t) for _, t in rows]) + 16
        bar_h = len(rows) * (fm.height() + 2)
    extra = info.get("extra") or []
    if extra:
        w = max([w] + [18 + fm.horizontalAdvance(t) + 16 for _, t in extra])
    h = fm.height() + 8 + bar_h + 6 + (len(extra) * (fm.height() + 2) + 4 if extra else 0)
    box = QtCore.QRectF(x, y, w, h)
    p.setPen(QtGui.QPen(QtGui.QColor(theme.BORDER), 1))
    p.setBrush(QtGui.QColor(10, 20, 18, 215))
    p.drawRect(box)
    p.setPen(QtGui.QColor(theme.ACCENT))
    p.drawText(QtCore.QRectF(x + 8, y + 4, w - 12, fm.height()), int(Qt.AlignmentFlag.AlignLeft), title)
    top = y + fm.height() + 8
    if info.get("kind") == "ramp":
        lut = render.LUTS.get(info.get("lut", "Rainbow"), render.LUTS["Rainbow"])
        img = np.ascontiguousarray(render.pack(lut[np.linspace(255, 0, bar_h).astype(int)]).reshape(bar_h, 1))
        im = QtGui.QImage(img.data, 1, bar_h, 4, QtGui.QImage.Format.Format_ARGB32).copy()
        p.drawImage(QtCore.QRectF(x + 8, top, 10, bar_h), im)
        p.setPen(QtGui.QColor(theme.TEXT))
        for k, t in enumerate(labels):
            yy = top + k * (bar_h - 1) / 2.0
            p.drawLine(QtCore.QPointF(x + 18, yy), QtCore.QPointF(x + 22, yy))
            p.drawText(QtCore.QPointF(x + 25, yy + fm.ascent() / 2 - 1), t)
    else:
        yy = top
        for rgb, t in rows:
            if t.startswith("+") and rgb == (0, 0, 0):
                p.setPen(QtGui.QColor(theme.DIM))
            else:
                p.fillRect(QtCore.QRectF(x + 8, yy + 2, 10, fm.height() - 4), QtGui.QColor(*[int(c) for c in rgb]))
                p.setPen(QtGui.QColor(theme.TEXT))
            p.drawText(QtCore.QPointF(x + 24, yy + fm.ascent()), t)
            yy += fm.height() + 2
    if extra:
        yy = top + bar_h + 8
        p.setPen(QtGui.QPen(QtGui.QColor(theme.BORDER), 1))
        p.drawLine(QtCore.QPointF(x + 6, yy - 3), QtCore.QPointF(x + w - 6, yy - 3))
        for rgb, t in extra:
            p.fillRect(QtCore.QRectF(x + 8, yy + 2, 10, fm.height() - 4), QtGui.QColor(*[int(c) for c in rgb]))
            p.setPen(QtGui.QColor(theme.TEXT))
            p.drawText(QtCore.QPointF(x + 24, yy + fm.ascent()), t)
            yy += fm.height() + 2
    p.restore()
