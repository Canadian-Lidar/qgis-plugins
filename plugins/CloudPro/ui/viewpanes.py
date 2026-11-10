"""The view panes: each view with one title strip (CloudPro 2.0.0).

The colour scheme, colour link, background grid, colour ramp and sizes are chosen from the right-click
menu on the view's legend (MainWindow._legend_menu); the panes keep them as hidden combos (colour, link,
grid) so the window reads them as before.

  PlanPane   plan view: zoom to the Inspection Area, zoom to the project, rejected points eye
  SlicePane  one slice view for both cross-sections and profiles: direction, zoom, eye, rotate 90 deg,
             WIDTH (0.5 to 16 m), first / previous, position slider, next / last, "slice n / N".
             The slices are whole slices of that width inside the Inspection Area, centred in it
             (never past its edges).
  View3DPane 3D view: what to show, presets, eye-dome lighting, zoom, eye
"""
import math

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
    """A view's or panel's title (PLAN, SLICE, 3D, AUTOMATIC FILTERS ...): all the same size and colour (2.0.3: the
    views' titles had the small ppKey style)."""
    lab = QtWidgets.QLabel(text)
    lab.setObjectName("cpViewTitle")
    f = theme.ui_font(13)
    lab.setFont(f)
    lab.setStyleSheet("color: %s; letter-spacing: 1px;" % theme.ACCENT)
    return lab


class _Pane(QtWidgets.QWidget):
    colourChanged = Signal(str)
    legendToggled = Signal(bool)
    linkChanged = Signal(str)            # "" = own colours, else "plan" / "slice" / "3d"
    rejToggled = Signal(bool)            # this view's "show rejected points" eye
    zoomArea = Signal()                  # zoom this view to the Inspection Area
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
        self.colour.setParent(self)
        self.colour.hide()                       # chosen from the legend's right-click menu
        self.colour.currentTextChanged.connect(self.colourChanged.emit)
        lay.addWidget(self.head)

    def add_zoom_area(self):
        """The 'zoom to the Inspection Area' button (zoomArea signal)."""
        self.zoom_area_btn = chrome.IconButton("zoom_area", "Zoom to the Inspection Area", size=28)
        self.zoom_area_btn.clicked.connect(self.zoomArea.emit)
        self.hl.addWidget(self.zoom_area_btn)

    def add_colour(self):
        self.rej_btn = chrome.IconButton("", options=[("hide", "eye_off", "Rejected points hidden in this view "
                                                                         "(click to show them in magenta)"),
                                                      ("show", "eye", "Rejected points shown in magenta in this view "
                                                                      "(click to hide them)")], size=28)
        self.rej_btn.optionChanged.connect(lambda k: self.rejToggled.emit(k == "show"))
        self.hl.addWidget(self.rej_btn)
        # the legend is always shown: right-clicking it is where the colours are chosen
        self.legend_btn = chrome.IconButton("legend", "Colour scale bar", checkable=True, size=28)
        self.legend_btn.setChecked(True)
        self.legend_btn.setParent(self)
        self.legend_btn.hide()
        self.link = QtWidgets.QComboBox(self)
        self.link.hide()
        self.link.setToolTip("Link this view's colours (scheme and scale) to another view")
        self.link.addItem("Own", "")
        for k, n in (("plan", "= Plan"), ("slice", "= Slice"), ("3d", "= 3D")):
            if k != self.KEY:
                self.link.addItem(n, k)
        self.link.currentIndexChanged.connect(lambda _i: self.linkChanged.emit(self.link.currentData() or ""))

    def set_show_rejected(self, on):
        """Set the eye without signalling (the tool bar's eye sets every view)."""
        self.rej_btn.set_key("show" if on else "hide")

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
        self.grid.setParent(self)
        self.grid.hide()                         # chosen from the legend's right-click menu
        self.target = dspin(1, 100000, DEFAULT_TARGET, 10, 0, " pts/m²")
        self.target.setFixedWidth(118)
        self.target.setToolTip("Coverage: the density the survey needs. Cells at or above it are green, "
                               "from half of it amber, below half red")
        self.target.setVisible(False)
        self.hl.addWidget(self.target)
        self.add_zoom_area()
        self.zoom_ext_btn = chrome.IconButton("zoom_extents", "Zoom to the extents of the whole project", size=28)
        self.zoom_ext_btn.clicked.connect(self.view.zoom_project)
        self.hl.addWidget(self.zoom_ext_btn)
        self.add_colour()
        self.layout().addWidget(self.view, 1)


WIDTHS = (0.5, 1.0, 2.0, 4.0, 8.0, 16.0)


def slice_plan(extent, width):
    """(n, width used, first centre) of whole slices across an extent (m), centred in it: n = the number of whole
    slices of that width that fit (at least 1; a slice wider than the extent is cut to it)."""
    extent = max(1e-6, float(extent))
    w = min(float(width), extent)
    n = max(1, int(math.floor(extent / w + 1e-9)))
    margin = (extent - n * w) / 2
    return n, w, -extent / 2 + margin + w / 2


