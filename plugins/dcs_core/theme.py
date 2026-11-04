"""Mobile Mapper look for the Canadian Lidar DCS plugins: the same dark palette, mint accent, maple
red, and Rajdhani / Outfit / Source Code Pro typefaces as the Canadian Lidar
Mobile Mapper dashboard (mapper/static/app.css), so the two read as one
system.

The style sheet is applied to PosPro's own panels only, never to QGIS.
"""
import os

from qgis.PyQt.QtGui import QColor, QFont, QFontDatabase, QPalette

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(HERE, "fonts")
ICON_DIR = os.path.join(HERE, "icons")
LOGO = os.path.join(ICON_DIR, "logo.png")

# design tokens: neutral graphite (DCS Core 1.2.0) so coloured points, tracks and classes stand out;
# accent, status and solution colours are the Mobile Mapper's (app.css)
BG = "#26292d"
SURF = "#2e3237"
SURF2 = "#363b41"
BORDER = "#464c53"
ACCENT = "#00e5aa"
GREEN = ACCENT
RED = "#e84855"
AMBER = "#f5a623"
BLUE = "#3ab4f2"
MAPLE = "#c8102e"
TEXT = "#c9cfd4"
BRIGHT = "#e3e7ea"
DIM = "#a0a9b1"            # quiet text (4.5:1 or better on every surface)
FAINT = "#6f777f"          # lines and marks that are not text
DISABLED = "#5c636a"
VIOLET = "#9b7bff"
FIELD = "#1f2226"          # input fields, a step darker than the window
VIEW_BG = "#2a2d31"        # 3D, slice, plan, point views, plots and map canvases
GRID = "#3a3f45"           # grid lines in those views

LEVEL = {"ok": GREEN, "warn": AMBER, "bad": RED, "off": DIM, "busy": BLUE}

UI = "Rajdhani"
BODY = "Outfit"
MONO = "Source Code Pro"
_loaded = False


def load_fonts():
    """Register the bundled typefaces once (OFL, see fonts/OFL.txt)."""
    global _loaded, UI, BODY, MONO
    if _loaded:
        return
    _loaded = True
    fams = set()
    if os.path.isdir(FONT_DIR):
        for f in sorted(os.listdir(FONT_DIR)):
            if f.lower().endswith((".ttf", ".otf")):
                i = QFontDatabase.addApplicationFont(os.path.join(FONT_DIR, f))
                if i >= 0:
                    fams.update(QFontDatabase.applicationFontFamilies(i))
    # fall back gracefully when a face could not be loaded
    if "Rajdhani" not in fams:
        UI = "Segoe UI"
    if "Outfit" not in fams:
        BODY = "Segoe UI"
    if "Source Code Pro" not in fams:
        MONO = "Consolas"


def font(kind="body", px=13, weight=None, spacing=None):
    """QFont in one of the three roles. spacing = letter spacing in percent."""
    load_fonts()
    fam = {"ui": UI, "body": BODY, "mono": MONO}[kind]
    f = QFont(fam)
    f.setPixelSize(int(px))
    if weight is None:
        weight = {"ui": 700, "body": 400, "mono": 400}[kind]
    try:
        f.setWeight(QFont.Weight(weight))
    except (TypeError, ValueError):
        f.setWeight({300: 25, 400: 50, 500: 57, 600: 63, 700: 75}.get(weight, 50))
    if spacing:
        f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, spacing)
    return f


def palette():
    p = QPalette()
    for role, col in ((QPalette.ColorRole.Window, SURF), (QPalette.ColorRole.Base, FIELD),
                      (QPalette.ColorRole.AlternateBase, SURF2), (QPalette.ColorRole.WindowText, TEXT),
                      (QPalette.ColorRole.Text, BRIGHT), (QPalette.ColorRole.Button, SURF2),
                      (QPalette.ColorRole.ButtonText, TEXT), (QPalette.ColorRole.Highlight, ACCENT),
                      (QPalette.ColorRole.HighlightedText, "#04130f"), (QPalette.ColorRole.ToolTipBase, SURF2),
                      (QPalette.ColorRole.ToolTipText, BRIGHT), (QPalette.ColorRole.Mid, BORDER),
                      (QPalette.ColorRole.Dark, DIM), (QPalette.ColorRole.Light, SURF2),
                      (QPalette.ColorRole.PlaceholderText, DIM), (QPalette.ColorRole.BrightText, "#ffffff")):
        p.setColor(role, QColor(col))
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor("#5c636a"))
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor("#5c636a"))
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor("#5c636a"))
    return p


def _u(path):
    return path.replace("\\", "/")


