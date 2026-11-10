"""Import, export and new-project dialogs."""
import glob
import json
import os

from ..core import clraw
from ..qt import QtCore, QtWidgets, Qt
from . import theme
from .navpanel import dspin


def _k(t):
    lab = QtWidgets.QLabel(t)
    lab.setObjectName("ppKey")
    return lab


STOP_SPEED = 0.3            # m/s: below this the vehicle counts as stopped (Skip points while stopped)

class ImportDialog(QtWidgets.QDialog):
    """Choose .clraw recordings, the trajectory and the sensor geometry."""

    def __init__(self, parent=None, start_dir=""):
        super().__init__(parent)
        self.setWindowTitle("CloudPro - import .clraw lines")
        self.resize(760, 680)
        self.start_dir = start_dir or os.path.expanduser("~")
        self.recs = []
        lay = QtWidgets.QVBoxLayout(self)

        g = QtWidgets.QGroupBox("1  RECORDINGS (.clraw from the mapper)")
        v = QtWidgets.QVBoxLayout(g)
        self.tbl = QtWidgets.QTableWidget(0, 5)
        self.tbl.setHorizontalHeaderLabels(["Line", "Files", "Duration", "Mount (R, P, H)", "Lever (F, S, D)"])
        self.tbl.horizontalHeader().setStretchLastSection(True)
        self.tbl.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        v.addWidget(self.tbl)
        row = QtWidgets.QHBoxLayout()
        for txt, fn in (("Add files...", self.add_files), ("Add folder...", self.add_folder), ("Remove", self.remove)):
            b = QtWidgets.QPushButton(txt)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        lay.addWidget(g)

        g = QtWidgets.QGroupBox("2  TRAJECTORY")
        f = QtWidgets.QGridLayout(g)
        self.r_sbet = QtWidgets.QRadioButton("PosPro SBET (post-processed, recommended)")
        self.r_rt = QtWidgets.QRadioButton("Certus real-time (packet 20 in the .clraw)")
        self.r_sbet.setChecked(True)
        self.sbet = QtWidgets.QLineEdit()
        self.sbet.setPlaceholderText("sbet_YYYYMMDD.out (SMRMSG next to it is read too)")
        b = QtWidgets.QToolButton()
        b.setText("...")
        b.clicked.connect(self._pick_sbet)
        self.sbet_date = QtWidgets.QDateEdit()
        self.sbet_date.setCalendarPopup(True)
        self.sbet_date.setDisplayFormat("yyyy-MM-dd")
        self.sbet_date.setDate(QtCore.QDate.currentDate())
        self.use_date = QtWidgets.QCheckBox("Date override")
        self.use_date.setToolTip("Only needed when the SBET file name has no date (GPS week)")
        self.src_crs = QtWidgets.QLineEdit("")
        self.src_crs.setPlaceholderText("trajectory datum, default = the project CRS's own (e.g. NAD83(CSRS))")
        f.addWidget(self.r_sbet, 0, 0, 1, 3)
        f.addWidget(self.sbet, 1, 0, 1, 2)
        f.addWidget(b, 1, 2)
        f.addWidget(self.use_date, 2, 0)
        f.addWidget(self.sbet_date, 2, 1)
        f.addWidget(self.r_rt, 3, 0, 1, 3)
        f.addWidget(_k("Trajectory datum (optional)"), 4, 0)
        f.addWidget(self.src_crs, 4, 1, 1, 2)
        lay.addWidget(g)

        g = QtWidgets.QGroupBox("3  SENSOR GEOMETRY")
        f = QtWidgets.QGridLayout(g)
        self.ovr = QtWidgets.QCheckBox("Override the mounting and lever arm stored in each .clraw (hardware.json)")
        f.addWidget(self.ovr, 0, 0, 1, 4)
        self.mr = dspin(-360, 360, 90, 0.01, 4, " deg")
        self.mp = dspin(-360, 360, 45, 0.01, 4, " deg")
        self.mh = dspin(-360, 360, 0, 0.01, 4, " deg")
        self.lf = dspin(-20, 20, 0, 0.001, 3, " m")
        self.ls = dspin(-20, 20, 0, 0.001, 3, " m")
        self.ld = dspin(-20, 20, 0, 0.001, 3, " m")
        f.addWidget(_k("Mounting R / P / H (mapper convention)"), 1, 0)
        f.addWidget(self.mr, 1, 1)
        f.addWidget(self.mp, 1, 2)
        f.addWidget(self.mh, 1, 3)
        f.addWidget(_k("Lever F / S / D"), 2, 0)
        f.addWidget(self.lf, 2, 1)
        f.addWidget(self.ls, 2, 2)
        f.addWidget(self.ld, 2, 3)
        self.br = dspin(-10, 10, 0, 0.001, 4, " deg")
        self.bp = dspin(-10, 10, 0, 0.001, 4, " deg")
        self.bh = dspin(-10, 10, 0, 0.001, 4, " deg")
        f.addWidget(_k("Boresight dR / dP / dH"), 3, 0)
        f.addWidget(self.br, 3, 1)
        f.addWidget(self.bp, 3, 2)
        f.addWidget(self.bh, 3, 3)
        b = QtWidgets.QPushButton("Load AlignPro / mapper result...")
        b.setToolTip("JSON with lidar_mount_navaq_deg {roll, pitch, heading} and optionally ins_to_lidar_m [F,S,D] "
                     "and boresight_deg; also reads a hardware.json")
        b.clicked.connect(self._load_alignpro)
        f.addWidget(b, 4, 0, 1, 2)
        lay.addWidget(g)

        g = QtWidgets.QGroupBox("4  POINTS")
        f = QtWidgets.QGridLayout(g)
        self.returns = QtWidgets.QComboBox()
        self.returns.addItems(["both", "last", "strongest"])
        self.dups = QtWidgets.QCheckBox("Drop the second return when it repeats the first")
        self.dups.setChecked(True)
        self.rmin = dspin(0, 50, 1.0, 0.1, 2, " m")
        self.rmax = dspin(1, 500, 120, 1, 1, " m")
        self.dec = QtWidgets.QSpinBox()
        self.dec.setRange(1, 50)
        self.dec.setValue(1)
        self.dec.setToolTip("Keep every Nth Hesai packet (1 = all points)")
        self.stopped = QtWidgets.QCheckBox("Skip points while stopped")
        self.stopped.setChecked(True)
        self.stopped.setToolTip("Leave out the Lidar points recorded while the vehicle stands still (below "
                                "0.3 m/s): parked or waiting, the scanner repeats the same spot about a million "
                                "times a second, which only adds noise, disk space and time")
        self.geoid = QtWidgets.QLineEdit()
        self.geoid.setPlaceholderText("optional geoid grid (e.g. CGG2013a .tif/.gtx) for orthometric heights")
        gb = QtWidgets.QToolButton()
        gb.setText("...")
        gb.clicked.connect(self._pick_geoid)
        f.addWidget(_k("Returns"), 0, 0)
        f.addWidget(self.returns, 0, 1)
        f.addWidget(self.dups, 0, 2, 1, 2)
        f.addWidget(_k("Range gate"), 1, 0)
        f.addWidget(self.rmin, 1, 1)
        f.addWidget(self.rmax, 1, 2)
        f.addWidget(_k("Keep every Nth packet"), 2, 0)
        f.addWidget(self.dec, 2, 1)
        f.addWidget(self.stopped, 2, 2, 1, 2)
        f.addWidget(_k("Geoid"), 3, 0)
        f.addWidget(self.geoid, 3, 1, 1, 2)
        f.addWidget(gb, 3, 3)
        lay.addWidget(g)

        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok |
                                        QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText("Import")
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        theme.polish_dialog(self)

    # ------------------------------------------------------------------
    def _add_paths(self, paths):
        groups = clraw.group_recordings(paths)
        have = {tuple(r.paths) for r in self.recs}
        for key, ps in groups.items():                 # in recording order
            if tuple(sorted(ps)) in have:
                continue
            try:
                rec = clraw.Recording(ps)
            except Exception as ex:
                QtWidgets.QMessageBox.warning(self, "CloudPro", "%s:\n%s" % (ps[0], ex))
                continue
            self.recs.append(rec)
            if not self.sbet.text():
                cand = sorted(glob.glob(os.path.join(os.path.dirname(ps[0]), "sbet*.out")) +
                              glob.glob(os.path.join(os.path.dirname(ps[0]), "..", "*", "sbet*.out")))
                if cand:
                    self.sbet.setText(os.path.abspath(cand[0]))
        self._refresh()

    def _refresh(self):
        self.tbl.setRowCount(len(self.recs))
        for i, r in enumerate(self.recs):
            t0, t1 = r.recording_window()
            dur = "%.1f s" % (t1 - t0) if t0 and t1 else "?"
            m = r.mount()
            l = r.lever_arm()
            vals = [r.line, "%d" % len(r.paths), dur, "%.3f, %.3f, %.3f" % tuple(m), "%.3f, %.3f, %.3f" % tuple(l)]
            for j, v in enumerate(vals):
                it = QtWidgets.QTableWidgetItem(v)
                it.setToolTip("\n".join(r.paths))
                self.tbl.setItem(i, j, it)
        if self.recs and not self.ovr.isChecked():
            m, l = self.recs[0].mount(), self.recs[0].lever_arm()
            for w, v in zip((self.mr, self.mp, self.mh, self.lf, self.ls, self.ld), list(m) + list(l)):
                w.setValue(v)
        self.tbl.resizeColumnsToContents()

    def add_files(self):
        ps, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Mapper recordings", self.start_dir,
                                                       "clraw (*.clraw)")
        if ps:
            self.start_dir = os.path.dirname(ps[0])
            self._add_paths(ps)

    def add_folder(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Capture folder(s)", self.start_dir)
        if d:
            self.start_dir = d
            ps = glob.glob(os.path.join(d, "**", "*.clraw"), recursive=True)
            if not ps:
                QtWidgets.QMessageBox.information(self, "CloudPro", "No .clraw files under %s" % d)
            self._add_paths(ps)

    def remove(self):
        rows = sorted({i.row() for i in self.tbl.selectedIndexes()}, reverse=True)
        for r in rows:
            del self.recs[r]
        self._refresh()

    def _pick_sbet(self):
        p, _ = QtWidgets.QFileDialog.getOpenFileName(self, "SBET from PosPro", self.start_dir,
                                                     "SBET (*.out *.sbet);;All files (*)")
        if p:
            self.sbet.setText(p)

    def _pick_geoid(self):
        p, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Geoid grid", self.start_dir,
                                                     "Grids (*.tif *.tiff *.gtx *.byn *.vrt);;All files (*)")
        if p:
            self.geoid.setText(p)

    def _load_alignpro(self):
        p, _ = QtWidgets.QFileDialog.getOpenFileName(self, "AlignPro result / hardware.json", self.start_dir,
                                                     "JSON (*.json)")
        if not p:
            return
        try:
            with open(p) as f:
                d = json.load(f)
        except Exception as ex:
            QtWidgets.QMessageBox.warning(self, "CloudPro", str(ex))
            return
        m = d.get("lidar_mount_navaq_deg") or d.get("mount") or d.get("mounting_deg")
        if isinstance(m, dict):
            m = [m.get("roll", 0), m.get("pitch", 0), m.get("heading", 0)]
        lev = d.get("ins_to_lidar_m") or d.get("lever") or d.get("lever_m")
        bo = d.get("boresight_deg") or d.get("boresight")
        if isinstance(bo, dict):
            bo = [bo.get("roll", 0), bo.get("pitch", 0), bo.get("heading", 0)]
        if m:
            for w, v in zip((self.mr, self.mp, self.mh), m):
                w.setValue(float(v))
        if lev:
            for w, v in zip((self.lf, self.ls, self.ld), lev):
                w.setValue(float(v))
        if bo:
            for w, v in zip((self.br, self.bp, self.bh), bo):
                w.setValue(float(v))
        self.ovr.setChecked(bool(m or lev))

    def _ok(self):
        if not self.recs:
            QtWidgets.QMessageBox.information(self, "CloudPro", "Add one or more .clraw recordings first.")
            return
        if self.r_sbet.isChecked() and not os.path.exists(self.sbet.text()):
            QtWidgets.QMessageBox.information(self, "CloudPro", "Choose the SBET file from PosPro, or use the "
                                                                "Certus real-time trajectory.")
            return
        self.accept()

    def options(self):
        from ..core.importer import ImportOptions
        o = ImportOptions(returns=self.returns.currentText(), drop_duplicates=self.dups.isChecked(),
                          rmin=self.rmin.value(), rmax=self.rmax.value(), decimate=self.dec.value(),
                          min_speed=STOP_SPEED if self.stopped.isChecked() else 0.0,
                          bore=(self.br.value(), self.bp.value(), self.bh.value()),
                          source_crs=self.src_crs.text().strip() or None)
        if self.ovr.isChecked():
            o.mount = (self.mr.value(), self.mp.value(), self.mh.value())
            o.lever = (self.lf.value(), self.ls.value(), self.ld.value())
        return o

    def week_start(self):
        if not self.use_date.isChecked():
            return None
        from ..core.trajectory import week_start_for
        import datetime as dt
        d = self.sbet_date.date()
        return week_start_for(dt.datetime(d.year(), d.month(), d.day(), 12, tzinfo=dt.timezone.utc).timestamp())


