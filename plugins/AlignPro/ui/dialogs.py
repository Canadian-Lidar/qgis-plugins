"""Recent projects, the trajectory choice and the settings dialog."""
import datetime as dt
import os

from qgis.PyQt.QtCore import QDate, QSettings
from qgis.PyQt.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox,
                                 QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                                 QListWidget, QPushButton, QRadioButton, QSpinBox, QTabWidget,
                                 QVBoxLayout, QWidget)

from ..core import engine
from ..core.project import sbet_paths
from ..core.trajectory import sbet_date_from_name

RECENT_KEY = "AlignPro/recent"


def recent_projects():
    v = QSettings().value(RECENT_KEY, [])
    if isinstance(v, str):
        v = [v]
    return [p for p in (v or []) if os.path.isfile(p)]


def add_recent(path):
    r = [p for p in recent_projects() if os.path.normcase(p) != os.path.normcase(path)]
    QSettings().setValue(RECENT_KEY, [path] + r[:14])


# ==================================================================== wizard
class TrajectoryBox(QGroupBox):
    """PosPro's published trajectory (the DCS chain), the Certus real-time one, or an SBET file."""

    def __init__(self, cfg=None, parent=None):
        super().__init__("Trajectory", parent)
        cfg = cfg or {}
        f = QFormLayout(self)
        self.r_pp = QRadioButton("PosPro trajectory published for this project (trajectory.json)")
        self.pp_state = QLabel("")
        self.pp_state.setObjectName("ppHint")
        self.pp_state.setWordWrap(True)
        self.r_rt = QRadioButton("Certus real-time (packet 20 in the .clraw), quick look")
        self.r_sb = QRadioButton("Post-processed SBET file(s): one per capture when each capture has its own")
        g = QButtonGroup(self)
        g.addButton(self.r_pp)
        g.addButton(self.r_rt)
        g.addButton(self.r_sb)
        f.addRow(self.r_pp)
        f.addRow(self.pp_state)
        f.addRow(self.r_sb)
        self.sbets = QListWidget()
        self.sbets.setMaximumHeight(84)
        self.sbets.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.sbets.setToolTip("Each SBET is matched to the captures it covers by its GPS time; captures recorded "
                              "with separate Start/Stop (and separate SBETs) each use their own")
        b = QPushButton("Add…")
        b.clicked.connect(self._browse)
        b_rm = QPushButton("Remove")
        b_rm.clicked.connect(self._remove)
        col = QVBoxLayout()
        col.addWidget(b)
        col.addWidget(b_rm)
        col.addStretch(1)
        row = QHBoxLayout()
        row.addWidget(self.sbets, 1)
        row.addLayout(col)
        w = QWidget()
        w.setLayout(row)
        row.setContentsMargins(0, 0, 0, 0)
        f.addRow("SBET files", w)
        self.set_paths(sbet_paths(cfg))
        self.date = QDateEdit()
        self.date.setCalendarPopup(True)
        self.date.setDisplayFormat("yyyy-MM-dd")
        self.auto_date = QCheckBox("Find the GPS week from the .clraw times")
        self.auto_date.setChecked(not cfg.get("date"))
        self.auto_date.toggled.connect(lambda on: self.date.setEnabled(not on))
        if cfg.get("date"):
            self.date.setDate(QDate.fromString(cfg["date"], "yyyy-MM-dd"))
        else:
            self.date.setDate(QDate.currentDate())
        self.date.setEnabled(not self.auto_date.isChecked())
        f.addRow("", self.auto_date)
        f.addRow("Date (UTC)", self.date)
        self.leap = QSpinBox()
        self.leap.setRange(0, 40)
        self.leap.setValue(int(cfg.get("leap", 18)))
        self.leap.setSuffix(" s  (GPS − UTC)")
        f.addRow("Leap seconds", self.leap)
        f.addRow(self.r_rt)
        note = QLabel("Use the PosPro trajectory for the calibration: without RTK fixed throughout, the real-time "
                      "position wanders between passes and the boresight solve absorbs it. When PosPro is re-run, "
                      "AlignPro shows as out of date in the chain bar.")
        note.setWordWrap(True)
        note.setObjectName("ppHint")
        f.addRow(note)
        self.set_type(cfg.get("type"))

    def set_type(self, typ):
        {"pospro": self.r_pp, "sbet": self.r_sb}.get(typ, self.r_rt).setChecked(True)

    def set_pospro_state(self, available, text):
        self.r_pp.setEnabled(True)
        self.pp_state.setText(text)
        self.pp_state.setStyleSheet("color: %s;" % ("#00e5aa" if available else "#f5a623"))

    def paths(self):
        return [self.sbets.item(i).text() for i in range(self.sbets.count())]

    def set_paths(self, paths):
        self.sbets.clear()
        for p in paths or []:
            self.sbets.addItem(p)

    def _browse(self):
        cur = self.paths()
        ps, _ = QFileDialog.getOpenFileNames(self, "SBET files (one per capture)",
                                             os.path.dirname(cur[-1]) if cur else "",
                                             "SBET (*.out *.sbet);;All files (*)")
        for p in ps:
            if p not in cur:
                self.sbets.addItem(p)
        if ps:
            self.r_sb.setChecked(True)
            d = sbet_date_from_name(ps[0])
            if d and not cur:
                self.date.setDate(QDate(d.year, d.month, d.day))

    def _remove(self):
        for it in self.sbets.selectedItems():
            self.sbets.takeItem(self.sbets.row(it))

    def value(self):
        ps = self.paths()
        sb = self.r_sb.isChecked() and bool(ps)
        return {"type": "pospro" if self.r_pp.isChecked() else ("sbet" if sb else "realtime"),
                "path": ps[0] if ps else "", "paths": ps,
                "date": "" if self.auto_date.isChecked() else self.date.date().toString("yyyy-MM-dd"),
                "leap": self.leap.value()}