def qss():
    load_fonts()
    tog_on, tog_off = _u(os.path.join(ICON_DIR, "tog_on.svg")), _u(os.path.join(ICON_DIR, "tog_off.svg"))
    rad_on, rad_off = _u(os.path.join(ICON_DIR, "radio_on.svg")), _u(os.path.join(ICON_DIR, "radio_off.svg"))
    caret = _u(os.path.join(ICON_DIR, "caret.svg"))
    return """
QWidget {{ font-family: '{BODY}'; font-size: 13px; color: {TEXT}; }}
QWidget#ppRoot {{ background: {BG}; }}
QWidget#ppHeader {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #2a1418, stop:0.45 {SURF}, stop:1 {SURF});
                    border-bottom: 1px solid {BORDER}; }}
QLabel#ppBrandTitle {{ font-family: '{UI}'; font-size: 16px; font-weight: 700; color: #ffffff; }}
QLabel#ppBrandSub {{ font-family: '{UI}'; font-size: 14px; font-weight: 700; color: {ACCENT}; }}
QToolButton#ppProject {{ background: rgba(0,229,170,13); border: 1.5px solid {GREEN}; border-radius: 3px;
                         padding: 5px 14px; font-family: '{UI}'; font-size: 14px; font-weight: 700; color: {GREEN}; }}
QToolButton#ppProject:hover {{ background: rgba(0,229,170,30); }}
QToolButton#ppProject::menu-indicator {{ image: none; width: 0; }}
QToolButton#ppWinBtn {{ background: transparent; border: 1px solid {BORDER}; border-radius: 3px; color: {TEXT};
    font-size: 17px; }}
QToolButton#ppWinBtn:hover {{ border-color: {ACCENT}; color: {ACCENT}; }}
QToolButton#ppWinBtn::menu-indicator {{ image: none; width: 0; }}
QToolButton::menu-indicator {{ image: none; width: 0; }}
QToolButton#ppProject[busy="true"] {{ border-color: {RED}; color: #ff8d96; background: rgba(232,72,85,20); }}
QWidget#ppSidebar {{ background: {BG}; border-right: 1px solid {BORDER}; }}
QWidget#ppContent, QScrollArea, QScrollArea > QWidget > QWidget {{ background: {BG}; }}
QStackedWidget {{ background: {BG}; }}

QLabel#ppTitle {{ font-family: '{UI}'; font-size: 18px; font-weight: 700; color: #ffffff; padding: 2px 0 4px 0; }}
QLabel#ppHint {{ color: {DIM}; font-size: 12px; }}
QLabel#ppKey {{ font-family: '{UI}'; font-size: 11px; font-weight: 600; color: {DIM}; }}
QLabel#ppVal {{ font-family: '{MONO}'; font-size: 13px; color: {BRIGHT}; }}
QLabel#ppVal[level="ok"] {{ color: {GREEN}; }}
QLabel#ppVal[level="warn"] {{ color: {AMBER}; }}
QLabel#ppVal[level="bad"] {{ color: {RED}; }}

QGroupBox {{ background: {SURF}; border: 1px solid {BORDER}; border-radius: 3px; margin-top: 22px;
             padding: 12px 10px 10px 10px; font-family: '{UI}'; font-size: 15px; font-weight: 700; color: #ffffff; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; left: 2px; top: 2px; padding: 0 4px;
                    color: #ffffff; background: transparent; }}
QFrame#ppCard {{ background: {SURF}; border: 1px solid {BORDER}; border-radius: 3px; }}
QFrame#ppSect {{ background: {SURF}; border: 1px solid {BORDER}; border-radius: 3px; }}
QToolButton#ppSectHead {{ background: transparent; border: none; padding: 8px 6px; font-family: '{UI}';
                          font-size: 15px; font-weight: 700; color: #ffffff; text-align: left; }}

QPushButton {{ font-family: '{UI}'; font-size: 13px; font-weight: 700; color: {TEXT}; background: transparent;
               border: 1px solid {BORDER}; border-radius: 2px; padding: 6px 14px; }}
QPushButton:hover {{ border-color: {DIM}; color: {BRIGHT}; }}
QPushButton:disabled {{ color: #5c636a; border-color: #00000000; }}
QPushButton#ppPrimary {{ background: {GREEN}; color: #04130f; border: 1px solid {GREEN}; padding: 8px 20px; font-size: 14px; }}
QPushButton#ppPrimary:hover {{ background: #35ffc9; border-color: #35ffc9; }}
QPushButton#ppPrimary:disabled {{ background: #3c4248; color: #7c858d; border-color: #3c4248; }}
QPushButton#ppStop {{ color: {RED}; border-color: {RED}; }}
QPushButton#ppStop:hover {{ background: rgba(232,72,85,36); }}
QToolButton {{ font-family: '{UI}'; font-weight: 700; color: {TEXT}; background: transparent;
               border: 1px solid {BORDER}; border-radius: 2px; padding: 4px 8px; }}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QDateTimeEdit, QPlainTextEdit, QListWidget, QTableWidget {{
    font-family: '{MONO}'; font-size: 12px; color: {BRIGHT}; background: {FIELD}; border: 1px solid {BORDER};
    border-radius: 2px; selection-background-color: rgba(0,229,170,70); selection-color: #ffffff; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QDateTimeEdit {{ padding: 4px 6px; min-height: 18px; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus, QDateTimeEdit:focus,
QPlainTextEdit:focus {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox::down-arrow {{ image: url({caret}); width: 10px; height: 7px; }}
QComboBox QAbstractItemView {{ background: {SURF}; color: {BRIGHT}; border: 1px solid {BORDER};
                               selection-background-color: rgba(0,229,170,50); }}
QSpinBox::up-button, QDoubleSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::down-button,
QDateTimeEdit::up-button, QDateTimeEdit::down-button {{ width: 0; border: none; }}
QListWidget::item:selected, QTableWidget::item:selected {{ background: rgba(0,229,170,45); color: #ffffff; }}
QHeaderView {{ background-color: {SURF2}; border: none; }}
QHeaderView::section {{ background-color: {SURF2}; color: {TEXT}; border: none; border-right: 1px solid {BORDER};
                        border-bottom: 1px solid {BORDER}; padding: 5px 8px; font-family: '{UI}'; font-size: 12px;
                        font-weight: 700; }}
QHeaderView::section:hover {{ background-color: {SURF2}; color: {BRIGHT}; }}
QAbstractItemView {{ background-color: {FIELD}; color: {BRIGHT}; alternate-background-color: {SURF2}; }}
QTableWidget {{ gridline-color: {BORDER}; }}
QTableCornerButton::section {{ background: {SURF2}; border: none; }}

QCheckBox, QRadioButton {{ font-family: '{UI}'; font-size: 13px; font-weight: 600; color: {TEXT}; spacing: 10px; }}
QCheckBox::indicator {{ width: 30px; height: 16px; image: url({tog_off}); }}
QCheckBox::indicator:checked {{ image: url({tog_on}); }}
QCheckBox::indicator:disabled {{ image: url({tog_off}); }}
QRadioButton::indicator {{ width: 14px; height: 14px; image: url({rad_off}); }}
QRadioButton::indicator:checked {{ image: url({rad_on}); }}

QProgressBar {{ background: {FIELD}; border: 1px solid {BORDER}; border-radius: 2px; height: 10px;
                font-family: '{MONO}'; font-size: 10px; color: {BRIGHT}; text-align: center; }}
QProgressBar::chunk {{ background: {ACCENT}; }}
QScrollBar:vertical {{ background: {BG}; width: 8px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {DIM}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar:horizontal {{ background: {BG}; height: 8px; }}
QScrollBar::handle:horizontal {{ background: {BORDER}; border-radius: 4px; }}
QToolTip {{ background: {SURF2}; color: {BRIGHT}; border: 1px solid {BORDER}; font-family: '{BODY}'; }}
QMenu {{ background-color: {SURF}; color: {BRIGHT}; border: 1px solid {DIM}; font-family: '{UI}'; font-size: 14px;
         font-weight: 600; padding: 4px 0; }}
QMenu::item {{ background-color: transparent; color: {BRIGHT}; padding: 7px 22px; }}
QMenu::item:selected {{ background-color: rgba(0,229,170,45); color: #ffffff; }}
QMenu::item:disabled {{ color: #6f777f; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 10px; }}
QDialog, QMessageBox, QInputDialog, QProgressDialog {{ background-color: {SURF}; }}
QDialog QLabel, QMessageBox QLabel, QInputDialog QLabel {{ color: {BRIGHT}; font-size: 13px; }}
QDialogButtonBox QPushButton {{ min-width: 80px; }}
QDockWidget {{ color: {BRIGHT}; font-family: '{UI}'; font-size: 13px; font-weight: 700; }}
QDockWidget::title {{ background-color: {SURF}; padding: 6px 8px; border-bottom: 1px solid {BORDER}; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; background: {BG}; }}
QTabBar::tab {{ background: transparent; color: {DIM}; padding: 6px 12px; font-family: '{UI}'; font-size: 12px;
                font-weight: 700; border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: {ACCENT}; border-bottom-color: {ACCENT}; }}
QLabel#ppToast {{ background: {SURF2}; color: {BRIGHT}; border: 1px solid {BORDER}; border-left: 3px solid {ACCENT};
                  border-radius: 2px; padding: 6px 10px; }}
QLabel#ppToast[level="warn"] {{ border-left-color: {AMBER}; }}
QLabel#ppToast[level="bad"] {{ border-left-color: {RED}; }}
QLabel#ppStatus {{ color: {DIM}; font-size: 12px; }}
""".format(BODY=BODY, UI=UI, MONO=MONO, BG=BG, SURF=SURF, SURF2=SURF2, BORDER=BORDER, ACCENT=ACCENT, GREEN=GREEN,
           RED=RED, AMBER=AMBER, TEXT=TEXT, BRIGHT=BRIGHT, DIM=DIM, FIELD=FIELD, tog_on=tog_on, tog_off=tog_off,
           rad_on=rad_on, rad_off=rad_off, caret=caret)


