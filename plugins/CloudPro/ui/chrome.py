"""Window chrome painted by CloudPro itself, so no platform style (Windows 11
in QGIS 4 draws tool bars, tool buttons and splitter handles natively and
ignores the theme) can turn it white: a dark tool bar that can still be
dragged out to float, icon buttons (plain, toggle, or cycling through
options), "?" help buttons, view header strips and the status strip."""
from ..qt import QtCore, QtGui, QtWidgets, Qt, Signal, exec_
from . import icons, theme


class Strip(QtWidgets.QWidget):
    """A dark bar (header of a view, the status strip); paints its own background."""

    def __init__(self, parent=None, colour=None, line="bottom"):
        super().__init__(parent)
        self._bg = QtGui.QColor(colour or theme.SURF)
        self._line = line
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

    def paintEvent(self, e):
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), self._bg)
        p.setPen(QtGui.QPen(QtGui.QColor(theme.BORDER), 1))
        y = self.height() - 1 if self._line == "bottom" else 0
        if self._line:
            p.drawLine(0, y, self.width(), y)
        p.end()


class HeaderStrip(Strip):
    """The window header: the maple-to-surface gradient of the DCS look, painted."""

    def paintEvent(self, e):
        p = QtGui.QPainter(self)
        g = QtGui.QLinearGradient(0, 0, self.width(), self.height())
        g.setColorAt(0.0, QtGui.QColor("#2a1418"))
        g.setColorAt(0.45, QtGui.QColor(theme.SURF))
        g.setColorAt(1.0, QtGui.QColor(theme.SURF))
        p.fillRect(self.rect(), g)
        p.setPen(QtGui.QPen(QtGui.QColor(theme.BORDER), 1))
        p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        p.end()


class ToolBar(QtWidgets.QToolBar):
    """Dark tool bar (movable, floatable) with a painted grip."""

    def __init__(self, title, parent=None):
        super().__init__(title, parent)
        self.setObjectName("tb_" + title.lower().replace(" ", "_"))
        self.setMovable(True)
        self.setFloatable(True)
        self.setIconSize(QtCore.QSize(20, 20))
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

    def paintEvent(self, e):
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtGui.QColor(theme.SURF))
        p.setPen(QtGui.QPen(QtGui.QColor(theme.BORDER), 1))
        p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QColor(theme.DIM))
        if self.orientation() == Qt.Orientation.Horizontal:
            for k in range(4):
                p.drawEllipse(QtCore.QPointF(5, 10 + k * 5), 1.2, 1.2)
        else:
            for k in range(4):
                p.drawEllipse(QtCore.QPointF(10 + k * 5, 5), 1.2, 1.2)
        p.end()

    def add_sep(self):
        w = QtWidgets.QWidget()
        w.setFixedSize(14, 26)
        w.paintEvent = lambda e, w=w: _sep_paint(w)
        self.addWidget(w)