THIN_SPACINGS = (0.02, 0.05, 0.1, 0.2, 0.25, 0.5, 1.0, 2.0, 5.0)      # m (density = 1 / spacing^2 points per m2)
TILE_SIZES = (50, 100, 200, 250, 500, 1000)                           # m
GRID_CELLS = (0.05, 0.1, 0.2, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0)          # m


def _spacing_label(s):
    d = 1.0 / (s * s)
    return "%g m  (%s pts/m\u00b2)" % (s, ("%g" % d) if d >= 1 else ("%.2f" % d))


class ExportDialog(QtWidgets.QDialog):
    def __init__(self, parent=None, lines=(), start=""):
        super().__init__(parent)
        self.setWindowTitle("CloudPro - export")
        self.resize(600, 540)
        f = QtWidgets.QFormLayout(self)
        self.kind = QtWidgets.QComboBox()
        self.kind.addItems(["LAS 1.4 point cloud", "XYZ text (CSV)", "Grid surface (GeoTIFF)"])
        self.kind.currentIndexChanged.connect(self._kind)
        self.path = QtWidgets.QLineEdit(start)
        b = QtWidgets.QToolButton()
        b.setText("...")
        b.clicked.connect(self._pick)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.path, 1)
        row.addWidget(b)
        self.scope = QtWidgets.QComboBox()
        self.scope.addItems(["Entire project", "Inspection Area"])
        self.lines = QtWidgets.QListWidget()
        self.lines.setMaximumHeight(110)
        for ln in lines:
            it = QtWidgets.QListWidgetItem("L%d  %s  (%s pts)" % (ln["id"] + 1, ln.get("name", ""),
                                                                    "{:,}".format(ln.get("points", 0))))
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked)
            it.setData(Qt.ItemDataRole.UserRole, ln["id"])
            self.lines.addItem(it)
        self.classes = QtWidgets.QLineEdit("")
        self.classes.setPlaceholderText("all classes (or e.g. 2 for ground only)")
        self.rejected = QtWidgets.QCheckBox("Include rejected points (LAS withheld flag)")
        self.per_line = QtWidgets.QCheckBox("One file per line")
        self.thin = QtWidgets.QComboBox()
        self.thin.addItem("All points (no thinning)", 0.0)
        for v in THIN_SPACINGS:
            self.thin.addItem(_spacing_label(v), v)
        self.thin.setToolTip("Keep one real point per cell of this spacing (the point nearest the cell centre: "
                             "coordinates and attributes untouched); the density it gives is shown beside it")
        self.thin_mode = QtWidgets.QComboBox()
        self.thin_mode.addItem("3D cells (keeps walls, poles and trees)", "3D")
        self.thin_mode.addItem("2D plan cells (one point per cell, e.g. ground models)", "2D")
        self.thin.currentIndexChanged.connect(self._kind)
        self.tile = QtWidgets.QComboBox()
        self.tile.addItem("One file (no tiles)", 0)
        for v in TILE_SIZES:
            self.tile.addItem("%d m tiles" % v, v)
        self.tile.setToolTip("Split the export into square tiles; each file is named by its tile's lower-left "
                             "corner (e.g. Shirley_231400_5080900.las)")
        self.cell = QtWidgets.QComboBox()
        for v in GRID_CELLS:
            self.cell.addItem("%g m" % v, v)
        self.cell.setCurrentIndex(GRID_CELLS.index(0.25))
        self.stat = QtWidgets.QComboBox()
        self.stat.addItems(["mean", "min", "max", "count", "intensity"])
        self.fill = QtWidgets.QSpinBox()
        self.fill.setRange(0, 50)
        self.fill.setSuffix(" cells")
        self.add_qgis = QtWidgets.QCheckBox("Add the result to the QGIS map")
        self.add_qgis.setChecked(True)
        f.addRow(_k("Format"), self.kind)
        f.addRow(_k("File"), row)
        f.addRow(_k("Area"), self.scope)
        f.addRow(_k("Lines"), self.lines)
        f.addRow(_k("Classes"), self.classes)
        f.addRow("", self.rejected)
        f.addRow("", self.per_line)
        f.addRow(_k("Thin to"), self.thin)
        f.addRow(_k("Thin in"), self.thin_mode)
        f.addRow(_k("Tiles"), self.tile)
        f.addRow(_k("Grid cell"), self.cell)
        f.addRow(_k("Grid statistic"), self.stat)
        f.addRow(_k("Fill gaps up to"), self.fill)
        f.addRow("", self.add_qgis)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok |
                                        QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText("Export")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)
        self._kind()
        theme.polish_dialog(self)

    def _kind(self, *_):
        k = self.kind.currentIndex()
        for w in (self.cell, self.stat, self.fill):
            w.setEnabled(k == 2)
        for w in (self.rejected, self.per_line):
            w.setEnabled(k == 0)
        for w in (self.thin, self.tile):
            w.setEnabled(k in (0, 1))
        self.thin_mode.setEnabled(k in (0, 1) and bool(self.thin.currentData()))
        ext = [".las", ".csv", ".tif"][k]
        p = self.path.text()
        if p:
            self.path.setText(os.path.splitext(p)[0] + ext)

    def _pick(self):
        flt = ["LAS (*.las)", "CSV (*.csv *.txt)", "GeoTIFF (*.tif)"][self.kind.currentIndex()]
        p, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Export to", self.path.text(), flt)
        if p:
            self.path.setText(p)

    def selected_lines(self):
        out = []
        for i in range(self.lines.count()):
            it = self.lines.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                out.append(it.data(Qt.ItemDataRole.UserRole))
        return out if len(out) != self.lines.count() else None


