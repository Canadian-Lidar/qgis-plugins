"""CloudPro main window (DCS stage 03).

  header   logo · name · MENU · PROJECT · PROPERTIES ........ chain bar · inputs · update
  tools    pan · selection shape · above/below line · measure · info | action · inside/outside ·
           class · line | undo · redo · rejected · zoom all · sizes   (movable, can float)
  quarters PLAN (+ project grid, drive lines, Inspection Area)  |  3D
           SLICE (cross-section or profile, slice tool bar)     |  AUTOMATIC FILTERS · LINES CLASSES INFO · HISTORY
  status   cursor · counts · messages · progress

Every bar, button and handle is painted by CloudPro (ui/chrome.py) so a
platform style cannot turn it white; the rest of the look comes from DCS
Core."""
import datetime as _dt
import math
import os
import threading
import time
import traceback

import numpy as np

from .. import __version__ as VERSION
from ..core import perflog
from ..core import separation as SEP
from ..core import chain
from ..core import filters as F
from ..core import importer as IM
from ..core.edits import EditManager
from ..core.pointset import PointSet, rect_corners, to_box_frame
from ..core.project import Project
from ..core.store import REJ_AUTO, REJ_MANUAL, REJ_ORIG, REJECTED
from ..qt import QShortcut, QtCore, QtGui, QtWidgets, Qt, Signal, exec_
from . import chrome, render, theme
from .dialogs import ExportDialog, ImportDialog
from .filterpanel import FilterPanel
from .props import AreaProperties, InfoPanel, ProjectProperties
from .planview import ATTRS, SEPARATION
from .viewpanes import COLOUR_MODES, PlanPane, SlicePane, View3DPane

PLUGIN = "CloudPro"
CLASS_SHORTLIST = [1, 2, 3, 4, 5, 6, 7, 9, 11, 14, 18, 64, 65, 66, 67, 68, 69, 70, 71]
def engine_status():
    from ..core import engine
    return engine.status()


SCALAR_MODES = ("Height", "Intensity", "Intensity (range normalised)", "Time", "Range")


class _HeaderTools:
    """The edit tools in DCS Core's header, right of the project box (2.0.0; was a tool bar)."""

    def __init__(self, header):
        self.h = header

    def addWidget(self, w):                     # noqa: N802 - QToolBar's name
        self.h.add_tool(w)
        return w

    def add_sep(self):
        f = QtWidgets.QFrame()
        f.setFixedSize(9, 24)
        f.setStyleSheet("QFrame { border: none; border-left: 1px solid %s; margin-left: 4px; }" % theme.BORDER)
        self.h.add_tool(f)
        return f


def _auto_range(mode, z, i, rng, t):
    """Automatic colour scale of a scalar scheme (z in m, i, rng in cm, t in s)."""
    if mode == "Height":
        return render.auto_range(np.asarray(z, float))
    if mode == "Intensity":
        return render.auto_range(np.asarray(i, float), (1, 99))
    if mode == "Intensity (range normalised)":
        rr = np.maximum(np.asarray(rng, float) * 0.01, 0.5)
        return render.auto_range(np.asarray(i, float) * (rr / 10.0) ** 2, (1, 99))
    if mode == "Time":
        return render.auto_range(np.asarray(t, float), (0, 100))
    return render.auto_range(np.asarray(rng, float) * 0.01, (1, 99))


FILE_COLOURS = np.array([render.hex_rgb(c) for c in ("#00e5aa", "#3ab4f2", "#9b7bff", "#f5a623", "#e84855", "#7bd389",
                                                     "#ff8fab", "#4dd0e1", "#ffd166", "#c792ea", "#80cbc4", "#f78c6c")],
                        np.uint8)