def _sep_paint(w):
    p = QtGui.QPainter(w)
    p.setPen(QtGui.QPen(QtGui.QColor(theme.BORDER), 1))
    p.drawLine(w.width() // 2, 4, w.width() // 2, w.height() - 4)
    p.end()


class IconButton(QtWidgets.QAbstractButton):
    """Square icon button painted completely here.

    options: list of (key, icon name, tooltip, colour) -> the button cycles
    through them on each click (right click or the small corner arrow shows
    them all). Without options it is a plain or checkable button."""
    optionChanged = Signal(str)

    def __init__(self, name, tip="", checkable=False, options=None, size=32, parent=None, text=None):
        super().__init__(parent)
        self.name = name
        self.text_label = text
        self.setToolTip(tip)
        self.base_tip = tip
        self.setCheckable(checkable)
        self.options = options or []
        self.index = 0
        self.active = False
        self.exclusive = False          # a tool of an exclusive group: first click only activates it
        self.accent = QtGui.QColor(theme.ACCENT)
        self.hover = False
        w = size if not text else max(size, 14 + QtGui.QFontMetrics(theme.ui_font(12)).horizontalAdvance(text) +
                                      (size - 6 if name else 0))
        self.setFixedSize(w, size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        if self.options:
            self.clicked.connect(self._cycle)
            self._update_tip()

    # cycling ---------------------------------------------------------------
    @property
    def key(self):
        return self.options[self.index][0] if self.options else self.name

    def set_key(self, key, emit=False):
        for i, o in enumerate(self.options):
            if o[0] == key:
                self.index = i
                self._update_tip()
                self.update()
                if emit:
                    self.optionChanged.emit(key)
                return

    def _cycle(self):
        """First click on an inactive tool activates it; the next clicks switch its option."""
        if self.exclusive and not self.active:
            self.optionChanged.emit(self.key)
            return
        self.index = (self.index + 1) % len(self.options)
        self._update_tip()
        self.update()
        self.optionChanged.emit(self.key)

    def set_active(self, on):
        self.active = bool(on)
        self.update()

    def _update_tip(self):
        o = self.options[self.index]
        names = "  ·  ".join(x[2].split(":")[0] for x in self.options)
        self.setToolTip("%s\n\nClick again to switch: %s\nCorner arrow or right click: choose from a list"
                        % (o[2], names))

    def corner_rect(self):
        """The bottom-right corner that opens the option list (a third of the button, at least 12 px)."""
        c = max(12, self.height() // 3 + 2)
        return QtCore.QRect(self.width() - c, self.height() - c, c, c)

    def show_options(self, pos=None):
        m = QtWidgets.QMenu(self)
        for i, o in enumerate(self.options):
            a = m.addAction(icons.icon(o[1], 18, o[3] if len(o) > 3 and o[3] else theme.TEXT), o[2])
            a.setCheckable(True)
            a.setChecked(i == self.index)
            a.triggered.connect(lambda _=False, i=i: self._pick(i))
        theme.dark_menu(m)
        pos = pos or self.mapToGlobal(self.rect().bottomLeft())
        exec_(m, pos)

    def mousePressEvent(self, e):
        if self.options and len(self.options) > 1:
            pt = e.position().toPoint() if hasattr(e, "position") else e.pos()
            if e.button() == Qt.MouseButton.RightButton:
                self.show_options(e.globalPosition().toPoint() if hasattr(e, "globalPosition") else e.globalPos())
                return
            if e.button() == Qt.MouseButton.LeftButton and self.corner_rect().contains(pt):
                self.show_options()
                return
        super().mousePressEvent(e)

    def _pick(self, i):
        self.index = i
        self._update_tip()
        self.update()
        self.optionChanged.emit(self.key)

    # painting ----------------------------------------------------------------
    def enterEvent(self, e):
        self.hover = True
        self.update()

    def leaveEvent(self, e):
        self.hover = False
        self.update()

    def paintEvent(self, e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        r = QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1)
        on = self.isChecked() or self.isDown() or self.active
        if on:
            c = QtGui.QColor(self.accent)
            c.setAlpha(46)
            p.setBrush(c)
            p.setPen(QtGui.QPen(self.accent, 1.2))
        elif self.hover and self.isEnabled():
            p.setBrush(QtGui.QColor(theme.SURF2))
            p.setPen(QtGui.QPen(QtGui.QColor(theme.DIM), 1))
        else:
            p.setBrush(Qt.BrushStyle.NoBrush)
            # text buttons (MENU) keep a thin outline so they read as buttons
            p.setPen(QtGui.QPen(QtGui.QColor(theme.BORDER), 1) if self.text_label else Qt.PenStyle.NoPen)
        p.drawRoundedRect(r, 3, 3)
        name = self.name
        col = QtGui.QColor(theme.BRIGHT if (on or self.hover) else theme.TEXT)
        if self.options:
            o = self.options[self.index]
            name = o[1]
            if len(o) > 3 and o[3]:
                col = QtGui.QColor(o[3])
        if not self.isEnabled():
            col = QtGui.QColor("#5c636a")
        h = self.height()
        if name:
            paint_rect = QtCore.QRectF(4, 4, h - 8, h - 8)
            icons.paint(p, name, paint_rect, col)
        if self.text_label:
            p.setPen(col)
            p.setFont(theme.ui_font(12))
            x0 = (h - 4) if name else 7
            p.drawText(QtCore.QRectF(x0, 0, self.width() - x0 - 4, h),
                       int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), self.text_label)
        if self.options and len(self.options) > 1:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QtGui.QColor(theme.DIM if not on else theme.ACCENT))
            w = self.width()
            if self.hover:
                p.setBrush(QtGui.QColor(theme.BRIGHT))
            p.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(w - 2, h - 11), QtCore.QPointF(w - 2, h - 2),
                                           QtCore.QPointF(w - 11, h - 2)]))
        p.end()


class HelpButton(IconButton):
    """'?' that opens a dark explanation box."""

    def __init__(self, title, text, parent=None, size=24):
        super().__init__("help", "What is this?", size=size, parent=parent)
        self.title, self.text = title, text
        self.clicked.connect(self.show_help)

    def show_help(self):
        t = self.text() if callable(self.text) else self.text
        d = QtWidgets.QDialog(self.window())
        d.setWindowTitle(self.title)
        lay = QtWidgets.QVBoxLayout(d)
        lab = QtWidgets.QLabel(t)
        lab.setWordWrap(True)
        lab.setTextFormat(Qt.TextFormat.RichText)
        lab.setOpenExternalLinks(True)
        lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        lab.setMinimumWidth(460)
        lay.addWidget(lab)
        b = QtWidgets.QPushButton("Close")
        b.clicked.connect(d.accept)
        lay.addWidget(b, 0, Qt.AlignmentFlag.AlignRight)
        theme.polish_dialog(d)
        exec_(d)


class Splitter(QtWidgets.QSplitter):
    """Splitter with a painted handle."""

    def createHandle(self):
        return _Handle(self.orientation(), self)


class _Handle(QtWidgets.QSplitterHandle):
    def paintEvent(self, e):
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtGui.QColor(theme.BORDER))
        p.end()


def label(text, obj="ppKey"):
    lab = QtWidgets.QLabel(text)
    lab.setObjectName(obj)
    return lab