class GeorefDialog(QtWidgets.QDialog):
    """What the points will be georeferenced with (from the DCS chain), point options, and whether the edits
    of an earlier georeferencing are carried over."""

    def __init__(self, parent, inp, project):
        super().__init__(parent)
        self.setWindowTitle("CloudPro - georeference the job's captures")
        self.resize(720, 560)
        lay = QtWidgets.QVBoxLayout(self)
        t = QtWidgets.QLabel("Georeference")
        t.setObjectName("ppTitle")
        lay.addWidget(t)
        h = QtWidgets.QLabel("Every Lidar return is placed at its own firing time with the trajectory PosPro "
                             "published and the mounting AlignPro published. Without an AlignPro result the "
                             "mounting recorded in the capture is used and the results say \"no calibration\".")
        h.setObjectName("ppHint")
        h.setWordWrap(True)
        lay.addWidget(h)
        g = QtWidgets.QGroupBox("Inputs")
        f = QtWidgets.QFormLayout(g)
        ti = inp.traj_info
        f.addRow(_k("Trajectory"), self._val(ti.get("description", "?"), "ok" if ti.get("source") == "pospro" else "warn"))
        if ti.get("handover_id"):
            f.addRow(_k("Trajectory id"), self._val(ti["handover_id"][:23]))
        if inp.align_info.get("calibration") == "applied":
            f.addRow(_k("Mounting"), self._val("AlignPro calibration  R %.4f  P %.4f  H %.4f deg" % inp.mount, "ok"))
            if inp.lever:
                f.addRow(_k("Lidar lever arm"), self._val("F %.3f  S %.3f  D %.3f m  (%s)" % (
                    inp.lever[0], inp.lever[1], inp.lever[2], inp.align_info.get("lever_source") or "")))
            f.addRow(_k("Latency"), self._val("%.2f ms" % (inp.latency * 1000)))
            q = inp.align_info.get("quality") or {}
            if q.get("avg_error_cm") is not None:
                f.addRow(_k("Calibration error"), self._val("%.2f cm (recorded mounting %.2f cm)" % (
                    q.get("avg_error_cm") or 0, q.get("avg_error_recorded_cm") or 0)))
        else:
            f.addRow(_k("Mounting"), self._val("as recorded in the capture%s  -  no calibration" % (
                ("  (R %.4f  P %.4f  H %.4f deg)" % inp.mount) if inp.mount else ""), "warn"))
        f.addRow(_k("Captures"), self._val("%d .clraw file(s) in %d line(s)" % (
            len(inp.files), len(clraw.group_recordings(inp.files)))))
        self._crs_val = self._val("")
        self._crs_text = project.crs
        f.addRow(_k("Coordinate system"), self._crs_val)
        lay.addWidget(g)
        if inp.warnings:
            w = QtWidgets.QLabel("\n".join("! " + x for x in inp.warnings))
            w.setObjectName("ppVal")
            w.setProperty("level", "warn")
            w.setWordWrap(True)
            lay.addWidget(w)
        g = QtWidgets.QGroupBox("Points")
        f = QtWidgets.QGridLayout(g)
        self.returns = QtWidgets.QComboBox()
        self.returns.addItems(["both", "last", "strongest"])
        self.dups = QtWidgets.QCheckBox("Drop the second return when it repeats the first")
        self.dups.setChecked(True)
        self.rmin = dspin(0, 50, 1.0, 0.1, 2, " m")
        self.rmax = dspin(1, 500, 120, 1, 1, " m")
        self.dec = QtWidgets.QSpinBox()
        self.dec.setRange(1, 50)
        self.dec.setValue(1)
        self.dec.setToolTip("Keep every Nth Lidar packet (1 = all points)")
        self.stopped = QtWidgets.QCheckBox("Skip points while stopped")
        self.stopped.setChecked(True)
        self.stopped.setToolTip("Leave out the Lidar points recorded while the vehicle stands still (below "
                                "0.3 m/s): parked or waiting, the scanner repeats the same spot about a million "
                                "times a second, which only adds noise, disk space and time")
        self.geoid = QtWidgets.QLineEdit(project.data.get("geoid_path", ""))
        self.geoid.setPlaceholderText("optional geoid grid (.tif / .gtx) for orthometric heights")
        if getattr(self, "_crs_val", None) is not None:
            def heights(t):
                g = (t or "").strip()
                self._crs_val.setText("%s, heights %s" % (self._crs_text, ("orthometric (geoid %s)" % os.path.basename(g))
                                                         if g else "ellipsoidal"))
            self.geoid.textChanged.connect(heights)
            heights(self.geoid.text())
        gb = QtWidgets.QToolButton()
        gb.setText("...")
        gb.clicked.connect(self._pick_geoid)
        f.addWidget(_k("Returns"), 0, 0)
        f.addWidget(self.returns, 0, 1)
        f.addWidget(self.dups, 0, 2, 1, 2)
        f.addWidget(_k("Range gate"), 1, 0)
        f.addWidget(self.rmin, 1, 1)
        f.addWidget(self.rmax, 1, 2)
        f.addWidget(_k("Keep every Nth packet"), 2, 0)
        f.addWidget(self.dec, 2, 1)
        f.addWidget(self.stopped, 2, 2, 1, 2)
        f.addWidget(_k("Geoid"), 3, 0)
        f.addWidget(self.geoid, 3, 1, 1, 2)
        f.addWidget(gb, 3, 3)
        lay.addWidget(g)
        self.keep_edits = QtWidgets.QCheckBox("Carry the manual and filter edits over to the new points")
        self.keep_edits.setChecked(True)
        self.keep_edits.setEnabled(bool(project.lines))
        self.keep_edits.setToolTip("Edits are matched by firing time, laser and return, so they follow the points "
                                   "to their new positions")
        lay.addWidget(self.keep_edits)
        self.auto = QtWidgets.QCheckBox("Update automatically when PosPro or AlignPro publish (in the background, "
                                        "with these settings and the edits carried over)")
        self.auto.setChecked(bool(project.data.get("auto_update", True)))
        self.auto.setToolTip("Once PosPro and AlignPro are current again, CloudPro georeferences the job in the "
                             "background, so it opens ready to clean. Publishing stays your step.")
        lay.addWidget(self.auto)
        if project.lines:
            prev = project.lines[0]
            self.returns.setCurrentText(prev.get("returns", "both"))
            self.rmin.setValue(prev.get("rmin", 1.0))
            self.rmax.setValue(prev.get("rmax", 120.0))
            self.dec.setValue(int(prev.get("decimate", 1)))
            # an update keeps what the lines used (older lines kept every point)
            self.stopped.setChecked(float(prev.get("min_speed", 0.0)) > 0)
        lay.addStretch(1)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok |
                                        QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText(
            "Update georeferencing" if project.lines else "Georeference")
        bb.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setObjectName("ppPrimary")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.project = project
        theme.polish_dialog(self)

    def _val(self, text, level=None):
        lab = QtWidgets.QLabel(text)
        lab.setObjectName("ppVal")
        lab.setWordWrap(True)
        if level:
            lab.setProperty("level", level)
        return lab

    def _pick_geoid(self):
        p, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Geoid grid", "", "Grids (*.tif *.tiff *.gtx *.byn *.vrt)")
        if p:
            self.geoid.setText(p)

    def options(self):
        from ..core.importer import ImportOptions
        self.project.data["geoid_path"] = self.geoid.text().strip()
        return ImportOptions(returns=self.returns.currentText(), drop_duplicates=self.dups.isChecked(),
                             rmin=self.rmin.value(), rmax=self.rmax.value(), decimate=self.dec.value(),
                             min_speed=STOP_SPEED if self.stopped.isChecked() else 0.0)


