"""Data View & Fine Tuning: plan, roll, pitch and heading views of the real points, re-georeferenced live
while roll / pitch / heading, lever arm and latency are nudged by hand. A grey ghost shows the data with the
comparison values (recorded, applied, advised, or as the view opened) so every change is visible.

The loaded area follows the inspection boxes (or the area of interest, or an area the user picks); inside the
boxes every recorded point is shown, re-read from the raw .clraw (core/fine.py)."""

import numpy as np
from qgis.PyQt.QtCore import Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QColor, QGuiApplication, QPen, QPolygonF
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QGridLayout, QGroupBox,
                                 QHBoxLayout, QLabel, QPushButton, QScrollArea, QSplitter, QVBoxLayout, QWidget)
from qgis.PyQt.QtCore import QPointF

from ..core import fine
from . import theme
from .common import BOX_COLORS
from .pointview import PointCanvas, height_colors
from .widgets import Card, KV

KEYS = ("roll", "pitch", "heading")
PLAN_MAX = 700000


def _key(text):
    l = QLabel(text.upper())
    l.setObjectName("ppKey")
    return l


def _small(text, tip=""):
    b = QPushButton(text)
    b.setProperty("keepcase", True)
    b.setFixedWidth(30)
    b.setStyleSheet("QPushButton { padding: 4px 0px; font-size: 15px; }")
    if tip:
        b.setToolTip(tip)
    return b


class Nudge(QWidget):
    """[-] value [+] with a shared step."""
    changed = pyqtSignal()

    def __init__(self, dec, lo, hi, step_fn, parent=None):
        super().__init__(parent)
        self.step_fn = step_fn
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        self.minus = _small("−")
        self.plus = _small("+")
        self.spin = QDoubleSpinBox()
        self.spin.setDecimals(dec)
        self.spin.setRange(lo, hi)
        self.spin.setKeyboardTracking(False)
        self.spin.setAlignment(Qt.AlignmentFlag.AlignRight)
        h.addWidget(self.minus)
        h.addWidget(self.spin, 1)
        h.addWidget(self.plus)
        self.minus.setAutoRepeat(True)
        self.plus.setAutoRepeat(True)
        self.minus.setAutoRepeatInterval(120)
        self.plus.setAutoRepeatInterval(120)
        self.minus.clicked.connect(lambda: self.bump(-1))
        self.plus.clicked.connect(lambda: self.bump(1))
        self.spin.valueChanged.connect(lambda _v: self.changed.emit())

    def bump(self, sign):
        self.spin.setValue(self.spin.value() + sign * self.step_fn())

    def value(self):
        return self.spin.value()

    def set(self, v):
        self.spin.blockSignals(True)
        self.spin.setValue(v)
        self.spin.blockSignals(False)