def apply(widget):
    """Dark palette, fonts and the style sheet on one of PosPro's panels."""
    load_fonts()
    widget.setObjectName("ppRoot")
    widget.setAutoFillBackground(True)
    widget.setPalette(palette())
    widget.setStyleSheet(qss())
    widget.setProperty("ppTheme", True)
    install_guard()


def apply_dock(dock):
    """The style sheet on the dock itself as well, so dialogs and menus opened
    from it (top-level windows) get the dark theme, not dark text on QGIS's
    light dialogs."""
    load_fonts()
    dock.setPalette(palette())
    dock.setStyleSheet(qss())
    dock.setProperty("ppTheme", True)
    install_guard()
    from .widgets import DockTitle
    if not isinstance(dock.titleBarWidget(), DockTitle):
        dock.setTitleBarWidget(DockTitle(dock, dock.windowTitle()))


_FUSION = None


def fusion():
    """Qt's Fusion style, kept alive for the session. The Windows 11 style of
    QGIS 4 ignores style-sheet backgrounds of table headers and the toggle
    images of check boxes; Fusion honours the palette and the style sheet."""
    global _FUSION
    if _FUSION is None:
        from qgis.PyQt.QtWidgets import QStyleFactory
        _FUSION = QStyleFactory.create("Fusion")
    return _FUSION


