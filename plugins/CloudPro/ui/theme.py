"""The DCS look comes from DCS Core (dcs_core.theme), shared by PosPro,
AlignPro and CloudPro, so a fix there reaches every plugin. This module adds
what only CloudPro's views need (view background, fonts for the painted
views, the main-window tool bars and status bar) on top of it."""
from dcs_core import theme as _t
from dcs_core.theme import (ACCENT, AMBER, BG, BLUE, BORDER, BRIGHT, DIM, FIELD, MAPLE, RED, SURF, SURF2,  # noqa: F401
                            TEXT, VIOLET, dark_menu, font, mark, msg_info, msg_question, msg_warn, polish)

VIEW_BG = (9, 16, 15)
UI = _t.UI
BODY = _t.BODY
MONO = _t.MONO

EXTRA_QSS = """
QWidget {{ background: transparent; }}
QMainWindow#ppRoot {{ background: {BG}; }}
QToolBar {{ background: {SURF}; border: none; border-bottom: 1px solid {BORDER}; spacing: 3px; padding: 2px; }}
QToolBar QToolButton {{ border: 1px solid transparent; padding: 3px 7px; font-size: 13px; }}
QToolBar QToolButton:hover {{ border-color: {BORDER}; background: {SURF2}; }}
QToolBar QToolButton:checked {{ background: rgba(0,229,170,40); border-color: {ACCENT}; color: {BRIGHT}; }}
QToolBar QLabel {{ font-family: '{UI}'; font-weight: 700; color: {DIM}; }}
QPushButton:checked {{ background: rgba(0,229,170,45); border-color: {ACCENT}; color: {BRIGHT}; }}
QStatusBar {{ background: {SURF}; color: {TEXT}; border-top: 1px solid {BORDER}; font-family: '{MONO}'; font-size: 12px; }}
QStatusBar QLabel {{ font-family: '{MONO}'; font-size: 12px; }}
QSplitter::handle {{ background: {BORDER}; }}
QPushButton#ppDanger {{ border-color: {RED}; color: {RED}; }}
"""


def qss():
    return _t.qss() + EXTRA_QSS.format(BG=BG, SURF=SURF, SURF2=SURF2, BORDER=BORDER, ACCENT=ACCENT, BRIGHT=BRIGHT,
                                       DIM=DIM, TEXT=TEXT, RED=RED, UI=_t.UI, MONO=_t.MONO)


def load_fonts():
    _t.load_fonts()


def apply(root):
    """The DCS theme on the CloudPro main window and everything under it."""
    _t.load_fonts()
    root.setObjectName("ppRoot")
    root.setAutoFillBackground(True)
    root.setPalette(_t.palette())
    root.setStyleSheet(qss())
    _t.mark(root)
    _t.polish(root)


def fusionize(w):
    _t.polish(w)


def polish_dialog(dlg):
    dlg.setPalette(_t.palette())
    dlg.setStyleSheet(qss())
    dlg.setObjectName("ppRoot")
    try:
        _t.mark(dlg)
        _t.polish(dlg)
    except Exception:          # noqa: BLE001
        pass


def mono(size=11):
    return _t.font("mono", size)


def ui_font(size=12, bold=True):
    return _t.font("ui", size, 700 if bold else 500)
