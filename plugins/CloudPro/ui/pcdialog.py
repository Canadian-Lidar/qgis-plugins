"""Import point clouds... (CloudPro 2.16.0): the files, what each holds, their coordinate system, and how they
become lines."""
import os

from ..core import pcread
from ..core.geodesy import identify_crs, same_crs
from ..qt import QtGui, QtWidgets
from . import theme


class PointCloudDialog(QtWidgets.QDialog):
    """files: paths to start with. job_crs: the job's CRS; new_job: the job has no points yet, so its CRS can
    still follow the files."""

    def __init__(self, parent, files=(), job_crs="EPSG:2953", new_job=False, start_dir=""):
        super().__init__(parent)
        self.setWindowTitle("CloudPro - import point clouds")
        self.new_job = new_job
        self.job_crs = job_crs or "EPSG:2953"
        self.start_dir = start_dir
        self.sources = {}                       # path -> Source or error text
        v = QtWidgets.QVBoxLayout(self)
        lab = QtWidgets.QLabel("Point clouds from other systems or finished elsewhere: LAS, LAZ, E57, PLY, PTS, PCD "
                               "and text (XYZ, TXT, CSV). They go into the job as lines, ready to check, clean, "
                               "classify and finalize; PosPro and AlignPro are not needed.")
        lab.setWordWrap(True)
        v.addWidget(lab)
        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["File", "Contents", "Coordinate system", "Lines"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setMinimumHeight(150)
        v.addWidget(self.table)
        row = QtWidgets.QHBoxLayout()
        b_add = QtWidgets.QPushButton("Add files...")
        b_add.clicked.connect(self.add_files)
        b_rm = QtWidgets.QPushButton("Remove")
        b_rm.clicked.connect(self.remove_selected)
        row.addWidget(b_add)
        row.addWidget(b_rm)
        row.addStretch(1)
        v.addLayout(row)
        form = QtWidgets.QFormLayout()
        self.crs_mode = QtWidgets.QComboBox()
        self.crs_mode.addItems(["As each file says (the job's where a file says nothing)", "All files are in:"])
        self.crs_mode.currentIndexChanged.connect(self._update)
        self.crs_text = QtWidgets.QLineEdit("")
        self.crs_text.setPlaceholderText("EPSG code, e.g. EPSG:2953")
        self.crs_text.textChanged.connect(self._update)
        c = QtWidgets.QHBoxLayout()
        c.addWidget(self.crs_mode, 1)
        c.addWidget(self.crs_text, 1)
        form.addRow("The files' coordinates", c)
        self.job_crs_edit = QtWidgets.QLineEdit(self.job_crs)
        self.job_crs_edit.setEnabled(new_job)
        self.job_crs_edit.setToolTip("The job's coordinate system: points in another are moved into it. It can be "
                                     "changed only while the job has no points." if new_job else
                                     "The job's coordinate system (Project properties); files in another are moved "
                                     "into it")
        self.job_crs_edit.textChanged.connect(self._update)
        form.addRow("The job's coordinate system", self.job_crs_edit)
        self.cb_split = QtWidgets.QCheckBox("A file holding several sources or scans becomes one line each "
                                            "(LAS / LAZ point source, E57 scan, PTS block)")
        self.cb_split.setChecked(True)
        self.cb_split.toggled.connect(self._update)
        form.addRow("Lines", self.cb_split)
        self.cb_cls = QtWidgets.QCheckBox("Keep the files' classes (otherwise everything starts unclassified)")
        self.cb_cls.setChecked(True)
        form.addRow("Classes", self.cb_cls)
        self.cb_wh = QtWidgets.QCheckBox("Points the file marks withheld start rejected (reason: original)")
        self.cb_wh.setChecked(True)
        form.addRow("Withheld", self.cb_wh)
        self.sp_dec = QtWidgets.QSpinBox()
        self.sp_dec.setRange(1, 100)
        self.sp_dec.setPrefix("every ")
        self.sp_dec.setSuffix(" point(s)")
        self.sp_dec.setToolTip("Keep every Nth point: 1 keeps them all")
        form.addRow("Points", self.sp_dec)
        v.addLayout(form)
        self.note = QtWidgets.QLabel("")
        self.note.setWordWrap(True)
        v.addWidget(self.note)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        self.b_ok = bb.addButton("Import", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.resize(820, 480)
        for p in files:
            self._add(p)
        self._update()

    # ------------------------------------------------------------------ files
    def add_files(self):
        ps, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Point-cloud files", self.start_dir,
                                                       pcread.FILTER + ";;All files (*)")
        for p in ps:
            self._add(p)
        if ps:
            self.start_dir = os.path.dirname(ps[0])
        self._update()

    def _add(self, p):
        p = os.path.abspath(p)
        if p in self.sources:
            return
        try:
            s = pcread.open_source(p)
        except (pcread.PointCloudError, OSError, ValueError) as e:
            s = str(e)
        self.sources[p] = s
        if self.new_job and isinstance(s, pcread.Source) and s.crs and len(self.sources) == 1:
            self.job_crs_edit.setText(identify_crs(s.crs))          # a new job takes the first file's CRS

    def remove_selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        keys = list(self.sources)
        for r in rows:
            if r < len(keys):
                del self.sources[keys[r]]
        self._update()

    def paths(self):
        return [p for p, s in self.sources.items() if isinstance(s, pcread.Source)]

    # ------------------------------------------------------------------ state
    def source_crs(self):
        return self.crs_text.text().strip() if self.crs_mode.currentIndex() == 1 else None

    def target_crs(self):
        return self.job_crs_edit.text().strip() or self.job_crs

    def _update(self):
        self.crs_text.setEnabled(self.crs_mode.currentIndex() == 1)
        self.table.setRowCount(len(self.sources))
        tgt = self.target_crs()
        forced = self.source_crs()
        moved = unknown = 0
        for r, (p, s) in enumerate(self.sources.items()):
            cells = [os.path.basename(p)]
            if isinstance(s, pcread.Source):
                crs = forced or s.crs
                txt = identify_crs(crs)[:40] if crs else "not in the file: the job's"
                if crs and not same_crs(crs, tgt):
                    txt += " (moved to the job's)"
                    moved += 1
                if not crs:
                    unknown += 1
                if len(s.parts) > 1:
                    lines = "%d (%ss)" % (len(s.parts), s.part_kind) if self.cb_split.isChecked() else "1"
                elif s.part_kind == "point source" and self.cb_split.isChecked():
                    lines = "1 per point source"
                else:
                    lines = "1"
                cells += [s.describe(), txt, lines]
            else:
                cells += ["cannot be read: %s" % s, "", ""]
            for c, t in enumerate(cells):
                it = QtWidgets.QTableWidgetItem(t)
                it.setToolTip(t)
                if not isinstance(s, pcread.Source):
                    it.setForeground(QtGui.QBrush(QtGui.QColor(theme.RED)))
                self.table.setItem(r, c, it)
        self.table.resizeColumnsToContents()
        bits = []
        if unknown:
            bits.append("%d file(s) do not say their coordinate system: their coordinates are taken as the job's "
                        "(%s). Choose 'All files are in' if they are in another." % (unknown, tgt))
        if moved:
            bits.append("%d file(s) are moved into the job's coordinate system (%s); heights are kept as they are."
                        % (moved, tgt))
        self.note.setText("\n".join(bits))
        self.b_ok.setEnabled(bool(self.paths()))

    def _ok(self):
        if self.crs_mode.currentIndex() == 1 and not self.source_crs():
            theme.msg_warn(self, "CloudPro", "Give the files' coordinate system (an EPSG code such as EPSG:2953).")
            return
        self.accept()

    def options(self):
        from ..core.pcimport import PcOptions
        return PcOptions(source_crs=self.source_crs(), split="smart" if self.cb_split.isChecked() else "file",
                         keep_classes=self.cb_cls.isChecked(), withheld_rejected=self.cb_wh.isChecked(),
                         decimate=self.sp_dec.value())