def polish(root):
    """Use Fusion under the dark style sheet for every widget of a PosPro
    panel, and set palette + sheet on popups and item views directly (some
    Windows styles take their colours from the palette only)."""
    from qgis.PyQt.QtWidgets import QAbstractItemView, QComboBox, QHeaderView, QMenu, QWidget
    pal = palette()
    sheet = qss()
    st = fusion()
    if st is not None:
        root.setStyle(st)
        for w in root.findChildren(QWidget):
            w.setStyle(st)
    for m in root.findChildren(QMenu):
        m.setPalette(pal)
        m.setStyleSheet(sheet)
    for v in root.findChildren(QAbstractItemView):
        v.setPalette(pal)
    for h in root.findChildren(QHeaderView):
        h.setPalette(pal)
        h.setStyleSheet(sheet)
        h.setHighlightSections(False)
    for cb in root.findChildren(QComboBox):
        cb.view().setPalette(pal)
        cb.view().window().setPalette(pal)
    force_paint(root)


# ---------------------------------------------------------------------------
# Painting that no QGIS style can override. QGIS 4 on Windows draws table
# headers, check boxes and combo-box lists with its own style whatever the
# style sheet says, so PosPro paints those three itself.
# ---------------------------------------------------------------------------
_PAINTER = None


def _painter():
    global _PAINTER
    if _PAINTER is None:
        from qgis.PyQt.QtCore import QEvent, QObject, QRect, QRectF, Qt
        from qgis.PyQt.QtGui import QColor, QPainter, QPen
        from qgis.PyQt.QtWidgets import QCheckBox, QComboBox, QHeaderView, QRadioButton

        def af(f):
            try:
                return int(f)
            except TypeError:
                return f.value

        class Painter(QObject):
            def eventFilter(self, obj, ev):     # noqa: N802
                try:
                    if ev.type() != QEvent.Type.Paint:
                        return False
                    if isinstance(obj, (QCheckBox, QRadioButton)):
                        self.paint_check(obj)
                        return True
                    if obj.property("ppTabs"):
                        self.paint_tabs(obj)
                        return True
                    if obj.property("ppScroll"):
                        self.paint_scroll(obj)
                        return True
                    if obj.property("ppProgress"):
                        self.paint_progress(obj)
                        return True
                    if obj.property("ppMenu"):
                        self.paint_menu(obj)
                        return True
                    if isinstance(obj, QComboBox) and not obj.isEditable():
                        self.paint_combo(obj)
                        return True
                    if obj.property("ppCorner"):
                        p = QPainter(obj)
                        p.fillRect(obj.rect(), QColor(SURF2))
                        p.end()
                        return True
                    hv = obj.parent()
                    if isinstance(hv, QHeaderView) and obj is hv.viewport():
                        self.paint_header(hv)
                        return True
                    if obj.property("ppPopup"):
                        p = QPainter(obj)
                        p.fillRect(obj.rect(), QColor(SURF))
                        p.setPen(QPen(QColor(DIM), 1))
                        p.drawRect(obj.rect().adjusted(0, 0, -1, -1))
                        p.end()
                        return True
                    if obj.property("ppViewport"):
                        p = QPainter(obj)
                        p.fillRect(obj.rect(), QColor(SURF))
                        p.end()
                        return False            # the items paint on top
                except RuntimeError:
                    return False
                return False

            def paint_header(self, hv):
                vp = hv.viewport()
                p = QPainter(vp)
                p.fillRect(vp.rect(), QColor(SURF2))
                p.setFont(font("ui", 12, 700, 108))
                horiz = hv.orientation() == Qt.Orientation.Horizontal
                model = hv.model()
                for i in range(hv.count()):
                    if hv.isSectionHidden(i):
                        continue
                    pos, size = hv.sectionViewportPosition(i), hv.sectionSize(i)
                    r = QRect(pos, 0, size, vp.height()) if horiz else QRect(0, pos, vp.width(), size)
                    if not r.intersects(vp.rect()):
                        continue
                    txt = model.headerData(i, hv.orientation(), Qt.ItemDataRole.DisplayRole) if model else ""
                    p.setPen(QColor(TEXT))
                    p.drawText(r.adjusted(6, 0, -6, 0), af(Qt.AlignmentFlag.AlignCenter), str(txt or ""))
                    p.setPen(QPen(QColor(BORDER), 1))
                    if horiz:
                        p.drawLine(r.right(), r.top() + 4, r.right(), r.bottom() - 4)
                    else:
                        p.drawLine(r.left(), r.bottom(), r.right(), r.bottom())
                p.setPen(QPen(QColor(BORDER), 1))
                if horiz:
                    p.drawLine(0, vp.height() - 1, vp.width(), vp.height() - 1)
                p.end()

            def paint_tabs(self, tb):
                p = QPainter(tb)
                p.fillRect(tb.rect(), QColor(BG))
                p.setFont(font("ui", 12, 700, 108))
                cur = tb.currentIndex()
                for i in range(tb.count()):
                    r = tb.tabRect(i)
                    sel = i == cur
                    p.fillRect(r.adjusted(0, 0, -1, 0), QColor(SURF2 if sel else SURF))
                    if sel:
                        p.fillRect(QRect(r.left(), r.bottom() - 2, r.width() - 1, 3), QColor(ACCENT))
                    p.setPen(QColor(BRIGHT if sel else DIM) if tb.isTabEnabled(i) else QColor("#5c636a"))
                    t = p.fontMetrics().elidedText(tb.tabText(i).replace("&", "").upper(),
                                                   Qt.TextElideMode.ElideRight, r.width() - 12)
                    p.drawText(r, af(Qt.AlignmentFlag.AlignCenter), t)
                p.setPen(QPen(QColor(BORDER), 1))
                p.drawLine(0, tb.height() - 1, tb.width(), tb.height() - 1)
                p.end()

            def paint_scroll(self, sb):
                from qgis.PyQt.QtWidgets import QStyle, QStyleOptionSlider
                opt = QStyleOptionSlider()
                opt.initFrom(sb)
                opt.orientation = sb.orientation()
                opt.minimum, opt.maximum = sb.minimum(), sb.maximum()
                opt.sliderPosition = opt.sliderValue = sb.sliderPosition()
                opt.singleStep, opt.pageStep = sb.singleStep(), sb.pageStep()
                opt.upsideDown = sb.invertedAppearance()
                if sb.orientation() == Qt.Orientation.Horizontal:
                    opt.state |= QStyle.StateFlag.State_Horizontal
                st = sb.style()
                h = st.subControlRect(QStyle.ComplexControl.CC_ScrollBar, opt, QStyle.SubControl.SC_ScrollBarSlider, sb)
                p = QPainter(sb)
                p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                p.fillRect(sb.rect(), QColor(BG))
                if sb.maximum() > sb.minimum() and not h.isEmpty():
                    hov = sb.underMouse()
                    c = QColor(ACCENT if hov else "#4a5058")
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(c)
                    r = QRectF(h).adjusted(2, 2, -2, -2)
                    rad = min(r.width(), r.height()) / 2
                    p.drawRoundedRect(r, rad, rad)
                for sc, ch in ((QStyle.SubControl.SC_ScrollBarSubLine,
                                "\u25b4" if sb.orientation() == Qt.Orientation.Vertical else "\u25c2"),
                               (QStyle.SubControl.SC_ScrollBarAddLine,
                                "\u25be" if sb.orientation() == Qt.Orientation.Vertical else "\u25b8")):
                    rr = st.subControlRect(QStyle.ComplexControl.CC_ScrollBar, opt, sc, sb)
                    if rr.width() > 4 and rr.height() > 4:
                        p.setPen(QColor(DIM))
                        p.setFont(font("ui", 10))
                        p.drawText(rr, af(Qt.AlignmentFlag.AlignCenter), ch)
                p.end()

            def paint_progress(self, pb):
                p = QPainter(pb)
                p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                r = QRectF(pb.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
                p.setPen(QPen(QColor(BORDER), 1))
                p.setBrush(QColor(BG))
                p.drawRoundedRect(r, 3, 3)
                lo, hi, v = pb.minimum(), pb.maximum(), pb.value()
                if hi > lo:
                    f = max(0.0, min(1.0, (v - lo) / float(hi - lo)))
                    if f > 0:
                        p.setPen(Qt.PenStyle.NoPen)
                        p.setBrush(QColor(ACCENT))
                        p.drawRoundedRect(QRectF(r.left() + 1, r.top() + 1, (r.width() - 2) * f, r.height() - 2), 2, 2)
                if pb.isTextVisible() and pb.height() >= 14:
                    p.setPen(QColor(BRIGHT))
                    p.setFont(font("mono", 11))
                    p.drawText(pb.rect(), af(Qt.AlignmentFlag.AlignCenter), pb.text())
                p.end()

            def paint_menu(self, m):
                p = QPainter(m)
                r = m.rect()
                p.fillRect(r, QColor(SURF))
                p.setPen(QPen(QColor(BORDER), 1))
                p.drawRect(r.adjusted(0, 0, -1, -1))
                act = m.activeAction()
                f = font("body", 13)
                p.setFont(f)
                for a in m.actions():
                    if not a.isVisible():
                        continue
                    g = m.actionGeometry(a)
                    if g.isEmpty():
                        continue
                    if a.isSeparator():
                        p.setPen(QPen(QColor(BORDER), 1))
                        y = g.center().y()
                        p.drawLine(g.left() + 8, y, g.right() - 8, y)
                        continue
                    en = a.isEnabled()
                    if a is act and en:
                        hl = QColor(ACCENT)
                        hl.setAlpha(45)
                        p.fillRect(g.adjusted(2, 1, -2, -1), hl)
                        p.fillRect(QRect(g.left() + 2, g.top() + 1, 3, g.height() - 2), QColor(ACCENT))
                    col = QColor(BRIGHT if (a is act and en) else TEXT if en else "#5c636a")
                    p.setPen(col)
                    tx = g.left() + 14
                    if a.isCheckable():
                        if a.isChecked():
                            p.setPen(QColor(ACCENT))
                            p.drawText(QRect(g.left() + 6, g.top(), 14, g.height()),
                                       af(Qt.AlignmentFlag.AlignCenter), "\u2713")
                        tx = g.left() + 24
                    p.setPen(col)
                    txt = a.text().replace("&&", "\x00").replace("&", "").replace("\x00", "&")
                    p.drawText(QRect(tx, g.top(), g.width() - tx + g.left() - 24, g.height()),
                               af(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), txt)
                    sc = a.shortcut().toString() if not a.shortcut().isEmpty() else ""
                    if sc:
                        p.setPen(QColor(DIM))
                        p.drawText(QRect(g.left(), g.top(), g.width() - 12, g.height()),
                                   af(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), sc)
                    if a.menu() is not None:
                        p.setPen(QColor(DIM))
                        p.drawText(QRect(g.left(), g.top(), g.width() - 8, g.height()),
                                   af(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), "\u203a")
                p.end()

            def paint_combo(self, cb):
                p = QPainter(cb)
                p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                r = QRectF(0.5, 0.5, cb.width() - 1, cb.height() - 1)
                en = cb.isEnabled()
                p.setBrush(QColor(SURF if en else BG))
                p.setPen(QPen(QColor(ACCENT if cb.hasFocus() else BORDER), 1))
                p.drawRoundedRect(r, 3, 3)
                p.setPen(QColor(BRIGHT if en else "#5c636a"))
                p.setFont(font("mono", 12))
                p.drawText(QRect(8, 0, cb.width() - 30, cb.height()),
                           af(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), cb.currentText())
                x, y = cb.width() - 15, cb.height() / 2 - 2
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(ACCENT if en else DIM))
                from qgis.PyQt.QtCore import QPointF
                from qgis.PyQt.QtGui import QPolygonF
                p.drawPolygon(QPolygonF([QPointF(x - 5, y), QPointF(x + 5, y), QPointF(x, y + 6)]))
                p.end()

            def paint_check(self, w):
                p = QPainter(w)
                p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                on, en = w.isChecked(), w.isEnabled()
                h = w.height()
                if isinstance(w, QRadioButton):
                    c = QRectF(1, h / 2 - 7, 14, 14)
                    p.setPen(QPen(QColor(ACCENT if on else DIM), 1.6))
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.drawEllipse(c)
                    if on:
                        p.setPen(Qt.PenStyle.NoPen)
                        p.setBrush(QColor(ACCENT))
                        p.drawEllipse(c.adjusted(3.6, 3.6, -3.6, -3.6))
                    tx = 24
                else:
                    tr = QRectF(1, h / 2 - 8, 30, 16)
                    p.setPen(Qt.PenStyle.NoPen)
                    track = QColor(ACCENT) if on else QColor("#41474e")
                    if on:
                        track.setAlpha(90)
                    p.setBrush(track)
                    p.drawRoundedRect(tr, 8, 8)
                    knob = QColor(ACCENT if on else "#9aa3ab")
                    if not en:
                        knob.setAlpha(110)
                    p.setBrush(knob)
                    p.drawEllipse(QRectF(tr.left() + (16 if on else 2), tr.top() + 2, 12, 12))
                    tx = 40
                col = QColor(TEXT if en else "#5c636a")
                p.setPen(col)
                p.setFont(font("ui", 13, 600, 106))
                p.drawText(QRect(tx, 0, w.width() - tx, h), af(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                           w.text())
                if w.hasFocus():
                    p.setPen(QPen(QColor(ACCENT), 1, Qt.PenStyle.DotLine))
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.drawRoundedRect(QRectF(0.5, 0.5, w.width() - 1, h - 1), 2, 2)
                p.end()

        _PAINTER = Painter()
    return _PAINTER


def _delegate_class():
    from qgis.PyQt.QtCore import QRect, QSize, Qt
    from qgis.PyQt.QtGui import QColor
    from qgis.PyQt.QtWidgets import QStyle, QStyledItemDelegate

    class DarkDelegate(QStyledItemDelegate):
        def paint(self, p, opt, idx):
            r = opt.rect
            sel = bool(opt.state & QStyle.StateFlag.State_Selected) or bool(opt.state & QStyle.StateFlag.State_MouseOver)
            p.save()
            p.fillRect(r, QColor(0, 229, 170, 55) if sel else QColor(SURF))
            p.setPen(QColor("#ffffff" if sel else BRIGHT))
            p.setFont(font("mono", 12))
            txt = idx.data(Qt.ItemDataRole.DisplayRole)
            try:
                flag = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            except TypeError:
                flag = (Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter).value
            p.drawText(QRect(r.left() + 8, r.top(), r.width() - 12, r.height()), flag, str(txt or ""))
            p.restore()

        def sizeHint(self, opt, idx):       # noqa: N802
            s = super().sizeHint(opt, idx)
            return QSize(s.width() + 16, max(26, s.height()))
    return DarkDelegate


_DELEGATES = []


def _treat_calendar(cw):
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QBrush, QTextCharFormat
    cw.setPalette(palette())
    cw.setStyleSheet(qss() + _CAL_QSS.format(**globals()))
    hf = QTextCharFormat()
    hf.setBackground(QBrush(QColor(SURF2)))
    hf.setForeground(QBrush(QColor(DIM)))
    cw.setHeaderTextFormat(hf)
    wf = QTextCharFormat()
    wf.setForeground(QBrush(QColor(AMBER)))
    for day in (Qt.DayOfWeek.Saturday, Qt.DayOfWeek.Sunday):
        cw.setWeekdayTextFormat(day, wf)


def treat(w):
    """Make one widget readable whatever the platform style does: every kind
    of control that Windows styles draw natively (ignoring palette and style
    sheet) is painted by PosPro itself. Safe to call repeatedly."""
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtWidgets import (QAbstractButton, QAbstractItemView, QCalendarWidget, QCheckBox, QComboBox,
                                     QDateTimeEdit, QHeaderView, QMenu, QProgressBar, QRadioButton, QScrollBar,
                                     QTableView)
    if w is None or w.property("ppDone"):
        return
    pt = _painter()
    if isinstance(w, QHeaderView):
        w.setProperty("ppDone", True)
        w.setPalette(palette())
        w.setHighlightSections(False)
        w.viewport().installEventFilter(pt)
        if w.orientation() == Qt.Orientation.Horizontal:
            w.setMinimumHeight(max(w.minimumHeight(), 28))
        w.viewport().update()
        return
    if isinstance(w, QMenu):
        w.setProperty("ppDone", True)
        dark_menu(w)
        return
    if isinstance(w, (QCheckBox, QRadioButton)):
        w.setProperty("ppDone", True)
        w.installEventFilter(pt)
        w.setMinimumWidth(w.sizeHint().width() + 30)
        w.setMinimumHeight(max(w.minimumHeight(), 22))
        return
    from qgis.PyQt.QtWidgets import QTabBar, QTabWidget
    if isinstance(w, QTabWidget):
        w.setProperty("ppDone", True)
        w.setDocumentMode(True)
        return
    if isinstance(w, QTabBar):
        w.setProperty("ppDone", True)
        w.setProperty("ppTabs", True)
        w.setDrawBase(False)
        w.setUsesScrollButtons(False)
        w.setElideMode(Qt.TextElideMode.ElideRight)
        w.installEventFilter(pt)
        return
    if isinstance(w, QScrollBar):
        w.setProperty("ppDone", True)
        w.setProperty("ppScroll", True)
        w.installEventFilter(pt)
        return
    if isinstance(w, QProgressBar):
        w.setProperty("ppDone", True)
        w.setProperty("ppProgress", True)
        w.installEventFilter(pt)
        return
    if isinstance(w, QCalendarWidget):
        w.setProperty("ppDone", True)
        _treat_calendar(w)
        return
    if isinstance(w, QDateTimeEdit):
        w.setProperty("ppDone", True)
        if w.calendarPopup() and w.calendarWidget() is not None:
            treat(w.calendarWidget())
            force_paint(w.calendarWidget())
        return
    if isinstance(w, QComboBox):
        w.setProperty("ppDone", True)
        if not w.isEditable():
            w.installEventFilter(pt)
        v = w.view()
        d = _delegate_class()(v)
        _DELEGATES.append(d)
        v.setItemDelegate(d)
        v.setPalette(palette())
        v.viewport().setProperty("ppViewport", True)
        v.viewport().installEventFilter(pt)
        win = v.window()
        if win is not None and win is not w.window():
            win.setPalette(palette())
            win.setProperty("ppPopup", True)
            win.installEventFilter(pt)
        force_paint(v)          # its scroll bars
        return
    if isinstance(w, QAbstractButton) and isinstance(w.parent(), QTableView) and \
            w.metaObject().className() == "QTableCornerButton":
        w.setProperty("ppDone", True)
        w.setProperty("ppCorner", True)
        w.installEventFilter(pt)
        return
    if isinstance(w, QAbstractItemView):
        w.setProperty("ppDone", True)
        w.setPalette(palette())
        return


def force_paint(root):
    """treat() root and every widget under it."""
    from qgis.PyQt.QtWidgets import QWidget
    treat(root)
    for w in root.findChildren(QWidget):
        treat(w)


# ---------------------------------------------------------------------------
# The guard: an application event filter that treats every widget belonging
# to a PosPro window the moment it is polished or shown, so controls created
# later (refreshed tables, popups, dialogs, menus, scroll bars) are covered
# without each page having to remember to call polish() again.
# ---------------------------------------------------------------------------
_GUARD = None


def _ours(w):
    k = 0
    while w is not None and k < 40:
        try:
            if w.property("ppTheme"):
                return True
            w = w.parentWidget()
        except RuntimeError:
            return False
        k += 1
    return False


def mark(w):
    """Declare w (a PosPro window or panel) and everything under it as PosPro's."""
    w.setProperty("ppTheme", True)
    install_guard()
    force_paint(w)


def install_guard():
    global _GUARD
    if _GUARD is not None:
        return _GUARD
    from qgis.PyQt.QtCore import QEvent, QObject
    from qgis.PyQt.QtWidgets import QApplication, QWidget
    types = (QEvent.Type.Polish, QEvent.Type.Show)

    class Guard(QObject):
        def eventFilter(self, obj, ev):     # noqa: N802
            try:
                if ev.type() in types and isinstance(obj, QWidget) and not obj.property("ppDone") and _ours(obj):
                    treat(obj)
            except Exception:       # noqa: BLE001 - never break QGIS's event loop
                pass
            return False
    _GUARD = Guard()
    app = QApplication.instance()
    if app is not None:
        app.installEventFilter(_GUARD)
    return _GUARD


def remove_guard():
    global _GUARD
    if _GUARD is None:
        return
    from qgis.PyQt.QtWidgets import QApplication
    app = QApplication.instance()
    if app is not None:
        app.removeEventFilter(_GUARD)
    _GUARD = None


_CAL_QSS = """
QCalendarWidget QWidget {{ background: {SURF}; color: {BRIGHT}; }}
QCalendarWidget QAbstractItemView {{ background: {SURF}; color: {BRIGHT}; selection-background-color: {ACCENT};
    selection-color: {BG}; }}
QCalendarWidget QAbstractItemView:disabled {{ color: #5c636a; }}
QCalendarWidget QToolButton {{ background: {SURF2}; color: {BRIGHT}; border: none; padding: 4px 8px; }}
QCalendarWidget QSpinBox {{ background: {SURF2}; color: {BRIGHT}; }}
"""


def dark_menu(m):
    """PosPro paints this menu itself (background, items, hover, separators):
    Windows styles draw menu panels natively and ignore palette and sheet."""
    if m is None or m.property("ppMenu"):
        return m
    m.setProperty("ppMenu", True)
    m.installEventFilter(_painter())
    m.setMinimumWidth(max(m.minimumWidth(), 200))
    try:
        from qgis.PyQt.QtCore import Qt as _Qt
        m.setAttribute(_Qt.WidgetAttribute.WA_TranslucentBackground, False)
        m.setWindowFlags(m.windowFlags() | _Qt.WindowType.NoDropShadowWindowHint)
    except Exception:       # noqa: BLE001
        pass
    for a in m.actions():
        if a.menu() is not None:
            dark_menu(a.menu())
    m.aboutToShow.connect(lambda mm=m: [dark_menu(a.menu()) for a in mm.actions() if a.menu() is not None])
    return m


def caps(widget, spacing=112):
    """Mapper labels and buttons are upper case with wide letter spacing."""
    try:
        t = widget.text()
        if t:
            widget.setText(t.upper())
    except AttributeError:
        pass
    f = widget.font()
    f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, spacing)
    widget.setFont(f)