class PublishDialog(QtWidgets.QDialog):
    """The delivered files (LAS 1.4 of the cleaned cloud, optional ground grid) and cloud.json for the job."""

    def __init__(self, parent, project, why):
        super().__init__(parent)
        self.setWindowTitle("CloudPro - publish for the job")
        self.resize(620, 420)
        lay = QtWidgets.QVBoxLayout(self)
        t = QtWidgets.QLabel("Publish")
        t.setObjectName("ppTitle")
        lay.addWidget(t)
        inp = project.data.get("inputs") or {}
        cal = (inp.get("alignment") or {}).get("calibration", "no calibration")
        h = QtWidgets.QLabel("Writes the files below to %s and cloud.json last (all or nothing). cloud.json "
                             "records the trajectory and calibration used (%s), the filters applied, the classes "
                             "and the CRS." % (project.out_dir, cal))
        h.setObjectName("ppHint")
        h.setWordWrap(True)
        lay.addWidget(h)
        if why or cal != "applied":
            w = QtWidgets.QLabel("\n".join(["! " + x for x in why] +
                                           (["! no calibration: the mounting recorded in the capture was used"]
                                            if cal != "applied" else [])))
            w.setObjectName("ppVal")
            w.setProperty("level", "warn")
            w.setWordWrap(True)
            lay.addWidget(w)
        f = QtWidgets.QFormLayout()
        self.las = QtWidgets.QCheckBox("LAS 1.4 point cloud (%s.las)" % project.name)
        self.las.setChecked(True)
        self.classes = QtWidgets.QLineEdit("")
        self.classes.setPlaceholderText("all classes (or e.g. 2,6 for ground and buildings)")
        self.rejected = QtWidgets.QCheckBox("Include rejected points (LAS withheld flag)")
        self.dem_on = QtWidgets.QCheckBox("Ground grid (mean of class 2, GeoTIFF)")
        self.dem = dspin(0.05, 50, 0.5, 0.05, 2, " m")
        self.add_qgis = QtWidgets.QCheckBox("Add the results to the QGIS map")
        self.add_qgis.setChecked(True)
        f.addRow("", self.las)
        f.addRow(_k("Classes"), self.classes)
        f.addRow("", self.rejected)
        f.addRow("", self.dem_on)
        f.addRow(_k("Grid cell"), self.dem)
        f.addRow("", self.add_qgis)
        lay.addLayout(f)
        lay.addStretch(1)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok |
                                        QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText("Publish")
        bb.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setObjectName("ppPrimary")
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        theme.polish_dialog(self)

    def _ok(self):
        if not self.las.isChecked() and not self.dem_on.isChecked():
            return
        self.accept()


