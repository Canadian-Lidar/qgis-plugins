"""Reusable widgets in the Mobile Mapper style for every DCS plugin: workflow
steps as sensor-style status pills, KPI cards, collapsible sections,
key/value grids, toasts, the logo and the dark dock title bar."""


from qgis.PyQt.QtCore import QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from qgis.PyQt.QtWidgets import (QAbstractButton, QFrame, QGridLayout, QLabel, QSizePolicy, QToolButton,
                                 QVBoxLayout, QWidget)

from . import theme


def align_flags(f):
    """Alignment flags as int for QPainter.drawText (PyQt5 and PyQt6)."""
    try:
        return int(f)
    except TypeError:
        return f.value


_af = align_flags          # old private name, kept for plugins built on DCS Core 1.0


ACCENT = QColor(theme.ACCENT)
OK = QColor(theme.GREEN)
WARN = QColor(theme.AMBER)
BAD = QColor(theme.RED)
MUTED = QColor(theme.DIM)

# workflow state -> pill level
STATE_LEVEL = {"done": "ok", "warn": "warn", "error": "bad", "todo": "off", "busy": "busy"}


class StepButton(QAbstractButton):
    """Workflow step drawn like the mapper's sensor pills: coloured outline
    and tint by state, upper-case Rajdhani title, mono subtitle."""

    def __init__(self, number, title, parent=None):
        super().__init__(parent)
        self.number = number
        self.title = title
        self.subtitle = ""
        self.state = "todo"
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(50)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_state(self, state, subtitle=""):
        self.state = state
        self.subtitle = subtitle
        self.update()

    def sizeHint(self):
        return QSize(160, 52)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(6, 3, -6, -3)
        level = STATE_LEVEL.get(self.state, "off")
        col = QColor(theme.LEVEL.get(level, theme.DIM))
        if self.isChecked() and level == "off":
            col = QColor(theme.ACCENT)
        bg = QColor(col)
        bg.setAlpha(34 if level != "off" or self.isChecked() else 0)
        if level == "off" and not self.isChecked():
            bg = QColor(14, 26, 24, 235)
        if self.underMouse():
            bg.setAlpha(min(255, bg.alpha() + 25))
        bd = QColor(col)
        bd.setAlpha(150 if level != "off" else 90)
        p.setPen(QPen(bd, 1))
        p.setBrush(bg)
        p.drawRoundedRect(r, 3, 3)
        if self.isChecked():
            p.setPen(QPen(col, 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r.adjusted(-2.5, -2.5, 2.5, 2.5), 4, 4)
        # status dot
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawEllipse(QPointF(r.left() + 12, r.top() + 15), 4, 4)
        # number + title
        p.setPen(col)
        p.setFont(theme.font("ui", 14, 700, 112))
        p.drawText(QRectF(r.left() + 24, r.top() + 4, r.width() - 28, 20),
                   _af(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   "%d  %s" % (self.number, self.title.upper()))
        sub = QColor(theme.TEXT if level != "off" else theme.DIM)
        sub.setAlpha(200)
        p.setPen(sub)
        f = theme.font("mono", 10, 400)
        p.setFont(f)
        el = p.fontMetrics().elidedText(self.subtitle, Qt.TextElideMode.ElideRight, int(r.width() - 16))
        p.drawText(QRectF(r.left() + 8, r.top() + 25, r.width() - 12, 16),
                   _af(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), el)

    def enterEvent(self, ev):
        self.update()

    def leaveEvent(self, ev):
        self.update()


class Card(QFrame):
    """KPI card: big mono value, upper-case label."""

    def __init__(self, label, parent=None):
        super().__init__(parent)
        self.setObjectName("ppCard")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(2)
        self.value = QLabel("–")
        self.value.setObjectName("ppVal")
        self.value.setStyleSheet("font-size: 20px; font-weight: 500;")
        self.label = QLabel(label.upper())
        self.label.setObjectName("ppKey")
        self.label.setWordWrap(True)
        lay.addWidget(self.value)
        lay.addWidget(self.label)

    def set(self, value, level=None):
        self.value.setText(value)
        self.value.setProperty("level", level or "")
        self.value.style().unpolish(self.value)
        self.value.style().polish(self.value)


class Section(QFrame):
    """Collapsible card like the mapper's Settings sections: Rajdhani header
    and a caret."""
    toggled = pyqtSignal(bool)

    def __init__(self, title, content, expanded=False, parent=None):
        super().__init__(parent)
        self.setObjectName("ppSect")
        self.btn = QToolButton()
        self.btn.setObjectName("ppSectHead")
        self.btn.setText(title.upper())
        self.btn.setCheckable(True)
        self.btn.setChecked(expanded)
        self.btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.btn.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        f = self.btn.font()
        f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 110)
        self.btn.setFont(f)
        self.content = content
        content.setVisible(expanded)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 2, 8, 8)
        lay.addWidget(self.btn)
        lay.addWidget(content)
        self.btn.toggled.connect(self._toggle)

    def _toggle(self, on):
        self.btn.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)
        self.content.setVisible(on)
        self.toggled.emit(on)