def mapperize(root):
    """Upper-case the titles, buttons and form labels of a page, as the
    mapper does, without touching the user's values."""
    from qgis.PyQt.QtWidgets import QCheckBox, QFormLayout, QGroupBox, QLabel, QPushButton, QRadioButton
    for g in root.findChildren(QGroupBox):
        g.setTitle(g.title().upper())
    for b in root.findChildren(QPushButton):
        if b.text() and len(b.text()) > 1 and not b.property("keepcase"):
            caps(b, 112)
    from qgis.PyQt.QtWidgets import QToolButton
    for b in root.findChildren(QToolButton):
        if b.objectName() not in ("ppProject", "ppSectHead") and b.text() and len(b.text()) > 2:
            caps(b, 110)
    for b in root.findChildren(QCheckBox) + root.findChildren(QRadioButton):
        if not b.property("keepcase"):
            caps(b, 106)
    for lab in root.findChildren(QLabel):
        if lab.objectName() == "ppTitle":
            caps(lab, 108)
    for fl in root.findChildren(QFormLayout):
        for r in range(fl.rowCount()):
            it = fl.itemAt(r, QFormLayout.ItemRole.LabelRole)
            w = it.widget() if it else None
            if isinstance(w, QLabel) and w.text():
                w.setObjectName("ppKey")
                caps(w, 114)


