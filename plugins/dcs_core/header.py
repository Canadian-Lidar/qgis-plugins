"""The header bar every DCS plugin shows, laid out the same way in all of them.

    [logo] DATA SYSTEMS   [01 PosPro]→[02 AlignPro]→[03 CloudPro]   …extras…   [PROJECT ▾] [⤢] [?]
           PosPro

- left: the logo, the menu group (menu.GROUP, upper case) and the plugin's name as written
  ("PosPro", "AlignPro", "CloudPro"), then the workflow boxes (StepBar) beside them;
- right: any plugin-specific controls (add_extra), the project box, the Window button (the
  standard window's menu) and the user guide button "?", always in that order.
When the header is narrower than WRAP_WIDTH (a docked window), the workflow boxes move to a
second row under it so nothing is squeezed.
"""
from qgis.PyQt.QtCore import QRectF, Qt
from qgis.PyQt.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen
from qgis.PyQt.QtWidgets import QHBoxLayout, QLabel, QMenu, QToolButton, QVBoxLayout, QWidget

from . import menu as dcs_menu
from . import theme
from .stepbar import StepBar
from .widgets import Logo

WRAP_WIDTH = 1080
CHAIN_WIDTH = 480          # the same in every plugin


class PluginHeader(QWidget):
    """key: the plugin's stage key ("pospro", ...); name: its name as written; window: its
    dcs_core.window.PluginWindow (for the Window button); guide: called by the "?" button;
    window_extras: [(label, callable)] added to the Window menu."""

    def __init__(self, key, name, window=None, guide=None, window_extras=(), parent=None):
        super().__init__(parent)
        self.setObjectName("ppHeaderBar")
        self.key = key
        self.name = name
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 8, 12, 8)
        outer.setSpacing(6)
        row = QHBoxLayout()
        row.setSpacing(12)
        outer.addLayout(row)
        row.addWidget(Logo(42))
        brand = QVBoxLayout()
        brand.setSpacing(1)
        self.group_label = QLabel(dcs_menu.GROUP)
        self.group_label.setObjectName("ppBrandTitle")
        theme.caps(self.group_label, 107)
        self.name_label = QLabel(name)           # the name as written: PosPro, AlignPro, CloudPro
        self.name_label.setObjectName("ppBrandSub")
        f = self.name_label.font()
        f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 104)
        self.name_label.setFont(f)
        brand.addWidget(self.group_label)
        brand.addWidget(self.name_label)
        row.addLayout(brand)
        row.addSpacing(10)
        self.chain = StepBar(key)
        self.chain.setFixedWidth(CHAIN_WIDTH)
        self._chain_row = row
        self._chain_index = row.count()
        row.addWidget(self.chain)
        row.addStretch(1)
        self._extras_at = row.count()
        self.project_btn = QToolButton()
        self.project_btn.setObjectName("ppProject")
        self.project_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.project_btn.setMinimumWidth(170)
        row.addWidget(self.project_btn)
        self.window_btn = QToolButton()
        self.window_btn.setObjectName("ppWinBtn")
        self.window_btn.setText("⤢")
        self.window_btn.setFixedSize(34, 34)
        if window is not None:
            window.attach_menu(self.window_btn, list(window_extras))
        else:
            self.window_btn.hide()
        row.addWidget(self.window_btn)
        self.help_btn = QToolButton()
        self.help_btn.setObjectName("ppWinBtn")
        self.help_btn.setText("?")
        self.help_btn.setFixedSize(34, 34)
        self.help_btn.setToolTip("User guide for this version of %s (PDF)" % name)
        if guide is not None:
            self.help_btn.clicked.connect(guide)
        row.addWidget(self.help_btn)
        self._row2 = QHBoxLayout()
        self._row2.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(self._row2)
        self._wrapped = False
        self.set_project_name(None)

    # ------------------------------------------------------------------ content
    def add_extra(self, widget, stretch=0):
        """A plugin-specific control, placed right of the workflow boxes, left of the project box."""
        self._chain_row.insertWidget(self._extras_at, widget, stretch)
        self._extras_at += 1
        return widget

    def set_project_menu(self, m):
        theme.dark_menu(m)
        self.project_btn.setMenu(m)
        return m

    def new_project_menu(self):
        return self.set_project_menu(QMenu(self.project_btn))

    def set_project_name(self, name):
        self.project_btn.setText(((name or "No project").upper()) + "  ▾")
        f = self.project_btn.font()
        f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 110)
        self.project_btn.setFont(f)

    def set_busy(self, on):
        b = self.project_btn
        b.setProperty("busy", bool(on))
        b.style().unpolish(b)
        b.style().polish(b)

    # ------------------------------------------------------------------ layout
    def resizeEvent(self, ev):                   # noqa: N802
        super().resizeEvent(ev)
        wrap = self.width() < WRAP_WIDTH
        if wrap != self._wrapped:
            self._wrapped = wrap
            if wrap:
                self._chain_row.removeWidget(self.chain)
                self.chain.setMinimumWidth(0)
                self.chain.setMaximumWidth(16777215)
                self._row2.addWidget(self.chain)
            else:
                self._row2.removeWidget(self.chain)
                self.chain.setFixedWidth(CHAIN_WIDTH)
                self._chain_row.insertWidget(self._chain_index, self.chain)

    def paintEvent(self, ev):                    # noqa: N802
        # painted, not styled: the platform style on Windows would otherwise draw it light
        p = QPainter(self)
        g = QLinearGradient(0, 0, self.width(), self.height())
        g.setColorAt(0.0, QColor("#2a1418"))
        g.setColorAt(0.45, QColor(theme.SURF))
        g.setColorAt(1.0, QColor(theme.SURF))
        p.fillRect(QRectF(self.rect()), g)
        p.setPen(QPen(QColor(theme.BORDER), 1))
        p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        p.end()