class KV(QWidget):
    """Key/value grid (mapper .kv2): upper-case dim keys, mono values."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(14)
        self.grid.setVerticalSpacing(5)
        self.rows = {}

    def set(self, key, value, level=None):
        if key not in self.rows:
            k = QLabel(key.upper())
            k.setObjectName("ppKey")
            f = k.font()
            f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 112)
            k.setFont(f)
            v = QLabel("")
            v.setObjectName("ppVal")
            v.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            v.setWordWrap(True)
            r = len(self.rows)
            self.grid.addWidget(k, r, 0)
            self.grid.addWidget(v, r, 1)
            self.rows[key] = v
        v = self.rows[key]
        v.setText(value)
        v.setProperty("level", level or "")
        v.style().unpolish(v)
        v.style().polish(v)

    def clear(self):
        while self.grid.count():
            it = self.grid.takeAt(0)
            w = it.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()
        self.rows = {}


class Toast(QLabel):
    """Short message with a coloured left edge (mapper toasts)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ppToast")
        self.setWordWrap(True)
        self.hide()
        self._t = QTimer(self)
        self._t.setSingleShot(True)
        self._t.timeout.connect(self.hide)

    def show_message(self, text, level="ok", ms=6000):
        self.setText(text)
        self.setProperty("level", level)
        self.style().unpolish(self)
        self.style().polish(self)
        self.show()
        self._t.start(ms)


class Logo(QLabel):
    def __init__(self, height=40, parent=None):
        super().__init__(parent)
        pm = QPixmap(theme.LOGO)
        if not pm.isNull():
            self.setPixmap(pm.scaledToHeight(height, Qt.TransformationMode.SmoothTransformation))


class DockTitle(QWidget):
    """Dark title bar for PosPro's dock panels, painted here (the platform
    draws the standard one light). Drag it to move or dock the panel, double-
    click to float; buttons: float/dock and close."""

    def __init__(self, dock, title):
        super().__init__(dock)
        self.dock = dock
        self.title = title
        self.hover = None
        self.setMouseTracking(True)
        self.setFixedHeight(26)

    def _btns(self):
        w = self.width()
        return {"close": QRectF(w - 26, 3, 20, 20), "float": QRectF(w - 50, 3, 20, 20)}

    def paintEvent(self, ev):       # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.fillRect(self.rect(), QColor(theme.BG))
        p.setPen(QPen(QColor(theme.BORDER), 1))
        p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        p.setPen(QColor(theme.TEXT))
        p.setFont(theme.font("ui", 12, 700, 106))
        p.drawText(QRectF(10, 0, self.width() - 70, self.height()),
                   _af(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self.title.upper())
        for k, r in self._btns().items():
            if self.hover == k:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(theme.RED) if k == "close" else QColor(theme.SURF2))
                p.drawRoundedRect(r, 3, 3)
            p.setPen(QPen(QColor(theme.BRIGHT if self.hover == k else theme.DIM), 1.4))
            p.setBrush(Qt.BrushStyle.NoBrush)
            c = r.center()
            if k == "close":
                p.drawLine(QPointF(c.x() - 4, c.y() - 4), QPointF(c.x() + 4, c.y() + 4))
                p.drawLine(QPointF(c.x() + 4, c.y() - 4), QPointF(c.x() - 4, c.y() + 4))
            else:
                p.drawRect(QRectF(c.x() - 4, c.y() - 2, 8, 7))
                p.drawLine(QPointF(c.x() - 2, c.y() - 5), QPointF(c.x() + 6, c.y() - 5))
                p.drawLine(QPointF(c.x() + 6, c.y() - 5), QPointF(c.x() + 6, c.y() + 2))
        p.end()

    def _hit(self, pos):
        for k, r in self._btns().items():
            if r.contains(QPointF(pos)):
                return k
        return None

    def mouseMoveEvent(self, ev):   # noqa: N802
        pos = ev.position().toPoint() if hasattr(ev, "position") else ev.pos()
        h = self._hit(pos)
        if h != self.hover:
            self.hover = h
            self.update()
        ev.ignore()

    def leaveEvent(self, ev):       # noqa: N802
        self.hover = None
        self.update()

    def mousePressEvent(self, ev):  # noqa: N802
        pos = ev.position().toPoint() if hasattr(ev, "position") else ev.pos()
        h = self._hit(pos)
        if h == "close":
            self.dock.close()
            ev.accept()
        elif h == "float":
            self.dock.setFloating(not self.dock.isFloating())
            ev.accept()
        else:
            ev.ignore()

    def sizeHint(self):             # noqa: N802
        return QSize(200, 26)
