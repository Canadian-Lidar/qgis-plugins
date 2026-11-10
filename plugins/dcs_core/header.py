"""The header bar every DCS plugin shows, laid out the same way in all of them (DCS Core 1.11.0).

    [logo] DATA SYSTEMS   PosPro › AlignPro › CloudPro      [PROJECT ▾]  tools…            [?]
           CloudPro v2.0  GEOREFERENCE › CLEAN & CLASSIFY › FINALIZE

- left: the logo, the menu group and the plugin's name and version; beside them two rows of
  breadcrumbs: the stages of the chain (StepBar) and the plugin's own steps (set_steps);
- then the project box (its menu holds the plugin's menu too), then the plugin's tools (add_tool),
  and the user guide button "?" at the right.
The window is a normal window (the system's title bar maximises, minimises and closes it): there is
no Window button and no docking. The steps' guidance sentences are in the breadcrumbs' tooltips.
"""
import configparser
import os
import sys

from qgis.PyQt.QtCore import QRectF, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen
from qgis.PyQt.QtWidgets import QHBoxLayout, QLabel, QMenu, QToolButton, QVBoxLayout, QWidget

from . import menu as dcs_menu
from . import theme
from .stepbar import Crumbs, StepBar
from .widgets import Logo



def plugin_version(path):
    """The version in the metadata.txt of the plugin that holds path (a module file), or ""."""
    d = os.path.dirname(os.path.abspath(path)) if path else ""
    for _ in range(4):
        md = os.path.join(d, "metadata.txt")
        if os.path.isfile(md):
            cp = configparser.ConfigParser(interpolation=None)
            try:
                cp.read(md, encoding="utf-8")
                return cp.get("general", "version", fallback="").strip()
            except (configparser.Error, OSError, UnicodeDecodeError):
                return ""
        up = os.path.dirname(d)
        if not d or up == d:
            break
        d = up
    return ""


class PluginHeader(QWidget):
    """key: the plugin's stage key ("pospro", ...); name: its name as written; guide: called by the "?" button.
    window / window_extras are accepted for older plugin code (the window has no Window menu any more)."""
    stepClicked = pyqtSignal(str)

    def __init__(self, key, name, window=None, guide=None, window_extras=(), parent=None, version=None):
        super().__init__(parent)
        if version is None:
            version = plugin_version(sys._getframe(1).f_globals.get("__file__"))
        self.setObjectName("ppHeaderBar")
        self.key = key
        self.name = name
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 6, 12, 6)
        outer.setSpacing(0)
        row = QHBoxLayout()
        row.setSpacing(12)
        outer.addLayout(row)
        row.addWidget(Logo(40))
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
        self.version = version or ""
        self.version_label = QLabel(("v" + self.version) if self.version else "")
        self.version_label.setObjectName("ppBrandVersion")
        self.version_label.setToolTip("%s version %s" % (name, self.version) if self.version else "")
        self.version_label.setVisible(bool(self.version))
        name_row = QHBoxLayout()
        name_row.setSpacing(6)
        name_row.addWidget(self.name_label)
        name_row.addWidget(self.version_label, 0, Qt.AlignmentFlag.AlignBaseline)
        name_row.addStretch(1)
        brand.addWidget(self.group_label)
        brand.addLayout(name_row)
        row.addLayout(brand)
        self.divider = QWidget()
        self.divider.setFixedWidth(1)
        self.divider.setObjectName("ppHeaderDivider")
        self.divider.setStyleSheet("background: %s;" % theme.BORDER)
        row.addWidget(self.divider)
        crumbs = QVBoxLayout()
        crumbs.setSpacing(2)
        crumbs.setContentsMargins(0, 2, 0, 2)
        self.chain = StepBar(key)
        self.steps = Crumbs(caps=True, px=12)
        self.steps.clicked.connect(self.stepClicked.emit)
        self.steps.hide()
        crumbs.addWidget(self.chain)
        crumbs.addWidget(self.steps)
        crumbs.addStretch(1)
        row.addLayout(crumbs)
        self._chain_row = row
        self._extras_at = row.count()
        row.addSpacing(14)
        self.project_btn = QToolButton()
        self.project_btn.setObjectName("ppProject")
        self.project_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.project_btn.setMinimumWidth(150)
        row.addWidget(self.project_btn)
        row.addSpacing(10)
        self.tools = QHBoxLayout()
        self.tools.setSpacing(2)
        row.addLayout(self.tools)
        row.addStretch(1)
        self.window_btn = QToolButton()          # older plugin code reads it; never shown
        self.window_btn.hide()
        self.help_btn = QToolButton()
        self.help_btn.setObjectName("ppWinBtn")
        self.help_btn.setText("?")
        self.help_btn.setFixedSize(32, 32)
        self.help_btn.setToolTip("User guide for this version of %s (PDF)" % name)
        if guide is not None:
            self.help_btn.clicked.connect(guide)
        row.addWidget(self.help_btn)
        self.set_project_name(None)

    def set_steps(self, items, current=None):
        """The plugin's own steps under the chain: [(key, label, state, tooltip)], state one of done / warn /
        error / todo / running (or the chain's words); current: the key of the step shown now."""
        self.steps.set_items(items, current)
        self.steps.setVisible(bool(items))

    def add_tool(self, widget):
        """A plugin tool (button, separator) right of the project box, in order."""
        self.tools.addWidget(widget)
        return widget

    # ------------------------------------------------------------------ content
    def add_extra(self, widget, stretch=0):
        """A plugin-specific control, placed right of the breadcrumbs, left of the project box."""
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