class DataView(QWidget):
    apply_requested = pyqtSignal(list, float, list)     # lever, latency, mount
    area_changed = pyqtSignal(object)                    # region now loaded (dict)
    boxes_edited = pyqtSignal(str)                       # a box redrawn / resized here
    extent_requested = pyqtSignal()                      # "Load map extent"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.p = None
        self.sub = None
        self.x = None
        self.ghost = None
        self.ghost_key = None
        self.line_rgb = None
        self.opened_with = None
        self.applied = None
        self.zlims = (0.0, 1.0)
        self.ground = 0.0
        self.region = None               # what is loaded: see core.fine.view_region
        self.boxes_sig = None
        self.manual = False              # the user chose the area (plan view / map extent)
        self.focus = None
        self.active = "heading"
        self.near = {}                   # box kind -> indices of loaded points near that box
        self.show = None                 # indices the views show (None: all)
        self._gbuf = None
        self._gcut = {}                  # ghost positions cut into each box (only redone when they change)
        self.loader = None               # callable(region) that loads full density in the background
        self.dense_state = ""
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._update)
        self._build()

    # ================================================================= layout
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(6)
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.bar = bar
        bar.addWidget(_key("Active box"))
        self.act = QComboBox()
        self.act.addItems(["Roll", "Pitch", "Heading"])
        self.act.setCurrentIndex(2)
        self.act.setToolTip("Click a view to make its box the active one. Right-drag in the plan redraws the "
                            "active box; the width below is the active box's width.")
        self.act.currentIndexChanged.connect(lambda i: self.set_active(KEYS[i]))
        bar.addWidget(self.act)
        bar.addWidget(_key("Width"))
        self.bw = QDoubleSpinBox()
        self.bw.setRange(0.1, 200)
        self.bw.setDecimals(2)
        self.bw.setSingleStep(0.25)
        self.bw.setSuffix(" m")
        self.bw.setKeyboardTracking(False)
        self.bw.setToolTip("Width of the active box (for the heading box: the length of the wall it covers)")
        self.bw.valueChanged.connect(self._width_live)
        self.bw.editingFinished.connect(self._width_done)
        bar.addWidget(self.bw)
        self.band = QCheckBox("Plan height band")
        self.band.setToolTip("Show only points in this height band (above ground) in the plan: walls and poles "
                             "stand out, which is where heading shows")
        self.band.toggled.connect(self._redraw)
        bar.addWidget(self.band)
        self.zlo = QDoubleSpinBox()
        self.zhi = QDoubleSpinBox()
        for s, v in ((self.zlo, 1.0), (self.zhi, 4.0)):
            s.setRange(-50, 200)
            s.setDecimals(1)
            s.setSingleStep(0.5)
            s.setValue(v)
            s.setSuffix(" m")
            s.valueChanged.connect(self._redraw)
            bar.addWidget(s)
        bar.addWidget(_key("Colour"))
        self.colour = QComboBox()
        self.colour.addItems(["Line", "Height", "Intensity"])
        self.colour.currentIndexChanged.connect(self._recolour)
        bar.addWidget(self.colour)
        bar.addWidget(_key("Ghost"))
        self.compare = QComboBox()
        self.compare.addItems(["None", "Recorded angles", "Applied values", "Advised angles", "When view opened"])
        self.compare.setCurrentIndex(2)
        self.compare.setToolTip("Grey points: the same data with these values, under the live coloured points")
        self.compare.currentIndexChanged.connect(self._schedule)
        bar.addWidget(self.compare)
        bar.addStretch(1)
        b_boxes = QPushButton("Around the boxes")
        b_boxes.setToolTip("Load the area around the roll / pitch / heading boxes (full density inside them)")
        b_boxes.clicked.connect(lambda: self.reload(None))
        b_area = QPushButton("Load view area")
        b_area.setToolTip("Load the area shown in the plan view (boxes inside it at full density)")
        b_area.clicked.connect(self.load_view_area)
        b_ext = QPushButton("Load map extent")
        b_ext.setToolTip("Load the area shown in the QGIS map (boxes inside it at full density)")
        b_ext.clicked.connect(self.extent_requested.emit)
        b_fit = QPushButton("Fit")
        b_fit.clicked.connect(self.fit_all)
        for b in (b_boxes, b_area, b_ext, b_fit):
            bar.addWidget(b)
        root.addLayout(bar)

        self.plan = PointCanvas("Plan", "Easting (m, local)", "Northing (m)", equal=True)
        self.plan.lineDrawn.connect(self._drawn)
        self.plan.hovered.connect(lambda x, y: self._hover("plan", x, y))
        self.views = {"plan": self.plan}
        titles = {"roll": ("Roll box · profile", "Along the box (m)", "Height (m)", False),
                  "pitch": ("Pitch box · profile", "Along the box (m)", "Height (m)", False),
                  "heading": ("Heading box · plan, wall edge-on", "Across the wall (m)", "Along the wall (m)",
                              True)}
        for k, (t, xl, yl, eq) in titles.items():
            v = PointCanvas(t, xl, yl, equal=eq)
            v.accent = BOX_COLORS.get(k, theme.AMBER)
            v.activated.connect(lambda k=k: self.set_active(k))
            v.hovered.connect(lambda x, y, k=k: self._hover(k, x, y))
            v.empty_text = ("No %s box. Draw one on the Pairs page (or Auto boxes), or pick %s as the active box "
                            "and right-drag across the feature in the plan." % (k, k.capitalize()))
            self.views[k] = v
        self.roll_v, self.pitch_v, self.heading_v = self.views["roll"], self.views["pitch"], self.views["heading"]
        left = QSplitter(Qt.Orientation.Vertical)
        left.setHandleWidth(6)
        left.addWidget(self.plan)
        left.addWidget(self.heading_v)
        mid = QSplitter(Qt.Orientation.Vertical)
        mid.setHandleWidth(6)
        mid.addWidget(self.roll_v)
        mid.addWidget(self.pitch_v)
        sp = QSplitter(Qt.Orientation.Horizontal)
        sp.setHandleWidth(6)
        sp.addWidget(left)
        sp.addWidget(mid)
        sp.addWidget(self._panel())
        sp.setStretchFactor(0, 5)
        sp.setStretchFactor(1, 4)
        sp.setStretchFactor(2, 0)
        self._split = sp
        self._sized = False
        root.addWidget(sp, 1)
        self.src = QLabel("")
        self.src.setObjectName("ppHint")
        self.src.setWordWrap(True)
        root.addWidget(self.src)
        self.hint = QLabel("Click a view to make its box active · right-drag in the plan redraws the active box "
                           "· wheel zoom, Shift+wheel vertical scale, drag to pan, double-click to fit")
        self.hint.setObjectName("ppHint")
        root.addWidget(self.hint)
        for v in self.views.values():
            v.overlays = []
        self.plan.overlays.append(self._plan_overlay)
        for k in KEYS:
            self.views[k].overlays.append(lambda p, to_s, k=k: self._box_overlay(k, p, to_s))
        self.set_active("heading")

    def _panel(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 0, 4, 0)
        lay.setSpacing(8)
        self.c_err = Card("Error now (planar patches)")
        lay.addWidget(self.c_err)
        self.dirty = QLabel("")
        self.dirty.setWordWrap(True)
        self.dirty.setStyleSheet("color: %s;" % theme.AMBER)
        lay.addWidget(self.dirty)

        g = QGroupBox("Mounting angles (deg)")
        gl = QGridLayout(g)
        self.ang_step = QComboBox()
        for s in ("0.001", "0.005", "0.01", "0.02", "0.05", "0.1", "0.5", "1"):
            self.ang_step.addItem(s)
        self.ang_step.setCurrentText("0.01")
        self.ang = {}
        for r, k in enumerate(KEYS):
            n = Nudge(4, -360, 360, lambda: float(self.ang_step.currentText()))
            n.changed.connect(self._schedule)
            self.ang[k] = n
            gl.addWidget(_key(k), r, 0)
            gl.addWidget(n, r, 1)
        gl.addWidget(_key("Step"), 3, 0)
        gl.addWidget(self.ang_step, 3, 1)
        lay.addWidget(g)

        g2 = QGroupBox("Lever arm (m)")
        gl2 = QGridLayout(g2)
        self.lev_step = QComboBox()
        for s in ("0.001", "0.005", "0.01", "0.02", "0.05", "0.1"):
            self.lev_step.addItem(s)
        self.lev_step.setCurrentText("0.01")
        self.lev = []
        for r, k in enumerate(("Forward", "Starboard", "Down")):
            n = Nudge(4, -50, 50, lambda: float(self.lev_step.currentText()))
            n.changed.connect(self._schedule)
            self.lev.append(n)
            gl2.addWidget(_key(k), r, 0)
            gl2.addWidget(n, r, 1)
        gl2.addWidget(_key("Step"), 3, 0)
        gl2.addWidget(self.lev_step, 3, 1)
        gl2.addWidget(_key("Latency ms"), 4, 0)
        self.lat = Nudge(2, -500, 500, lambda: 1.0)
        self.lat.changed.connect(self._schedule)
        gl2.addWidget(self.lat, 4, 1)
        lay.addWidget(g2)

        self.kv = KV()
        lay.addWidget(self.kv)
        row = QGridLayout()
        b1 = QPushButton("Recorded")
        b1.setToolTip("Start from the angles and lever arm recorded in the .clraw")
        b1.clicked.connect(lambda: self.load_values("recorded"))
        b2 = QPushButton("Applied")
        b2.setToolTip("Back to the values applied to the project")
        b2.clicked.connect(lambda: self.load_values("applied"))
        b3 = QPushButton("Advised")
        b3.setToolTip("The auto calibration result")
        b3.clicked.connect(lambda: self.load_values("advised"))
        row.addWidget(_key("Start from"), 0, 0, 1, 3)
        row.addWidget(b1, 1, 0)
        row.addWidget(b2, 1, 1)
        row.addWidget(b3, 1, 2)
        lay.addLayout(row)
        self.b_apply = QPushButton("Apply to project")
        self.b_apply.setObjectName("ppPrimary")
        self.b_apply.clicked.connect(self._apply)
        self.b_copy = QPushButton("Copy for mapper")
        self.b_copy.clicked.connect(self._copy)
        lay.addWidget(self.b_apply)
        lay.addWidget(self.b_copy)
        lay.addStretch(1)
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.Shape.NoFrame)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sa.setWidget(w)
        sa.setMinimumWidth(340)
        sa.setMaximumWidth(400)
        return sa

    # =============================================================== values
    def values(self):
        return ([self.ang[k].value() for k in KEYS], [n.value() for n in self.lev], self.lat.value())

    def set_values(self, mount, lever, lat):
        for k, v in zip(KEYS, mount):
            self.ang[k].set(v)
        for n, v in zip(self.lev, lever):
            n.set(v)
        self.lat.set(lat)
        self._schedule()

    def load_values(self, which):
        if self.p is None:
            return
        c = self.p.cfg
        if which == "recorded":
            self.set_values(c["mount_preset"], c["lever_preset"], 0.0)
        elif which == "advised" and c.get("results"):
            a = c["results"]["advised"]
            self.set_values([a[k] for k in KEYS], c["lever"], c["latency_ms"])
        else:
            self.set_values(c["mount"], c["lever"], c["latency_ms"])

    def showEvent(self, ev):
        super().showEvent(ev)
        self._size_split()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if not self._sized:
            self._size_split()

    def _size_split(self):
        w = self._split.width()
        if w > 800:
            panel = 350
            rest = w - panel - 12
            self._split.setSizes([int(rest * 0.5), int(rest * 0.5), panel])
            self._sized = True

    # ============================================================== project
    def set_project(self, p, keep=False):
        """New project, or the project changed (applied values, lines, boxes)."""
        old_applied = self.applied
        new = p is not self.p or not keep
        self.p = p
        if p is None or not p.lines:
            self.sub = None
            self.region = None
            return
        c = p.cfg
        self.applied = (list(c["mount"]), list(c["lever"]), float(c["latency_ms"]))
        trial = self.values()
        follow = new or old_applied is None or \
            (np.allclose(trial[0], old_applied[0]) and np.allclose(trial[1], old_applied[1]) and
             abs(trial[2] - old_applied[2]) < 1e-9)
        cols = [QColor(ln.color) for ln in p.lines]
        self.line_rgb = np.array([[q.red(), q.green(), q.blue()] for q in cols], np.uint8)
        if follow:
            self.set_values(*self.applied)
        if self.opened_with is None or new:
            self.opened_with = self.applied
        if new:
            self.manual = False
            self.focus = None
            self._load(p.view_region())
            return
        # same project: reload only when the boxes moved (or the loaded area no longer fits them)
        if fine.boxes_signature(p) != self.boxes_sig or self.region is None:
            if self.manual and self.region is not None:
                region = p.area_region(self.region["center"], self.region["half"], self.region["source"])
            else:
                region = p.view_region(self.focus)
            if self.region is None or region["sig"] != self.region["sig"]:
                self._load(region)
                return
            self.boxes_sig = fine.boxes_signature(p)
        self.ghost_key = None
        self._prepare()
        self._schedule()

    def reload_region(self):
        """Load the same area again (e.g. after the full-density read was stopped to apply values)."""
        if self.p is None or not self.p.lines or self.region is None:
            return
        r = self.region
        region = self.p.area_region(r["center"], r["half"], r["source"]) if self.manual else \
            self.p.view_region(self.focus)
        self._load(region)

    def reload(self, focus=None):
        """Around the boxes (focus: the box just drawn, or 'aoi' for the area of interest)."""
        if self.p is None or not self.p.lines:
            return
        self.manual = False
        self.focus = focus
        if focus in KEYS:
            self.set_active(focus)
        self._load(self.p.view_region(focus))

    def load_view_area(self):
        if self.p is None or not self.p.lines:
            return
        x0, x1, y0, y1 = self.plan._range()
        self.load_area(((x0 + x1) / 2, (y0 + y1) / 2), max(x1 - x0, y1 - y0) / 2, "plan view area")

    def load_area(self, center, half, source="map extent"):
        if self.p is None or not self.p.lines:
            return
        self.manual = True
        self._load(self.p.area_region(center, half, source))

    def _load(self, region):
        p = self.p
        self.region = region
        self.boxes_sig = fine.boxes_signature(p)
        self.ground = p.ground_level(region["center"])
        self.sub = p.fine_view(region)              # thinned first: the views fill at once
        self.ghost_key = None
        self.dense_state = "loading" if region["boxes"] else ""
        if self.sub is not None:
            z0 = p.georef_subset(self.sub, *self.applied)[:, 2]
            z0 = z0[np.isfinite(z0)]
            if len(z0):                 # an area with no points (e.g. after a new area of interest) keeps the old limits
                self.zlims = tuple(np.percentile(z0, [2, 98]))
        self._prepare()
        b = (p.cfg.get("boxes") or {}).get("heading")
        if b and b.get("zmin") is not None:
            for s, v in ((self.zlo, b["zmin"] - self.ground), (self.zhi, b["zmax"] - self.ground)):
                s.blockSignals(True)
                s.setValue(round(v, 1))
                s.blockSignals(False)
        c, h = region["center"], region["half"]
        self.plan.view = (c[0] - h, c[0] + h, c[1] - h, c[1] + h)
        self.set_active(self.active)
        self._status()
        self.area_changed.emit(region)
        self._schedule(refit=True)
        if region["boxes"]:
            if self.loader is not None:
                self.loader(region)
            else:
                try:
                    self.set_dense(region, p.dense_points(region["boxes"]))
                except Exception as e:  # noqa: BLE001
                    self.set_dense(region, None, str(e))

    def set_dense(self, region, dense, error=""):
        """Full-density points for region arrived (from the background job)."""
        if self.p is None or self.region is None or region["sig"] != self.region["sig"]:
            return
        if dense is None:
            self.dense_state = "failed: " + (error or "no points")
            self._status()
            return
        self.sub = self.p.fine_view(region, dense)
        self.ghost_key = None
        self.dense_state = "ok"
        self._prepare()
        self._status()
        self._schedule()

    def _status(self):
        r, s = self.region, self.sub
        if r is None:
            self.src.setText("")
            return
        where = {"boxes": "around the inspection boxes", "aoi": "around the area of interest",
                 "default": "around the wall patches (no inspection boxes yet)"}.get(r["source"], r["source"])
        txt = "Loaded %s: %.0f × %.0f m" % (where, 2 * r["half"], 2 * r["half"])
        if s is not None:
            txt += " · %s points" % "{:,}".format(len(s["t"]))
        if not r["boxes"]:
            txt += " · no inspection box in this area, thinned points only"
        elif self.dense_state == "loading":
            txt += " · reading every recorded point inside the boxes from the raw data…"
        elif self.dense_state == "ok" and s is not None:
            st = s.get("dense_stats") or {}
            txt += " · %s at full density inside %s" % ("{:,}".format(s["n_dense"]),
                                                             ", ".join(k for k in KEYS if k in r["boxes"]))
            th = [k for k, v in (st.get("per_box") or {}).items() if v.get("thinned_to")]
            if th:
                txt += " (thinned to the per-box limit in %s)" % ", ".join(th)
            if st.get("missing"):
                txt += " · raw files not found: %s" % ", ".join(st["missing"][:3])
        elif self.dense_state.startswith("failed"):
            txt += " · full density not available (%s)" % self.dense_state[8:]
        self.src.setText(txt)

    def fit_all(self):
        for v in self.views.values():
            v.fit()
            v.update()

    # ================================================================ boxes
    def _boxes(self):
        return (self.p.cfg.get("boxes") or {}) if self.p is not None else {}

    def set_active(self, kind):
        if kind not in KEYS:
            return
        self.active = kind
        self.act.blockSignals(True)
        self.act.setCurrentIndex(KEYS.index(kind))
        self.act.blockSignals(False)
        b = self._boxes().get(kind)
        self.bw.blockSignals(True)
        self.bw.setValue(float(b["width"]) if b else {"heading": 4.0}.get(kind, 1.0))
        self.bw.setEnabled(b is not None)
        self.bw.blockSignals(False)
        for k in KEYS:
            self.views[k].active = (k == kind)
            self.views[k].update()
        self.plan.accent = BOX_COLORS.get(kind, theme.ACCENT)
        self.plan.update()

    def _width_live(self, w):
        b = self._boxes().get(self.active)
        if b is None:
            return
        b["width"] = float(w)
        self._prepare()
        self._redraw()

    def _width_done(self):
        if self._boxes().get(self.active) is not None:
            self.boxes_edited.emit(self.active)

    def _drawn(self, x0, y0, x1, y1):
        """Right-drag in the plan: the active box, from the start of the drag to its end."""
        if self.p is None:
            return
        boxes = self.p.cfg.setdefault("boxes", {})
        old = boxes.get(self.active, {})
        boxes[self.active] = {"p0": [float(x0), float(y0)], "p1": [float(x1), float(y1)],
                              "width": float(old.get("width", {"heading": 4.0}.get(self.active, 1.0))),
                              "view": old.get("view", "plan" if self.active == "heading" else "profile"),
                              "zmin": old.get("zmin"), "zmax": old.get("zmax")}
        self.set_active(self.active)
        self.boxes_edited.emit(self.active)

    def _prepare(self):
        """Indices of the loaded points near each box (so a nudge only cuts those), and the points the views
        show (the plan's first PLAN_MAX of the random order + the points near the boxes): a nudge places only
        those. The other rows stay NaN."""
        self.near = {}
        self.show = None
        self._gbuf = None
        if self.p is None or self.sub is None:
            return
        x = self.p.georef_subset(self.sub, *self.values())
        for k, b in self._boxes().items():
            if k in KEYS and fine.box_frame(b) is not None:
                self.near[k] = np.nonzero(fine.in_box(x[:, :2], b, fine.PAD_M))[0]
        n = len(x)
        keep = np.zeros(n, bool)
        keep[:min(n, PLAN_MAX)] = True
        for ix in self.near.values():
            keep[ix] = True
        self.show = None if keep.all() else np.nonzero(keep)[0]
        if self.show is not None:
            x[~keep] = np.nan
        self.x = x

    def _hover(self, view, x, y):
        self.hint.setText("%s  %.3f, %.3f m" % (view.upper(), x, y))

    # =============================================================== update
    def _schedule(self, *_a, refit=False):
        self._refit = getattr(self, "_refit", False) or refit
        self._timer.start()

    def _update(self):
        if self.p is None or self.sub is None:
            return
        mount, lever, lat = self.values()
        if self.x is None or len(self.x) != len(self.sub["t"]):
            self._prepare()
        self.x = self.p.georef_subset(self.sub, mount, lever, lat, idx=self.show, out=self.x)
        ref = self._ghost_values()
        if ref is None:
            self.ghost = None
        else:
            key = (tuple(ref[0]), tuple(ref[1]), ref[2], id(self.sub))
            if key != self.ghost_key:
                if self.show is None:
                    self.ghost = self.p.georef_subset(self.sub, *ref)
                else:
                    if getattr(self, "_gbuf", None) is None or len(self._gbuf) != len(self.sub["t"]):
                        self._gbuf = np.full((len(self.sub["t"]), 3), np.nan)
                    self.ghost = self.p.georef_subset(self.sub, *ref, idx=self.show, out=self._gbuf)
                self.ghost_key = key
        try:
            e = self.p.live_error(mount, lever, lat)
            akey = (tuple(self.applied[0]), tuple(self.applied[1]), self.applied[2], id(self.p.P))
            if getattr(self, "_ea_key", None) != akey:
                self._ea, self._ea_key = self.p.live_error(*self.applied), akey
            ea = self._ea
        except Exception:  # noqa: BLE001
            e = ea = None
        if e is not None:
            self.c_err.set("%.2f cm" % e, "ok" if e < 1 else ("warn" if e < 2.5 else "bad"))
            self.c_err.label.setText("ERROR NOW · APPLIED %.2f · Δ %+.2f CM" % (ea, e - ea))
        a = self.applied
        dm = [m - b for m, b in zip(mount, a[0])]
        dl = [m - b for m, b in zip(lever, a[1])]
        self.kv.set("Δ R P H", "%+.4f %+.4f %+.4f" % tuple(dm))
        self.kv.set("Δ F S D", "%+.3f %+.3f %+.3f" % tuple(dl))
        changed = not self._same_as_applied(mount, lever, lat)
        self.dirty.setText("● Trial values not applied yet" if changed else "")
        refit = getattr(self, "_refit", False)
        self._refit = False
        self._redraw(refit=refit)

    def _ghost_values(self):
        c = self.p.cfg
        i = self.compare.currentIndex()
        if i == 1:
            return (c["mount_preset"], c.get("lever_preset") or c["lever"], 0.0)
        if i == 2:
            return self.applied
        if i == 3 and c.get("results"):
            a = c["results"]["advised"]
            return ([a[k] for k in KEYS], c["lever"], c["latency_ms"])
        if i == 4:
            return self.opened_with
        return None

    def _colours(self, sel):
        mode = self.colour.currentIndex()
        if mode == 1:
            return height_colors(self.x[sel, 2], *self.zlims)
        if mode == 2:
            i = self.sub["i"][sel].astype(float)
            t = np.clip(i / max(np.percentile(self.sub["i"], 98), 1), 0, 1)
            return (np.stack([40 + 200 * t, 60 + 190 * t, 55 + 180 * t], -1)).astype(np.uint8)
        return self.line_rgb[self.sub["line"][sel]]

    def _recolour(self):
        self._redraw()

    def _redraw(self, *_a, refit=False):
        if self.x is None or self.sub is None or len(self.x) != len(self.sub["t"]):
            return
        x, g = self.x, self.ghost
        # plan
        if self.band.isChecked():
            lo, hi = self.ground + self.zlo.value(), self.ground + self.zhi.value()
            m = (x[:, 2] >= lo) & (x[:, 2] <= hi)
            gm = None if g is None else (g[:, 2] >= lo) & (g[:, 2] <= hi)
            self.plan.note = "height band %.1f–%.1f m above ground" % (self.zlo.value(), self.zhi.value())
        else:
            m = slice(0, min(len(x), PLAN_MAX))
            gm = None if g is None else m
            self.plan.note = "%s points" % "{:,}".format(len(x)) + \
                (" (showing %s)" % "{:,}".format(PLAN_MAX) if len(x) > PLAN_MAX else "")
        # the plan shows at most PLAN_MAX points (the loaded points are in random order, so the first ones are a
        # fair sample); the box views always show every point in their box
        mi = np.arange(len(x))[m] if isinstance(m, slice) else np.nonzero(m)[0][:PLAN_MAX]
        gi = None if g is None else (mi if isinstance(gm, slice) else np.nonzero(gm)[0][:PLAN_MAX])
        self.plan.set_data(x[mi, 0], x[mi, 1], self._colours(mi),
                           None if g is None else g[gi, 0], None if g is None else g[gi, 1], keep_view=True)
        # one view per box
        boxes = self._boxes()
        for k in KEYS:
            v = self.views[k]
            b = boxes.get(k)
            f = fine.box_frame(b) if b else None
            idx = self.near.get(k)
            if f is None or idx is None:
                v.note = ""
                v.set_data(np.zeros(0), np.zeros(0), np.zeros((0, 3), np.uint8), keep_view=not refit)
                continue
            p0, u, vv, L, hw = f

            def cut(arr, idx=idx, p0=p0, u=u, vv=vv):
                rel = arr[idx, :2] - p0
                return rel @ u, rel @ vv
            al, ac = cut(x)
            sel = (al >= 0) & (al <= L) & (np.abs(ac) <= hw)
            if k == "heading":
                lo, hi = self._heading_band(b)
                sel &= (x[idx, 2] >= lo) & (x[idx, 2] <= hi)
            gal = gac = gsel = None
            if g is not None:
                gk = (self.ghost_key, k, id(idx), len(idx), p0[0], p0[1], u[0], L, hw)
                if self._gcut.get(k, (None,))[0] != gk:
                    self._gcut[k] = (gk,) + cut(g)
                gal, gac = self._gcut[k][1], self._gcut[k][2]
                gsel = (gal >= 0) & (gal <= L) & (np.abs(gac) <= hw)
                if k == "heading":
                    gsel &= (g[idx, 2] >= lo) & (g[idx, 2] <= hi)
            ii = idx[sel]
            dens = "full density" if (self.dense_state == "ok" and k in (self.region or {}).get("boxes", {})) \
                else ("loading full density…" if self.dense_state == "loading" else "thinned")
            if k == "heading":
                v.note = "band %.1f–%.1f m · %s pts · %s" % (lo - self.ground, hi - self.ground,
                                                                         "{:,}".format(len(ii)), dens)
                v.set_data(al[sel], ac[sel], self._colours(ii),
                           None if g is None else gal[gsel], None if g is None else gac[gsel], keep_view=not refit)
            else:
                v.note = "width %.2f m · %s pts · %s" % (2 * hw, "{:,}".format(len(ii)), dens)
                v.set_data(al[sel], x[ii, 2], self._colours(ii),
                           None if g is None else gal[gsel], None if g is None else g[idx[gsel], 2],
                           keep_view=not refit)

    def _heading_band(self, b):
        if b.get("zmin") is not None and b.get("zmax") is not None:
            return float(b["zmin"]), float(b["zmax"])
        return self.ground + self.zlo.value(), self.ground + self.zhi.value()

    # ============================================================= overlays
    def _plan_overlay(self, p, to_s):
        if self.p is not None:
            for kind, b in self._boxes().items():
                act = kind == self.active
                self._rect(p, to_s, np.array(b["p0"]), np.array(b["p1"]), b["width"], QColor(BOX_COLORS.get(kind)),
                           2.0 if act else 0.9, Qt.PenStyle.SolidLine if act else Qt.PenStyle.DotLine)
                f = fine.box_frame(b)
                if f is not None and act:
                    p0, u, v, L, hw = f
                    p1 = p0 + u * L
                    s = min(1.5, L * 0.08)
                    tri = [p1, p1 - u * s + v * min(0.6, hw), p1 - u * s - v * min(0.6, hw)]
                    p.setBrush(QColor(BOX_COLORS.get(kind)))
                    p.setPen(Qt.PenStyle.NoPen)
                    p.drawPolygon(QPolygonF([to_s(*q) for q in tri]))
                    p.setBrush(Qt.BrushStyle.NoBrush)
        # legend
        if self.p is not None:
            p.setFont(theme.font("ui", 11, 700, 106))
            y = 34
            for ln in self.p.lines:
                if not ln.use:
                    continue
                p.setPen(QColor(ln.color))
                p.drawText(QPointF(60, y), "■ " + ln.name)
                y += 14
            if self.ghost is not None:
                p.setPen(QColor(160, 169, 177))
                p.drawText(QPointF(60, y), "■ ghost: " + self.compare.currentText().lower())
                y += 14
            p.setPen(QColor(BOX_COLORS.get(self.active, theme.AMBER)))
            p.drawText(QPointF(60, y + 4), "right-drag: redraw the %s box" % self.active)

    def _box_overlay(self, kind, p, to_s):
        """Box limits in the box views (dashed)."""
        b = self._boxes().get(kind)
        f = fine.box_frame(b) if b else None
        if f is None:
            return
        L = f[3]
        p.setPen(QPen(QColor(BOX_COLORS.get(kind, theme.AMBER)), 1, Qt.PenStyle.DashLine))
        for s in (0.0, L):
            a = to_s(s, 0.0)
            p.drawLine(QPointF(a.x(), 0), QPointF(a.x(), 5000))

    def _rect(self, p, to_s, p0, p1, width, col, pw, style):
        L = float(np.linalg.norm(p1 - p0))
        if L < 1e-6:
            return
        u = (p1 - p0) / L
        v = np.array([-u[1], u[0]]) * width / 2
        ring = [p0 - v, p1 - v, p1 + v, p0 + v]
        p.setPen(QPen(col, pw, style))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPolygon(QPolygonF([to_s(*q) for q in ring]))

    # the fields show angles to 0.0001 deg, lever arms to the mm and latency to 0.1 ms; values equal to the
    # applied ones at that precision are the applied ones (not a manual change)
    TOL = (5e-5, 5e-4, 0.05)

    def _same_as_applied(self, mount, lever, lat):
        a = self.applied
        return (all(abs(m - b) < self.TOL[0] for m, b in zip(mount, a[0]))
                and all(abs(m - b) < self.TOL[1] for m, b in zip(lever, a[1])) and abs(lat - a[2]) < self.TOL[2])

    # ================================================================ output
    def _apply(self):
        mount, lever, lat = self.values()
        a = self.applied
        mount = [b if abs(m - b) < self.TOL[0] else m for m, b in zip(mount, a[0])]
        lever = [b if abs(m - b) < self.TOL[1] else m for m, b in zip(lever, a[1])]
        lat = a[2] if abs(lat - a[2]) < self.TOL[2] else lat
        self.apply_requested.emit(list(lever), lat, list(mount))

    def _copy(self):
        mount = self.values()[0]
        QGuiApplication.clipboard().setText("Roll %.4f   Pitch %.4f   Heading %.4f" % tuple(mount))
        self.b_copy.setText("COPIED ✓")
        QTimer.singleShot(2000, lambda: self.b_copy.setText("COPY FOR MAPPER"))

    def grab_views(self):
        return {k: v.grab() for k, v in self.views.items()}
