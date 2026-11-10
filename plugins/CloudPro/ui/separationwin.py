"""Line separation results (CloudPro 2.2.0): what the check found, per line pair and per line, with the report."""
import os
import time

from ..core import separation as S
from ..qt import QtGui, QtWidgets, Qt
from . import theme
from .planview import COVER_LOW, COVER_OK, COVER_POOR
from .props import _ToolWindow

PAIR_COLS = ["Lines", "Patches", "Median", "95 %", "East", "North", "Up", "Rest"]


def _cm(v, signed=False):
    if v is None:
        return "–"
    return S._cm(v, signed)


class SeparationWindow(_ToolWindow):
    def __init__(self, mw):
        super().__init__(mw, "CloudPro - line separation", (760, 560))
        self.res = None
        self.current = True
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        self.lbl_verdict = QtWidgets.QLabel("")
        self.lbl_verdict.setWordWrap(True)
        self.lbl_verdict.setFont(theme.ui_font(13))
        lay.addWidget(self.lbl_verdict)
        self.lbl_info = QtWidgets.QLabel("")
        self.lbl_info.setObjectName("ppKey")
        self.lbl_info.setWordWrap(True)
        lay.addWidget(self.lbl_info)

        lay.addWidget(self._title("LINE PAIRS  ·  cm  ·  shift of the second line against the first"))
        self.pairs = self._table(PAIR_COLS)
        lay.addWidget(self.pairs, 3)
        lay.addWidget(self._title("LINES  ·  cm  ·  median against the lines each one overlaps"))
        self.lines = self._table(["Line", "Median", "Pairs", "Patches"])
        lay.addWidget(self.lines, 2)

        self.pairs.itemSelectionChanged.connect(lambda: self._picked(self.pairs))
        self.lines.itemSelectionChanged.connect(lambda: self._picked(self.lines))

        row = QtWidgets.QHBoxLayout()
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
        b = QtWidgets.QPushButton("Close")
        b.clicked.connect(self.close)
        row.addWidget(b)
        lay.addLayout(row)

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
            self.btn_save.setEnabled(False)
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