class ScaleDialog(QtWidgets.QDialog):
    """Fixed colour scale of one view: minimum and maximum in the units the scale bar shows (heights as
    elevations, ranges in metres, times in UTC)."""

    def __init__(self, parent, view_name, mode, lo, hi, z_offset=0.0):
        super().__init__(parent)
        self.mode, self.z_offset = mode, float(z_offset)
        self.setWindowTitle("CloudPro - colour scale")
        lay = QtWidgets.QFormLayout(self)
        lab = QtWidgets.QLabel("%s view, %s: the colours run from the minimum to the maximum; points "
                               "outside get the end colours." % (view_name, mode))
        lab.setWordWrap(True)
        lab.setMaximumWidth(340)
        lay.addRow(lab)
        if mode == "Time":
            self.w_lo, self.w_hi = (QtWidgets.QDateTimeEdit() for _ in range(2))
            for w, v in ((self.w_lo, lo), (self.w_hi, hi)):
                w.setTimeSpec(Qt.TimeSpec.UTC)
                w.setDisplayFormat("yyyy-MM-dd HH:mm:ss 'UTC'")
                w.setDateTime(QtCore.QDateTime.fromMSecsSinceEpoch(int(round(v * 1000)), Qt.TimeSpec.UTC))
        else:
            unit, dec = {"Height": (" m", 2), "Range": (" m", 2)}.get(mode, ("", 1))
            shift = self.z_offset if mode == "Height" else 0.0
            self.w_lo = dspin(-1e9, 1e9, lo + shift, 0.1 if dec == 2 else 1, dec, unit)
            self.w_hi = dspin(-1e9, 1e9, hi + shift, 0.1 if dec == 2 else 1, dec, unit)
        lay.addRow("Minimum", self.w_lo)
        lay.addRow("Maximum", self.w_hi)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok |
                                        QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addRow(bb)

    def values(self):
        """(lo, hi) in CloudPro's internal units (local heights, metres, UTC seconds)."""
        if self.mode == "Time":
            return (self.w_lo.dateTime().toMSecsSinceEpoch() / 1000.0,
                    self.w_hi.dateTime().toMSecsSinceEpoch() / 1000.0)
        shift = self.z_offset if self.mode == "Height" else 0.0
        return self.w_lo.value() - shift, self.w_hi.value() - shift

    def _ok(self):
        lo, hi = self.values()
        if not hi > lo:
            theme.msg_warn(self, "CloudPro", "The maximum must be larger than the minimum.")
            return
        self.accept()