# ---------------------------------------------------------------------------
# message boxes and input dialogs in the PosPro look (Fusion + dark sheet), with
# the same arguments as the QMessageBox / QInputDialog static functions
# ---------------------------------------------------------------------------
def _dark(dlg):
    dlg.setPalette(palette())
    dlg.setStyleSheet(qss())
    dlg.setProperty("ppTheme", True)
    install_guard()
    try:
        force_paint(dlg)
    except Exception:           # noqa: BLE001
        pass
    st = fusion()
    if st is not None:
        from qgis.PyQt.QtWidgets import QWidget
        dlg.setStyle(st)
        for w in dlg.findChildren(QWidget):
            w.setStyle(st)
    return dlg


def _box(icon, parent, title, text, buttons=None, default=None):
    from qgis.PyQt.QtWidgets import QMessageBox
    if buttons is None:
        buttons = QMessageBox.StandardButton.Ok
    mb = QMessageBox(icon, title, text, buttons, parent)
    if default is not None:
        mb.setDefaultButton(default)
    _dark(mb)
    r = mb.exec()
    try:
        return QMessageBox.StandardButton(r)
    except (TypeError, ValueError):
        return r


def msg_info(parent, title, text, buttons=None, default=None):
    from qgis.PyQt.QtWidgets import QMessageBox
    return _box(QMessageBox.Icon.Information, parent, title, text, buttons, default)