class SlicePane(_Pane):
    """Slice view: a slab of the Inspection Area seen from the side. The slices have the width chosen in WIDTH and
    are whole slices inside the area, centred in it; the slider, arrows and keys step one slice (« » the first /
    last)."""
    KEY = "slice"
    sliceChanged = Signal()
    stepRequested = Signal(int)        # -1 previous, +1 next

    def __init__(self, parent=None):
        super().__init__("SLICE", parent)
        self.view = CloudView2D("section")
        self.view.title = ""
        self.view.b_label = "Z"
        self.extent = 60.0
        self.index = 0
        hl = self.hl
        hl.removeItem(hl.itemAt(hl.count() - 1))                # the stretch after the title
        self.dir = chrome.IconButton("", options=[
            ("across", "across", "Cross-section: across the Inspection Area, looking along it"),
            ("along", "along", "Profile: along the Inspection Area, seen from the side")], size=28)
        self.dir.optionChanged.connect(lambda _k: self._dir_changed())
        hl.addWidget(self.dir)
        self.add_zoom_area()
        self.add_colour()
        self.rot = chrome.IconButton("rotate", "Rotate the slice 90 degrees (cross-section <-> profile)", size=28)
        self.rot.clicked.connect(self._rotate)
        hl.addWidget(self.rot)
        hl.addStretch(1)
        hl.addWidget(chrome.label("WIDTH"))
        self.width_cb = QtWidgets.QComboBox()
        for w in WIDTHS:
            self.width_cb.addItem(("%g m" % w), w)
        self.width_cb.setCurrentIndex(1)
        self.width_cb.setToolTip("Slice width: the slices are whole slices of this width inside the Inspection Area")
        self.width_cb.setFixedWidth(84)
        self.width_cb.currentIndexChanged.connect(lambda _i: self._width_changed())
        hl.addWidget(self.width_cb)
        self.first = chrome.IconButton("first", "First slice of the Inspection Area (Ctrl+PgDn)", size=28)
        self.prev = chrome.IconButton("prev", "Previous slice (PgDn or [)", size=28)
        self.next = chrome.IconButton("next", "Next slice (PgUp or ])", size=28)
        self.last = chrome.IconButton("last", "Last slice of the Inspection Area (Ctrl+PgUp)", size=28)
        self.first.clicked.connect(lambda: self.go(0))
        self.prev.clicked.connect(lambda: self.stepRequested.emit(-1))
        self.next.clicked.connect(lambda: self.stepRequested.emit(1))
        self.last.clicked.connect(lambda: self.go(self.count() - 1))
        self.pos = QtWidgets.QSlider(Qt.Orientation.Horizontal)
        self.pos.setRange(0, 0)
        self.pos.setPageStep(1)
        self.pos.setToolTip("Slice in the Inspection Area")
        self.pos.valueChanged.connect(self._slider)
        self.lbl = QtWidgets.QLabel("1 / 1")
        self.lbl.setObjectName("ppVal")
        self.lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl.setMinimumWidth(78)
        self.lbl.setToolTip("Slice number of the slices in the Inspection Area at this width")
        hl.addWidget(self.first)
        hl.addWidget(self.prev)
        hl.addWidget(self.pos, 2)
        hl.addWidget(self.next)
        hl.addWidget(self.last)
        hl.addWidget(self.lbl)
        self.layout().addWidget(self.view, 1)
        self._blk = False
        self.index = self.count() // 2               # the middle slice until the window places it
        self._sync()

    # state ------------------------------------------------------------------
    @property
    def along(self):
        return self.dir.key == "along"

    def width(self):                                     # noqa: D401 - the chosen slice width (m)
        return float(self.width_cb.currentData() or 1.0)

    def set_width(self, w):
        i = min(range(len(WIDTHS)), key=lambda k: abs(WIDTHS[k] - float(w)))
        self.width_cb.setCurrentIndex(i)

    def plan(self):
        return slice_plan(self.extent, self.width())

    def count(self):
        return self.plan()[0]

    def thickness(self):
        """The slice width used (the chosen width, or the extent when the area is narrower)."""
        return self.plan()[1]

    def centre(self, i=None):
        n, w, c0 = self.plan()
        i = self.index if i is None else i
        return c0 + max(0, min(n - 1, i)) * w

    def _sync(self):
        n = self.count()
        self.index = max(0, min(n - 1, self.index))
        self._blk, old = True, self._blk
        self.pos.setRange(0, n - 1)
        self.pos.setValue(self.index)
        self._blk = old
        self.lbl.setText("%d / %d" % (self.index + 1, n))
        self._c = self.centre()                       # where the slice is, for a width change
        for b, on in ((self.first, self.index > 0), (self.prev, self.index > 0), (self.next, self.index < n - 1),
                      (self.last, self.index < n - 1)):
            b.setEnabled(on)

    def set_extent(self, extent):
        """The Inspection Area's length (cross-sections) or width (profiles) changed: keep the slice nearest the
        same place."""
        c = self.centre()
        self.extent = max(1e-6, float(extent))
        self._nearest(c)

    def _nearest(self, c):
        n, w, c0 = self.plan()
        self.index = int(max(0, min(n - 1, round((c - c0) / w))))
        self._sync()

    def go(self, i):
        n = self.count()
        i = max(0, min(n - 1, int(i)))
        if i != self.index:
            self.index = i
            self._sync()
            self._changed()
        else:
            self._sync()

    def _slider(self, v):
        if not self._blk:
            self.go(v)

    def _rotate(self):
        self.dir.set_key("across" if self.along else "along")
        self._dir_changed()

    def _dir_changed(self):
        self._changed()

    def _width_changed(self):
        self._nearest(getattr(self, "_c", 0.0))
        self._changed()

    def _changed(self):
        if not self._blk:
            self.sliceChanged.emit()

    def position(self, half=None):
        """Centre of the current slice, in metres from the box centre."""
        return self.centre()

    def set_position(self, val, half=None):
        """Go to the slice nearest val (m from the box centre)."""
        if half is not None:
            self.extent = max(1e-6, 2 * float(half))
        self._blk = True
        self._nearest(float(val))
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
        self.add_zoom_area()
        self.add_colour()
        self.layout().addWidget(self.view, 1)

    def _edl(self, on):
        self.view.edl = on
        self.view._dirty = True
        self.view.update()