# ================================================================== settings
SPEC = [
    ("Data", [
        ("returns", "Return used", ["strongest", "last", "both"], "Dual return: one return per firing avoids "
                                                                  "near-duplicate points"),
        ("range_min", "Range min (m)", (0.5, 50, 0.5), ""),
        ("range_max", "Range max (m)", (5, 200, 5), "Far points carry the most angle signal but also the most "
                                                   "noise; 60 m suits the XT-32"),
        ("intensity_min", "Intensity min", (0, 255, 1), ""),
        ("points_per_line", "Points kept per line", (100000, 8000000, 250000), "Random thinning budget; "
                                                                              "re-reads the lines"),
        ("fine_points_per_box", "Fine Tune: points per inspection box", (100000, 8000000, 250000),
         "Inside the roll / pitch / heading boxes the Data View re-reads every recorded point from the raw "
         ".clraw; above this many a box is thinned at random"),
    ]),
    ("Passes and pairs", [
        ("split_turns", "Split captures into passes at turns", bool, ""),
        ("min_speed", "Minimum speed (m/s)", (0.1, 10, 0.1), "Slower periods (statics) are dropped"),
        ("turn_deg", "Turn limit (deg)", (5, 90, 5), ""),
        ("min_pass_s", "Minimum pass length (s)", (1, 120, 1), ""),
        ("reciprocal_deg", "Reciprocal pair: heading difference ≥ (deg)", (90, 180, 5), ""),
        ("same_dir_deg", "Same direction: heading difference ≤ (deg)", (0, 90, 5), ""),
        ("min_overlap_pct", "Minimum overlap (%)", (0, 100, 5), ""),
        ("speed_margin", "Latency pair: speed difference ≥ (m/s)", (0, 20, 0.5), ""),
    ]),
    ("Patches", [
        ("voxel_m", "Patch size (m)", (0.5, 10, 0.25), "Voxel edge; 1.5–3 m for roadside scenes"),
        ("thickness_m", "Max thickness per line (m)", (0.005, 0.5, 0.005), "A patch must be planar within this "
                                                                           "in every line that sees it"),
        ("min_pts_line", "Min points per line", (5, 500, 5), ""),
        ("max_pts_patch_line", "Max points per line per patch", (10, 2000, 10), ""),
        ("flat_ratio", "Flat patches per non-flat patch", (0, 10, 0.25), "Caps ground so walls drive heading"),
        ("max_patches", "Max patches", (50, 50000, 250), ""),
        ("use_aoi", "Use only the area of interest", bool, "Draw it with the AOI tool (pole, building corner)"),
    ]),
    ("Solver", [
        ("huber_k", "Huber threshold (sigma)", (0.5, 10, 0.5), ""),
        ("reject_sigma", "Reject after convergence (sigma, 0 = off)", (0, 20, 0.5), ""),
        ("max_iter", "Max iterations", (1, 50, 1), ""),
        ("solve_lever", "Also solve the Lidar lever arm (weak; for diagnosis)", bool, ""),
    ]),
    ("Display", [
        ("grid_cell_m", "Grid cell size (m)", (0.1, 10, 0.1), "Display only"),
    ]),
]
RELOAD_KEYS = {"returns", "range_min", "range_max", "intensity_min", "points_per_line"}
PASS_KEYS = {"split_turns", "min_speed", "turn_deg", "min_pass_s"}


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(560, 420)
        self.orig = dict(settings)
        self.w = {}
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        for title, items in SPEC:
            page = QWidget()
            f = QFormLayout(page)
            for key, label, kind, tip in items:
                v = settings.get(key, engine.DEFAULT_SETTINGS.get(key))
                if kind is bool:
                    wd = QCheckBox()
                    wd.setChecked(bool(v))
                elif isinstance(kind, list):
                    wd = QComboBox()
                    wd.addItems(kind)
                    wd.setCurrentText(str(v))
                else:
                    lo, hi, st = kind
                    if isinstance(st, int) and isinstance(lo, int):
                        wd = QSpinBox()
                        wd.setRange(int(lo), int(hi))
                        wd.setSingleStep(int(st))
                        wd.setValue(int(v))
                    else:
                        wd = QDoubleSpinBox()
                        wd.setDecimals(3)
                        wd.setRange(lo, hi)
                        wd.setSingleStep(st)
                        wd.setValue(float(v))
                if tip:
                    wd.setToolTip(tip)
                self.w[key] = wd
                f.addRow(label, wd)
            tabs.addTab(page, title)
        lay.addWidget(tabs)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel |
                              QDialogButtonBox.StandardButton.RestoreDefaults)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        bb.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(self._defaults)
        lay.addWidget(bb)

    def _defaults(self):
        for k, wd in self.w.items():
            v = engine.DEFAULT_SETTINGS[k]
            if isinstance(wd, QCheckBox):
                wd.setChecked(bool(v))
            elif isinstance(wd, QComboBox):
                wd.setCurrentText(str(v))
            else:
                wd.setValue(v)

    def values(self):
        out = {}
        for k, wd in self.w.items():
            if isinstance(wd, QCheckBox):
                out[k] = wd.isChecked()
            elif isinstance(wd, QComboBox):
                out[k] = wd.currentText()
            else:
                out[k] = wd.value()
        return out

    def changed(self):
        v = self.values()
        return {k for k in v if v[k] != self.orig.get(k)}