def msg_warn(parent, title, text, buttons=None, default=None):
    from qgis.PyQt.QtWidgets import QMessageBox
    return _box(QMessageBox.Icon.Warning, parent, title, text, buttons, default)


def msg_question(parent, title, text, buttons=None, default=None):
    from qgis.PyQt.QtWidgets import QMessageBox
    if buttons is None:
        buttons = QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
    return _box(QMessageBox.Icon.Question, parent, title, text, buttons, default)


def get_text(parent, title, label, echo=None, text=""):
    from qgis.PyQt.QtWidgets import QDialog, QInputDialog
    d = QInputDialog(parent)
    d.setWindowTitle(title)
    d.setLabelText(label)
    if echo is not None:
        d.setTextEchoMode(echo)
    d.setTextValue(text)
    _dark(d)
    ok = d.exec() == QDialog.DialogCode.Accepted
    return d.textValue(), ok


def get_item(parent, title, label, items, current=0, editable=False):
    from qgis.PyQt.QtWidgets import QDialog, QInputDialog
    d = QInputDialog(parent)
    d.setWindowTitle(title)
    d.setLabelText(label)
    d.setComboBoxItems(list(items))
    d.setComboBoxEditable(editable)
    if items:
        d.setTextValue(list(items)[max(0, min(current, len(items) - 1))])
    _dark(d)
    ok = d.exec() == QDialog.DialogCode.Accepted
    return d.textValue(), ok
