"""Line separation results (CloudPro 2.2.0): what the check found, per line pair and per line, with the report.
From 2.3.0 a panel in the bottom-right tab group (LINE SEPARATION), showing the saved result, with Open in PosPro
(a problem area, or a trajectory finding) and Open in AlignPro (a calibration finding)."""
import os
import time

from ..core import separation as S
from ..qt import QtGui, QtWidgets, Qt
from . import theme
from .planview import COVER_LOW, COVER_OK, COVER_POOR

PAIR_COLS = ["Lines", "Patches", "Median", "95 %", "East", "North", "Up", "Rest"]
AREA_COLS = ["#", "Line", "Easting", "Northing", "Size", "Median", "Max", "Time (UTC)", "GPS seconds of week"]
STEADY_COLS = ["Lines", "Driven", "Along", "Sideways", "Up", "Roll per 10 m", "Rest", "Points at"]
UNITS = ["cm; size m². Line = the line at fault when the area shows against several lines, else the pair.",
         "cm, second line against the first, in the first line's direction of travel.",
         "cm; shift of the second line against the first; rest = left after the steady offset.",
         "cm; median against the lines each one overlaps."]


def _cm(v, signed=False):
    if v is None:
        return "–"
    return S._cm(v, signed)


class SeparationPanel(QtWidgets.QWidget):
    def __init__(self, mw):
        super().__init__()
        self.mw = mw
        self.res = None
        self.current = True
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        self.lbl_verdict = QtWidgets.QLabel("")
        self.lbl_verdict.setWordWrap(True)
        self.lbl_verdict.setFont(theme.ui_font(13))
        lay.addWidget(self.lbl_verdict)
        self.lbl_info = QtWidgets.QLabel("")
        self.lbl_info.setObjectName("ppKey")
        self.lbl_info.setWordWrap(True)
        lay.addWidget(self.lbl_info)

        self.tabs = QtWidgets.QTabWidget()
        self.areas = self._table(AREA_COLS)
        self.pairs = self._table(PAIR_COLS)
        self.lines = self._table(["Line", "Median", "Pairs", "Patches"])
        self.steady = self._table(STEADY_COLS)
        for w, t in ((self.areas, "PROBLEM AREAS"), (self.steady, "STEADY OFFSETS"), (self.pairs, "LINE PAIRS"),
                     (self.lines, "LINES")):
            self.tabs.addTab(w, t)
        lay.addWidget(self.tabs, 1)
        self.lbl_findings = QtWidgets.QLabel("")
        self.lbl_findings.setWordWrap(True)
        lay.addWidget(self.lbl_findings)
        self.lbl_units = self._title("")
        lay.addWidget(self.lbl_units)
        self.steady.itemSelectionChanged.connect(self._steady_findings)
        self.tabs.currentChanged.connect(self._units)

        self.areas.itemSelectionChanged.connect(self._area_picked)
        self.pairs.itemSelectionChanged.connect(lambda: self._picked(self.pairs))
        self.lines.itemSelectionChanged.connect(lambda: self._picked(self.lines))

        row = QtWidgets.QHBoxLayout()
        self.btn_pospro = QtWidgets.QPushButton("Open in PosPro")
        self.btn_pospro.setToolTip("PosPro's Trajectory Review, on the chosen problem area (C rows)")
        self.btn_pospro.clicked.connect(self.open_pospro)
        row.addWidget(self.btn_pospro)
        self.btn_alignpro = QtWidgets.QPushButton("Open in AlignPro")
        self.btn_alignpro.setToolTip("AlignPro's Pairs page, on the line pair with the calibration finding")
        self.btn_alignpro.clicked.connect(self.open_alignpro)
        row.addWidget(self.btn_alignpro)
        row.addSpacing(12)
        self.btn_all = QtWidgets.QPushButton("All pairs on the Plan")
        self.btn_all.clicked.connect(self.show_all)
        row.addWidget(self.btn_all)
        self.btn_again = QtWidgets.QPushButton("Check again")
        self.btn_again.clicked.connect(self.mw.check_separation)
        self.btn_save = QtWidgets.QPushButton("Save report…")
        self.btn_save.clicked.connect(self.save_report)
        row.addWidget(self.btn_again)
        row.addWidget(self.btn_save)
        row.addStretch(1)
        lay.addLayout(row)
        for t in (self.areas, self.steady, self.pairs):
            t.itemSelectionChanged.connect(self._handoff_state)
        self.tabs.currentChanged.connect(lambda *_: self._handoff_state())
        self._handoff_state()

    @staticmethod
    def _title(t):
        lab = QtWidgets.QLabel(t)
        lab.setObjectName("ppKey")
        return lab

    @staticmethod
    def _table(cols):
        t = QtWidgets.QTableWidget(0, len(cols))
        t.setHorizontalHeaderLabels(cols)
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        t.horizontalHeader().setStretchLastSection(True)
        return t

    def _units(self, i=None):
        self.lbl_units.setText(UNITS[self.tabs.currentIndex()])
        self._steady_findings()

    def _steady_findings(self):
        """On the STEADY OFFSETS tab: what the chosen (else the first) pair's steady offset means."""
        if self.res is None or self.tabs.currentIndex() != 1 or not self.res.pairs:
            self.lbl_findings.setVisible(False)
            return
        rows = self.steady.selectionModel().selectedRows() if self.steady.selectionModel() else []
        p = self.res.pairs[rows[0].row() if rows else 0]
        nm = self.res.data.get("names", {})
        lab = {"calibration": "Calibration (AlignPro)", "trajectory": "Trajectory (PosPro)",
               "either": "Calibration or trajectory", "note": "Note"}
        fi = S.pair_findings(p, self.tolerance)
        self.lbl_findings.setText("<b>%s / %s</b><br>%s" % (
            nm.get(str(p["a"]), p["a"]), nm.get(str(p["b"]), p["b"]),
            "<br>".join("<b>%s:</b> %s" % (lab[k], t) for k, t in fi) or "Nothing beyond the tolerance."))
        self.lbl_findings.setVisible(True)

    def _area_picked(self):
        """A problem area chosen: the Plan shows its line (or pair) and the Inspection Area goes there."""
        if getattr(self, "_filling", False) or self.res is None:
            return
        rows = self.areas.selectionModel().selectedRows() if self.areas.selectionModel() else []
        if not rows:
            return
        it = self.areas.item(rows[0].row(), 0)
        aid = it.data(Qt.ItemDataRole.UserRole) if it is not None else None
        a = next((x for x in self.res.problem_areas(self.tolerance) if x["id"] == aid), None)
        if a is not None:
            self.mw.goto_problem(a)

    def _picked(self, table):
        """A pair or a line chosen: the Plan shows only that one's separation."""
        if getattr(self, "_filling", False):
            return
        rows = table.selectionModel().selectedRows() if table.selectionModel() else []
        if not rows:
            return
        other = self.lines if table is self.pairs else self.pairs
        self._filling = True
        other.clearSelection()
        self._filling = False
        it = table.item(rows[0].row(), 0)
        f = it.data(Qt.ItemDataRole.UserRole) if it is not None else None
        self.mw.show_separation_of(tuple(f) if f else None)

    def show_all(self):
        self._filling = True
        self.pairs.clearSelection()
        self.lines.clearSelection()
        self._filling = False
        self.mw.show_separation_of(None)

    @property
    def tolerance(self):
        return self.mw.plan.tolerance

    def _colour(self, v):
        t = self.tolerance
        return QtGui.QColor(*(COVER_OK if v <= t else COVER_LOW if v <= 2 * t else COVER_POOR))

    def set_result(self, res, current=True):
        self.res, self.current = res, current
        self.refresh()

    def refresh(self):
        self._filling = True
        try:
            self._refresh()
        finally:
            self._filling = False

    def _refresh(self):
        r = self.res
        if r is None:
            self.lbl_verdict.setText("Not checked yet.")
            self.lbl_info.setText("")
            self.pairs.setRowCount(0)
            self.lines.setRowCount(0)
            self.areas.setRowCount(0)
            self.steady.setRowCount(0)
            self.tabs.setTabText(0, "PROBLEM AREAS")
            self.btn_save.setEnabled(False)
            self.lbl_findings.setVisible(False)
            self._handoff_state()
            return
        self.btn_save.setEnabled(True)
        self.lbl_verdict.setText(S.verdict(r, self.tolerance))
        d = r.data
        info = "Measured %s · %.0f s · %s patches of %.1f m in %d of %d tiles · %.0f%% within %g cm" % (
            d.get("utc", ""), d.get("seconds", 0), "{:,}".format(d.get("patches", 0)), r.patch,
            d.get("tiles_with_overlap", 0), d.get("tiles", 0), 100 * r.share_within(self.tolerance),
            100 * self.tolerance)
        if not self.current:
            info = "OUT OF DATE: the points were georeferenced again since.  " + info
        self.lbl_info.setText(info)
        self.lbl_info.setStyleSheet("color: %s;" % (theme.AMBER if not self.current else theme.DIM))
        nm = d.get("names", {})
        self.pairs.setRowCount(len(r.pairs))
        for i, p in enumerate(r.pairs):
            s = p["shift"]
            vals = ["%s / %s" % (nm.get(str(p["a"]), p["a"]), nm.get(str(p["b"]), p["b"])),
                    "{:,}".format(p["patches"]), _cm(p["median"]), _cm(p["p95"]), _cm(s[0], True),
                    _cm(s[1], True), _cm(s[2], True), _cm(p["rest"])]
            for j, v in enumerate(vals):
                it = QtWidgets.QTableWidgetItem(v)
                if j == 0:
                    it.setData(Qt.ItemDataRole.UserRole, ["pair", p["a"], p["b"]])
                if j:
                    it.setTextAlignment(int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))
                if j == 2:
                    it.setForeground(self._colour(p["median"]))
                if j in (4, 5) and s[j - 4] is None:
                    it.setToolTip("No walls in enough directions to measure it")
                self.pairs.setItem(i, j, it)
        self.pairs.resizeColumnsToContents()
        self.pairs.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.lines.setRowCount(len(r.lines))
        for i, l in enumerate(r.lines):
            for j, v in enumerate([l["name"], _cm(l["median"]), str(l["pairs"]), "{:,}".format(l["patches"])]):
                it = QtWidgets.QTableWidgetItem(v)
                if j == 0:
                    it.setData(Qt.ItemDataRole.UserRole, ["line", l["id"]])
                if j:
                    it.setTextAlignment(int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))
                if j == 1:
                    it.setForeground(self._colour(l["median"]))
                self.lines.setItem(i, j, it)
        self.lines.resizeColumnsToContents()
        self.lines.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        areas = r.problem_areas(self.tolerance)
        self.areas.setRowCount(len(areas))
        for i, a in enumerate(areas):
            t0, t1 = a["t0"], a["t1"]
            vals = [str(a["id"]), S.area_lines_text(a, nm), "%.1f" % a["e"], "%.1f" % a["n"], "%.0f" % a["m2"],
                    _cm(a["median"]), _cm(a["max"]),
                    S.area_times_text(a, nm), S.area_times_text(a, nm, gps=True)]
            for j, v in enumerate(vals):
                it = QtWidgets.QTableWidgetItem(v)
                if j == 0:
                    it.setData(Qt.ItemDataRole.UserRole, a["id"])
                if j in (2, 3, 4, 5, 6):
                    it.setTextAlignment(int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))
                if j in (5, 6):
                    it.setForeground(self._colour(a[("median", "max")[j - 5]]))
                if j == 7 and t0 is not None:
                    it.setToolTip(S.time_text(t0, t1))
                self.areas.setItem(i, j, it)
        self.areas.resizeColumnsToContents()
        self.areas.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.tabs.setTabText(0, "PROBLEM AREAS  %d" % len(areas))
        self.steady.setRowCount(len(r.pairs))
        drv = {"opposite": "both ways", "same": "same way", "crossing": "crossing"}
        for i, p in enumerate(r.pairs):
            m = p.get("model") or {}
            fi = S.pair_findings(p, self.tolerance)
            kinds = {k for k, _ in fi}
            at = ("Calibration (AlignPro)" if "calibration" in kinds else
                  "Trajectory (PosPro)" if "trajectory" in kinds else
                  "Calibration or trajectory" if "either" in kinds else "–")
            vals = ["%s / %s" % (nm.get(str(p["a"]), p["a"]), nm.get(str(p["b"]), p["b"])),
                    drv.get(m.get("relation"), "–"), _cm(m.get("along"), True), _cm(m.get("cross"), True),
                    _cm(m.get("up"), True), ("%.1f" % (1000 * m["roll"])) if m.get("roll") is not None else "–",
                    _cm(m.get("rest")), at]
            for j, v in enumerate(vals):
                it = QtWidgets.QTableWidgetItem(v)
                if 2 <= j <= 6:
                    it.setTextAlignment(int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))
                if j == 7 and at != "–":
                    it.setForeground(QtGui.QColor(theme.AMBER))
                self.steady.setItem(i, j, it)
        self.steady.resizeColumnsToContents()
        self.steady.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self._units()
        self._handoff_state()

    # ------------------------------------------------------------------ hand-off (2.3.0)
    def _selected_area(self):
        rows = self.areas.selectionModel().selectedRows() if self.areas.selectionModel() else []
        it = self.areas.item(rows[0].row(), 0) if rows else None
        return it.data(Qt.ItemDataRole.UserRole) if it is not None else None

    def _selected_pair(self):
        """The pair chosen on STEADY OFFSETS or LINE PAIRS (same order as res.pairs), else None."""
        for t in ((self.steady, self.pairs) if self.tabs.currentIndex() != 2 else (self.pairs, self.steady)):
            rows = t.selectionModel().selectedRows() if t.selectionModel() else []
            if rows and self.res is not None and rows[0].row() < len(self.res.pairs):
                return self.res.pairs[rows[0].row()]
        return None

    def _kinds(self, p):
        return {k for k, _ in S.pair_findings(p, self.tolerance)}

    def pospro_target(self):
        """What Open in PosPro shows: the chosen problem area (C<n>), else the point cloud rows."""
        if self.res is None:
            return None
        aid = self._selected_area()
        if aid is not None:
            return {"kind": "cloud_area", "id": "C%d" % aid}
        traj = any(self._kinds(p) & {"trajectory", "either"} for p in self.res.pairs)
        if self.res.problem_areas(self.tolerance) or traj:
            return {"kind": "cloud_area"}
        return None

    def alignpro_target(self):
        """What Open in AlignPro shows: the chosen pair when it has a calibration finding, else the first such pair."""
        if self.res is None:
            return None
        p = self._selected_pair()
        if p is None or not self._kinds(p) & {"calibration", "either"}:
            p = next((q for q in self.res.pairs if self._kinds(q) & {"calibration", "either"}), None)
        if p is None:
            return None
        nm = self.res.data.get("names", {})
        rec = {int(l["id"]): l for l in (self.mw.project.lines if self.mw.project is not None else [])}
        return {"kind": "cloud_pair", "lines": [nm.get(str(p["a"]), str(p["a"])), nm.get(str(p["b"]), str(p["b"]))],
                "line_numbers": [rec.get(p["a"], {}).get("line"), rec.get(p["b"], {}).get("line")]}

    def _handoff_state(self):
        ok = self.res is not None and self.current
        self.btn_pospro.setEnabled(ok and self.pospro_target() is not None)
        self.btn_alignpro.setEnabled(ok and self.alignpro_target() is not None)

    def open_pospro(self):
        t = self.pospro_target()
        if t is not None:
            self.mw.open_in("pospro", t)

    def open_alignpro(self):
        t = self.alignpro_target()
        if t is not None:
            self.mw.open_in("alignpro", t)

    def save_report(self):
        if self.res is None or self.mw.project is None:
            return
        name = "CloudPro_line_separation_%s_%s.txt" % (self.mw.project.name, time.strftime("%Y%m%d_%H%M"))
        start = os.path.join(self.mw.project.out_dir, name)
        p, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save the line separation report", start, "Text (*.txt)")
        if not p:
            return
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(S.report_lines(self.res, self.tolerance, self.mw.project.name)) + "\n")
        self.mw.status("Line separation report saved: %s" % p, 10000)


SeparationWindow = SeparationPanel             # the name before 2.3.0
