"""The four screen quarters' view panes: each view with its own header strip
(title, colour scheme for that view, and its view tools).

  PlanPane   plan view + colour, background grid attribute
  SlicePane  one slice view for both cross-sections and profiles, with the
             slice tool bar on top: direction, rotate 90 deg, whole area,
             thickness, step, previous / next, position slider
  View3DPane 3D view + colour, what to show, presets, eye-dome lighting
"""
from ..qt import QtCore, QtWidgets, Qt, Signal
from . import chrome, render, theme
from .navpanel import dspin
from .planview import ATTRS, DEFAULT_TARGET, PlanView
from .view2d import CloudView2D
from .view3d import CloudView3D

COLOUR_MODES = ["Height", "Intensity", "Intensity (range normalised)", "Classification", "Line", "File", "Return",
                "Channel", "Time", "Range"]


def _combo(items, tip, width=None):
    c = QtWidgets.QComboBox()
    c.addItems(items)
    c.setToolTip(tip)
    if width:
        c.setFixedWidth(width)
    return c


def _title(text):
    lab = QtWidgets.QLabel(text)
    lab.setObjectName("ppKey")
    f = theme.ui_font(13)
    lab.setFont(f)
    lab.setStyleSheet("color: %s; letter-spacing: 1px;" % theme.ACCENT)
    return lab


class _Pane(QtWidgets.QWidget):
    colourChanged = Signal(str)
    legendToggled = Signal(bool)
    linkChanged = Signal(str)            # "" = own colours, else "plan" / "slice" / "3d"
    KEY = ""

    def __init__(self, title, parent=None):
        super().__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.head = chrome.Strip(self)
        self.head.setFixedHeight(34)
        self.hl = QtWidgets.QHBoxLayout(self.head)
        self.hl.setContentsMargins(8, 2, 6, 2)
        self.hl.setSpacing(4)
        self.title = _title(title)
        self.hl.addWidget(self.title)
        self.hl.addStretch(1)
        self.colour = _combo(COLOUR_MODES, "Point colours in this view")
        self.colour.setFixedWidth(170)
        self.colour.currentTextChanged.connect(self.colourChanged.emit)
        lay.addWidget(self.head)

    def add_colour(self):
        self.hl.addWidget(chrome.label("COLOUR"))
        self.hl.addWidget(self.colour)
        self.legend_btn = chrome.IconButton("legend", "Colour scale bar of this view on / off", checkable=True, size=28)
        self.legend_btn.setChecked(True)
        self.legend_btn.toggled.connect(self.legendToggled.emit)
        self.hl.addWidget(self.legend_btn)
        self.link = QtWidgets.QComboBox()
        self.link.setToolTip("Link this view's colours (scheme and scale) to another view")
        self.link.addItem("Own", "")
        for k, n in (("plan", "= Plan"), ("slice", "= Slice"), ("3d", "= 3D")):
            if k != self.KEY:
                self.link.addItem(n, k)
        self.link.setFixedWidth(84)
        self.link.currentIndexChanged.connect(lambda _i: self.linkChanged.emit(self.link.currentData() or ""))
        self.hl.addWidget(icons_label())
        self.hl.addWidget(self.link)

    def set_linked(self, key, mode=None):
        """Follow another view's colour scheme (the combo is greyed out while linked)."""
        self.colour.setEnabled(not key)
        if mode is not None and self.colour.currentText() != mode:
            self.colour.blockSignals(True)
            self.colour.setCurrentText(mode)
            self.colour.blockSignals(False)


def icons_label():
    return chrome.label("LINK")


class PlanPane(_Pane):
    KEY = "plan"

    def __init__(self, parent=None):
        super().__init__("PLAN", parent)
        self.view = PlanView()
        self.view.title = ""
        self.grid = _combo(ATTRS, "Background grid of the whole project under the points", 130)
        self.hl.addWidget(chrome.label("GRID"))
        self.hl.addWidget(self.grid)
        self.target = dspin(1, 100000, DEFAULT_TARGET, 10, 0, " pts/m²")
        self.target.setFixedWidth(118)
        self.target.setToolTip("Coverage: the density the survey needs. Cells at or above it are green, "
                               "from half of it amber, below half red")
        self.target.setVisible(False)
        self.hl.addWidget(self.target)
        self.add_colour()
        self.layout().addWidget(self.view, 1)


