"""Data View & Fine Tuning: plan, profile and section of the real points, re-georeferenced live while the
roll / pitch / heading, lever arm and latency are nudged by hand. A grey ghost shows the data with the
comparison values (recorded, applied, advised, or as the view opened) so every change is visible."""
import math

import numpy as np
from qgis.PyQt.QtCore import Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QColor, QGuiApplication, QPen, QPolygonF
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QGridLayout, QGroupBox,
                                 QHBoxLayout, QLabel, QPushButton, QScrollArea, QSplitter, QVBoxLayout, QWidget)
from qgis.PyQt.QtCore import QPointF

from ..core import geo
from . import theme
from .common import BOX_COLORS, fmt
from .pointview import PointCanvas, height_colors
from .widgets import Card, KV

KEYS = ("roll", "pitch", "heading")


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

    def __init__(self, parent=None):
        super().__init__(parent)
        self.p = None
        self.sub = None
        self.x = None
        self.ghost = None
        self.ghost_key = None
        self.line_rgb = None
        self.profile = None              # dict p0, p1
        self.station = None
        self.opened_with = None
        self.applied = None
        self.zlims = (0.0, 1.0)
        self.ground = 0.0
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
        bar.addWidget(_key("Section"))
        self.preset = QComboBox()
        self.preset.addItems(["Roll box", "Pitch box", "Heading box", "Custom (right-drag in plan)"])
        self.preset.currentIndexChanged.connect(self._preset)
        bar.addWidget(self.preset)
        bar.addWidget(_key("Profile width"))
        self.pw = QDoubleSpinBox()
        self.pw.setRange(0.1, 50)
        self.pw.setValue(1.0)
        self.pw.setSuffix(" m")
        self.pw.setSingleStep(0.25)
        self.pw.valueChanged.connect(self._redraw)
        bar.addWidget(self.pw)
        bar.addWidget(_key("Section"))
        self.sw = QDoubleSpinBox()
        self.sw.setRange(0.05, 20)
        self.sw.setValue(0.5)
        self.sw.setSuffix(" m")
        self.sw.setSingleStep(0.1)
        self.sw.setToolTip("Thickness of the cross-section, along the profile")
        self.sw.valueChanged.connect(self._redraw)
        bar.addWidget(self.sw)
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
        b_area = QPushButton("Load view area")
        b_area.setToolTip("Load the points around the centre of the plan view (up to 400 000)")
        b_area.clicked.connect(self.load_view_area)
        b_fit = QPushButton("Fit")
        b_fit.clicked.connect(self.fit_all)
        bar.addWidget(b_area)
        bar.addWidget(b_fit)
        root.addLayout(bar)

        sp = QSplitter(Qt.Orientation.Horizontal)
        sp.setHandleWidth(6)
        self.plan = PointCanvas("Plan", "Easting (m, local)", "Northing (m)", equal=True)
        self.plan.lineDrawn.connect(self._custom_line)
        self.plan.hovered.connect(lambda x, y: self._hover("plan", x, y))
        self.profile_v = PointCanvas("Profile", "Distance along profile (m)", "Height (m)")
        self.profile_v.clicked.connect(lambda x, y: self._set_station(x))
        self.profile_v.hovered.connect(lambda x, y: self._hover("profile", x, y))
        self.section_v = PointCanvas("Section", "Across the profile (m)", "Height (m)", equal=True)
        self.section_v.hovered.connect(lambda x, y: self._hover("section", x, y))
        right = QSplitter(Qt.Orientation.Vertical)
        right.setHandleWidth(6)
        right.addWidget(self.profile_v)
        right.addWidget(self.section_v)
        sp.addWidget(self.plan)
        sp.addWidget(right)
        sp.addWidget(self._panel())
        sp.setStretchFactor(0, 5)
        sp.setStretchFactor(1, 4)
        sp.setStretchFactor(2, 0)
        self._split = sp
        self._sized = False
        root.addWidget(sp, 1)
        self.hint = QLabel("Right-drag in the plan to draw a profile · click the profile to move the section "
                           "· wheel zoom, Shift+wheel vertical scale, drag to pan, double-click to fit")
        self.hint.setObjectName("ppHint")
        root.addWidget(self.hint)
        for v in (self.plan, self.profile_v, self.section_v):
            v.overlays = []
        self.plan.overlays.append(self._plan_overlay)
        self.profile_v.overlays.append(self._profile_overlay)
        self.section_v.overlays.append(self._section_overlay)

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
            self._split.setSizes([int(rest * 0.52), int(rest * 0.48), panel])
            self._sized = True

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

    # ============================================================== project
    def set_project(self, p, keep=False):
        """New project or the project changed (applied values, lines, boxes)."""
        old_applied = self.applied
        self.p = p
        if p is None or not p.lines:
            self.sub = None
            return
        c = p.cfg
        self.applied = (list(c["mount"]), list(c["lever"]), float(c["latency_ms"]))
        trial = self.values()
        follow = (not keep) or old_applied is None or \
            (np.allclose(trial[0], old_applied[0]) and np.allclose(trial[1], old_applied[1]) and
             abs(trial[2] - old_applied[2]) < 1e-9)
        cols = [QColor(ln.color) for ln in p.lines]
        self.line_rgb = np.array([[q.red(), q.green(), q.blue()] for q in cols], np.uint8)
        center = self.sub["center"] if (keep and self.sub) else p.view_center()
        half = self.sub["half"] if (keep and self.sub) else 40.0
        self.ground = p.ground_level(center)
        self.sub = p.view_subset(center, half)
        if self.sub is not None:
            z0 = p.georef_subset(self.sub, *self.applied)[:, 2]
            self.zlims = tuple(np.percentile(z0, [2, 98]))
        if follow:
            self.set_values(*self.applied)
        if self.opened_with is None or not keep:
            self.opened_with = self.applied
        self.ghost_key = None
        if not keep or self.profile is None:
            self._preset()
        else:
            self._schedule()

    def load_view_area(self):
        if self.p is None:
            return
        x0, x1, y0, y1 = self.plan._range()
        half = min(150.0, max(10.0, max(x1 - x0, y1 - y0) / 2))
        self.ground = self.p.ground_level(((x0 + x1) / 2, (y0 + y1) / 2))
        self.sub = self.p.view_subset(((x0 + x1) / 2, (y0 + y1) / 2), half)
        self.ghost_key = None
        self._schedule()

    def fit_all(self):
        for v in (self.plan, self.profile_v, self.section_v):
            v.fit()
            v.update()

    # ============================================================== section
    def _preset(self):
        if self.p is None:
            return
        i = self.preset.currentIndex()
        kind = ("roll", "pitch", "heading", None)[i]
        boxes = self.p.cfg.get("boxes") or {}
        if kind and kind in boxes:
            b = boxes[kind]
            self.profile = {"p0": np.array(b["p0"], float), "p1": np.array(b["p1"], float), "kind": kind}
            self.pw.blockSignals(True)
            self.pw.setValue(min(max(b["width"], 0.3), 6.0) if kind != "heading" else 1.0)
            self.pw.blockSignals(False)
            L = float(np.linalg.norm(self.profile["p1"] - self.profile["p0"]))
            self.station = L / 2
            if kind == "heading":
                self.band.blockSignals(True)
                self.band.setChecked(True)
                self.band.blockSignals(False)
                zr = (b.get("zmin"), b.get("zmax"))
                if zr[0] is not None:
                    self.zlo.setValue(round(zr[0] - self.ground, 1))
                    self.zhi.setValue(round(zr[1] - self.ground, 1))
                c = (self.profile["p0"] + self.profile["p1"]) / 2
                hw = max(5.0, b["width"] / 2 + 2)
                self._zoom_plan(c, hw)
            else:
                c = (self.profile["p0"] + self.profile["p1"]) / 2
                self._zoom_plan(c, max(8.0, L / 2 + 4))
        elif self.profile is None and self.sub is not None:
            c = np.array(self.sub["center"])
            self.profile = {"p0": c - [10, 0], "p1": c + [10, 0], "kind": None}
            self.station = 10.0
        self._schedule(refit=True)

    def _zoom_plan(self, c, hw):
        self.plan.view = (c[0] - hw, c[0] + hw, c[1] - hw, c[1] + hw)

    def _custom_line(self, x0, y0, x1, y1):
        self.profile = {"p0": np.array([x0, y0]), "p1": np.array([x1, y1]), "kind": None}
        self.station = math.hypot(x1 - x0, y1 - y0) / 2
        self.preset.blockSignals(True)
        self.preset.setCurrentIndex(3)
        self.preset.blockSignals(False)
        self._redraw(refit=True)

    def _set_station(self, s):
        if self.profile is None:
            return
        L = float(np.linalg.norm(self.profile["p1"] - self.profile["p0"]))
        self.station = min(max(0.0, s), L)
        self._redraw(refit_section=True)

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
        self.x = self.p.georef_subset(self.sub, mount, lever, lat)
        ref = self._ghost_values()
        if ref is None:
            self.ghost = None
        else:
            key = (tuple(ref[0]), tuple(ref[1]), ref[2], id(self.sub))
            if key != self.ghost_key:
                self.ghost = self.p.georef_subset(self.sub, *ref)
                self.ghost_key = key
        try:
            e = self.p.live_error(mount, lever, lat)
            ea = self.p.live_error(*self.applied)
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

    def _redraw(self, *_a, refit=False, refit_section=False):
        if self.x is None:
            return
        x, g = self.x, self.ghost
        # plan
        if self.band.isChecked():
            lo, hi = self.ground + self.zlo.value(), self.ground + self.zhi.value()
            m = (x[:, 2] >= lo) & (x[:, 2] <= hi)
            gm = None if g is None else (g[:, 2] >= lo) & (g[:, 2] <= hi)
            self.plan.note = "height band %.1f–%.1f m above ground" % (self.zlo.value(), self.zhi.value())
        else:
            m = np.ones(len(x), bool)
            gm = None if g is None else m
            self.plan.note = "%s points" % "{:,}".format(len(x))
        self.plan.set_data(x[m, 0], x[m, 1], self._colours(m),
                           None if g is None else g[gm, 0], None if g is None else g[gm, 1])
        if self.profile is None:
            return
        p0, p1 = self.profile["p0"], self.profile["p1"]
        L = float(np.linalg.norm(p1 - p0))
        if L < 1e-3:
            return
        u = (p1 - p0) / L
        v = np.array([-u[1], u[0]])

        def cut(arr):
            rel = arr[:, :2] - p0
            return rel @ u, rel @ v

        al, ac = cut(x)
        w2 = self.pw.value() / 2
        mp = (al >= 0) & (al <= L) & (np.abs(ac) <= w2)
        s, t2 = self.station if self.station is not None else L / 2, self.sw.value() / 2
        ms = (np.abs(al - s) <= t2) & (np.abs(ac) <= max(8.0, L / 2))
        gp = gs = gal = gac = None
        if g is not None:
            gal, gac = cut(g)
            gp = (gal >= 0) & (gal <= L) & (np.abs(gac) <= w2)
            gs = (np.abs(gal - s) <= t2) & (np.abs(gac) <= max(8.0, L / 2))
        kind = self.profile.get("kind")
        self.profile_v.accent = BOX_COLORS.get(kind, theme.AMBER) if kind else theme.AMBER
        if kind == "heading":
            # the wall seen from above, edge-on: a heading error turns the passes against each other
            box = (self.p.cfg.get("boxes") or {}).get("heading", {})
            hw = max(w2, box.get("width", 4.0) / 2)
            lo, hi = self.ground + self.zlo.value(), self.ground + self.zhi.value()
            mp = (al >= 0) & (al <= L) & (np.abs(ac) <= hw) & (x[:, 2] >= lo) & (x[:, 2] <= hi)
            if g is not None:
                gp = (gal >= 0) & (gal <= L) & (np.abs(gac) <= hw) & (g[:, 2] >= lo) & (g[:, 2] <= hi)
            self.profile_v.title = "Heading box · plan, wall edge-on"
            self.profile_v.ylabel = "Along the wall (m)"
            self.profile_v.note = "band %.1f–%.1f m · %s pts" % (self.zlo.value(), self.zhi.value(),
                                                               "{:,}".format(int(mp.sum())))
            self.profile_v.set_data(al[mp], ac[mp], self._colours(mp),
                                    None if g is None else gal[gp], None if g is None else gac[gp],
                                    keep_view=not refit)
        else:
            self.profile_v.title = "Profile" + (" · %s box" % kind if kind else "")
            self.profile_v.ylabel = "Height (m)"
            self.profile_v.note = "width %.2f m · %s pts" % (2 * w2, "{:,}".format(int(mp.sum())))
            self.profile_v.set_data(al[mp], x[mp, 2], self._colours(mp),
                                    None if g is None else gal[gp], None if g is None else g[gp, 2],
                                    keep_view=not refit)
        self.section_v.title = "Section at %.2f m" % s
        self.section_v.note = "thickness %.2f m · %s pts" % (2 * t2, "{:,}".format(int(ms.sum())))
        self.section_v.set_data(ac[ms], x[ms, 2], self._colours(ms),
                                None if g is None else gac[gs], None if g is None else g[gs, 2],
                                keep_view=not (refit or refit_section))

    # ============================================================= overlays
    def _plan_overlay(self, p, to_s):
        if self.p is not None:
            for kind, b in (self.p.cfg.get("boxes") or {}).items():
                self._rect(p, to_s, np.array(b["p0"]), np.array(b["p1"]), b["width"], QColor(BOX_COLORS.get(kind)), 0.8,
                           Qt.PenStyle.DotLine)
        if self.profile is not None:
            p0, p1 = self.profile["p0"], self.profile["p1"]
            col = QColor(self.profile_v.accent)
            self._rect(p, to_s, p0, p1, self.pw.value(), col, 1.6, Qt.PenStyle.SolidLine)
            L = float(np.linalg.norm(p1 - p0))
            if L > 1e-3 and self.station is not None:
                u = (p1 - p0) / L
                v = np.array([-u[1], u[0]])
                c = p0 + u * self.station
                half = max(8.0, L / 2)
                a, b = c - v * half, c + v * half
                p.setPen(QPen(QColor(theme.BRIGHT), 1.2, Qt.PenStyle.DashLine))
                p.drawLine(to_s(*a), to_s(*b))
                tri = [p1, p1 - u * min(1.5, L * 0.08) + v * 0.6, p1 - u * min(1.5, L * 0.08) - v * 0.6]
                p.setBrush(col)
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

    def _profile_overlay(self, p, to_s):
        if self.station is None:
            return
        a, b = to_s(self.station, -1e4), to_s(self.station, 1e4)
        p.setPen(QPen(QColor(theme.BRIGHT), 1, Qt.PenStyle.DashLine))
        p.drawLine(QPointF(a.x(), 0), QPointF(a.x(), 5000))

    def _section_overlay(self, p, to_s):
        a = to_s(0.0, 0.0)
        p.setPen(QPen(QColor(self.profile_v.accent), 1, Qt.PenStyle.DashLine))
        p.drawLine(QPointF(a.x(), 0), QPointF(a.x(), 5000))

    # ================================================================ output
    # the fields show angles to 0.0001 deg, lever arms to the mm and latency to 0.1 ms; values equal to the
    # applied ones at that precision are the applied ones (not a manual change)
    TOL = (5e-5, 5e-4, 0.05)

    def _same_as_applied(self, mount, lever, lat):
        a = self.applied
        return (all(abs(m - b) < self.TOL[0] for m, b in zip(mount, a[0]))
                and all(abs(m - b) < self.TOL[1] for m, b in zip(lever, a[1])) and abs(lat - a[2]) < self.TOL[2])

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
        return {"plan": self.plan.grab(), "profile": self.profile_v.grab(), "section": self.section_v.grab()}