class Bridge(QtCore.QObject):
    progress = Signal(float, str)
    done = Signal(object, object)
    grids = Signal(object)                  # tile keys whose grids a worker thread rebuilt


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, iface=None, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.map_pick = None
        self.setWindowTitle("CloudPro")
        self.resize(1680, 1000)
        self.project = None
        self.edits = None
        self.ps = PointSet(0)
        self.box = None
        self.lut = "Rainbow"
        self.luts = {"plan": "Rainbow", "slice": "Rainbow", "3d": "Rainbow"}   # colour ramp per view (2.0.3)
        self.sizes = {"plan": 2, "slice": 2, "3d": 2}                         # point size per view (2.0.3)
        # rejected points stay in the store (nothing is deleted); each view hides them unless its own
        # "show rejected" eye is on. Hidden by default: views then draw and colour only accepted points.
        self.show_rej = {"plan": False, "slice": False, "3d": False}
        self.tool = "pan"
        self.shape = "rect"
        self.linemode = "above"
        self.action = "reject"
        self.where = "inside"
        self.edit_class = 2
        self.edit_line = -1
        self.line_enabled = {}
        self.class_hidden = set()
        self.file_hidden = set()
        self.track_hidden = set()
        self.file_idx = np.zeros(0, np.int32)
        self.selected = None
        self.preview = None
        self.follow_line = -1
        self.tracks = {}
        self._col_cache = {}
        self._busy = False
        self._busy_tag = None
        self._closing = False
        self._thread = None
        self._locked = []
        self._pending_geo = None
        self._why = None
        self._prev_cancel = None
        self._prev_pending = False
        self._edit_lock = None
        self._props = None
        self._sepwin = None
        self._area = None
        self.bridge = Bridge()
        self.bridge.progress.connect(self._on_progress)
        self.bridge.done.connect(self._on_done)
        self.bridge.grids.connect(self._grids_rebuilt)
        self._grid_worker = None
        self._build()
        theme.apply(self)
        self._grid_timer = QtCore.QTimer(self)
        self._grid_timer.timeout.connect(self._rebuild_dirty_grids)
        self._grid_timer.start(2500)
        self._chain_timer = QtCore.QTimer(self)
        self._chain_timer.timeout.connect(lambda: self.refresh_chain(timer=True))
        self._chain_timer.start(10000)
        self._save_timer = QtCore.QTimer(self)
        self._save_timer.timeout.connect(self._autosave)
        self._save_timer.start(120000)

    # =================================================================== build
    def _build(self):
        self._header()
        self._tools()
        self.plan_pane = PlanPane()
        self.slice_pane = SlicePane()
        self.v3d_pane = View3DPane()
        self.plan = self.plan_pane.view
        self.slice = self.slice_pane.view
        self.v3d = self.v3d_pane.view
        for v in (self.plan, self.slice, self.v3d):
            v.edit_mode = self.action                     # the selection shape's colour (set_mode keeps it)
        # linked cursor (2.1.0): the point under the mouse in one view is marked in the other two
        self._hover = None
        self._hover_timer = QtCore.QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(30)
        self._hover_timer.timeout.connect(self._update_linked)
        self.plan.cursorMoved.connect(lambda a, b: self._hover_at("plan", a, b))
        self.slice.cursorMoved.connect(lambda a, b: self._hover_at("slice", a, b))
        self.v3d.hovered.connect(lambda x, y: self._hover_at("3d", x, y))
        for v in (self.plan, self.slice, self.v3d):
            v.cursorLeft.connect(self._hover_off)
        for v in (self.plan, self.slice):
            v.selectionMade.connect(lambda m, t, vv=v: self.edit_selection(vv, m))
            v.pointPicked.connect(lambda j, vv=v: self.show_point(vv.idx[j] if j >= 0 else -1))
            v.cursorMoved.connect(lambda a, b, vv=v: self._cursor(vv, a, b))
            v.keyNav.connect(self._key_nav)
            v.measured.connect(lambda t: self.status("Measure: " + t, 15000))
            v.toolRequested.connect(self.set_tool)
            v.menu_provider = self._view_menu
        self.v3d.selectionMade.connect(lambda m, t: self.edit_selection(self.v3d, m))
        self.v3d.gpuStatus.connect(self._gpu_status)
        self.v3d.pointPicked.connect(lambda j: self.show_point(self.v3d.idx[j] if j >= 0 else -1))
        self.plan.boxChanged.connect(self._box_from_plan)
        self.plan.slice_lines = self._plan_slice_lines
        self.slice.overlays.append(self._slice_overlay)
        self.panes = {"plan": self.plan_pane, "slice": self.slice_pane, "3d": self.v3d_pane}
        self.links = {"plan": "", "slice": "", "3d": ""}
        # colour scale per view and scheme: ("area",) auto over the Inspection Area (default), ("project",) auto
        # over the whole project, ("manual", lo, hi); a linked view uses the scale of the view it follows
        self.scales = {"plan": {}, "slice": {}, "3d": {}}
        self._proj_ranges = {}
        for k, pane in self.panes.items():
            pane.colourChanged.connect(lambda m, k=k: self._view_colour(k, m))
            pane.legendToggled.connect(lambda _on, k=k: self.refresh_views(which=(k,)))
            pane.linkChanged.connect(lambda t, k=k: self._set_link(k, t))
            pane.rejToggled.connect(lambda on, k=k: self._toggle_view_rej(k, on))
        self.plan_pane.grid.currentTextChanged.connect(self._grid_attr)
        self.plan_pane.target.setValue(self._saved_target())
        self.plan.set_target(self.plan_pane.target.value())
        self.plan.set_tolerance(self._saved_tolerance())
        self.plan_pane.target.valueChanged.connect(self._target)
        self.slice_pane.sliceChanged.connect(self._slice_changed)
        self.slice_pane.stepRequested.connect(lambda d: self.step("sec_next" if d > 0 else "sec_prev"))
        self.v3d_pane.what.currentTextChanged.connect(lambda _t: self.refresh_views(which=("3d",)))
        self.plan_pane.zoomArea.connect(self.plan.zoom_box)
        self.slice_pane.zoomArea.connect(self._zoom_slice_area)
        self.v3d_pane.zoomArea.connect(self.v3d.fit)
        for k, v in (("plan", self.plan), ("slice", self.slice), ("3d", self.v3d)):
            v.legendMenu.connect(lambda gpos, k=k: self._legend_menu(k, gpos))

        self.filters = FilterPanel()
        self.filters.previewRequested.connect(self.run_preview)
        self.filters.previewOff.connect(self.clear_preview)
        self.filters.acceptRequested.connect(self.accept_preview)
        self.filters.runRequested.connect(self.run_scope)
        self.filters.tuneRequested.connect(self.tune_filters)
        self.infop = InfoPanel()
        self.lines_w, self.classes_w, self.info = self.infop.lines_w, self.infop.classes_w, self.infop.info
        self.lines_w.itemChanged.connect(self._line_toggled)
        self.lines_w.customContextMenuRequested.connect(self._lines_menu)
        self.classes_w.itemChanged.connect(self._class_toggled)
        self.history = QtWidgets.QListWidget()
        self.history.setFont(theme.mono(10))
        # every view and panel is its own dock window: move, resize, float, tab together or close
        self.setDockOptions(QtWidgets.QMainWindow.DockOption.AnimatedDocks |
                            QtWidgets.QMainWindow.DockOption.AllowNestedDocks |
                            QtWidgets.QMainWindow.DockOption.AllowTabbedDocks |
                            QtWidgets.QMainWindow.DockOption.GroupedDragging)
        self.setTabPosition(Qt.DockWidgetArea.AllDockWidgetAreas, QtWidgets.QTabWidget.TabPosition.South)
        self.docks = {}
        self._view_vis = {"plan": True, "slice": True, "3d": True}
        self._view_stale = set()
        for key, title, pane in (("plan", "PLAN", self.plan_pane), ("slice", "SLICE", self.slice_pane),
                                 ("3d", "3D", self.v3d_pane)):
            pane.layout().removeWidget(pane.head)           # the pane's header becomes the dock's title bar
            self._make_dock(key, title, pane, pane.head, pane.hl)
        for key, title, w in (("filters", "AUTOMATIC FILTERS", self.filters),
                              ("info", "LINES · CLASSES · INFO", self.infop), ("history", "HISTORY", self.history),
                              ("separation", "LINE SEPARATION", self._sep_panel())):
            head = chrome.Strip()
            head.setFixedHeight(34)
            hl = QtWidgets.QHBoxLayout(head)
            hl.setContentsMargins(8, 2, 6, 2)
            hl.setSpacing(4)
            from .viewpanes import _title
            hl.addWidget(_title(title))
            hl.addStretch(1)
            self._make_dock(key, title, w, head, hl)
        self._default_layout()
        self.top_host = QtWidgets.QWidget()
        tl = QtWidgets.QVBoxLayout(self.top_host)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(0)
        tl.addWidget(self.header)
        self._visited_clean = False             # the workflow is the header's step breadcrumbs (2.0.0)
        self.setMenuWidget(self.top_host)
        sb = QtWidgets.QStatusBar()
        sb.setSizeGripEnabled(False)
        sb.setContentsMargins(0, 0, 0, 0)
        sb.setStyleSheet("QStatusBar { padding: 0; margin: 0; } QStatusBar::item { border: 0; }")
        sb.addWidget(self._status_strip(), 1)
        self.setStatusBar(sb)
        self._restore_layout()
        self._shortcuts()
        self._apply_tool()

    # ------------------------------------------------------------------ dock windows
    LAYOUT_VERSION = 3                          # 2.0.0: views and panels never float; 2.3.0: LINE SEPARATION tab

    def _make_dock(self, key, title, widget, head, head_layout):
        d = QtWidgets.QDockWidget(title, self)
        d.setObjectName("cloudpro_dock_" + key)            # names are what saveState / restoreState match
        # 2.0.0: views and panels stay inside CloudPro's window (no floating, no dragging around)
        d.setFeatures(QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetClosable)
        d.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        d.setWidget(widget)
        cl = chrome.IconButton("close", "Hide this view (project menu > Windows brings it back)", size=26)
        cl.clicked.connect(d.close)
        if key in ("plan", "slice", "3d"):
            # 2.0.3: the view's controls right after its title; only the close button on the right
            for i in reversed(range(head_layout.count())):
                it = head_layout.itemAt(i)
                if it.spacerItem() is not None:
                    head_layout.takeAt(i)
            head_layout.insertSpacing(1, 10)
            if key != "slice":                  # the Slice's slider takes the free space
                head_layout.addStretch(1)
        head_layout.addSpacing(4)
        head_layout.addWidget(cl)
        d.setTitleBarWidget(head)
        if key in self._view_vis:
            d.visibilityChanged.connect(lambda on, k=key: self._dock_visible(k, on))
        self.docks[key] = d
        return d

    def _default_layout(self):
        """Plan over Slice on the left; 3D over the tabbed panels (filters, lines, history, line separation) on the
        right."""
        D = self.docks
        for d in D.values():
            d.setFloating(False)
            d.show()
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, D["plan"])
        self.splitDockWidget(D["plan"], D["slice"], Qt.Orientation.Vertical)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, D["3d"])
        self.splitDockWidget(D["3d"], D["filters"], Qt.Orientation.Vertical)
        self.tabifyDockWidget(D["filters"], D["info"])
        self.tabifyDockWidget(D["info"], D["history"])
        self.tabifyDockWidget(D["history"], D["separation"])
        D["filters"].raise_()
        self.resizeDocks([D["plan"], D["3d"]], [840, 840], Qt.Orientation.Horizontal)
        self.resizeDocks([D["plan"], D["slice"]], [500, 500], Qt.Orientation.Vertical)
        self.resizeDocks([D["3d"], D["filters"]], [500, 500], Qt.Orientation.Vertical)

    def reset_layout(self):
        self._default_layout()
        self.status("Window layout reset", 4000)

    def _restore_layout(self):
        try:
            from qgis.PyQt.QtCore import QSettings
            st = QSettings().value("CloudPro/window_layout")
            if st:
                self.restoreState(st, self.LAYOUT_VERSION)
        except Exception:       # noqa: BLE001  (a layout from another version: keep the default)
            pass
        for d in self.docks.values():
            if d.isFloating():
                d.setFloating(False)

    def save_layout(self):
        try:
            from qgis.PyQt.QtCore import QSettings
            QSettings().setValue("CloudPro/window_layout", self.saveState(self.LAYOUT_VERSION))
        except Exception:       # noqa: BLE001
            pass

    def show_dock(self, key):
        d = self.docks[key]
        d.show()
        d.raise_()

    def _dock_visible(self, key, on):
        """A view that is closed or behind another tab is not drawn or refreshed; it catches up when shown."""
        self._view_vis[key] = on
        if on and key in self._view_stale:
            self._view_stale.discard(key)
            QtCore.QTimer.singleShot(0, lambda: self.refresh_views(which=(key,)))
        self._shortcuts()
        self._apply_tool()

    # ------------------------------------------------------------------ header
    def _header(self):
        """DCS Core's header, the same in every plugin: logo, name, the chain and CloudPro's steps as breadcrumbs,
        the project box (which also holds CloudPro's menu), then the edit tools and the user guide."""
        from dcs_core.header import PluginHeader
        h = PluginHeader("cloudpro", "CloudPro", window=None, guide=self.open_guide)
        self.header = h
        self.chainbar = h.chain
        h.stepClicked.connect(self._step_clicked)
        # the inputs line: only text for the Georeference step's tooltip (2.0.3: kept inside the window and never
        # shown - in 2.0.0-2.0.2 it had no parent and popped up as a small window of its own)
        self.lbl_inputs = QtWidgets.QLabel("", self)
        self.lbl_inputs.hide()
        m = QtWidgets.QMenu(h.project_btn)
        m.addAction("New job project...", self.new_project)
        m.addAction("Open project...", self.open_project)
        self.recent_menu = m.addMenu("Recent")
        self.recent_menu.aboutToShow.connect(self._fill_recent)
        m.addSeparator()
        self._fill_main_menu(m)
        m.addSeparator()
        m.addAction("Open the project folder", self.open_folder)
        m.addAction("Raw data moved? Relocate the job folder...", self.relocate)
        m.addAction("Save the QGIS project in the project folder", self.save_qgis_project)
        m.addAction("Performance report...", self.performance_report)
        h.set_project_menu(m)
        theme.dark_menu(self.recent_menu)
        self.btn_project = h.project_btn
        self.btn_menu = h.project_btn                # the menu is the project box's (2.0.0)
        self.btn_window = h.window_btn
        self.btn_guide = h.help_btn
        return h

    def attach_window(self, panel):
        """Sit inside DCS Core's standard plugin window (a normal window: nothing to attach since 2.0.0)."""
        self._panel = panel

    def open_guide(self):
        from .. import guide
        return guide.open_guide(self)

    def _fill_main_menu(self, m):
        """CloudPro's actions, in the project box's menu (the MENU button before 2.0.0)."""
        for txt, fn, sc in (("Project properties (information, geodetics, survey configuration, files)...",
                             self.show_properties, "Ctrl+R"), (None, None, None),
                            ("Georeference / update the points...", self.georeference, "Ctrl+I"),
                            ("Check line separation...", self.check_separation, ""),
                            ("Finalize for the job (LAS, ground grid, cloud.json)...", self.publish_dialog, "Ctrl+P"),
                            ("Export (LAS, XYZ, grid) anywhere...", self.export, "Ctrl+E"),
                            ("Save", self.save_project, "Ctrl+S"), (None, None, None),
                            ("Pick the Inspection Area on the QGIS map", self.pick_on_map, ""),
                            ("Add the drive lines and Inspection Area to QGIS", self.add_qgis_layers, ""),
                            (None, None, None),
                            ("Add other .clraw files to the job...", self.import_lines, ""),
                            ("Memory limit (points loaded at once)...", self.set_max_load, ""),
                            ("Remove all points (keep settings)...", self.reset_points, ""),
                            ("Windows", "windows", ""),
                            (None, None, None), ("Keys and mouse", self.show_help, "F1"),
                            ("User guide (this version)", self.open_guide, "Shift+F1")):
            if txt is None:
                m.addSeparator()
                continue
            if fn == "windows":
                w = m.addMenu("Windows")
                w.aboutToShow.connect(lambda w=w: self._fill_windows(w))
                theme.dark_menu(w)
                continue
            a = m.addAction(txt, fn)
            if sc:
                a.setShortcut(QtGui.QKeySequence(sc))
        return m

    def _fill_windows(self, w):
        w.clear()
        for k in ("plan", "slice", "3d", "filters", "info", "history", "separation"):
            w.addAction(self.docks[k].toggleViewAction())
        w.addSeparator()
        w.addAction("Reset the window layout", self.reset_layout)

    def _main_menu(self):
        """The project box's menu (kept for keyboard and test code that opened MENU)."""
        self.btn_project.showMenu()

    # ------------------------------------------------------------------ tool bar
    def _tools(self):
        tb = _HeaderTools(self.header)            # 2.0.0: the edit tools sit in the header, right of the project
        self.b_pan = chrome.IconButton("hand", "Pan (Space): drag to move the view; drag the Inspection Area's "
                                               "edge, corners or round handle in the Plan view")
        self.b_shape = chrome.IconButton("", options=[
            ("rect", "rect", "Rectangle: drag a box (G)"),
            ("poly", "poly", "Polygon: click the corners, right click or Enter to finish (P)"),
            ("lasso", "lasso", "Lasso: draw freehand (L)"),
            ("brush", "brush", "Brush: paint over the points (B)")])
        self.b_line = chrome.IconButton("", options=[
            ("above", "above", "Above a line: draw a line in the Slice view; everything above it is edited (U)"),
            ("below", "below", "Below a line: everything below the drawn line is edited (J)")])
        self.b_measure = chrome.IconButton("ruler", "Measure (M): distance, bearing, height difference, slope")
        self.b_info = chrome.IconButton("info", "Point information (I): click a point")
        for b in (self.b_shape, self.b_line):
            b.exclusive = True
        self.b_pan.clicked.connect(lambda: self.set_tool("pan"))
        self.b_shape.optionChanged.connect(lambda k: self.set_tool(k))
        self.b_line.optionChanged.connect(lambda k: self.set_tool(k))
        self.b_measure.clicked.connect(lambda: self.set_tool("measure"))
        self.b_info.clicked.connect(lambda: self.set_tool("pick"))
        for b in (self.b_pan, self.b_shape, self.b_line):
            tb.addWidget(b)
        tb.add_sep()
        self.b_action = chrome.IconButton("", options=[
            ("reject", "reject", "Reject the points (X)", theme.RED),
            ("accept", "accept", "Accept (re-accept) rejected points (V)", theme.ACCENT),
            ("select", "select", "Select the points (then right click: reject, accept, classify, filter)",
             theme.BRIGHT),
            ("class", "classify", "Classify the points with the class chosen next to it (K)", theme.AMBER),
            ("filter", "filter", "Run the current filter group on the points (F)", theme.BLUE)])
        self.b_action.optionChanged.connect(self.set_mode)
        self.b_where = chrome.IconButton("", options=[
            ("inside", "inside", "Edit the points INSIDE the shape"),
            ("outside", "outside", "Edit the visible points OUTSIDE the shape")])
        self.b_where.optionChanged.connect(lambda k: setattr(self, "where", k))
        tb.addWidget(self.b_action)
        tb.addWidget(self.b_where)
        self.cmb_class = QtWidgets.QComboBox()
        for k in CLASS_SHORTLIST:
            self.cmb_class.addItem("%d  %s" % (k, F.ASPRS.get(k, "")), k)
        self.cmb_class.setCurrentIndex(self.cmb_class.findData(2))
        self.cmb_class.setToolTip("Class used by Classify")
        self.cmb_class.setFixedWidth(170)
        self.cmb_class.currentIndexChanged.connect(lambda _: setattr(self, "edit_class", self.cmb_class.currentData()))
        self.a_class = tb.addWidget(self.cmb_class)
        self.a_class.setVisible(False)
        self.cmb_eline = QtWidgets.QComboBox(self)  # one line to edit: chosen in the Lines list (2.0.0)
        self.cmb_eline.addItem("All lines", -1)
        self.cmb_eline.hide()
        self.cmb_eline.currentIndexChanged.connect(lambda _: setattr(self, "edit_line", self.cmb_eline.currentData()))
        tb.add_sep()
        self.b_undo = chrome.IconButton("undo", "Undo (Ctrl+Z)")
        self.b_redo = chrome.IconButton("redo", "Redo (Ctrl+Y)")
        self.b_undo.clicked.connect(self.undo)
        self.b_redo.clicked.connect(self.redo)
        self.b_rej = chrome.IconButton("", options=[("hide", "eye_off", "Rejected points hidden in every view (H). "
                                                                        "Each view has its own eye too."),
                                                    ("show", "eye", "Rejected points shown in magenta in every view (H)")])
        self.b_rej.optionChanged.connect(lambda k: self._toggle_rej(k == "show"))
        self.b_zoom = chrome.IconButton("zoom_area", "Zoom all views to the Inspection Area (Ctrl+Home)")
        self.b_zoom.clicked.connect(self.zoom_all)
        self.b_extents = chrome.IconButton("zoom_extents", "Zoom the Plan view to the whole project")
        self.b_extents.clicked.connect(lambda: self.plan.zoom_project())
        for b in (self.b_undo, self.b_redo):
            tb.addWidget(b)
        tb.add_sep()
        for b in (self.b_measure, self.b_zoom, self.b_extents, self.b_rej):
            tb.addWidget(b)
        self.b_info.setParent(self)                  # Point information: key I (no button since 2.0.0)
        self.b_info.hide()
        # sizes and the colour ramp: chosen from the legends' right-click menus (2.0.0); kept as hidden fields
        self.cb_cell = QtWidgets.QComboBox(self)
        from .planview import PlanView
        for c in PlanView.CELL_CHOICES:
            self.cb_cell.addItem("%g m" % c, c)
        self.cb_cell.setToolTip("Cell size of the project grid under the Plan view (the stored 0.5 m grid summed "
                                "into larger cells)")
        self.cb_cell.currentIndexChanged.connect(lambda i: self._cell(self.cb_cell.itemData(i)))
        self.sp_size = QtWidgets.QSpinBox(self)
        self.sp_size.setRange(1, 6)
        self.sp_size.setValue(2)
        self.sp_size.setToolTip("Point size (pixels)")
        self.sp_size.valueChanged.connect(self._point_size)
        self.sp_brush = QtWidgets.QSpinBox(self)
        self.sp_brush.setRange(2, 200)
        self.sp_brush.setValue(18)
        self.sp_brush.setSuffix(" px")
        self.sp_brush.setToolTip("Brush radius")
        self.sp_brush.valueChanged.connect(lambda v: [setattr(w, "brush_px", v) for w in (self.plan, self.slice)])
        self.cmb_lut = QtWidgets.QComboBox(self)
        self.cmb_lut.addItems(list(render.LUTS))
        self.cmb_lut.setToolTip("Colour ramp for height, intensity, range")
        self.cmb_lut.currentTextChanged.connect(self._lut)
        for w in (self.cb_cell, self.sp_size, self.sp_brush, self.cmb_lut):
            w.hide()
        self.toolbar = tb
        return tb

    def _status_strip(self):
        s = chrome.Strip(line="top")
        s.setFixedHeight(26)
        lay = QtWidgets.QHBoxLayout(s)
        lay.setContentsMargins(8, 0, 8, 0)
        self.lbl_msg = QtWidgets.QLabel("")
        self.lbl_msg.setObjectName("ppHint")
        self.lbl_xyz = QtWidgets.QLabel("")
        self.lbl_xyz.setObjectName("ppVal")
        self.lbl_cnt = QtWidgets.QLabel("")
        self.lbl_cnt.setObjectName("ppVal")
        self.prog = QtWidgets.QProgressBar()
        self.prog.setMaximumWidth(220)
        self.prog.setRange(0, 1000)
        self.prog.hide()
        self.btn_cancel = QtWidgets.QPushButton("Cancel")
        self.btn_cancel.setObjectName("ppStop")
        self.btn_cancel.hide()
        self.btn_cancel.clicked.connect(self._cancel)
        lay.addWidget(self.lbl_msg, 1)
        lay.addWidget(self.lbl_xyz)
        lay.addSpacing(16)
        lay.addWidget(self.lbl_cnt)
        lay.addWidget(self.prog)
        lay.addWidget(self.btn_cancel)
        self._msg_timer = QtCore.QTimer(self)
        self._msg_timer.setSingleShot(True)
        self._msg_timer.timeout.connect(lambda: self.lbl_msg.setText(""))
        return s

    def status(self, msg, ms=6000):
        self.lbl_msg.setText(msg)
        if ms:
            self._msg_timer.start(ms)

    def statusBar(self):                     # older code paths: status messages go to the painted strip
        return _StatusShim(self)

    def _shortcuts(self):
        for seq, fn in (("PgUp", lambda: self.step("sec_next")), ("PgDown", lambda: self.step("sec_prev")),
                        ("Shift+PgUp", lambda: self.step("fwd")), ("Shift+PgDown", lambda: self.step("back")),
                        ("Ctrl+PgUp", lambda: self.step("sec_last")),
                        ("Ctrl+PgDown", lambda: self.step("sec_first")),
                        ("Ctrl+Z", self.undo), ("Ctrl+Y", self.redo), ("Ctrl+Home", self.zoom_all),
                        ("Ctrl+I", self.georeference), ("Ctrl+P", self.publish_dialog), ("Ctrl+E", self.export),
                        ("Ctrl+S", self.save_project), ("F1", self.show_help), ("Ctrl+R", self.show_properties), ("Shift+F1", self.open_guide),
                        ("Space", lambda: self.set_tool("pan")), ("G", lambda: self._shape_key("rect")),
                        ("P", lambda: self._shape_key("poly")), ("L", lambda: self._shape_key("lasso")),
                        ("B", lambda: self._shape_key("brush")), ("U", lambda: self._line_key("above")),
                        ("J", lambda: self._line_key("below")), ("M", lambda: self.set_tool("measure")),
                        ("I", lambda: self.set_tool("pick")), ("X", lambda: self.set_mode("reject")),
                        ("V", lambda: self.set_mode("accept")), ("K", lambda: self.set_mode("class")),
                        ("F", lambda: self.set_mode("filter")), ("S", lambda: self.set_mode("select")),
                        ("H", lambda: self.b_rej.set_key("hide" if any(self.show_rej.values()) else "show", emit=True)),
                        ("Delete", lambda: self.edit_selected("reject")), ("Ctrl+D", self.clear_selection)):
            s = QShortcut(QtGui.QKeySequence(seq), self)
            s.setContext(Qt.ShortcutContext.WindowShortcut)
            s.activated.connect(fn)

    # ------------------------------------------------------------------ tools
    def _shape_key(self, k):
        self.b_shape.set_key(k)
        self.set_tool(k)

    def _line_key(self, k):
        self.b_line.set_key(k)
        self.set_tool(k)

    def set_tool(self, k):
        self.tool = k
        if k in ("rect", "poly", "lasso", "brush"):
            self.shape = k
            self.b_shape.set_key(k)
        if k in ("above", "below"):
            self.linemode = k
            self.b_line.set_key(k)
        self._apply_tool()

    def _apply_tool(self):
        k = self.tool
        self.b_pan.set_active(k == "pan")
        self.b_shape.set_active(k in ("rect", "poly", "lasso", "brush"))
        self.b_line.set_active(k in ("above", "below"))
        self.b_measure.set_active(k == "measure")
        self.b_info.set_active(k == "pick")
        for w in (self.plan, self.slice):
            w.set_tool(k)
        self.v3d.sel_mode = "rect" if k == "rect" else "lasso"

    def set_mode(self, k):
        self.action = k
        self.b_action.set_key(k)
        for v in (self.plan, self.slice, self.v3d):         # selection shapes drawn in the edit's colour
            v.edit_mode = k
            v.update()
        self.a_class.setVisible(k == "class")
        if k == "accept" and not all(self.show_rej.values()):
            self.b_rej.set_key("show", emit=True)          # re-accepting needs the rejected points in sight
        if self.tool in ("pan", "measure", "pick"):
            self.set_tool(self.shape)

    # =============================================================== project
    def new_project(self):
        if self._guard("starting another project"):
            return
        from dcs_core import dialogs
        dcs = dialogs.new_project(self, PLUGIN)
        if dcs is None:
            return
        try:
            p = Project.from_dcs(dcs)
        except Exception as ex:
            self._error("Cannot create the project", ex)
            return
        self.set_project(p)
        if chain.clraw_files(dcs):
            self.georeference()

    def open_project(self, path=None):
        if self._guard("opening another project"):
            return
        if not path:
            from dcs_core import dialogs
            path = dialogs.pick_project_file(self, ";;Older CloudPro project (*.cloudpro.json)")
        if not path:
            return
        bb = getattr(self, "background_busy", None)
        if bb is not None and bb(path):
            # the job's points are being rebuilt in the background: it opens here when that has finished
            if getattr(self, "open_after_background", None) is not None:
                self.open_after_background(path)
            self.status("This job is being georeferenced in the background (progress in the QGIS message bar); "
                        "it opens here when that has finished", 15000)
            return
        if self.project is not None and os.path.abspath(path) in (os.path.abspath(self.project.path),
                                                                  os.path.abspath(getattr(self.project.dcs, "path", ""))):
            self.window().raise_()
            return
        try:
            p = Project.open_any(path)
        except Exception as ex:
            self._error("Cannot open the project", ex)
            return
        if p.dcs is not None:
            from dcs_core import dialogs
            dialogs.check_raw(self, p.dcs, PLUGIN)
        self.set_project(p)

    def _reset_view_state(self):
        """What belongs to one project only (line and class visibility, the edited line, selection,
        preview, followed line): cleared when another project opens."""
        self.clear_preview(refresh=False)
        self.line_enabled = {}
        self.class_hidden = set()
        self.edit_line = -1
        self.follow_line = -1
        self.selected = None
        self._loaded_box = None
        self._why = None
        self.file_idx = np.zeros(0, np.int32)
        self.cmb_eline.blockSignals(True)
        self.cmb_eline.setCurrentIndex(0)
        self.cmb_eline.blockSignals(False)

    def set_project(self, p, reopen=False):
        if self._guard("opening another project") and not reopen:
            return
        same = reopen or (self.project is not None and os.path.abspath(p.path) == os.path.abspath(self.project.path))
        new_lock = None
        if p.dcs is not None and not (same and self._edit_lock is not None):
            # the new project's lock first: when it is taken elsewhere the open project stays as it is
            from dcs_core import locks
            lk = locks.Lock(p.root, "CloudPro editing", PLUGIN, name=".edit.lock")
            try:
                lk.acquire()
                new_lock = lk
            except locks.Locked as ex:
                theme.msg_warn(self, "CloudPro", "This job's point cloud is open elsewhere:\n%s\n\nClose it there "
                               "first; two editors would overwrite each other's edits." % ex)
                return
        if self.project is not None and not same:
            self._wait_grids()
            self.save_project(quiet=True)
            self.project.store.close()
            if self._edit_lock is not None:
                self._edit_lock.release()
                self._edit_lock = None
        if new_lock is not None:
            self._edit_lock = new_lock
        if not same:
            self._reset_view_state()
        self.project = p
        perflog.session(p, ["3D view: %s" % (self.v3d.gpu_why or "not drawn yet")])
        if getattr(p, "store_findings", None):
            QtCore.QTimer.singleShot(0, lambda f=list(p.store_findings): self.status(
                "Point store checked and repaired on opening (see the log): " + "; ".join(f[:3]) +
                (" ..." if len(f) > 3 else ""), 20000))
        self._remember(p.dcs.path if p.dcs is not None else p.path)
        self.edits = EditManager(p, on_change=self._edited)
        self.tracks = p.tracks()
        self._ensure_file_spans()
        self.file_hidden = set(p.data.get("view", {}).get("files_hidden", []))
        self.track_hidden = set(p.data.get("view", {}).get("tracks_hidden", []))
        sc = p.data.get("view", {}).get("scales", {})
        self.scales = {k: {m: tuple(v) for m, v in sc.get(k, {}).items()} for k in ("plan", "slice", "3d")}
        self._proj_ranges = {}
        vv = p.data.get("view", {})
        for k in ("plan", "slice", "3d"):
            lut = (vv.get("luts") or {}).get(k)
            if lut in render.LUTS:
                self.luts[k] = lut
            sz = (vv.get("sizes") or {}).get(k)
            if sz:
                self.set_point_size(k, sz)
        self.plan.lut = self.luts["plan"]
        self._col_cache = {}
        self.filters.load(p.filters_dir)
        self._fill_lines()
        self.plan.set_project(p.store, self.tracks, {l["id"]: l["colour"] for l in p.lines})
        self._load_separation()
        self.plan.b_offset_z = float(self._origin()[2])
        self._track_masks()
        self.setWindowTitle("CloudPro")
        self.header.set_project_name(p.name)
        self.btn_project.setToolTip(p.dcs.path if p.dcs is not None else p.path)
        v = p.data.get("view", {})
        if v.get("slice"):
            sl = v["slice"]
            self.slice_pane.dir.set_key("along" if sl.get("along") else "across")
            self.slice_pane.set_width(sl.get("w", sl.get("th", 1.0)))
        box = v.get("box") or self._default_box()
        self.set_box(box, load=True, fit=True)
        if v.get("slice"):
            self.slice_pane.set_position(v["slice"].get("pos", 0.0), self._slice_half())
        self.plan.zoom_box()
        self._refresh_history()
        self._raster_layers()
        self.refresh_chain()
        if self._area is not None:
            self._area.set_lines(p.lines)
        if self._props is not None and self._props.isVisible():
            self._props.refresh()

    def _origin(self):
        if self.project is None or self.project.store.origin is None:
            return np.zeros(3)
        return self.project.store.origin

    def _default_box(self):
        if self.tracks:
            lid = sorted(self.tracks)[0]
            tr = self.tracks[lid]
            j = len(tr["E"]) // 2
            j0, j1 = max(0, j - 20), min(len(tr["E"]) - 1, j + 20)
            brg = math.degrees(math.atan2(tr["E"][j1] - tr["E"][j0], tr["N"][j1] - tr["N"][j0])) % 360
            return {"cx": float(tr["E"][j]), "cy": float(tr["N"][j]), "length": 60.0, "width": 30.0, "bearing": brg}
        ext = self.project.store.extent() if self.project else None
        if ext:
            return {"cx": (ext[0] + ext[2]) / 2, "cy": (ext[1] + ext[3]) / 2, "length": 60.0, "width": 30.0,
                    "bearing": 0.0}
        return {"cx": 0.0, "cy": 0.0, "length": 60.0, "width": 30.0, "bearing": 0.0}

    def _autosave(self):
        """Every 2 minutes: flush the point store and save the project quietly (crash safety)."""
        if self.project is None or self._busy:
            return
        try:
            self.save_project(quiet=True)
        except Exception as ex:     # noqa: BLE001
            traceback.print_exc()
            self.status("Automatic save failed: %s" % ex, 15000)

    def save_project(self, quiet=False):
        if self.project is None:
            return
        if self._busy:                          # the job saves the project itself when it ends
            if not quiet:
                self.status("Saved when the running task ends (edits are on disk already)")
            return
        sp = self.slice_pane
        self.project.data["view"] = {"box": self.box, "files_hidden": sorted(self.file_hidden),
                                     "tracks_hidden": sorted(self.track_hidden),
                                     "slice": {"along": sp.along, "w": sp.width(), "th": sp.thickness(),
                                               "pos": sp.position(self._slice_half())},
                                     "scales": {k: {m: list(v) for m, v in d.items()} for k, d in self.scales.items()},
                                     "luts": dict(self.luts), "sizes": dict(self.sizes)}
        self.project.store.sync()
        self.project.save()
        if quiet:
            self.filters.save_quiet()
            return
        self.filters.save()
        self.status("Project saved (edits are written to disk as you make them)")

    def shutdown(self, wait_s=60.0):
        """Window closed or plugin unloaded: stop a running job (Cancel) and wait for it, so nothing writes
        into the project after its edit lock is released; save; release the lock."""
        t = self._thread
        if t is not None and t.is_alive():
            self._cancel()
            if self._prev_cancel is not None:
                self._prev_cancel[0] = True
            t.join(wait_s)
        still = t is not None and t.is_alive()
        self._closing = still or self._closing
        self.save_layout()
        self._wait_grids()
        try:
            if not still:
                self.save_project(quiet=True)
        finally:
            if self._edit_lock is not None and not still:
                # (a job that did not stop keeps the lock; DCS Core takes it over once QGIS has exited)
                self._edit_lock.release()
                self._edit_lock = None
            for w in (self._props, self._area):
                if w is not None:
                    w.close()
            f3 = self.v3d.take_frame_stats()
            if f3 and self.project is not None:
                perflog.log(self.project, "3d", f3)
            self.v3d.release_gpu()

    def ensure_lock(self):
        """The window is shown again after being closed: take the job's edit lock again (closing released it).
        When another editor has the job meanwhile, the project is closed here."""
        self._closing = False
        p = self.project
        if p is None or p.dcs is None or self._edit_lock is not None:
            return True
        from dcs_core import locks
        lk = locks.Lock(p.root, "CloudPro editing", PLUGIN, name=".edit.lock")
        try:
            lk.acquire()
            self._edit_lock = lk
            return True
        except locks.Locked as ex:
            theme.msg_warn(self, "CloudPro", "This job's point cloud was opened elsewhere while CloudPro was "
                           "closed:\n%s\n\nIt is closed here." % ex)
            self.close_project()
            return False

    def close_project(self):
        """Nothing open (the lock could not be had)."""
        self.clear_preview(refresh=False)
        if self.project is not None:
            try:
                self.project.store.close()
            except Exception:       # noqa: BLE001
                pass
        self.project = None
        self.edits = None
        self.ps = PointSet(0)
        self.tracks = {}
        self._reset_view_state()
        self.plan.set_project(None, {}, {})
        self.plan.set_separation(None)
        if self._sepwin is not None:
            self._sepwin.set_result(None)
        self.lines_w.clear()
        self.header.set_project_name("")
        self.header.set_steps([])
        self.lbl_inputs.hide()
        self.refresh_views()

    def closeEvent(self, e):
        try:
            self.shutdown()
        except Exception:       # noqa: BLE001
            pass
        super().closeEvent(e)

    def _fill_recent(self):
        from qgis.PyQt.QtCore import QSettings
        self.recent_menu.clear()
        for f in (QSettings().value("CloudPro/recent", []) or [])[:10]:
            self.recent_menu.addAction(f, lambda f=f: self.open_project(f))

    def _remember(self, path):
        try:
            from qgis.PyQt.QtCore import QSettings
            r = [f for f in (QSettings().value("CloudPro/recent", []) or []) if f != path]
            QSettings().setValue("CloudPro/recent", [path] + r[:9])
        except Exception:          # noqa: BLE001
            pass

    def open_folder(self):
        if self.project is not None:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(
                self.project.dcs.folder if self.project.dcs is not None else self.project.root))

    def relocate(self):
        p = self.project
        if p is None or p.dcs is None or self._guard("relocating the job folder"):
            return
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "New location of the job folder", p.dcs.job_folder)
        if not d:
            return
        still = p.dcs.relocate(d, PLUGIN)
        if still:
            theme.msg_warn(self, "Relocate", "Not found under %s:\n%s" % (d, "\n".join(still[:5])))
        else:
            theme.msg_info(self, "Relocate", "The job folder now points to %s" % d)
        self.refresh_chain()

    def save_qgis_project(self):
        if self.project is None:
            return
        try:
            from qgis.core import QgsProject
            path = self.project.qgis_project_path
            QgsProject.instance().write(path)
            self.status("QGIS project saved: %s" % path, 8000)
        except Exception as ex:        # noqa: BLE001
            self._error("Cannot save the QGIS project", ex)

    def reset_points(self):
        if self.project is None or self._guard("removing the points"):
            return
        if theme.msg_question(self, "CloudPro", "Remove all imported points and edits of this project? The raw "
                              "captures are not touched; Georeference rebuilds the points.") != \
                QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self.project.reset_points()
        self.set_project(self.project, reopen=True)
        self.edits = EditManager(self.project, on_change=self._edited)

    # ------------------------------------------------------------------ chain header
    def refresh_chain(self, timer=False):
        p = self.project
        if p is None:
            return
        if timer and (self._busy or not self.isVisible() or self.window().isMinimized()):
            return              # nothing to see, or the job owns the project (it refreshes when it ends)
        if p.dcs is not None:
            self.chainbar.set_project(p.dcs)
        inp = p.data.get("inputs") or {}
        why = chain.quick_state(p) if p.dcs is not None else []
        self._why = why
        if not p.lines:
            txt, lvl = "No points yet: Menu > Georeference", "warn"
        elif why:
            txt, lvl = "Out of date: " + "; ".join(why), "warn"
        else:
            cal = (inp.get("alignment") or {}).get("calibration", "no calibration")
            src = (inp.get("trajectory") or {}).get("source", "")
            txt = "%s trajectory · %s" % ({"pospro": "PosPro", "strippro": "StripPro", "realtime": "Real-time"}.get(
                src, src or "?"),
                                          "AlignPro calibration" if cal == "applied" else cal)
            lvl = "ok" if cal == "applied" and src in ("pospro", "strippro") else "warn"
        self.lbl_inputs.setText(txt)
        self.lbl_inputs.setToolTip(txt)
        self.lbl_inputs.setProperty("level", lvl)
        self.lbl_inputs.style().unpolish(self.lbl_inputs)
        self.lbl_inputs.style().polish(self.lbl_inputs)
        self.refresh_workflow()

    def refresh_workflow(self):
        """CloudPro's steps in the header (Georeference › Clean & classify › Finalize), coloured by state, the next
        one to do marked; the guidance is in their tooltips. Clicking a step does it."""
        p = self.project
        if p is None or not hasattr(self, "header"):
            return
        from .workflow import STEPS, next_step, states
        hist = p.data.get("history", [])
        n_edits = len(p.data.get("filter_runs", [])) + sum(1 for h in hist if str(h.get("text", "")).endswith(
            "points)") and not str(h.get("text", "")).startswith(("Undo", "Redo")))
        why = getattr(self, "_why", None)
        qs = (lambda _p: why) if why is not None else chain.quick_state      # computed once per refresh
        st = states(p, n_edits, self._visited_clean, qs)
        nxt, _label = next_step(st, self._visited_clean)
        inputs = self.lbl_inputs.text()
        items = []
        for k, t in STEPS:
            state, reason = st[k]
            tip = reason
            if k == "georef" and inputs:
                tip = (inputs + ("\n" + reason if reason and reason != inputs else "")).strip()
            items.append((k, t, state, tip + "\nClick to %s." % {"georef": "georeference (or update)",
                                                                 "clean": "open the automatic filters",
                                                                 "publish": "finalize for the job"}[k]))
        self.header.set_steps(items, nxt or "publish")

    def _step_clicked(self, key):
        if self.project is None:
            return
        if key == "georef":
            self.georeference()
        elif key == "clean":
            self._visited_clean = True
            self.show_dock("filters")
        elif key == "publish":
            self._publish_or_export()
        self.refresh_workflow()

    def _publish_or_export(self):
        if self.project is not None and self.project.dcs is None:
            self.export()
        else:
            self.publish_dialog()
        self.refresh_workflow()

    # ------------------------------------------------------------------ properties windows
    def show_properties(self):
        if self.project is None:
            theme.msg_info(self, "CloudPro", "Open or create a job project first (PROJECT button).")
            return
        if self._props is None:
            self._props = ProjectProperties(self)
        self._props.show()
        self._props.raise_()
        self._props.refresh()

    def show_area_properties(self):
        if self._area is None:
            a = AreaProperties(self)
            a.boxEdited.connect(lambda b: self.set_box(b, load=True))
            a.stepRequested.connect(self.step)
            a.followChanged.connect(self._follow)
            a.gotoRequested.connect(self.goto)
            a.vstep.valueChanged.connect(self._view_step)
            self._area = a
        if self.project is not None:
            self._area.set_lines(self.project.lines)
        if self.box:
            self._area.set_box(self.box, self._origin())
        self._area.show()
        self._area.raise_()

    def apply_geodetics(self, crs, ortho, geoid, share, epoch=None):
        p = self.project
        if p is None:
            return
        if ortho and not geoid:
            theme.msg_warn(self, "CloudPro", "Choose a geoid grid for orthometric heights.")
            return
        if self._guard("changing the geodetics"):
            return
        if p.lines:
            # applied by the georeferencing, and only when it succeeds: the points and the CRS they are
            # labelled with never disagree (cancel the dialog = nothing changes)
            g = {"crs": crs, "geoid_path": geoid if ortho else "", "share": share}
            if epoch is not None:
                g["epoch"] = epoch
            self.georeference(force=True, geo=g)
            return
        if share and p.dcs is not None:
            c = dict(p.dcs.shared("crs") or {})
            c["horizontal"] = crs
            p.dcs.set_shared("crs", c, PLUGIN)
        p.data["crs"] = crs
        p.store.crs = crs
        p.data["geoid_path"] = geoid if ortho else ""
        if epoch is not None:
            p.data["epoch"] = epoch
        p.save()

    def apply_override(self, ov):
        p = self.project
        if p is None or self._guard("changing the georeferencing values"):
            return
        p.data["override"] = ov if ov.get("source") != "alignpro" else {}
        p.save()
        self.refresh_chain()
        if p.lines:
            self.georeference(force=True)

    def lat_lon(self, E, N):
        try:
            import pyproj
            tr = pyproj.Transformer.from_crs(self.project.crs, pyproj.CRS(self.project.crs).geodetic_crs,
                                             always_xy=True)
            lon, lat = tr.transform(E, N)
            return lat, lon
        except Exception:       # noqa: BLE001
            pass
        try:
            from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY, QgsProject
            src = QgsCoordinateReferenceSystem(self.project.crs)
            q = QgsCoordinateTransform(src, src.toGeographicCrs(), QgsProject.instance()).transform(QgsPointXY(E, N))
            return q.y(), q.x()
        except Exception:       # noqa: BLE001
            return None

    # ------------------------------------------------------------------ files
    def _ensure_file_spans(self):
        """Time span of every .clraw file of every line (older projects: read from the files)."""
        changed = False
        for ln in self.project.lines:
            if ln.get("file_spans") and len(ln["file_spans"]) == len(ln.get("files", [])):
                continue
            spans, sizes = [], []
            for f in ln.get("files", []):
                try:
                    from ..core import mcap
                    m = mcap.McapFile(f).scan()
                    spans.append([min(c[5] for c in m.chunk_pos) * 1e-9, max(c[6] for c in m.chunk_pos) * 1e-9]
                                 if m.chunk_pos else [None, None])
                    sizes.append(os.path.getsize(f))
                except Exception:       # noqa: BLE001
                    spans.append([None, None])
                    sizes.append(0)
            ln["file_spans"], ln["file_sizes"] = spans, sizes
            changed = True
        if changed:
            self.project.save()
        self._file_base = {}
        g = 0
        for ln in self.project.lines:
            self._file_base[ln["id"]] = g
            g += max(1, len(ln.get("files", [])))

    def file_table(self):
        rows = []
        for ln in self.project.lines:
            base = self._file_base.get(ln["id"], 0)
            for k, f in enumerate(ln.get("files", [])):
                sp = (ln.get("file_spans") or [[None, None]] * (k + 1))[k] if ln.get("file_spans") else [None, None]
                gid = base + k
                rows.append({"gid": gid, "line": ln["id"], "path": f, "t0": sp[0], "t1": sp[1],
                             "size": (ln.get("file_sizes") or [0] * (k + 1))[k] if ln.get("file_sizes") else 0,
                             "visible": gid not in self.file_hidden, "track": gid not in self.track_hidden,
                             "colour": ln.get("colour", theme.ACCENT)})
        return rows

    def _compute_file_idx(self):
        ps = self.ps
        out = np.zeros(len(ps), np.int32)
        if self.project is None:
            self.file_idx = out
            return
        for ln in self.project.lines:
            m = ps.line == ln["id"]
            if not m.any():
                continue
            base = self._file_base.get(ln["id"], 0)
            starts = [s[0] if s and s[0] is not None else -1e18 for s in (ln.get("file_spans") or [[None, None]])]
            k = np.searchsorted(np.array(starts), ps.t[m], side="right") - 1
            out[m] = base + np.clip(k, 0, max(0, len(starts) - 1))
        self.file_idx = out

    def set_files_visible(self, gids, on):
        if gids is None:
            gids = [r["gid"] for r in self.file_table()]
        for g in gids:
            (self.file_hidden.discard if on else self.file_hidden.add)(g)
        if self._props is not None and gids and len(gids) > 1:
            self._props.refresh()
        self.refresh_views()

    def set_file_track(self, gid, on):
        (self.track_hidden.discard if on else self.track_hidden.add)(gid)
        self._track_masks()
        self.plan.update()

    def _track_masks(self):
        """Which drive-line samples to draw: those inside a file whose Track is ticked."""
        vis = {}
        for ln in self.project.lines if self.project else []:
            tr = self.tracks.get(ln["id"])
            if tr is None:
                continue
            m = np.zeros(len(tr["t"]), bool)
            base = self._file_base.get(ln["id"], 0)
            spans = ln.get("file_spans") or [[None, None]]
            for k, sp in enumerate(spans):
                if base + k in self.track_hidden:
                    continue
                if sp[0] is None:
                    m[:] = True
                else:
                    m |= (tr["t"] >= sp[0] - 0.5) & (tr["t"] <= sp[1] + 0.5)
            vis[ln["id"]] = m
        self.plan.track_masks = vis
        self.plan.track_visible = {k: bool(v.any()) for k, v in vis.items()}

    def select_file(self, gid):
        """Select (highlight) one file's points in the Inspection Area and show its drive line."""
        if len(self.ps) == 0:
            return
        self.selected = self.file_idx == gid
        lid = None
        for ln in self.project.lines:
            b = self._file_base.get(ln["id"], 0)
            if b <= gid < b + max(1, len(ln.get("files", []))):
                lid = ln["id"]
        self.plan.highlight_line = lid
        n = int(self.selected.sum())
        self.status("File selected: %s points in the Inspection Area%s" % (
            "{:,}".format(n), "" if n else " (move the Inspection Area onto its drive line)"), 8000)
        self.refresh_views()
        self._area_info()

    # ------------------------------------------------------------------ lines / classes lists
    def _fill_lines(self):
        self.lines_w.blockSignals(True)
        self.lines_w.clear()
        self.cmb_eline.blockSignals(True)
        self.cmb_eline.clear()
        self.cmb_eline.addItem("All lines", -1)
        for ln in self.project.lines:
            it = QtWidgets.QListWidgetItem("L%d  %s   %s pts" % (ln["id"] + 1, ln.get("name", ""),
                                                                 "{:,}".format(ln.get("points", 0))))
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            on = self.line_enabled.get(ln["id"], ln.get("enabled", True))
            self.line_enabled[ln["id"]] = on
            it.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
            it.setData(Qt.ItemDataRole.UserRole, ln["id"])
            pm = QtGui.QPixmap(12, 12)
            pm.fill(QtGui.QColor(ln.get("colour", theme.ACCENT)))
            it.setIcon(QtGui.QIcon(pm))
            it.setToolTip("%s\n%s" % (ln.get("trajectory", ""), "\n".join(ln.get("files", []))))
            self.lines_w.addItem(it)
            self.cmb_eline.addItem("Only L%d" % (ln["id"] + 1), ln["id"])
        if self.cmb_eline.findData(self.edit_line) < 0:
            self.edit_line = -1                 # that line is gone
        self.cmb_eline.setCurrentIndex(max(0, self.cmb_eline.findData(self.edit_line)))
        ids = {ln["id"] for ln in self.project.lines}
        self.line_enabled = {k: v for k, v in self.line_enabled.items() if k in ids}
        self.lines_w.blockSignals(False)
        self.cmb_eline.blockSignals(False)
        self.plan.line_enabled = dict(self.line_enabled)

    def _line_toggled(self, it):
        self.line_enabled[it.data(Qt.ItemDataRole.UserRole)] = it.checkState() == Qt.CheckState.Checked
        self.plan.line_enabled = dict(self.line_enabled)
        self.refresh_views()

    def _lines_menu(self, pos):
        it = self.lines_w.itemAt(pos)
        if it is None:
            return
        lid = it.data(Qt.ItemDataRole.UserRole)
        m = QtWidgets.QMenu(self)
        a1 = m.addAction("Show only this line")
        a2 = m.addAction("Show all lines")
        a3 = m.addAction("Edit only this line")
        a5 = m.addAction("Edit all lines") if self.edit_line >= 0 else None
        a4 = m.addAction("Inspection Area follows this line")
        theme.dark_menu(m)
        a = exec_(m, self.lines_w.mapToGlobal(pos))
        if a is a1:
            for i in range(self.lines_w.count()):
                w = self.lines_w.item(i)
                w.setCheckState(Qt.CheckState.Checked if w.data(Qt.ItemDataRole.UserRole) == lid else
                                Qt.CheckState.Unchecked)
        elif a is a2:
            for i in range(self.lines_w.count()):
                self.lines_w.item(i).setCheckState(Qt.CheckState.Checked)
        elif a is a3:
            self.cmb_eline.setCurrentIndex(self.cmb_eline.findData(lid))
            self.status("Edits change only L%d (Lines list: right click > Edit all lines)" % (lid + 1), 8000)
        elif a5 is not None and a is a5:
            self.cmb_eline.setCurrentIndex(0)
            self.status("Edits change every line", 5000)
        elif a is a4:
            self._follow(lid)

    def _fill_classes(self):
        self.classes_w.blockSignals(True)
        self.classes_w.clear()
        if len(self.ps):
            cls = self.preview.cls if self.preview is not None else self.ps.cls
            u, c = np.unique(cls, return_counts=True)
            for k, n in zip(u, c):
                it = QtWidgets.QListWidgetItem("%3d  %-20s %s" % (k, F.ASPRS.get(int(k), "class %d" % k),
                                                                  "{:,}".format(int(n))))
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                it.setCheckState(Qt.CheckState.Unchecked if int(k) in self.class_hidden else Qt.CheckState.Checked)
                it.setData(Qt.ItemDataRole.UserRole, int(k))
                rgb = render.CLASS_COLOURS[int(k)]
                pm = QtGui.QPixmap(12, 12)
                pm.fill(QtGui.QColor(int(rgb[0]), int(rgb[1]), int(rgb[2])))
                it.setIcon(QtGui.QIcon(pm))
                self.classes_w.addItem(it)
        self.classes_w.blockSignals(False)

    def _class_toggled(self, it):
        k = it.data(Qt.ItemDataRole.UserRole)
        (self.class_hidden.discard if it.checkState() == Qt.CheckState.Checked else self.class_hidden.add)(k)
        self.refresh_views()

    # ======================================================= georeference (chain)
    def georeference(self, force=False, geo=None):
        if self._guard("georeferencing again"):
            return
        p = self.project
        if p is None:
            if theme.msg_question(self, "CloudPro", "Create or open a job project first. Create one now?") == \
                    QtWidgets.QMessageBox.StandardButton.Yes:
                self.new_project()
            return
        if p.dcs is None:
            self.import_lines()
            return
        try:
            inp = chain.resolve(p.dcs, override=p.data.get("override"))
        except Exception as ex:
            self._error("Cannot read the chain's inputs", ex)
            return
        if not inp.files:
            theme.msg_warn(self, "CloudPro", "No .clraw files were found in the job's captures (%s).\nUse "
                           "Menu > Add other .clraw files, or record the captures in PosPro." % p.dcs.job_folder)
            return
        from .dialogs import GeorefDialog
        d = GeorefDialog(self, inp, p)
        gp = (geo or {}).get("geoid_path", p.data.get("geoid_path")) if geo else p.data.get("geoid_path")
        if gp:
            d.geoid.setText(gp)
        if exec_(d) != QtWidgets.QDialog.DialogCode.Accepted:
            return
        opts = d.options()
        keep = d.keep_edits.isChecked()
        p.data["auto_update"] = d.auto.isChecked()
        gtxt = d.geoid.text().strip()
        if gtxt:
            try:
                from ..core.export import geoid_sampler
                opts.geoid = geoid_sampler(gtxt)
            except Exception as ex:
                self._error("Cannot read the geoid grid", ex)
                return
        # settings that describe the points: set inside the job, after Project.begin_rebuild, so a failed or
        # cancelled georeferencing puts the previous ones back with the previous points
        newvals = {"vertical": "orthometric" if gtxt else "ellipsoidal",
                   "geoid": os.path.basename(gtxt) if gtxt else "", "geoid_path": gtxt}
        if geo:
            newvals["crs"] = geo["crs"]
            if "epoch" in geo:
                newvals["epoch"] = geo["epoch"]
        self._pending_geo = geo
        self.clear_preview(refresh=False)
        self.edits.ps = None
        self.ps = PointSet(0)
        project = p

        def job(progress, cancel):
            with project.dcs.run("cloudpro", PLUGIN, VERSION):
                out, rest = IM.import_chain(project, inp, opts, None, progress, cancel, keep_edits=keep,
                                            settings=newvals)
            project.run_log("georeference", inp.summary() + ["warning: " + w for w in inp.warnings] +
                            ["L%d: %s points %s" % (l + 1, st["points"], st) for l, st in out] +
                            ["edits carried over: %d" % rest])
            return out, rest, inp
        self._run("georef", job, "Georeferencing %d .clraw file(s)" % len(inp.files))

    def auto_update(self, why=""):
        """Georeference again with the last settings, edits carried over, because PosPro or AlignPro published
        (DCS Core told CloudPro; core/autoupdate.decide said the upstream is current). No dialog; the result is
        reported in the status bar and History, not in a message box."""
        if self._busy:
            self._auto_pending = why or "the chain changed"
            return
        from ..core import autoupdate
        p = self.project
        try:
            inp = chain.resolve(p.dcs, override=p.data.get("override"))
            opts = autoupdate.options(p)
        except Exception as ex:                 # noqa: BLE001
            self.status("Automatic georeferencing not started: %s" % ex, 10000)
            return
        if not inp.files:
            return
        self._auto_georef = True
        self._pending_geo = None
        self.clear_preview(refresh=False)
        self.edits.ps = None
        self.ps = PointSet(0)
        project = p

        def job(progress, cancel):
            with project.dcs.run("cloudpro", PLUGIN, VERSION):
                out, rest = IM.import_chain(project, inp, opts, None, progress, cancel, keep_edits=True, settings={})
            project.run_log("georeference (automatic)", inp.summary() + ["warning: " + w for w in inp.warnings] +
                            ["L%d: %s points %s" % (l + 1, st["points"], st) for l, st in out] +
                            ["edits carried over: %d" % rest])
            return out, rest, inp
        self.status("Georeferencing again because %s" % (why or "the chain changed"), 10000)
        self._run("georef", job, "Georeferencing again (%s)" % (why or "the chain changed"))

    def publish_dialog(self):
        p = self.project
        if p is None or self._guard("finalizing"):
            return
        if p.dcs is None:
            theme.msg_info(self, "CloudPro", "Finalizing is for job projects of the DCS chain; use Export for "
                           "this older stand-alone project.")
            return
        if not p.lines:
            theme.msg_info(self, "CloudPro", "Georeference the captures first.")
            return
        from .dialogs import PublishDialog
        d = PublishDialog(self, p, chain.quick_state(p))
        if exec_(d) != QtWidgets.QDialog.DialogCode.Accepted:
            return
        from ..core import publish as PB
        las, cell = d.las.isChecked(), d.dem.value() if d.dem_on.isChecked() else 0.0
        classes = d.classes.selected()
        p.data["finalize_classes"] = sorted(classes) if classes is not None else None
        inc = d.rejected.isChecked()
        add = d.add_qgis.isChecked()
        p.store.sync()
        p.save()

        def job(progress, cancel):
            with p.dcs.run("cloudpro", PLUGIN, VERSION):
                doc = PB.deliver(p, las=las, dem_cell=cell, include_rejected=inc, classes=classes,
                                 progress=progress, cancel=cancel, version=VERSION)
            p.run_log("publish", ["cloud.json %s" % doc["id"], "calibration: %s" % doc["calibration"]] +
                      ["%s: %s" % (k, v) for k, v in doc["files"].items()] + doc.get("warnings", []))
            return doc, add
        self._run("publish", job, "Finalizing")

    def import_lines(self):
        if self._guard("adding files"):
            return
        if self.project is None:
            if theme.msg_question(self, "CloudPro", "Create a new project first?") == \
                    QtWidgets.QMessageBox.StandardButton.Yes:
                self.new_project()
            return
        if self.project.dcs is not None:
            dcs = self.project.dcs
            ps, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Other .clraw captures for this job", dcs.job_folder,
                                                           "Mapper recordings (*.clraw)")
            if not ps:
                return
            if theme.msg_question(self, "CloudPro", "Add %d file(s) to the job's captures? The captures are shared "
                                  "by PosPro, AlignPro and CloudPro." % len(ps)) != \
                    QtWidgets.QMessageBox.StandardButton.Yes:
                return
            dcs.set_capture_files(sorted(set(dcs.capture_files()) | {os.path.abspath(x) for x in ps}), PLUGIN)
            self.georeference()
            return
        d = ImportDialog(self, self.project.data.get("last_dir", ""))
        if exec_(d) != QtWidgets.QDialog.DialogCode.Accepted:
            return
        recs = d.recs
        self.project.data["last_dir"] = d.start_dir
        opts = d.options()
        use_rt = d.r_rt.isChecked()
        sbet = d.sbet.text().strip()
        ws = d.week_start()
        project = self.project

        def job(progress, cancel):
            traj = None if use_rt else IM.load_trajectory(sbet, week_start=ws)
            out = []
            for k, rec in enumerate(recs):
                tr = IM.load_trajectory(rec) if use_rt else traj
                lid, st = IM.import_recording(project, rec, tr, opts, cancel=cancel,
                                              progress=lambda f, s, k=k: progress((k + f) / len(recs), "line %s" %
                                                                                  rec.line))
                out.append((lid, st))
                if cancel():
                    break
            IM.rebuild_grids(project)
            return out
        self._run("import", job, "Importing %d line(s)" % len(recs))

    # ================================================================== Inspection Area
    @staticmethod
    def whole_metres(box):
        """The Inspection Area is sized in whole metres (2.0.0): length and width rounded, at least 1 m."""
        b = dict(box)
        for k in ("length", "width"):
            if k in b:
                b[k] = float(max(1, int(round(float(b[k])))))
        return b

    def set_box(self, box, load=True, fit=False):
        box = self.whole_metres(box)
        self.box = dict(box)
        self.plan.box = dict(box)
        self.v3d.box_bearing = box["bearing"]
        self.v3d.set_area(box)                       # 3D turns around the area's centre
        if hasattr(self, "slice_pane"):
            self.slice_pane.set_extent(self._slice_half() * 2)
        if self._area is not None:
            self._area.set_box(box, self._origin())
        self.plan.update()
        if load:
            self.load_box(fit=fit)

    def _box_from_plan(self, box, final):
        if not box:
            return
        box = self.whole_metres(box)
        self.box = dict(box)
        if self._area is not None:
            self._area.set_box(box, self._origin())
        if final:
            self.set_box(box, load=True, fit=False)

    def max_load(self):
        try:
            from qgis.PyQt.QtCore import QSettings
            return float(QSettings().value("CloudPro/max_load_m", 15.0)) * 1e6
        except Exception:       # noqa: BLE001
            return 15e6

    def set_max_load(self):
        v, ok = theme.get_number(self, "Memory", "Ask before loading an Inspection Area with more "
                                 "than (million points):", self.max_load() / 1e6, 1, 500, 1)
        if ok:
            from qgis.PyQt.QtCore import QSettings
            QSettings().setValue("CloudPro/max_load_m", v)

    def estimate_points(self, box):
        """Points inside a box from the project grids (no tile is read): for the memory check."""
        st = self.project.store
        cor = rect_corners(box["cx"], box["cy"], box["length"], box["width"], box["bearing"])
        n = 0.0
        from ..core.store import GRID_CELL, parse_key
        for k in st.tiles_in_bbox(cor[:, 0].min(), cor[:, 1].min(), cor[:, 0].max(), cor[:, 1].max()):
            g = st.load_grid(k)
            if "count" not in g:
                continue
            i, j = parse_key(k)
            c = g["count"].astype(float) + g["rej"]
            ny, nx = c.shape
            yy, xx = np.mgrid[0:ny, 0:nx]
            u, v = to_box_frame(i * st.tile + (xx.ravel() + 0.5) * GRID_CELL, j * st.tile + (yy.ravel() + 0.5) *
                                GRID_CELL, box["cx"], box["cy"], box["bearing"])
            m = (np.abs(u) <= box["length"] / 2) & (np.abs(v) <= box["width"] / 2)
            n += float(c.ravel()[m].sum())
        return n

    def load_box(self, fit=False):
        if self.project is None or self.box is None:
            return
        if self._busy and self._busy_tag in ("georef", "import", "filter_tiled"):
            return                              # the points are being rebuilt or edited by the job
        try:
            est = self.estimate_points(self.box)
        except Exception:       # noqa: BLE001
            est = 0
        if est > self.max_load() and getattr(self, "_loaded_box", None) != self.box:
            r = theme.msg_question(self, "CloudPro", "This Inspection Area holds about %.1f million points (the limit "
                                   "is %.0f million; Menu > Memory limit). Loading it may be slow or run out of "
                                   "memory.\n\nLoad it anyway?" % (est / 1e6, self.max_load() / 1e6))
            if r != QtWidgets.QMessageBox.StandardButton.Yes:
                self.status("Not loaded: make the Inspection Area smaller (right click > Inspection Area properties)",
                            10000)
                return
        self._loaded_box = dict(self.box)
        f3 = self.v3d.take_frame_stats()                 # the previous area's 3D frames
        if f3:
            perflog.log(self.project, "3d", f3)
        t = time.time()
        QtWidgets.QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.clear_preview(refresh=False)
            ps = PointSet.from_store(self.project.store, self.box)
            t_read = time.time()
            if len(ps):                                  # random order: any first part is an even sample (panning)
                ps = ps.subset(np.random.default_rng(int(len(ps)) % 9973).permutation(len(ps)))
            t_mix = time.time()
            self.ps = ps
            self.edits.ps = ps
            self.selected = None
            self._compute_file_idx()
            self._col_cache = {}
            self._ranges = {}
            self._fill_classes()
            t_prep = time.time()
            self.refresh_views(fit=fit)
            self._area_info()
            t_draw = time.time()
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        # where the time went, for comparing machines and data (also in the History log)
        msg = "Loaded %s points in %.1f s (read %.1f, order %.1f, prepare %.1f, draw %.1f; %s)" % (
            "{:,}".format(len(self.ps)), time.time() - t, t_read - t, t_mix - t_read, t_prep - t_mix, t_draw - t_prep,
            engine_status())
        self.status(msg, 15000)
        perflog.log(self.project, "load", "%s; area %.0f x %.0f m" % (msg, self.box["length"], self.box["width"]))
        if time.time() - t > 2.0:                        # slow loads only: stepping the area would flood History
            self.project.log(msg)

    # ================================================================ navigation
    def _slice_half(self):
        b = self.box or {"length": 60, "width": 30}
        return (b["width"] if self.slice_pane.along else b["length"]) / 2.0

    def _slice(self):
        """(along, centre, thickness, full) of the slice in the box frame."""
        sp = self.slice_pane
        half = self._slice_half()
        if abs(sp.extent - 2 * half) > 1e-6:
            sp.set_extent(2 * half)              # whole slices inside the area (2.0.0)
        return sp.along, sp.position(), sp.thickness(), False

    def _slice_changed(self):
        self.plan.update()
        self.refresh_views(which=("slice", "3d", "plan"))

    def step(self, k):
        if self.box is None:
            return
        b = dict(self.box)
        br = math.radians(b["bearing"])
        u = np.array([math.sin(br), math.cos(br)])
        v = np.array([math.cos(br), -math.sin(br)])
        a = self._area
        st = a.box_step() if a is not None else b["length"] * 0.9
        if k in ("fwd", "back"):
            d = st if k == "fwd" else -st
            if self.follow_line >= 0 and self.follow_line in self.tracks:
                b = self._along_track(b, d)
            else:
                b["cx"] += u[0] * d
                b["cy"] += u[1] * d
            self.set_box(b)
        elif k in ("left", "right"):
            d = a.side_step() if a is not None else b["width"] * 0.9
            d = d if k == "right" else -d
            b["cx"] += v[0] * d
            b["cy"] += v[1] * d
            self.set_box(b)
        elif k in ("rotl", "rotr"):
            r = a.rot.value() if a is not None else 5.0
            b["bearing"] = (b["bearing"] + (r if k == "rotr" else -r)) % 360
            self.set_box(b)
        elif k.startswith("sec_"):
            # 2.0.0: one whole slice at a time (big: 5), never past the Inspection Area's edges
            sp = self.slice_pane
            self._slice()
            if k in ("sec_first", "sec_last"):
                sp.go(0 if k == "sec_first" else sp.count() - 1)
                return
            d = (5 if k.endswith("big") else 1) * (1 if k.startswith("sec_next") else -1)
            sp.go(sp.index + d)

    def _along_track(self, b, d):
        tr = self.tracks[self.follow_line]
        E, N = tr["E"], tr["N"]
        s = np.r_[0, np.cumsum(np.hypot(np.diff(E), np.diff(N)))]
        j = int(np.argmin(np.hypot(E - b["cx"], N - b["cy"])))
        j2, j1 = min(len(E) - 1, j + 5), max(0, j - 5)
        tb = math.degrees(math.atan2(E[j2] - E[j1], N[j2] - N[j1])) % 360
        sign = 1 if abs(((tb - b["bearing"] + 180) % 360) - 180) < 90 else -1
        s1 = float(np.clip(s[j] + sign * d, 0, s[-1]))
        e0, n0 = float(np.interp(max(0, s1 - 5), s, E)), float(np.interp(max(0, s1 - 5), s, N))
        e1, n1 = float(np.interp(min(s[-1], s1 + 5), s, E)), float(np.interp(min(s[-1], s1 + 5), s, N))
        brg = math.degrees(math.atan2(e1 - e0, n1 - n0)) % 360
        if sign < 0:
            brg = (brg + 180) % 360
        b = dict(b)
        b.update(cx=float(np.interp(s1, s, E)), cy=float(np.interp(s1, s, N)), bearing=brg)
        if self._area is not None:
            self._area.chain.setText("L%d chainage %.2f m of %.1f" % (self.follow_line + 1, s1, s[-1]))
        return b

    def _follow(self, lid):
        self.follow_line = lid if lid is not None else -1
        if self._area is not None:
            self._area.follow.blockSignals(True)
            self._area.follow.setCurrentIndex(max(0, self._area.follow.findData(self.follow_line)))
            self._area.follow.blockSignals(False)
        if self.follow_line >= 0 and self.box:
            self.set_box(self._along_track(self.box, 0.0))

    def goto(self, E, N):
        if not self.box:
            return
        o = self._origin()
        b = dict(self.box)
        b["cx"], b["cy"] = E - o[0], N - o[1]
        self.set_box(b)
        self.plan.center_on(b["cx"], b["cy"])

    def goto_world(self, E, N):
        self.goto(E, N)

    def _key_nav(self, name):
        self.step({"next": "sec_next", "prev": "sec_prev", "next_big": "sec_next_big", "prev_big": "sec_prev_big",
                   "box_fwd": "fwd", "box_back": "back", "box_left": "left", "box_right": "right"}.get(name, name))

    def _view_step(self, v):
        for w in (self.plan, self.slice):
            w.pan_step = v
        self.v3d.step = v

    def zoom_all(self):
        self.plan.zoom_box()
        self._zoom_slice_area()
        self.v3d.fit()

    def _zoom_slice_area(self):
        """Slice view: the Inspection Area's width (cross-section) or length (profile) and its points' height."""
        if not self.box:
            self.slice.zoom_all()
            return
        half = (self.box["length"] if self.slice_pane.along else self.box["width"]) / 2
        self.slice.zoom_range(-half, half)

    # ================================================================== display
    def _colours_for(self, mode, view=None):
        if len(self.ps) == 0:
            return np.zeros(0, np.uint32)
        scalar = mode in SCALAR_MODES
        rng = self._range(mode, view) if scalar else None
        lut = self._lut_of(view)
        key = (mode, lut, self.preview is not None, rng)
        c = self._col_cache.get(key)
        if c is not None:
            return c
        if mode == "File":
            c = render.pack(FILE_COLOURS[self.file_idx % len(FILE_COLOURS)])
        elif scalar:
            c = render.colour_points(self.ps if self.preview is None else _Proxy(self.ps, z=self.preview.z), mode,
                                     lut, rng=rng)
        else:
            lc = np.array([render.hex_rgb(l.get("colour", "#00e5aa")) for l in self.project.lines] or [(0, 229, 170)],
                          np.uint8)
            src = self.ps
            if self.preview is not None and mode == "Classification":
                src = _Proxy(self.ps, cls=self.preview.cls)
            c = render.colour_points(src, mode, lut, line_colours=lc)
        self._col_cache[key] = c
        return c

    def _range(self, mode, view=None):
        """Colour scale of a scalar scheme for a view (following its link): automatic over all points of the
        Inspection Area (the default, the same in every view), automatic over the whole project, or the user's
        own minimum and maximum."""
        st = self.scales.get(self._root(view), {}).get(mode, ("area",)) if view else ("area",)
        if st[0] == "manual":
            return float(st[1]), float(st[2])
        if st[0] == "project":
            r = self._project_range(mode)
            if r is not None:
                return r
        key = (mode, len(self.ps))
        r = self._ranges.get(key) if hasattr(self, "_ranges") else None
        if r is None:
            r = _auto_range(mode, self.ps.z, self.ps.i, self.ps.rng, self.ps.t)
            if not hasattr(self, "_ranges") or len(self._ranges) > 40:
                self._ranges = {}
            self._ranges[key] = r
        return r

    def _project_range(self, mode):
        """Automatic colour scale over the whole project: an even sample (up to about 2 million accepted
        points) read from every tile of the point store."""
        st = self.project.store if self.project is not None else None
        if st is None or not st.keys():
            return None
        key = (mode, st.total, getattr(st, "edits_rev", 0))
        if key in self._proj_ranges:
            return self._proj_ranges[key]
        step = max(1, st.total // 2_000_000)
        parts = []
        for k in st.keys():
            a = st.tile_array(k)
            if len(a):
                s_ = a[::step]
                parts.append(s_[(s_["flag"] & REJECTED) == 0])
        if not parts:
            return None
        a = np.concatenate(parts)
        if len(a) == 0:
            return None
        r = _auto_range(mode, a["z"] * 1e-3, a["i"], a["rng"], a["t"])
        if len(self._proj_ranges) > 20:
            self._proj_ranges = {}
        self._proj_ranges[key] = r
        return r

    VIEW_NAMES = {"plan": "Plan", "slice": "Slice", "3d": "3D"}

    BRUSH_SIZES = (6, 12, 18, 24, 36, 48, 72, 100, 150, 200)

    def _legend_menu(self, key, gpos):
        """Right-click on a view's legend (2.0.0): its colours (scheme, scale, ramp, link), and the sizes - point
        size in every view, the background grid, cell size and brush size in the Plan view."""
        pane = self.panes[key]
        m = theme.dark_menu(QtWidgets.QMenu(self))
        linked = self.links.get(key)

        def radio(menu, choices, current, setter):
            grp = QtGui.QActionGroup(menu) if hasattr(QtGui, "QActionGroup") else QtWidgets.QActionGroup(menu)
            for label, val in choices:
                a = menu.addAction(label)
                a.setCheckable(True)
                a.setChecked(val == current)
                grp.addAction(a)
                a.triggered.connect(lambda _=False, v=val: setter(v))
            theme.dark_menu(menu)
            return menu

        mode = self._mode(key)
        cm = m.addMenu("Colour  ·  %s" % mode)
        radio(cm, [(t, t) for t in COLOUR_MODES], pane.colour.currentText(), pane.colour.setCurrentText)
        if linked:
            cm.setTitle("Colour  ·  %s (linked to the %s view)" % (mode, self.VIEW_NAMES[self._root(key)]))
            cm.setEnabled(False)
        if not linked and mode in SCALAR_MODES:
            sm = m.addMenu("Colour scale")
            cur = self.scales[key].get(mode, ("area",))[0]
            radio(sm, [("Automatic: Inspection Area", "area"), ("Automatic: whole project", "project"),
                       ("Fixed minimum / maximum…", "manual")], cur,
                  lambda kind: self._scale_dialog(key, mode) if kind == "manual" else
                  self.set_scale(key, mode, (kind,)))
        rm = m.addMenu("Colour ramp  ·  %s" % self._lut_of(key))
        radio(rm, [(t, t) for t in render.LUTS], self.luts[key], lambda t: self.set_lut(key, t))
        if linked:
            rm.setTitle("Colour ramp  ·  %s (linked to the %s view)" % (self._lut_of(key),
                                                                       self.VIEW_NAMES[self._root(key)]))
            rm.setEnabled(False)
        lm = m.addMenu("Link colours  ·  %s" % (pane.link.currentText().replace("= ", "to ")
                                                if linked else "own"))
        radio(lm, [("Own colours and scale" if not pane.link.itemData(i) else
                    "Same as the %s view" % self.VIEW_NAMES[pane.link.itemData(i)], i) for i in range(pane.link.count())],
              pane.link.currentIndex(), pane.link.setCurrentIndex)
        m.addSeparator()
        if key == "plan":
            gm = m.addMenu("Grid  ·  %s" % self.plan_pane.grid.currentText())
            radio(gm, [(t, t) for t in ATTRS], self.plan_pane.grid.currentText(), self.plan_pane.grid.setCurrentText)
            if self.plan_pane.grid.currentText() == SEPARATION:
                tm = m.addMenu("Tolerance  ·  %g cm" % (100 * self.plan.tolerance))
                radio(tm, [("%g cm" % (100 * t), t) for t in SEP.TOLERANCES], self.plan.tolerance, self.set_tolerance)
                act = m.addAction("Steady offsets removed")
                act.setCheckable(True)
                act.setChecked(self.plan.steady_off)
                act.toggled.connect(self.plan.set_steady_off)
                m.addAction("Line separation results…", self.show_separation)
                m.addAction("Check line separation again", self.check_separation)
            cs = m.addMenu("Cell size  ·  %s" % self.cb_cell.currentText())
            radio(cs, [(self.cb_cell.itemText(i), i) for i in range(self.cb_cell.count())], self.cb_cell.currentIndex(),
                  self.cb_cell.setCurrentIndex)
        ps_ = m.addMenu("Point size  ·  %d px" % self.sizes[key])
        radio(ps_, [("%d px" % v, v) for v in range(self.sp_size.minimum(), self.sp_size.maximum() + 1)],
              self.sizes[key], lambda v: self.set_point_size(key, v))
        if key == "plan":
            bs = m.addMenu("Brush size  ·  %d px" % self.sp_brush.value())
            sizes = sorted(set(self.BRUSH_SIZES) | {self.sp_brush.value()})
            radio(bs, [("%d px" % v, v) for v in sizes], self.sp_brush.value(), self.sp_brush.setValue)
        exec_(m, gpos)

    def _scale_menu(self, key, gpos):
        """Right-click on a view's colour scale bar: automatic over the Inspection Area or the whole project,
        or a fixed minimum and maximum. A linked view follows the scale of the view it is linked to."""
        mode = self._mode(key)
        m = QtWidgets.QMenu(self)
        t = m.addAction("Colour scale  ·  %s" % mode)
        t.setEnabled(False)
        m.addSeparator()
        if self.links.get(key):
            root = self._root(key)
            a = m.addAction("Linked to the %s view: uses its colour scale" % self.VIEW_NAMES[root])
            a.setEnabled(False)
            m.addAction("Unlink (own colours and scale)", lambda: self.panes[key].link.setCurrentIndex(0))
            exec_(m, gpos)
            return
        if mode not in SCALAR_MODES:
            a = m.addAction("Only for Height, Intensity, Range and Time colours")
            a.setEnabled(False)
            exec_(m, gpos)
            return
        cur = self.scales[key].get(mode, ("area",))[0]
        grp = QtGui.QActionGroup(m) if hasattr(QtGui, "QActionGroup") else QtWidgets.QActionGroup(m)
        for kind, text in (("area", "Automatic: Inspection Area"), ("project", "Automatic: whole project"),
                           ("manual", "Fixed minimum / maximum…")):
            a = m.addAction(text)
            a.setCheckable(True)
            a.setChecked(cur == kind)
            grp.addAction(a)
            if kind == "manual":
                a.triggered.connect(lambda _=False: self._scale_dialog(key, mode))
            else:
                a.triggered.connect(lambda _=False, kind=kind: self.set_scale(key, mode, (kind,)))
        exec_(m, gpos)

    def _scale_dialog(self, key, mode):
        from .dialogs import ScaleDialog
        lo, hi = self._range(mode, key)
        d = ScaleDialog(self, self.VIEW_NAMES[key], mode, lo, hi, z_offset=float(self._origin()[2]))
        if exec_(d) == QtWidgets.QDialog.DialogCode.Accepted:
            self.set_scale(key, mode, ("manual",) + tuple(d.values()))

    def set_scale(self, key, mode, setting):
        """Set a view's colour scale for a scheme and redraw it and every view linked to it."""
        self.scales[key][mode] = tuple(setting)
        self.refresh_views(which=tuple(k for k in self.panes if self._root(k) == key))
        self.status("%s view, %s colours: %s" % (self.VIEW_NAMES[key], mode, {
            "area": "automatic over the Inspection Area", "project": "automatic over the whole project",
            "manual": "a fixed minimum and maximum"}[setting[0]]),
            5000)

    def _root(self, key):
        """The view whose colours a view shows: itself, or the end of its chain of links."""
        seen = set()
        while key and self.links.get(key) and key not in seen:
            seen.add(key)
            key = self.links[key]
        return key

    def _mode(self, key):
        """Colour scheme of a view, following its link (and the link's link)."""
        return self.panes[self._root(key)].colour.currentText()

    def _set_link(self, key, target):
        t = target
        while t:                                   # no loops: a view cannot follow one that follows it
            if t == key:
                self.status("That would link the views in a circle; %s keeps its own colours" % key.title(), 6000)
                pane = self.panes[key]
                pane.link.blockSignals(True)
                pane.link.setCurrentIndex(0)
                pane.link.blockSignals(False)
                target = ""
                break
            t = self.links.get(t, "")
        self.links[key] = target
        self.panes[key].set_linked(target, self._mode(key))
        self.refresh_views(which=(key,))

    def _view_colour(self, view, mode):
        which = [view]
        for k in self.panes:                       # views linked to this one (directly or through another)
            if k != view and self.links[k] and self._mode(k) == mode:
                self.panes[k].set_linked(self.links[k], mode)
                which.append(k)
        self.refresh_views(which=tuple(which))

    def _legend_info(self, mode, view=None):
        if len(self.ps) == 0:
            return None
        o = self._origin()
        info = {"title": mode}
        if mode in ("Height", "Intensity", "Intensity (range normalised)", "Time", "Range"):
            lo, hi = self._range(mode, view)
            st = self.scales.get(self._root(view), {}).get(mode, ("area",)) if view else ("area",)
            lut = self._lut_of(view)
            info.update(kind="ramp", lo=lo, hi=hi, lut=lut,
                        note={"area": "", "project": "project", "manual": "fixed"}[st[0]])
            if mode == "Height":
                info["fmt"] = lambda v: "%.2f m" % (v + o[2])
            elif mode.startswith("Intensity"):
                info["lut"] = "Grey" if lut == "Rainbow" else lut
                info["fmt"] = lambda v: "%.0f" % v
            elif mode == "Time":
                info["lut"] = "Viridis"
                info["fmt"] = lambda v: _dt.datetime.fromtimestamp(v, _dt.timezone.utc).strftime("%H:%M:%S UTC")
            else:
                info["fmt"] = lambda v: "%.1f m" % v
        elif mode == "Channel":
            info.update(kind="ramp", lo=1, hi=32, lut="Rainbow", fmt=lambda v: "laser %d" % round(v))
        elif mode == "Classification":
            cls = self.preview.cls if self.preview is not None else self.ps.cls
            u = np.unique(cls)
            info.update(kind="swatches", items=[(tuple(render.CLASS_COLOURS[int(k)]), F.ASPRS.get(int(k), "") or
                                                 "Class %d" % k) for k in u])     # 2.0.3: names only, no numbers
        elif mode == "Line":
            ids = set(np.unique(self.ps.line).tolist())
            info.update(kind="swatches", items=[(render.hex_rgb(l.get("colour", "#00e5aa")), "L%d %s" % (
                l["id"] + 1, l.get("name", ""))) for l in self.project.lines if l["id"] in ids])
        elif mode == "File":
            names = {r["gid"]: os.path.basename(r["path"]) for r in self.file_table()}
            info.update(kind="swatches", items=[(tuple(FILE_COLOURS[g % len(FILE_COLOURS)]), names.get(int(g), "?"))
                                                for g in np.unique(self.file_idx)])
        elif mode == "Return":
            info.update(kind="swatches", items=[((0, 229, 170), "Last / single"), ((245, 166, 35), "Strongest")])
        extra = []
        if self.preview is not None:
            prej = (self.preview.flag & REJECTED) != 0
            rej = self.ps.rejected
            if (prej & ~rej).any():
                extra.append((render.PREVIEW_REJECT_RGB, "Rejected by the preview"))
            if (rej & ~prej).any():
                extra.append(((120, 255, 120), "Re-accepted by the preview"))
        # no "Rejected" row: rejected points are magenta everywhere in CloudPro
        if self.selected is not None and self.selected.any():
            extra.append((render.SELECT_RGB, "Selected"))
        info["extra"] = extra
        return info

    def _set_legend(self, key, view):
        pane = self.panes[key]
        view.legend = self._legend_info(self._mode(key), key) if pane.legend_btn.isChecked() else None

    def _grid_attr(self, a):
        self.plan.attr = a
        self.plan_pane.target.setVisible(a == "Coverage")
        self.plan.invalidate()
        if a == SEPARATION and self.project is not None and self.plan.separation is None and not self._busy:
            self.check_separation()             # nothing measured yet: measure it

    # ------------------------------------------------------------------ line separation (2.2.0)
    def _load_separation(self):
        """The saved line separation result (read again only when its file changed: this runs after every
        georeferencing and tiled filter run) and whether it is still current."""
        res = None
        if self.project is not None:
            stamp = SEP.stamp(self.project)
            old = self.plan.separation
            if old is not None and stamp is not None and getattr(old, "stamp", None) == stamp:
                res = old
            else:
                res = SEP.load(self.project)
        cur = res.is_current(self.project) if res is not None else True
        self.plan.set_separation(res, cur)
        if self._sepwin is not None:
            self._sepwin.set_result(res, cur)

    @staticmethod
    def _saved_tolerance():
        try:
            from qgis.PyQt.QtCore import QSettings
            return float(QSettings().value("CloudPro/separation_tolerance", SEP.DEFAULT_TOLERANCE))
        except (ImportError, TypeError, ValueError):
            return SEP.DEFAULT_TOLERANCE

    def set_tolerance(self, tol):
        self.plan.set_tolerance(tol)
        try:
            from qgis.PyQt.QtCore import QSettings
            QSettings().setValue("CloudPro/separation_tolerance", float(tol))
        except ImportError:
            pass
        if self._sepwin is not None:
            self._sepwin.refresh()
        if self.plan.separation is not None and self.plan.separation_current:
            SEP.write_feedback(self.project, self.plan.separation, self.plan.tolerance)

    def check_separation(self):
        """Project menu > Check line separation: how far overlapping lines sit apart (nothing is changed)."""
        if self.project is None or not self.project.store.keys():
            theme.msg_info(self, "CloudPro", "Georeference the lines first: the check reads the points.")
            return
        if len(self.project.lines) < 2:
            theme.msg_info(self, "CloudPro", "The check compares lines with each other: this job has one line.")
            return
        prj = self.project
        self._run("separation", lambda progress, cancel: SEP.run(prj, progress, cancel),
                  "Checking line separation")

    def show_separation_of(self, f):
        """The Plan's Line separation grid for every pair (None), one pair ("pair", a, b) or one line ("line", l)."""
        if self.plan_pane.grid.currentText() != SEPARATION:
            self.plan_pane.grid.setCurrentText(SEPARATION)
        self.plan.set_separation_filter(f)

    def goto_problem(self, area):
        """A problem area chosen: the Plan shows the line at fault (or the pair), the Inspection Area goes onto the
        area along the nearest drive line of that line, the views zoom to it."""
        if self.box is None:
            return
        f = ("line", area["line"]) if area["line"] is not None else \
            (("pair",) + tuple(area["pairs"][0]) if len(area["pairs"]) == 1 else None)
        self.show_separation_of(f)
        boxes = area.get("boxes") or [area["bbox"]]
        x0, y0, x1, y1 = max(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))       # the largest part
        side = min(60.0, max(x1 - x0, y1 - y0) + 10.0)
        brg = 0.0
        lid = area["line"] if area["line"] is not None else area["lines"][0]
        tr = self.tracks.get(lid)
        if tr is not None and len(tr["E"]) > 2:
            d = np.hypot(tr["E"] - area["x"], tr["N"] - area["y"])
            j = int(np.argmin(d))
            j0, j1 = max(0, j - 10), min(len(tr["E"]) - 1, j + 10)
            brg = math.degrees(math.atan2(tr["E"][j1] - tr["E"][j0], tr["N"][j1] - tr["N"][j0])) % 360
        b = dict(self.box, cx=float((x0 + x1) / 2), cy=float((y0 + y1) / 2), length=side, width=side, bearing=brg)
        self.set_box(b, load=True, fit=True)
        self.plan.zoom_box()

    def _sep_panel(self):
        from .separationwin import SeparationPanel
        self._sepwin = SeparationPanel(self)
        return self._sepwin

    def show_separation(self):
        """The LINE SEPARATION tab (bottom right) brought to the front, with the saved result."""
        self._sepwin.set_result(self.plan.separation, self.plan.separation_current)
        self.show_dock("separation")

    def open_in(self, stage, target):
        """Open another DCS plugin on this project and show target there (DCS Core API 13; with an older DCS Core
        the plugin opens on the project without going to the item)."""
        if self.project is None or self.project.dcs is None:
            theme.msg_info(self, "CloudPro", "This project is not part of a DCS project, so there is nothing to open.")
            return False
        from dcs_core import registry
        path = self.project.dcs.path
        fn = getattr(registry, "focus", None)
        ok = fn(stage, path, target) if fn is not None else registry.open(stage, path)
        if not ok:
            theme.msg_info(self, "CloudPro", "%s is not installed or not enabled in QGIS." % {
                "pospro": "PosPro", "alignpro": "AlignPro", "strippro": "StripPro"}.get(stage, stage))
        return ok

    @staticmethod
    def _saved_target():
        try:
            from qgis.PyQt.QtCore import QSettings
            return float(QSettings().value("CloudPro/coverage_target", 100.0))
        except (ImportError, TypeError, ValueError):
            return 100.0

    def _target(self, v):
        self.plan.set_target(v)
        try:
            from qgis.PyQt.QtCore import QSettings
            QSettings().setValue("CloudPro/coverage_target", float(v))
        except ImportError:
            pass

    def _cell(self, v):
        if v is None:
            return
        c = self.plan.set_cell(float(v))
        i = self.cb_cell.findData(c)
        if i >= 0 and i != self.cb_cell.currentIndex():
            self.cb_cell.blockSignals(True)
            self.cb_cell.setCurrentIndex(i)
            self.cb_cell.blockSignals(False)

    def _lut(self, m):
        """Every view's colour ramp at once (the hidden Colours field; the legend menu sets one view)."""
        self.lut = m
        for k in self.luts:
            self.luts[k] = m
        self.plan.lut = m
        self.plan.invalidate()
        self._col_cache = {}
        self.refresh_views()

    def _lut_of(self, view):
        """The colour ramp a view uses: its own, or the one of the view it is linked to."""
        if not view:
            return self.lut
        return self.luts.get(self._root(view), self.lut)

    def set_lut(self, key, m):
        """One view's colour ramp (legend menu). The Plan's ramp also colours its project grid."""
        self.luts[key] = m
        if key == "plan":
            self.plan.lut = m
            self.plan.invalidate()
        self._col_cache = {}
        self.refresh_views(which=tuple(k for k in self.panes if self._root(k) == key))

    def set_point_size(self, key, v):
        """One view's point size (legend menu)."""
        self.sizes[key] = int(v)
        w = {"plan": self.plan, "slice": self.slice, "3d": self.v3d}[key]
        w.point_size = int(v)
        w._dirty = True
        w.update()

    def _point_size(self, v):
        """Every view's point size at once (the hidden Point field; the legend menu sets one view)."""
        for k in self.sizes:
            self.sizes[k] = int(v)
        for w in (self.plan, self.slice, self.v3d):
            w.point_size = v
            w._dirty = True
            w.update()

    def _toggle_rej(self, on):
        """Tool bar eye (H): rejected points shown or hidden in every view."""
        for k in self.show_rej:
            self.show_rej[k] = on
            self.panes[k].set_show_rejected(on)
        self.refresh_views()

    def _toggle_view_rej(self, key, on):
        """A view's own eye: show its rejected points (magenta) or hide them."""
        self.show_rej[key] = on
        self.b_rej.set_key("show" if all(self.show_rej.values()) else "hide")
        self.refresh_views(which=(key,))

    def _visible(self, key=None):
        """Points a view shows: lines, files and classes switched on; rejected points only where that view's
        "show rejected" eye is on (key None: shown if any view shows them). Points a filter preview would
        reject stay visible (orange) so the preview can be judged."""
        ps = self.ps
        n = len(ps)
        if n == 0:
            return np.zeros(0, bool)
        vis = np.ones(n, bool)
        off = [l for l, on in self.line_enabled.items() if not on]
        if off:
            vis &= ~np.isin(ps.line, off)
        if self.file_hidden:
            vis &= ~np.isin(self.file_idx, list(self.file_hidden))
        if self.class_hidden:
            cls = self.preview.cls if self.preview is not None else ps.cls
            vis &= ~np.isin(cls, list(self.class_hidden))
        show = any(self.show_rej.values()) if key is None else self.show_rej.get(key, False)
        if not show:
            vis &= ~ps.rejected
        return vis

    def _colours(self, mode, idx=None, view=None):
        """Colours of the points idx (all when None): the scheme's cached colours, gathered for those points
        only, then rejected / preview / selected marks over the same points."""
        base = self._colours_for(mode, view)
        if len(base) == 0:
            return base.copy()
        if idx is None:
            idx = slice(None)
        col = base[idx]
        if not isinstance(idx, slice) and len(col) == 0:
            return col
        rej = (self.ps.flag[idx] & REJECTED) != 0
        if self.preview is not None:
            prej = (self.preview.flag[idx] & REJECTED) != 0
            newly = prej & ~rej
            reacc = rej & ~prej
            col[rej & prej] = render.pack(np.array(render.REJECT_RGB, np.uint8))
            col[newly] = render.pack(np.array(render.PREVIEW_REJECT_RGB, np.uint8))
            col[reacc] = render.pack(np.array((120, 255, 120), np.uint8))
        else:
            col[rej] = render.pack(np.array(render.REJECT_RGB, np.uint8))
        if self.selected is not None and len(self.selected) == len(self.ps):
            col[self.selected[idx]] = render.pack(np.array(render.SELECT_RGB, np.uint8))
        return col

    def refresh_views(self, fit=False, which=("plan", "slice", "3d")):
        vis_now = getattr(self, "_view_vis", {})
        hidden = [k for k in which if not vis_now.get(k, True)]
        if hidden:
            self._view_stale.update(hidden)
            which = tuple(k for k in which if k not in hidden)
        ps = self.ps
        o = self._origin()
        b = self.box or {"cx": 0, "cy": 0, "bearing": 0, "length": 1, "width": 1}
        z = ps.z if self.preview is None else self.preview.z
        along, pos, th, full = self._slice()
        base = self._visible(key=None) if len(ps) else np.zeros(0, bool)
        rej = ps.rejected if len(ps) else np.zeros(0, bool)

        def shown(key):                         # each view: rejected points only if its eye is on
            return np.flatnonzero(base if self.show_rej.get(key, False) else base & ~rej)

        # along / across the Inspection Area for every loaded point: computed once per load and box, so an edit
        # (which changes only flags and classes) does not rotate millions of points again
        uk = (id(ps.x), len(ps), b["cx"], b["cy"], b["bearing"])
        if getattr(self, "_uv_key", None) != uk:
            self._uv = to_box_frame(ps.x, ps.y, b["cx"], b["cy"], b["bearing"]) if len(ps) else \
                (np.zeros(0, np.float32), np.zeros(0, np.float32))
            self._uv_key = uk
        U, V = self._uv

        def in_slab(idx):                       # the slice slab, from the cached frame coordinates
            if len(idx) == 0:
                return idx, np.zeros(0), np.zeros(0)
            u, v = U[idx], V[idx]
            if along:
                keep = np.ones(len(idx), bool) if full else np.abs(v - pos) <= th / 2
            else:
                keep = np.abs(u - pos) <= th / 2
            return idx[keep], u[keep], v[keep]
        if "plan" in which:
            idx = shown("plan")
            col = self._colours(self._mode("plan"), idx, "plan")
            self._legend_key = "plan"
            self._set_legend("plan", self.plan)
            self.plan.a_offset, self.plan.b_offset = o[0], o[1]
            self.plan.set_points(ps.x[idx], ps.y[idx], z[idx], col, idx, fit=False)
            self.plan_pane.title.setText("PLAN")
        if "slice" in which:
            idx, u, v = in_slab(shown("slice"))
            col = self._colours(self._mode("slice"), idx, "slice")
            self._legend_key = "slice"
            self._set_legend("slice", self.slice)
            self.slice.b_offset = o[2]
            if along:
                self.slice.kind = "profile"
                self.slice.a_offset = b["length"] / 2
                self.slice.set_points(u, z[idx], v, col, idx, fit=fit or len(self.slice.a) == 0)
            else:
                self.slice.kind = "section"
                self.slice.a_offset = 0.0
                self.slice.set_points(v, z[idx], -u, col, idx, fit=fit or len(self.slice.a) == 0)
            self.slice_pane.title.setText("SLICE")
        if "3d" in which:
            idx = shown("3d")
            if self.v3d_pane.what.currentText() == "Slice":
                idx = in_slab(idx)[0]
            col = self._colours(self._mode("3d"), idx, "3d")
            self._legend_key = "3d"
            self._set_legend("3d", self.v3d)
            P = np.column_stack([ps.x[idx], ps.y[idx], z[idx]])
            self.v3d.set_points(P, col, idx, fit=fit or len(self.v3d.P) == 0)
            self.v3d_pane.title.setText("3D")
            self.v3d.overlay_lines = self._box_lines3d()
        nsel = int(self.selected.sum()) if self.selected is not None else 0
        self.lbl_cnt.setText("%s pts  ·  %s rejected%s" % (
            "{:,}".format(len(ps)), "{:,}".format(int(ps.rejected.sum())) if len(ps) else 0,
            ("  ·  %s selected" % "{:,}".format(nsel)) if nsel else ""))
        self._update_undo()

    def _box_lines3d(self):
        if not self.box or len(self.ps) == 0:
            return []
        b = self.box
        zk = (id(self.ps.z), len(self.ps))                 # the loaded area's ground level, once per load
        if getattr(self, "_z0_key", None) != zk:
            zz = self.ps.z if len(self.ps) <= 2_000_000 else self.ps.z[::max(1, len(self.ps) // 2_000_000)]
            self._z0, self._z0_key = float(np.percentile(zz, 2)), zk
        z0 = self._z0
        cor = rect_corners(b["cx"], b["cy"], b["length"], b["width"], b["bearing"])
        out = [(np.column_stack([np.r_[cor[:, 0], cor[0, 0]], np.r_[cor[:, 1], cor[0, 1]], np.full(5, z0)]),
                "#ffffff")]                                  # the Inspection Area outline: white
        for l, tr in self.tracks.items():
            if not self.line_enabled.get(l, True):
                continue
            uu, vv = to_box_frame(tr["E"], tr["N"], b["cx"], b["cy"], b["bearing"])
            m = (np.abs(uu) <= b["length"] / 2) & (np.abs(vv) <= b["width"] / 2)
            if m.sum() > 1:
                out.append((np.column_stack([tr["E"][m], tr["N"][m], tr["Z"][m]]),
                            self.project.lines[l].get("colour", theme.ACCENT)))
        return out

    def _plan_slice_lines(self):
        if not self.box:
            return []
        b = self.box
        br = math.radians(b["bearing"])
        u = np.array([math.sin(br), math.cos(br)])
        v = np.array([math.cos(br), -math.sin(br)])
        c = np.array([b["cx"], b["cy"]])
        along, pos, th, full = self._slice()
        out = []
        if along:
            if full:
                return []
            for s in (pos - th / 2, pos + th / 2):
                out.append((tuple(c - u * b["length"] / 2 + v * s), tuple(c + u * b["length"] / 2 + v * s),
                            theme.BRIGHT, False))
        else:
            for s in (pos - th / 2, pos + th / 2):
                out.append((tuple(c + u * s - v * b["width"] / 2), tuple(c + u * s + v * b["width"] / 2),
                            theme.BRIGHT, False))
        return out

    def _slice_overlay(self, p, view):
        if not self.box:
            return
        b = self.box
        along, pos, th, full = self._slice()
        for l, tr in self.tracks.items():
            if not self.line_enabled.get(l, True):
                continue
            uu, vv = to_box_frame(tr["E"], tr["N"], b["cx"], b["cy"], b["bearing"])
            col = QtGui.QColor(self.project.lines[l].get("colour", theme.ACCENT))
            if along:
                m = np.abs(uu) <= b["length"] / 2
                if m.sum() < 2:
                    continue
                sx, sy = view.to_screen(uu[m], tr["Z"][m])
                p.setPen(QtGui.QPen(col, 1))
                p.drawPolyline(QtGui.QPolygonF([QtCore.QPointF(a, bb) for a, bb in zip(sx, sy)]))
            else:
                m = np.abs(uu - pos) <= max(th, 1.0)
                if not m.any():
                    continue
                j = np.flatnonzero(m)[0]
                sx, sy = view.to_screen(vv[j], tr["Z"][j])
                p.setPen(QtGui.QPen(col, 1.5))
                p.setBrush(col)
                p.drawEllipse(QtCore.QPointF(sx, sy), 4, 4)
                p.drawText(QtCore.QPointF(sx + 6, sy - 6), "L%d" % (l + 1))

    # ------------------------------------------------------------------ linked cursor (2.1.0)
    LINK_RADIUS = 10                             # px: the point under the mouse, else the cursor position

    def _hover_at(self, key, a, b):
        self._hover = (key, a, b)
        if not self._hover_timer.isActive():
            self._hover_timer.start()

    def _hover_off(self):
        self._hover = None
        self._hover_timer.stop()
        self._set_linked(None, None)

    def linked_point(self, key, a, b):
        """(x, y, z or None) in local project coordinates of what is under the mouse in a view: the nearest shown
        point within LINK_RADIUS pixels, else the cursor's place (the Plan has no height there)."""
        ps = self.ps
        if ps is None or len(ps) == 0:
            return None
        z = ps.z if self.preview is None else self.preview.z
        if key == "3d":
            j = self.v3d.pick(a, b, self.LINK_RADIUS)
            if j < 0:
                return None
            i = int(self.v3d.idx[j])
            return float(ps.x[i]), float(ps.y[i]), float(z[i])
        v = self.plan if key == "plan" else self.slice
        sx, sy = v.to_screen(a, b)
        j = -1
        if v._sx is not None and len(v._sx):
            r = self.LINK_RADIUS
            d2 = (v._sx - float(sx)) ** 2 + (v._sy - float(sy)) ** 2
            cand = np.flatnonzero(d2 <= r * r)
            if len(cand):
                # the point that is drawn there: the top one in the Plan (highest key), else the nearest
                j = int(cand[np.argmax(v.key[cand])]) if key == "plan" and v.key is not None else \
                    int(cand[np.argmin(d2[cand])])
        if j >= 0 and v.idx is not None and j < len(v.idx):
            i = int(v.idx[j])
            return float(ps.x[i]), float(ps.y[i]), float(z[i])
        if key == "plan":
            return float(a), float(b), None
        if not self.box:
            return None
        along, pos, _th, _full = self._slice()
        u, w = (a, pos) if along else (pos, a)
        br = math.radians(self.box["bearing"])
        return (self.box["cx"] + u * math.sin(br) + w * math.cos(br),
                self.box["cy"] + u * math.cos(br) - w * math.sin(br), float(b))

    def _update_linked(self):
        if self._hover is None:
            return
        key, a, b = self._hover
        try:
            pt = self.linked_point(key, a, b)
        except Exception:                       # noqa: BLE001 - a view mid-update: next move
            pt = None
        self._set_linked(key, pt)

    def _set_linked(self, source, pt):
        """Mark pt (x, y, z or None) in every view but the source: a cross on the point in the Plan and the 3D view
        (a dashed vertical line in 3D when there is no height), and always a dashed vertical line at its place
        across the slice in the Slice view (Sean's choice, 10 Nov 2026)."""
        marks = {"plan": None, "slice": None, "3d": None}
        if pt is not None and self.box:
            x, y, z = pt
            marks["plan"] = ("plus", x, y)
            b = self.box
            u, w = to_box_frame(np.array([x]), np.array([y]), b["cx"], b["cy"], b["bearing"])
            u, w = float(u[0]), float(w[0])
            along, pos, th, _full = self._slice()
            a_ = u if along else w
            inside = abs(u) <= b["length"] / 2 + 1e-6 and abs(w) <= b["width"] / 2 + 1e-6
            if inside:
                marks["slice"] = ("line", a_)        # Sean: the Slice always shows a vertical line (HYPACK style)
            if z is not None:
                marks["3d"] = ("plus", (x, y, z))
            elif inside and len(self.ps):
                z0 = getattr(self, "_z0", float(np.percentile(self.ps.z[::max(1, len(self.ps) // 200000)], 2)))
                marks["3d"] = ("line", x, y, z0, z0 + 25.0)
        for k, v in (("plan", self.plan), ("slice", self.slice), ("3d", self.v3d)):
            m = None if k == source else marks[k]
            if getattr(v, "linked", None) != m:
                v.linked = m
                v.update()

    def _cursor(self, view, a, b):
        o = self._origin()
        if view is None or view.kind == "plan":
            txt = "E %.3f  N %.3f" % (a + o[0], b + o[1])
            if self.plan.attr == "Span" and self.plan.has_underlay():
                v = self.plan.value_at(a, b)          # 2.4.0: the cell's span under the cursor
                if v is not None:
                    txt += "  ·  span %s (%g m cell)" % ("%.0f cm" % (100 * v) if v < 1 else "%.2f m" % v,
                                                      getattr(self.plan, "cell", 0.5))
            self.lbl_xyz.setText(txt)
        elif view.kind == "profile" and self.box:
            self.lbl_xyz.setText("chainage %.3f  Z %.3f" % (a + self.box["length"] / 2, b + o[2]))
        else:
            self.lbl_xyz.setText("offset %+.3f  Z %.3f" % (a, b + o[2]))

    # ------------------------------------------------------------------ right-click menus of the views
    def _view_menu(self, m, a, b, view):
        if self.selected is not None and self.selected.any():
            n = int(self.selected.sum())
            sm = m.addMenu("Selection (%s points)" % "{:,}".format(n))
            sm.addAction("Reject the selection", lambda: self.edit_selected("reject"))
            sm.addAction("Accept the selection", lambda: self.edit_selected("accept"))
            cm = sm.addMenu("Classify the selection as")
            for k in CLASS_SHORTLIST:
                cm.addAction("%d  %s" % (k, F.ASPRS.get(k, "")), lambda k=k: self.edit_selected("class", k))
            sm.addAction("Run the filter group on the selection", lambda: self.edit_selected("filter"))
            sm.addAction("Clear the selection", self.clear_selection)
            theme.dark_menu(sm)
            theme.dark_menu(cm)
            m.addSeparator()
        if view is self.plan:
            m.addAction("Place the Inspection Area here", lambda: self._place_box(a, b))
            m.addAction("Inspection Area properties...", self.show_area_properties)
            m.addAction("Place the slice here", lambda: self._slice_at(a, b))
            m.addAction("Align the Inspection Area with the nearest line", lambda: self.plan.align_to_track(a, b))
            m.addSeparator()
            m.addAction("Zoom to the Inspection Area", self.plan.zoom_box)
            m.addAction("Zoom to the whole project", self.plan.zoom_project)
            m.addAction("New Inspection Area: Shift + drag with the Pan tool").setEnabled(False)
        if view is self.slice:
            m.addAction("Rotate the slice 90 degrees", self.slice_pane._rotate)
            m.addAction("Previous slice", lambda: self.step("sec_prev"))
            m.addAction("Next slice", lambda: self.step("sec_next"))

    def _place_box(self, x, y):
        if self.box:
            b = dict(self.box)
            b["cx"], b["cy"] = x, y
            self.set_box(b)

    def _slice_at(self, x, y):
        if not self.box:
            return
        b = self.box
        u, v = to_box_frame(np.array([x]), np.array([y]), b["cx"], b["cy"], b["bearing"])
        if abs(u[0]) > b["length"] / 2 or abs(v[0]) > b["width"] / 2:
            nb = dict(b)
            nb["cx"], nb["cy"] = x, y
            self.set_box(nb)
            u, v = np.array([0.0]), np.array([0.0])
        self.slice_pane.set_position(float(v[0] if self.slice_pane.along else u[0]), self._slice_half())
        self._slice_changed()

    # =============================================================== editing
    def edit_selection(self, view, mask):
        if self.edits is None or len(self.ps) == 0 or self.edits.ps is None:
            return
        if self.action != "select" and self._guard("editing"):
            return
        if self.where == "outside":
            mask = ~mask
        idx = view.idx[mask]
        if self.edit_line >= 0:
            idx = idx[self.ps.line[idx] == self.edit_line]
        if self.action == "select":
            add = bool(QtWidgets.QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier)
            if self.selected is None or not add or len(self.selected) != len(self.ps):
                self.selected = np.zeros(len(self.ps), bool)
            self.selected[idx] = True
            self.status("%s points selected (Shift adds); right click for what to do with them, Delete rejects"
                        % "{:,}".format(int(self.selected.sum())), 8000)
            self.refresh_views()
            return
        self._edit_idx(idx, self.action, self.edit_class)

    def edit_selected(self, action, cls=None):
        if self.selected is None or not self.selected.any() or self._guard("editing"):
            return
        idx = np.flatnonzero(self.selected)
        self.selected = None
        self._edit_idx(idx, action, cls if cls is not None else self.edit_class)

    def clear_selection(self):
        self.selected = None
        self.plan.highlight_line = None
        self.refresh_views()

    def _edit_idx(self, idx, action, cls):
        if len(idx) == 0:
            return
        if self.preview is not None:
            self.clear_preview(refresh=False)
            self.filters.preview_done()
        if action == "filter":
            g = self.filters.group
            if g is None:
                return
            scope = np.zeros(len(self.ps), bool)
            scope[idx] = True
            ctx = F.Ctx(self.ps, tracks=self.tracks, scope_mask=scope, origin=self._origin(),
                        reference=self._reference())
            try:
                g.run(ctx)
            except Exception as ex:
                self._error("Filter failed", ex)
                return
            act = self.edits.apply_ctx(ctx, "Filter '%s' on a selection" % g.name, scope=scope)
            self.edits.commit(act)
            if act is not None:
                self.project.record_filter(g.to_dict(), "selection", act.size)
            if ctx.messages:
                self.status("; ".join(ctx.messages), 8000)
        elif action == "class":
            self.edits.apply(idx, "class", int(cls))
        else:
            self.edits.apply(idx, action)
        self.after_edit()

    def after_edit(self):
        self._edit_rev = getattr(self, "_edit_rev", 0) + 1     # a preview from before this edit is stale
        self._col_cache = {}
        self._fill_classes()
        self.refresh_views()
        self._area_info()

    def _edited(self, act):
        self._refresh_history()
        self._update_undo()

    def _update_undo(self):
        if self.edits is None:
            return
        self.b_undo.setEnabled(self.edits.can_undo() and not self._busy)
        self.b_redo.setEnabled(self.edits.can_redo() and not self._busy)
        self.b_undo.setToolTip("Undo " + (self.edits.undo_stack[-1].label if self.edits.undo_stack else "") +
                               " (Ctrl+Z)")

    def undo(self):
        if self._guard("undoing"):
            return
        if self.edits and self.edits.undo():
            self.after_edit()

    def redo(self):
        if self._guard("redoing"):
            return
        if self.edits and self.edits.redo():
            self.after_edit()

    def _rebuild_dirty_grids(self):
        """Grids of tiles edited by hand, rebuilt in a worker thread (a tile of 10 M points takes about half
        a second, which would freeze the window); the Plan redraws those tiles when the worker is done. The
        tiled filter runs rebuild theirs in their own worker."""
        st = self.project.store if self.project else None
        if st is None or self._busy:
            return
        d = getattr(st, "dirty_tiles", set())
        if not d or (self._grid_worker is not None and self._grid_worker.is_alive()):
            return
        keys = list(d)
        d.difference_update(keys)              # edits made meanwhile mark their tiles again

        def work(keys=keys, st=st):
            done = []
            for k in keys:
                try:
                    st.build_grid(k)
                    done.append(k)
                except Exception:       # noqa: BLE001  (a tile changing under us: rebuilt on the next tick)
                    st.dirty_tiles = getattr(st, "dirty_tiles", set()) | {k}
            try:
                self.bridge.grids.emit(done)
            except RuntimeError:        # the window is gone
                pass
        import threading
        self._grid_worker = threading.Thread(target=work, daemon=True, name="CloudPro grids")
        self._grid_worker.start()

    def _grids_rebuilt(self, keys):
        if keys and self.project is not None:
            self.plan.invalidate(keys)

    def _wait_grids(self, timeout=30.0):
        """Let a running grid rebuild finish (before a job, a save or closing)."""
        w = self._grid_worker
        if w is not None and w.is_alive():
            w.join(timeout)

    def _refresh_history(self):
        if self.project is None:
            return
        self.history.clear()
        for h in self.project.data.get("history", [])[-400:]:
            self.history.addItem("%s  %s" % (h["utc"], h["text"]))
        self.history.scrollToBottom()
        self.refresh_workflow()

    # ================================================================== info
    def show_point(self, j):
        if j is None or j < 0 or j >= len(self.ps):
            return
        ps = self.ps
        o = self._origin()
        t = _dt.datetime.fromtimestamp(float(ps.t[j]), _dt.timezone.utc)
        fl = int(ps.flag[j])
        st = "rejected" if fl & REJECTED else "accepted"
        why = [n for b, n in ((REJ_MANUAL, "manual"), (REJ_AUTO, "automatic"), (REJ_ORIG, "original")) if fl & b]
        ln = self.project.lines[ps.line[j]]
        fk = int(self.file_idx[j]) - self._file_base.get(ln["id"], 0) if len(self.file_idx) > j else 0
        fname = os.path.basename(ln["files"][fk]) if 0 <= fk < len(ln.get("files", [])) else ""
        txt = ("E        %.3f\nN        %.3f\nZ        %.3f\n" % (ps.x[j] + o[0], ps.y[j] + o[1], ps.z[j] + o[2]) +
               "time     %s.%06d UTC\n" % (t.strftime("%Y-%m-%d %H:%M:%S"), t.microsecond) +
               "line     L%d  %s\nfile     %s\n" % (ps.line[j] + 1, ln.get("name", ""), fname) +
               "laser    %d   return %s\n" % (ps.ch[j] + 1, {0: "last", 1: "strongest", 2: "single"}[int(ps.ret[j])]) +
               "range    %.2f m   azimuth %.2f deg\n" % (ps.rng[j] * 0.01, ps.az[j] * 0.01) +
               "intens.  %d\nclass    %d  %s\nstatus   %s %s\n" % (ps.i[j], ps.cls[j], F.ASPRS.get(int(ps.cls[j]), ""),
                                                                 st, ("(" + ", ".join(why) + ")") if why else "") +
               ("dz       %+.3f m\n" % (ps.dz[j] * 1e-3) if ps.dz[j] else ""))
        self.info.setPlainText(txt + "\n" + self._area_text())
        self.show_dock("info")

    def _area_text(self):
        ps = self.ps
        if len(ps) == 0:
            return ""
        rej = ps.rejected
        lines = ["AREA  %s points, %s rejected (%.2f%%)" % ("{:,}".format(len(ps)), "{:,}".format(int(rej.sum())),
                                                            100 * rej.mean())]
        if self.selected is not None and self.selected.any():
            lines.append("      %s selected" % "{:,}".format(int(self.selected.sum())))
        for l in np.unique(ps.line):
            m = ps.line == l
            lines.append("  L%-3d %10s pts  %8s rej" % (l + 1, "{:,}".format(int(m.sum())),
                                                        "{:,}".format(int((rej & m).sum()))))
        return "\n".join(lines)

    def _area_info(self):
        self.info.setPlainText(self._area_text())

    # ================================================================== filters
    def _reference(self):
        p = self.filters.reference()
        if not p:
            return None
        try:
            from ..core.export import raster_sampler
            return raster_sampler(p, self._origin())
        except Exception as ex:     # noqa: BLE001
            self.status("Reference raster: %s" % ex, 8000)
            return None

    def _scope_mask(self, scope):
        ps = self.ps
        if len(ps) == 0 or scope != "Slice":
            return None
        b = self.box
        u, v = to_box_frame(ps.x, ps.y, b["cx"], b["cy"], b["bearing"])
        along, pos, th, full = self._slice()
        if along:
            return np.ones(len(ps), bool) if full else np.abs(v - pos) <= th / 2
        return np.abs(u - pos) <= th / 2

    def tune_filters(self):
        """Measure the noise of the loaded points, combine it with the XT-32 / Certus error budget and the
        trajectory's own standard deviations, and set the filters' tolerances from it."""
        from ..core import precision as PR
        if len(self.ps) < 1000:
            theme.msg_info(self, "CloudPro", "Load an Inspection Area with points (ideally on a road seen by two or more "
                           "passes) first: the noise is measured on its ground.")
            return
        p = self.project
        smr = None
        if p.dcs is not None:
            try:
                from dcs_core import handover
                src = ((p.data.get("inputs") or {}).get("trajectory") or {}).get("source")
                doc = handover.read(p.dcs, "strippro" if src == "strippro" else "pospro", required=False)
                smr = (doc or {}).get("paths", {}).get("smrmsg")
            except Exception:       # noqa: BLE001
                smr = None
        cls = self.preview.cls if self.preview is not None else None
        pr = PR.assess(self.ps, (p.data.get("inputs") or {}).get("trajectory"), smr, cls)
        groups = [F.FilterGroup.from_dict(g.to_dict()) for g in self.filters.groups]
        changes = PR.tune(groups, pr)
        d = QtWidgets.QDialog(self)
        d.setWindowTitle("Tune the filters to the sensor precision")
        theme.polish_dialog(d)
        lay = QtWidgets.QVBoxLayout(d)
        t = QtWidgets.QTextBrowser()
        t.setHtml("<h3 style='color:%s'>Precision of this data</h3>%s<p style='color:%s'>Measured on the ground of the "
                  "Inspection Area; the larger of measurement and specification is used. Feature settings that "
                  "describe the objects (paint contrast, curb height, rail shape) are only raised above the noise, "
                  "never lowered.</p>" % (theme.ACCENT, PR.report_html(pr, changes), theme.DIM))
        t.setMinimumSize(900, 520)
        lay.addWidget(t)
        bb = QtWidgets.QHBoxLayout()
        b_cur = QtWidgets.QPushButton("Apply to this group")
        b_all = QtWidgets.QPushButton("Apply to all groups")
        b_all.setObjectName("ppPrimary")
        b_no = QtWidgets.QPushButton("Close")
        for b in (b_cur, b_all, b_no):
            bb.addWidget(b)
        lay.addLayout(bb)
        res = {"which": None}
        b_cur.clicked.connect(lambda: (res.update(which="one"), d.accept()))
        b_all.clicked.connect(lambda: (res.update(which="all"), d.accept()))
        b_no.clicked.connect(d.reject)
        b_cur.setEnabled(bool(changes))
        b_all.setEnabled(bool(changes))
        if exec_(d) != QtWidgets.QDialog.DialogCode.Accepted or not res["which"]:
            return
        cur = self.filters.group
        targets = [cur] if res["which"] == "one" else list(self.filters.groups)
        n = len(PR.tune(targets, pr))
        p.data["precision"] = {k: pr[k] for k in ("s_pass", "s_between", "s_all", "s_horiz", "measured_pass",
                                                  "measured_between", "road_intensity_median", "road_intensity_sd")}
        p.data["precision"]["trajectory"] = pr["traj"]
        p.save()
        self.filters.save()
        self.filters.refresh_values()
        p.run_log("tune", ["%s / %s: %s %s -> %s (%s)" % c for c in changes])
        self.status("%d filter settings tuned to the data" % n, 8000)

    def run_preview(self):
        if self.edits is None or len(self.ps) == 0 or self.filters.group is None or self._busy:
            self.filters.preview_done()
            return
        if self._prev_cancel is not None:
            self._prev_cancel[0] = True
            self._prev_pending = True
            return
        g = F.FilterGroup.from_dict(self.filters.group.to_dict())
        scope_name = self.filters.cmb_scope.currentText()
        scope = self._scope_mask(scope_name)
        ps = self.ps
        flag = [False]
        self._prev_cancel = flag
        self._prev_key = (repr(g.to_dict()), scope_name, id(ps), getattr(self, "_edit_rev", 0))
        ref, tracks, origin = self._reference(), self.tracks, self._origin()
        self.filters.set_status("Preview running...")

        def work():
            t = time.time()
            try:
                ctx = F.Ctx(ps, tracks=tracks, scope_mask=scope, cancel=lambda: flag[0], origin=origin, reference=ref)
                g.run(ctx)
                res = (ctx, time.time() - t, ps)
            except Exception as ex:     # noqa: BLE001
                res = ex
                traceback.print_exc()
            self.bridge.done.emit("preview", res)
        threading.Thread(target=work, daemon=True).start()

    def _preview_done(self, res):
        cancelled = self._prev_cancel is not None and self._prev_cancel[0]
        self._prev_cancel = None
        if self._prev_pending:
            self._prev_pending = False
            self.run_preview()
            return
        if cancelled or not self.filters.btn_prev.isChecked():
            return
        if isinstance(res, Exception):
            self.filters.set_status("Preview failed: %s" % res)
            return
        ctx, dt_, ps = res
        if ps is not self.ps:
            return
        self.preview = ctx
        self.preview_key = getattr(self, "_prev_key", None)
        newly = int((((ctx.flag & REJECTED) != 0) & ~self.ps.rejected).sum())
        reacc = int((((ctx.flag & REJECTED) == 0) & self.ps.rejected).sum())
        ncls = int((ctx.cls != self.ps.cls).sum())
        tim = ", ".join("%s %.1fs" % (n, t) for n, t in getattr(ctx, "timings", []))
        perflog.log(self.project, "preview", "'%s' on %s points: %.1f s (%s)" % (
            self.filters.group.name, "{:,}".format(len(ps)), dt_, tim))
        self.filters.set_status("Preview: %s to reject (orange), %s re-accepted, %s re-classified in %.1f s\n%s%s" % (
            "{:,}".format(newly), "{:,}".format(reacc), "{:,}".format(ncls), dt_, tim,
            ("\n" + "\n".join(ctx.messages)) if ctx.messages else ""))
        self._col_cache = {}
        self._fill_classes()
        self.refresh_views()

    def clear_preview(self, refresh=True):
        if self._prev_cancel is not None:
            self._prev_cancel[0] = True
        had = self.preview is not None
        self.preview = None
        if had:
            self._col_cache = {}
            if refresh:
                self._fill_classes()
                self.refresh_views()

    def accept_preview(self):
        if self.preview is None or self.edits is None or self.edits.ps is None or self._guard("accepting"):
            return
        g = self.filters.group
        act = self.edits.apply_ctx(self.preview, "Filter '%s' (preview accepted)" % (g.name if g else ""))
        self.edits.commit(act)
        if act is not None and g is not None:
            self.project.record_filter(g.to_dict(), "inspection area (preview accepted)", act.size)
        self.preview = None
        self.filters.preview_done()
        self.filters.set_status("Accepted: %s points changed" % ("{:,}".format(act.size) if act else 0))
        self.after_edit()

    def run_scope(self, scope):
        if self.edits is None or self.filters.group is None or self._guard("running another filter"):
            return
        g = F.FilterGroup.from_dict(self.filters.group.to_dict())
        if scope in ("Inspection Area", "Slice"):
            # the live preview already ran exactly this group on these points: use its result, no second run
            key = (repr(g.to_dict()), scope, id(self.ps), getattr(self, "_edit_rev", 0))
            if self.preview is not None and self._prev_cancel is None and getattr(self, "preview_key", None) == key:
                self.accept_preview()
                return
        # never two runs of a group at once: a preview still computing is stopped (it would share the cores and
        # double the memory)
        if self._prev_cancel is not None:
            self._prev_cancel[0] = True
            self._prev_pending = False
        if self.filters.btn_prev.isChecked():
            self.clear_preview(refresh=False)
            self.filters.preview_done()
        if scope in ("Inspection Area", "Slice"):
            mask = self._scope_mask(scope)
            ps = self.ps
            tracks, origin, ref = self.tracks, self._origin(), self._reference()

            def job(progress, cancel):
                ctx = F.Ctx(ps, tracks=tracks, scope_mask=mask, cancel=cancel, origin=origin, reference=ref)
                g.run(ctx, progress=lambda f, n: progress(f, n))
                return ctx, ps
            self._run("filter_area", job, "Filter '%s' on the %s" % (g.name, scope.lower()))
            return
        lines = None
        if scope.startswith("Selected"):
            lines = [self.edit_line] if self.edit_line >= 0 else [l for l, on in self.line_enabled.items() if on]
        if theme.msg_question(self, "CloudPro", "Run '%s' on %s?\n\nThis edits the stored points tile by tile.%s" % (
                g.name, "the selected lines" if lines else "the entire project",
                "" if self.project.store.total < 20_000_000 else
                "\nLarge runs cannot be undone; use 'Reset modifications' to revert automatic rejections.")) != \
                QtWidgets.QMessageBox.StandardButton.Yes:
            return
        from ..core.runner import run_group_tiled
        project, edits = self.project, self.edits
        self.edits.ps = None
        # the worker commits the undo step: no window callback from that thread (given back in _on_done)
        self.edits.on_change = None

        def job(progress, cancel):
            def inner():
                st = {}
                try:
                    n = run_group_tiled(project, g, edits, lines=lines, progress=progress, cancel=cancel, stats=st)
                except IM.Cancelled as e:
                    return e                    # not a failed run of the stage: nothing was written
                project.record_filter(g.to_dict(), scope, n)
                perflog.log(project, "filter", "'%s' on %s: %s changed in %.1f s; %d parts, %d threads (%d at "
                            "once), %.0f GB memory allowed, %s of %s points loaded with buffers, %d parts with a "
                            "smaller buffer" % (g.name, "the selected lines" if lines else "the entire project",
                                                "{:,}".format(n), st.get("total_s", 0), st.get("units", 0),
                                                st.get("workers", 0), st.get("peak_running", 0),
                                                st.get("memory_gb", 0), "{:,}".format(st.get("loaded", 0)),
                                                "{:,}".format(st.get("points", 0)), st.get("reduced_buffer", 0)))
                return n
            if project.dcs is not None:
                with project.dcs.run("cloudpro", PLUGIN, VERSION):
                    res = inner()
            else:
                res = inner()
            if isinstance(res, Exception):
                raise res
            return res
        self._run("filter_tiled", job, "Filter '%s'" % g.name)

    # ================================================================== export
    def export(self):
        if self.project is None or self._guard("exporting"):
            return
        d = ExportDialog(self, self.project.lines, os.path.join(self.project.root, self.project.name + ".las"),
                         self.project.store.class_counts(), self.project.data.get("export_classes"))
        if exec_(d) != QtWidgets.QDialog.DialogCode.Accepted:
            return
        from ..core import export as EX
        path, kind = d.path.text().strip(), d.kind.currentIndex()
        box = self.box if d.scope.currentText() == "Inspection Area" else None
        lines, classes = d.selected_lines(), d.classes.selected()
        self.project.data["export_classes"] = sorted(classes) if classes is not None else None
        inc, per_line = d.rejected.isChecked(), d.per_line.isChecked()
        cell, stat, fill = float(d.cell.currentData()), d.stat.currentText(), d.fill.value()
        spacing, thin_mode = float(d.thin.currentData() or 0) or None, d.thin_mode.currentData()
        tile = int(d.tile.currentData() or 0) or None
        add = d.add_qgis.isChecked()
        project = self.project
        if tile and kind in (0, 1):
            nt = EX.plan(project, box, tile)
            if nt > 400 and theme.msg_question(self, "CloudPro", "%d m tiles make up to %d files here.\n\nExport "
                                               "anyway?" % (tile, nt)) != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        project.store.sync()

        def job(progress, cancel):
            if kind == 0:
                return ("las", path, EX.export_las(project, path, box, lines, inc, classes, per_line, progress,
                                                   cancel, spacing=spacing, thin_mode=thin_mode, tile=tile), add)
            if kind == 1:
                return ("xyz", path, EX.export_xyz(project, path, box, lines, inc, classes, progress, cancel,
                                                   spacing=spacing, thin_mode=thin_mode, tile=tile), add)
            p, n = EX.export_grid(project, path, cell, stat, box, lines, classes, fill, progress, cancel)
            return ("grid", p, n, add)
        self._run("export", job, "Exporting")

    def pick_on_map(self):
        if self.map_pick is None or self.iface is None:
            theme.msg_info(self, "CloudPro", "Available inside QGIS")
            return
        self.map_pick()

    def add_qgis_layers(self):
        if self.iface is None or self.project is None:
            theme.msg_info(self, "CloudPro", "Available inside QGIS")
            return
        from . import qgis_layers
        qgis_layers.add_project_layers(self.iface, self.project, self.tracks, self.box)

    def _raster_layers(self):
        try:
            from qgis.core import QgsProject, QgsRasterLayer
            self.filters.set_raster_layers([(l.name(), l.source()) for l in QgsProject.instance().mapLayers().values()
                                            if isinstance(l, QgsRasterLayer)])
        except Exception:       # noqa: BLE001
            pass

    # ========================================================= background jobs
    def _guard(self, what="this"):
        """True (and a status message) while a job runs: actions that would change the points, the project or
        start another job wait until it has finished."""
        if self._busy:
            self.status("Wait until '%s' has finished (or press Cancel) before %s" % (
                (self._busy_tag or "the task").replace("_", " "), what), 6000)
            return True
        return False

    def _lock_actions(self, busy):
        """While a job runs, everything that would change the points or the project, or start another job, is
        off (looking at the points, navigating, the guide and Cancel stay on)."""
        if busy:
            self._locked = []
            ws = [self.btn_project, self.header.steps, self.filters, self.b_undo,
                  self.b_redo, self.b_action, self.b_where, self.cmb_class, self.cmb_eline, self.infop]
            if self._props is not None:
                ws.append(self._props)
            for w in ws:
                if w is not None and w.isEnabled():
                    w.setEnabled(False)
                    self._locked.append(w)
        else:
            for w in self._locked:
                try:
                    w.setEnabled(True)
                except RuntimeError:            # deleted meanwhile
                    pass
            self._locked = []
            self._update_undo()

    def _run(self, tag, fn, label):
        if self._busy:
            theme.msg_info(self, "CloudPro", "Another task is still running.")
            return
        self._wait_grids()                       # a job may replace or close the store
        self._busy = True
        self._busy_tag = tag
        self._lock_actions(True)
        self.header.set_busy(True)              # the project box turns red while CloudPro works
        self._cancel_flag = [False]
        self._job_t0 = (tag, label, time.time())
        cancel = lambda: self._cancel_flag[0]      # noqa: E731
        self.prog.setValue(0)
        self.prog.show()
        self.btn_cancel.show()
        self.status(label + "...", 0)

        def progress(f, msg=None):
            self.bridge.progress.emit(float(f), msg or "")

        def work():
            try:
                res = fn(progress, cancel)
            except Exception as ex:     # noqa: BLE001
                traceback.print_exc()
                res = ex
            try:
                self.bridge.done.emit(tag, res)
            except RuntimeError:                # the window is gone (QGIS closing)
                pass
        self._thread = threading.Thread(target=work, daemon=True)
        self._thread.start()

    def _cancel(self):
        if getattr(self, "_cancel_flag", None):
            self._cancel_flag[0] = True

    def _on_progress(self, f, msg):
        self.prog.setValue(int(1000 * f))
        if msg:
            self.status(msg, 0)

    def _after_points_changed(self):
        self.tracks = self.project.tracks()
        self._ensure_file_spans()
        self._fill_lines()
        self.edits = EditManager(self.project, on_change=self._edited)
        self.plan.set_project(self.project.store, self.tracks, {l["id"]: l["colour"] for l in self.project.lines})
        self._load_separation()
        self.plan.b_offset_z = float(self._origin()[2])
        self._track_masks()
        if self._area is not None:
            self._area.set_lines(self.project.lines)
        if self.box is None or not self.project.data.get("view", {}).get("box"):
            self.set_box(self._default_box(), load=True, fit=True)
            self.plan.zoom_box()
        else:
            self.load_box(fit=True)
        self._refresh_history()
        self.refresh_chain()
        if self._props is not None and self._props.isVisible():
            self._props.refresh()

    def _log_job(self, tag, res):
        """Every job's time in the performance log (georeferencing, tiled filters, publish, export ...)."""
        j, self._job_t0 = getattr(self, "_job_t0", None), None
        if not j or j[0] != tag or self.project is None:
            return
        from ..core.export import Cancelled as ExportCancelled
        how = "cancelled" if isinstance(res, (IM.Cancelled, ExportCancelled)) else \
            ("failed: %s" % res) if isinstance(res, Exception) else "done"
        extra = ""
        if tag == "georef" and not isinstance(res, Exception):
            try:
                extra = ", %s points" % "{:,}".format(int(self.project.store.total))
            except Exception:                   # noqa: BLE001
                pass
        perflog.log(self.project, "job", "%s (%s): %.1f s, %s%s%s" % (
            j[1], tag, time.time() - j[2], how, extra, " (automatic)" if getattr(self, "_auto_georef", False) else ""))

    def _gpu_status(self, text):
        self.status("3D view: " + text, 8000)
        perflog.log(self.project, "3d", "drawn by the " + text)

    def performance_report(self):
        """MENU > Performance report...: one text file with this computer, the job, the performance log, recent
        History and run logs - to send for review."""
        if self.project is None:
            theme.msg_info(self, "CloudPro", "Open a job first: the report is written from its performance log.")
            return
        f3 = self.v3d.take_frame_stats()
        if f3:
            perflog.log(self.project, "3d", f3)
        self.save_project(quiet=True)
        name = "CloudPro_performance_%s_%s.txt" % (self.project.name, time.strftime("%Y%m%d_%H%M"))
        home = os.path.expanduser("~")
        start = next((d for d in (os.path.join(home, "Desktop"), os.path.join(home, "Documents"), home)
                      if os.path.isdir(d)), home)
        p, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save the performance report", os.path.join(start, name),
                                                     "Text (*.txt)")
        if not p:
            return
        try:
            perflog.write_report(self.project, p, extra=["Inspection Area: %s" % (
                ", ".join("%s %.1f" % (k, v) for k, v in (self.box or {}).items() if isinstance(v, (int, float))))])
        except Exception as e:                  # noqa: BLE001
            theme.msg_info(self, "CloudPro", "The report could not be written: %s" % e)
            return
        self.status("Performance report saved: %s" % p, 15000)
        theme.msg_info(self, "CloudPro", "Performance report saved:\n%s\n\nSend this file for review." % p)

    def _on_done(self, tag, res):
        if self._closing:
            return
        if tag == "preview":
            self._preview_done(res)
            return
        self._busy = False
        self._busy_tag = None
        self._thread = None
        self._log_job(tag, res)
        self._lock_actions(False)
        self.header.set_busy(False)
        self.prog.hide()
        self.btn_cancel.hide()
        self.status("", 1)
        if self.edits is not None and self.edits.on_change is None:
            self.edits.on_change = self._edited             # given back after a tiled filter run
        pend, self._pending_geo = self._pending_geo, None
        from ..core.export import Cancelled as ExportCancelled
        if isinstance(res, (IM.Cancelled, ExportCancelled)) and tag == "georef":
            self._auto_georef = False
        if isinstance(res, (IM.Cancelled, ExportCancelled)):
            self.status("%s cancelled: %s" % (tag.replace("_", " ").title(),
                                               "the previous points and edits are kept" if tag == "georef"
                                               else "nothing was written"), 10000)
            if tag == "filter_tiled":
                self._after_points_changed()
            elif tag == "georef":
                self.load_box()
            return
        if isinstance(res, Exception):
            if tag == "georef" and getattr(self, "_auto_georef", False):     # automatic: no box to dismiss
                self._auto_georef = False
                self.project.log("Automatic georeferencing failed: %s (the previous points and edits are kept)" % res)
            else:
                self._error("%s failed" % tag.replace("_", " ").title(), res)
            if tag == "filter_tiled":
                self._after_points_changed()
            elif tag == "georef":
                self.status("Georeferencing failed: the previous points and edits are kept", 10000)
                self.load_box()
            return
        if tag == "georef" and pend and pend.get("share") and self.project.dcs is not None:
            c = dict(self.project.dcs.shared("crs") or {})
            c["horizontal"] = self.project.crs
            self.project.dcs.set_shared("crs", c, PLUGIN)
        if tag == "georef":
            out, rest, inp = res
            n = sum(st["points"] for _, st in out)
            self._after_points_changed()
            msg = "Georeferenced %d line(s): %s points.\n\n%s" % (len(out), "{:,}".format(n), "\n".join(inp.summary()))
            if rest:
                msg += "\n\n%s edited points carried over from the previous georeferencing." % "{:,}".format(rest)
            if inp.warnings:
                msg += "\n\n" + "\n".join("! " + w for w in inp.warnings)
            if getattr(self, "_auto_georef", False):     # automatic: no message box to dismiss
                self._auto_georef = False
                short = "Georeferenced again automatically: %d line(s), %s points%s" % (
                    len(out), "{:,}".format(n), ", %s edits carried over" % "{:,}".format(rest) if rest else "")
                self.project.log(short)
                self.status(short, 15000)
            else:
                theme.msg_info(self, "CloudPro", msg)
        if tag == "georef" and getattr(self, "_auto_pending", None) and not isinstance(res, Exception):
            self._auto_pending = None
            from ..core import autoupdate
            ok, why2 = autoupdate.decide(self.project.dcs.path, self.project) if self.project.dcs else (False, "")
            if ok:
                QtCore.QTimer.singleShot(0, lambda: self.auto_update(why2))
        elif tag == "import":
            self._after_points_changed()
            theme.msg_info(self, "CloudPro", "Imported %d line(s), %s points" % (
                len(res), "{:,}".format(sum(st["points"] for _, st in res))))
        elif tag == "publish":
            doc, add = res
            self.refresh_chain()
            self._refresh_history()
            if add and self.iface is not None:
                from . import qgis_layers
                for k, f in doc["files"].items():
                    path = os.path.join(self.project.out_dir, f)
                    kind = "las" if f.endswith(".las") else "grid" if f.endswith((".tif", ".asc")) else None
                    if kind:
                        qgis_layers.add_file(self.iface, path, kind)
            theme.msg_info(self, "CloudPro", "Finalized cloud.json for the job (%s).\n\n%s\n\nFolder: %s%s" % (
                doc["calibration"], "\n".join("%s: %s" % kv for kv in doc["files"].items()), self.project.out_dir,
                ("\n\n" + "\n".join("! " + w for w in doc.get("warnings", []))) if doc.get("warnings") else ""))
        elif tag == "filter_area":
            ctx, ps = res
            if ps is not self.ps:
                return
            act = self.edits.apply_ctx(ctx, "Filter '%s'" % self.filters.group.name, scope=ctx.scope)
            self.edits.commit(act)
            if act is not None:
                self.project.record_filter(self.filters.group.to_dict(), self.filters.cmb_scope.currentText(),
                                           act.size)
            tim = getattr(ctx, "timings", [])
            ttxt = ("%s points in %.1f s: " % ("{:,}".format(len(ps)), sum(t for _n, t in tim)) +
                    ", ".join("%s %.1f s" % (n, t) for n, t in tim)) if tim else ""
            self.filters.set_status("%s points changed%s%s" % ("{:,}".format(act.size if act else 0),
                                                               ("\n" + ttxt) if ttxt else "",
                                                               ("\n" + "\n".join(ctx.messages)) if ctx.messages else ""))
            if ttxt:
                perflog.log(self.project, "filter", "'%s' on the %s: %s" % (
                    self.filters.group.name, self.filters.cmb_scope.currentText().lower(), ttxt))
                self.project.log("Filter '%s' on the %s: %s" % (self.filters.group.name,
                                                               self.filters.cmb_scope.currentText().lower(), ttxt))
            self.after_edit()
        elif tag == "filter_tiled":
            self.filters.set_status("%s points changed" % "{:,}".format(res))
            self.edits.ps = self.ps
            self.plan.invalidate()                  # the worker rebuilt the changed grids
            self.load_box()
            self._edited(None)
        elif tag == "separation":
            self.plan.set_separation(res, True)
            self.project.log("Line separation checked: %s" % SEP.verdict(res, self.plan.tolerance))
            self.project.run_log("line separation", SEP.report_lines(res, self.plan.tolerance, self.project.name))
            SEP.write_feedback(self.project, res, self.plan.tolerance)          # for PosPro's Review
            if self.plan_pane.grid.currentText() != SEPARATION:
                self.plan_pane.grid.setCurrentText(SEPARATION)
            self.show_separation()
        elif tag == "export":
            kind, path, n, add = res
            files = getattr(self.project, "last_export_files", None) if kind != "grid" else None
            files = files if files else [path]
            where = path if len(files) == 1 else "%d files in %s" % (len(files), os.path.dirname(files[0]))
            self.status("Exported %s to %s" % ("{:,}".format(n), where), 10000)
            if add and self.iface is not None:
                from . import qgis_layers
                for fp in files[:50]:                    # many tiles: the first 50 on the map
                    qgis_layers.add_file(self.iface, fp, kind)
            theme.msg_info(self, "CloudPro", "Exported %s %s to\n%s%s" % (
                "{:,}".format(n), "cells" if kind == "grid" else "points", where,
                "\n\nThe first 50 tiles were added to the map." if add and len(files) > 50 else ""))
        self._refresh_history()

    def _error(self, title, ex):
        theme.msg_warn(self, "CloudPro", "%s:\n%s" % (title, ex))

    def show_help(self):
        theme.msg_info(self, "CloudPro - keys and mouse", HELP_TEXT)


class _StatusShim:
    def __init__(self, w):
        self.w = w

    def showMessage(self, msg, ms=6000):     # noqa: N802
        self.w.status(msg, ms)


class _Proxy:
    """A point set view with some fields replaced (preview classes)."""

    def __init__(self, ps, **over):
        self._ps = ps
        self._over = over

    def __getattr__(self, k):
        if k in self._over:
            return self._over[k]
        return getattr(self._ps, k)


HELP_TEXT = """VIEWS
  Wheel: zoom at the cursor.  Shift+wheel: vertical exaggeration (slice).
  Middle drag: pan in every tool.  Double click: centre here.  Home: zoom to the points.
  Arrows: pan by the step (Inspection Area properties).  Right click: the view's menu.

PLAN AND INSPECTION AREA
  With the Pan tool (hand): drag the area's edge to move it, a corner to resize, the round
  handle to rotate; Shift+drag draws a new one.  Right click: place here, properties
  (exact values, steps, follow a line, go to), place the slice, align with a line.
  Shift+arrows or Shift+PgUp/PgDn: step the area.

SLICE
  The bar on top: cross-section / profile, rotate 90, whole area, thickness, step, previous /
  next, position.  PgUp / PgDn or ] / [ : next / previous slice; Ctrl+PgUp / Ctrl+PgDn
  or } / { : 5 steps.

EDITING
  Tools: Pan (Space), shape button (click to cycle: rectangle G, polygon P, lasso L, brush B),
  line button (above U / below J), Measure (M), Info (I).  Right click a cycling button to pick.
  Action button: Reject (X), Accept (V), Select (S), Classify (K), Filter (F); next to it, inside or
  outside the shape.  Selected points: right click for reject / accept / classify / filter,
  Delete rejects them, Ctrl+D clears.  H shows / hides rejected points.  Ctrl+Z / Ctrl+Y.

3D
  Arrows top left: click an arrow head to turn the cloud that way (blue up / down, red left / right,
  green clockwise / anticlockwise on the screen); hold to keep turning.  The presets reset the turn.
  Left drag: orbit.  Shift+left or middle drag: pan.  Wheel: zoom, Ctrl+wheel: Z scale.
  Right drag: lasso / rectangle edit.  Double click: new centre.  W/S/A/D/Q/E (click the 3D view
  first): fly by the step.
  1 top, 2 along, 3 across, 4 oblique, O orthographic, 0 fit.
"""
