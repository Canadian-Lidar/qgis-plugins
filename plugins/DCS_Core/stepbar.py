"""The chain bar every DCS plugin shows in its header: one pill per stage
(PosPro, AlignPro, CloudPro) coloured by its state, with the reasons on
hover. Clicking another stage's pill opens that plugin on the same project
when it is installed (plugins register themselves in registry)."""
from qgis.PyQt.QtCore import QRectF, QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QPainter, QPen
from qgis.PyQt.QtWidgets import QToolTip, QWidget

from . import registry, theme
from .project import STAGES

STATE_STYLE = {"done": (theme.ACCENT, "✓"), "stale": (theme.AMBER, "⚠ stale"),
               "running": (theme.BLUE, "running"), "failed": (theme.RED, "failed"), "none": (theme.DIM, "–")}


def _af(f):
    try:
        return int(f)
    except TypeError:
        return f.value


class StepBar(QWidget):
    stageClicked = pyqtSignal(str)

    def __init__(self, current=None, parent=None):
        super().__init__(parent)
        self.current = current          # this plugin's stage key
        self.items = [(k, label, "none", []) for k, _f, label, *_ in STAGES]
        self.project = None
        self.setMouseTracking(True)
        self.setFixedHeight(30)
        self.setToolTip("")

    def sizeHint(self):                 # noqa: N802
        return QSize(420, 30)

    def set_project(self, project):
        self.project = project
        self.refresh()

    def refresh(self):
        p = self.project
        if p is None:
            self.items = [(k, label, "none", []) for k, _f, label, *_ in STAGES]
        else:
            try:
                p.reload()
            except Exception:       # noqa: BLE001
                pass
            self.items = p.chain()
        self.update()

    def _rects(self):
        n = len(self.items)
        gap = 18
        w = (self.width() - gap * (n - 1)) / max(1, n)
        return [QRectF(i * (w + gap), 2, w, self.height() - 4) for i in range(n)]

    def paintEvent(self, ev):           # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rs = self._rects()
        for i, ((k, label, st, _r), r) in enumerate(zip(self.items, rs)):
            col, word = STATE_STYLE.get(st, STATE_STYLE["none"])
            c = QColor(col)
            fill = QColor(col)
            fill.setAlpha(40 if k == self.current else 18)
            p.setBrush(fill)
            p.setPen(QPen(c, 2 if k == self.current else 1))
            p.drawRoundedRect(r, 3, 3)
            p.setFont(theme.font("ui", 12, 700, 108))
            p.setPen(QColor(theme.BRIGHT if k == self.current else theme.TEXT))
            p.drawText(r.adjusted(10, 0, -6, 0), _af(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                       "%02d %s" % (i + 1, label.upper()))
            p.setPen(c)
            p.setFont(theme.font("mono", 11))
            p.drawText(r.adjusted(6, 0, -10, 0), _af(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), word)
            if i < len(rs) - 1:
                p.setPen(QPen(QColor(theme.DIM), 1.2))
                y = r.center().y()
                x = r.right() + 4
                p.drawLine(int(x), int(y), int(x + 10), int(y))
                p.drawLine(int(x + 10), int(y), int(x + 6), int(y - 4))
                p.drawLine(int(x + 10), int(y), int(x + 6), int(y + 4))
        p.end()

    def _hit(self, pos):
        for (k, label, st, reasons), r in zip(self.items, self._rects()):
            if r.contains(pos.x(), pos.y()):
                return k, label, st, reasons
        return None

    def mouseMoveEvent(self, ev):       # noqa: N802
        pos = ev.position() if hasattr(ev, "position") else ev.pos()
        h = self._hit(pos)
        if not h:
            QToolTip.hideText()
            return
        k, label, st, reasons = h
        words = {"done": "results are current", "stale": "results are out of date", "running": "running now",
                 "failed": "the last run failed", "none": "not run yet"}.get(st, st)
        tip = "%s: %s" % (label, words)
        if reasons:
            tip += "\n• " + "\n• ".join(reasons)
        if k != self.current:
            tip += "\n\nClick to open %s on this project" % label if registry.has(k) else \
                "\n\n%s is not installed" % label
        gp = ev.globalPosition().toPoint() if hasattr(ev, "globalPosition") else ev.globalPos()
        QToolTip.showText(gp, tip, self)

    def mousePressEvent(self, ev):      # noqa: N802
        pos = ev.position() if hasattr(ev, "position") else ev.pos()
        h = self._hit(pos)
        if h and h[0] != self.current:
            self.stageClicked.emit(h[0])
            if self.project is not None and registry.has(h[0]):
                registry.open(h[0], self.project.path)
