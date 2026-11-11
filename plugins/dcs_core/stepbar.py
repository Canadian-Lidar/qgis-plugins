"""Breadcrumbs in the header of every DCS plugin (DCS Core 1.11.0):

    PosPro  ›  AlignPro  ›  CloudPro                      (StepBar: the stages of the chain)
    GEOREFERENCE  ›  CLEAN & CLASSIFY  ›  FINALIZE         (Crumbs: this plugin's own steps)

Each item is coloured by its state (done green, out of date amber, running blue, failed red, not done
grey); the current one is bright with a line under it. Hovering shows the state and its reasons (the
"what next" sentences that used to sit under the header). Clicking another stage opens that plugin on
the same project (registry); clicking a step emits clicked(key).
"""
from qgis.PyQt.QtCore import QRectF, QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QFontMetricsF, QPainter, QPen
from qgis.PyQt.QtWidgets import QToolTip, QWidget

from . import registry, theme
from .project import STAGES

STATE_STYLE = {"done": (theme.ACCENT, "✓"), "stale": (theme.AMBER, "⚠ stale"),
               "running": (theme.BLUE, "running"), "failed": (theme.RED, "failed"), "none": (theme.DIM, "–")}
# step states of the plugins (StepNav / sidebar words) on the same colours
STEP_STATE = {"done": "done", "ok": "done", "warn": "stale", "stale": "stale", "error": "failed", "failed": "failed",
              "bad": "failed", "running": "running", "busy": "running", "todo": "none", "none": "none", "": "none"}
WORDS = {"done": "done", "stale": "out of date / check", "running": "running now", "failed": "needs attention",
         "none": "not done yet"}
SEP = "  ›  "


def _af(f):
    try:
        return int(f)
    except TypeError:
        return f.value


class Crumbs(QWidget):
    """items: [(key, label, state, tip)]; current: the key shown as the current one."""
    clicked = pyqtSignal(str)

    def __init__(self, caps=True, px=12, parent=None):
        super().__init__(parent)
        self.items = []
        self.current = None
        self.caps = caps
        self.px = px
        self.setMouseTracking(True)
        self.setFixedHeight(px + 10)
        self._boxes = []

    def set_items(self, items, current=None):
        self.items = [tuple(it) + ("",) * (4 - len(it)) for it in items]
        if current is not None:
            self.current = current
        self.updateGeometry()
        self.update()

    def _font(self, cur=False):
        return theme.font("ui", self.px, 700 if (self.caps or cur) else 600, 106 if self.caps else 100)

    def _text(self, label):
        return label.upper() if self.caps else label

    def sizeHint(self):                 # noqa: N802
        return QSize(int(self._layout()[-1]) + 4 if self.items else 120, self.px + 10)

    def minimumSizeHint(self):          # noqa: N802
        return self.sizeHint()

    def _layout(self):
        fm = QFontMetricsF(self._font(True))
        sep = QFontMetricsF(self._font()).horizontalAdvance(SEP)
        x, self._boxes = 0.0, []
        for k, label, _st, _tip in self.items:
            w = fm.horizontalAdvance(self._text(label))
            self._boxes.append(QRectF(x, 0, w, self.height()))
            x += w + sep
        return [b.right() for b in self._boxes] or [0.0]

    def paintEvent(self, ev):           # noqa: N802
        self._layout()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for i, ((k, label, st, _tip), r) in enumerate(zip(self.items, self._boxes)):
            col = QColor(STATE_STYLE.get(STEP_STATE.get(st, st), STATE_STYLE["none"])[0])
            cur = k == self.current
            p.setFont(self._font(cur))
            p.setPen(QColor(theme.BRIGHT) if cur and STEP_STATE.get(st, st) == "none" else col)
            p.drawText(r.adjusted(0, 0, 0, -3), _af(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                       self._text(label))
            if cur:
                p.setPen(QPen(col if STEP_STATE.get(st, st) != "none" else QColor(theme.BRIGHT), 2))
                p.drawLine(int(r.left()), int(r.bottom() - 1), int(r.right()), int(r.bottom() - 1))
            if i < len(self.items) - 1:
                p.setFont(self._font())
                p.setPen(QColor(theme.DIM))
                p.drawText(QRectF(r.right(), 0, 200, self.height() - 3),
                           _af(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), SEP)
        p.end()

    def _hit(self, pos):
        self._layout()
        for it, r in zip(self.items, self._boxes):
            if r.adjusted(-6, 0, 6, 0).contains(pos.x(), pos.y()):
                return it
        return None

    def tip_for(self, item):
        k, label, st, tip = item
        s = "%s: %s" % (label, WORDS.get(STEP_STATE.get(st, st), st))
        return s + ("\n" + tip if tip else "")

    def mouseMoveEvent(self, ev):       # noqa: N802
        pos = ev.position() if hasattr(ev, "position") else ev.pos()
        h = self._hit(pos)
        self.setCursor(Qt.CursorShape.PointingHandCursor if h else Qt.CursorShape.ArrowCursor)
        if not h:
            QToolTip.hideText()
            return
        gp = ev.globalPosition().toPoint() if hasattr(ev, "globalPosition") else ev.globalPos()
        QToolTip.showText(gp, self.tip_for(h), self)

    def mousePressEvent(self, ev):      # noqa: N802
        pos = ev.position() if hasattr(ev, "position") else ev.pos()
        h = self._hit(pos)
        if h:
            self.clicked.emit(h[0])


class StepBar(Crumbs):
    """The chain: PosPro › AlignPro › CloudPro, by the project's stage states. Clicking another stage
    opens that plugin on the same project when it is installed."""
    stageClicked = pyqtSignal(str)

    def __init__(self, current=None, parent=None):
        super().__init__(caps=False, px=13, parent=parent)
        self.current = current          # this plugin's stage key
        self.project = None
        self.refresh()
        self.clicked.connect(self._open)

    def set_project(self, project):
        self.project = project
        self.refresh()

    def refresh(self):
        p = self.project
        chain = None
        if p is not None:
            try:
                p.reload()
            except Exception:       # noqa: BLE001
                pass
            try:
                chain = p.chain()
            except Exception:       # noqa: BLE001
                chain = None
        if chain is None:
            chain = [(k, label, "none", []) for k, _f, label, *_ in STAGES]
        from .project import OPTIONAL
        # an optional stage (StripPro) shows only when its plugin is installed or it has been run
        chain = [c for c in chain if c[0] not in OPTIONAL or c[2] != "none" or registry.has(c[0])]
        self.set_items([(k, label, st, "\n".join("• " + r for r in reasons)) for k, label, st, reasons in chain])

    def tip_for(self, item):
        k, label, st, tip = item
        words = {"done": "results are current", "stale": "results are out of date", "running": "running now",
                 "failed": "the last run failed", "none": "not run yet"}.get(st, st)
        from .project import OPTIONAL
        if k in OPTIONAL and st == "none":
            words = "optional, not run"
        s = "%s: %s" % (label, words) + ("\n" + tip if tip else "")
        if k != self.current:
            s += "\n\nClick to open %s on this project" % label if registry.has(k) else \
                "\n\n%s is not installed" % label
        return s

    def _open(self, k):
        if k != self.current:
            self.stageClicked.emit(k)
            if self.project is not None and registry.has(k):
                registry.open(k, self.project.path)