class SlicePane(_Pane):
    """Slice view: a thin slab of the Inspection Area seen from the side."""
    KEY = "slice"
    sliceChanged = Signal()
    stepRequested = Signal(int)        # -1 previous, +1 next

    def __init__(self, parent=None):
        super().__init__("SLICE", parent)
        self.view = CloudView2D("section")
        self.view.title = ""
        self.view.b_label = "Z"
        self.add_colour()
        bar = chrome.Strip(self, theme.BG)
        bar.setFixedHeight(36)
        bl = QtWidgets.QHBoxLayout(bar)
        bl.setContentsMargins(6, 2, 6, 2)
        bl.setSpacing(4)
        self.dir = chrome.IconButton("", options=[
            ("across", "across", "Cross-section: across the Inspection Area, looking along it"),
            ("along", "along", "Profile: along the Inspection Area, seen from the side")], size=30)
        self.dir.optionChanged.connect(lambda _k: self._changed())
        self.rot = chrome.IconButton("rotate", "Rotate the slice 90 degrees (cross-section <-> profile)")
        self.rot.clicked.connect(self._rotate)
        self.full = chrome.IconButton("full", "Slice covers the whole Inspection Area (a profile of the full width)",
                                      checkable=True)
        self.full.toggled.connect(lambda _on: self._changed())
        self.thick = dspin(0.01, 1000, 0.5, 0.1, 2, " m")
        self.thick.setToolTip("Slice thickness")
        self.thick.setFixedWidth(92)
        self.step = dspin(0.001, 1000, 0.5, 0.1, 2, " m")
        self.step.setToolTip("Step for previous / next (PgUp / PgDn, [ ]). 'Step = thickness' keeps them equal")
        self.step.setFixedWidth(92)
        self.step_eq = chrome.IconButton("copy", "Step = thickness", checkable=True)
        self.step_eq.setChecked(True)
        self.prev = chrome.IconButton("prev", "Previous slice (PgDn or [)")
        self.next = chrome.IconButton("next", "Next slice (PgUp or ])")
        self.prev.clicked.connect(lambda: self.stepRequested.emit(-1))
        self.next.clicked.connect(lambda: self.stepRequested.emit(1))
        self.pos = QtWidgets.QSlider(Qt.Orientation.Horizontal)
        self.pos.setRange(0, 1000)
        self.pos.setValue(500)
        self.pos.setToolTip("Slice position in the Inspection Area")
        self.pos.valueChanged.connect(lambda _v: self._changed())
        self.lbl = QtWidgets.QLabel("")
        self.lbl.setObjectName("ppVal")
        self.lbl.setMinimumWidth(120)
        for w in (self.dir, self.rot, self.full):
            bl.addWidget(w)
        bl.addWidget(chrome.label("THICK"))
        bl.addWidget(self.thick)
        bl.addWidget(chrome.label("STEP"))
        bl.addWidget(self.step)
        bl.addWidget(self.step_eq)
        bl.addWidget(self.prev)
        bl.addWidget(self.pos, 1)
        bl.addWidget(self.next)
        bl.addWidget(self.lbl)
        self.thick.valueChanged.connect(self._thick)
        self.layout().addWidget(bar)
        self.layout().addWidget(self.view, 1)
        self._blk = False

    # state ------------------------------------------------------------------
    @property
    def along(self):
        return self.dir.key == "along"

    def _rotate(self):
        self.dir.set_key("across" if self.along else "along")
        self._changed()

    def _thick(self, v):
        if self.step_eq.isChecked():
            self.step.setValue(v)
        self._changed()

    def _changed(self):
        if not self._blk:
            self.sliceChanged.emit()

    def position(self, half):
        """Slice centre from the slider, in metres from the box centre (half = half extent)."""
        return (self.pos.value() / 1000.0 * 2 - 1) * half

    def set_position(self, val, half):
        self._blk = True
        self.pos.setValue(int(round((val / max(half, 1e-9) + 1) / 2 * 1000)))
        self._blk = False


class View3DPane(_Pane):
    KEY = "3d"
    def __init__(self, parent=None):
        super().__init__("3D", parent)
        self.view = CloudView3D()
        self.view.title = ""
        self.what = _combo(["Inspection Area", "Slice"], "Points shown in 3D", 150)
        self.hl.addWidget(self.what)
        for name, key, tip in (("top", "top", "Top view (1)"), ("along", "along", "Look across: see the side (2)"),
                               ("across", "across", "Look along the area (3)"), ("iso", "iso", "Oblique (4)")):
            b = chrome.IconButton(name, tip, size=28)
            b.clicked.connect(lambda _=False, k=key: self.view.preset(k))
            self.hl.addWidget(b)
        self.edl = chrome.IconButton("edl", "Eye-dome lighting (depth shading)", checkable=True, size=28)
        self.edl.setChecked(True)
        self.edl.toggled.connect(self._edl)
        self.hl.addWidget(self.edl)
        self.add_colour()
        self.layout().addWidget(self.view, 1)

    def _edl(self, on):
        self.view.edl = on
        self.view._dirty = True
        self.view.update()
