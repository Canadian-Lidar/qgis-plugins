"""Tool windows that stay open beside the views while you work.

ProjectProperties  tabs: Project information · Geodetics · Survey configuration · Project files
AreaProperties     the Inspection Area by numbers: centre, size, bearing, steps, follow a line, go to
InfoPanel          the Lines · Classes · Info tab of the bottom-right quarter
"""
import datetime as _dt
import json
import math
import os

import numpy as np

from ..qt import QtCore, QtGui, QtWidgets, Qt, Signal, exec_
from . import chrome, theme
from .navpanel import dspin


def _utc(t):
    return _dt.datetime.fromtimestamp(float(t), _dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _fmt(v):
    """Readable value: dicts as 'key value, ...', lists as 'a, b, c'."""
    if isinstance(v, dict):
        return ",  ".join("%s %s" % (k, _fmt(x)) for k, x in v.items())
    if isinstance(v, (list, tuple)):
        return ", ".join(_fmt(x) for x in v)
    if isinstance(v, float):
        return ("%.4f" % v).rstrip("0").rstrip(".")
    return str(v)


def _kv_table(rows):
    """Read-only two-column table."""
    t = QtWidgets.QTableWidget(len(rows), 2)
    t.setHorizontalHeaderLabels(["Item", "Value"])
    t.verticalHeader().setVisible(False)
    t.horizontalHeader().setStretchLastSection(True)
    t.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
    for i, (k, v) in enumerate(rows):
        a = QtWidgets.QTableWidgetItem(str(k))
        b = QtWidgets.QTableWidgetItem("" if v is None else str(v))
        if k.isupper():
            f = a.font()
            f.setBold(True)
            a.setFont(f)
            a.setForeground(QtGui.QColor(theme.ACCENT))
        t.setItem(i, 0, a)
        t.setItem(i, 1, b)
    t.resizeColumnToContents(0)
    return t


class _ToolWindow(QtWidgets.QWidget):
    def __init__(self, mw, title, size=(760, 560)):
        super().__init__(mw, Qt.WindowType.Tool)
        self.mw = mw
        self.setWindowTitle(title)
        self.resize(*size)

    def showEvent(self, e):
        theme.polish_dialog(self)
        super().showEvent(e)


# =============================================================================
class ProjectProperties(_ToolWindow):
    def __init__(self, mw):
        super().__init__(mw, "CloudPro - project properties", (820, 600))
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        self.tabs = QtWidgets.QTabWidget()
        lay.addWidget(self.tabs)
        self.t_info = QtWidgets.QWidget()
        self.t_geo = QtWidgets.QWidget()
        self.t_cfg = QtWidgets.QWidget()
        self.t_files = QtWidgets.QWidget()
        for w, name in ((self.t_info, "Project information"), (self.t_geo, "Geodetics"),
                        (self.t_cfg, "Survey configuration"), (self.t_files, "Project files")):
            QtWidgets.QVBoxLayout(w).setContentsMargins(6, 6, 6, 6)
            self.tabs.addTab(w, name)
        self._build_geo()
        self._build_cfg()
        self._build_files()
        self._info_table = None

    def refresh(self):
        self._fill_info()
        self._fill_geo()
        self._fill_cfg()
        self._fill_files()

    def showEvent(self, e):
        self.refresh()
        super().showEvent(e)

    # ---------------------------------------------------------------- information
    def _fill_info(self):
        lay = self.t_info.layout()
        if self._info_table is not None:
            lay.removeWidget(self._info_table)
            self._info_table.deleteLater()
        p = self.mw.project
        rows = []
        if p is None:
            rows.append(("Project", "none open"))
        else:
            d = p.dcs
            rows += [("PROJECT", ""), ("Name", p.name),
                     ("Project file", d.path if d else p.path), ("Job folder (raw data)", d.job_folder if d else "-"),
                     ("CloudPro folder", p.root), ("Created", p.data.get("created")),
                     ("Last saved", p.data.get("modified"))]
            lines = p.lines
            t0 = [l.get("t0") for l in lines if l.get("t0")]
            t1 = [l.get("t1") for l in lines if l.get("t1")]
            rows += [("SURVEY", "")]
            if t0:
                rows += [("Start", _utc(min(t0))), ("End", _utc(max(t1))),
                         ("Time recording", "%.1f min" % (sum(l.get("t1", 0) - l.get("t0", 0) for l in lines) / 60))]
            length = 0.0
            for tr in self.mw.tracks.values():
                length += float(np.sum(np.hypot(np.diff(tr["E"]), np.diff(tr["N"]))))
            rows += [("Lines", len(lines)), ("Survey length (drive lines)", "%.2f km" % (length / 1000)),
                     ("Points", "{:,}".format(p.store.total))]
            ext = p.store.extent()
            if ext:
                o = p.store.origin
                rows += [("Extent E", "%.1f - %.1f" % (ext[0] + o[0], ext[2] + o[0])),
                         ("Extent N", "%.1f - %.1f" % (ext[1] + o[1], ext[3] + o[1]))]
                ll = self.mw.lat_lon((ext[0] + ext[2]) / 2 + o[0], (ext[1] + ext[3]) / 2 + o[1])
                if ll:
                    rows.append(("Location (centre)", "%.6f N  %.6f E" % ll))
            inp = p.data.get("inputs") or {}
            rows += [("GEOREFERENCING", ""),
                     ("Trajectory", (inp.get("trajectory") or {}).get("description", "-")),
                     ("Calibration", (inp.get("alignment") or {}).get("calibration", "-")),
                     ("Georeferenced", inp.get("georeferenced", "-")),
                     ("Coordinate system", p.crs), ("Heights", p.data.get("vertical", "ellipsoidal"))]
            if d:
                rows.append(("CHAIN", ""))
                for k, label, st, why in d.chain():
                    rows.append((label, st + (("  (" + "; ".join(why) + ")") if why else "")))
            pubs = p.data.get("published") or []
            if pubs:
                rows.append(("Last published", "%s  %s" % (pubs[-1]["utc"], ", ".join(pubs[-1].get("outputs", [])))))
        self._info_table = _kv_table(rows)
        lay.addWidget(self._info_table)

    # ---------------------------------------------------------------- geodetics
    def _build_geo(self):
        lay = self.t_geo.layout()
        h = QtWidgets.QLabel("The coordinate system and heights CloudPro georeferences into. They are kept in the "
                             "project, apart from the raw captures (which never change). Applying a change "
                             "re-georeferences the points and keeps every edit.")
        h.setObjectName("ppHint")
        h.setWordWrap(True)
        lay.addWidget(h)
        f = QtWidgets.QFormLayout()
        self.crs = QtWidgets.QLineEdit()
        b = QtWidgets.QPushButton("Select...")
        b.clicked.connect(self._pick_crs)
        r = QtWidgets.QHBoxLayout()
        r.addWidget(self.crs, 1)
        r.addWidget(b)
        f.addRow(chrome.label("Horizontal CRS"), r)
        self.crs_desc = QtWidgets.QLabel("")
        self.crs_desc.setObjectName("ppHint")
        f.addRow("", self.crs_desc)
        self.frame = QtWidgets.QLineEdit()
        self.frame.setReadOnly(True)
        f.addRow(chrome.label("Trajectory frame / epoch"), self.frame)
        self.epoch = QtWidgets.QComboBox()
        for label, val in (("Provincial standard for the CRS (New Brunswick 1997.0, else 2010.0)", "default"),
                           ("1997.0 (New Brunswick)", "1997.0"),
                           ("2010.0 (federal; Nova Scotia, PEI, Newfoundland and Labrador)", "2010.0"),
                           ("As observed (no epoch change)", "observed")):
            self.epoch.addItem(label, val)
        self.epoch.setEditable(False)
        self.epoch.setToolTip("NAD83(CSRS) coordinates move a few millimetres a year. The points are brought to "
                              "this epoch with NRCan's velocity grid when the trajectory is in another one (a "
                              "PPP trajectory is at the observation date; a PPK one at its base position's epoch).")
        f.addRow(chrome.label("Coordinate epoch"), self.epoch)
        self.heights = QtWidgets.QComboBox()
        self.heights.addItems(["ellipsoidal", "orthometric (geoid grid)"])
        f.addRow(chrome.label("Heights"), self.heights)
        self.geoid = QtWidgets.QLineEdit()
        gb = QtWidgets.QPushButton("Browse...")
        gb.clicked.connect(self._pick_geoid)
        r2 = QtWidgets.QHBoxLayout()
        r2.addWidget(self.geoid, 1)
        r2.addWidget(gb)
        f.addRow(chrome.label("Geoid grid"), r2)
        self.share = QtWidgets.QCheckBox("Also set this CRS as the job's shared coordinate system (PosPro and "
                                         "AlignPro then show out of date)")
        f.addRow("", self.share)
        lay.addLayout(f)
        bb = QtWidgets.QHBoxLayout()
        bb.addStretch(1)
        ab = QtWidgets.QPushButton("Apply and re-georeference")
        ab.setObjectName("ppPrimary")
        ab.clicked.connect(self._apply_geo)
        bb.addWidget(ab)
        lay.addLayout(bb)
        lay.addStretch(1)

    def _fill_geo(self):
        p = self.mw.project
        if p is None:
            return
        self.crs.setText(p.crs)
        try:
            from qgis.core import QgsCoordinateReferenceSystem
            c = QgsCoordinateReferenceSystem(p.crs)
            self.crs_desc.setText(c.description() if c.isValid() else "unknown CRS")
        except Exception:       # noqa: BLE001
            self.crs_desc.setText("")
        fr = ((p.data.get("inputs") or {}).get("trajectory") or {}).get("frame") or {}
        if p.dcs is not None and not fr:
            c = p.dcs.shared("crs") or {}
            fr = {"name": c.get("frame"), "epoch": c.get("epoch")}
        self.frame.setText("%s  %s" % (fr.get("name") or "-", fr.get("epoch") or ""))
        self.heights.setCurrentIndex(1 if p.data.get("vertical", "").startswith("ortho") else 0)
        self.geoid.setText(p.data.get("geoid_path", ""))
        self.epoch.setCurrentIndex(max(0, self.epoch.findData(p.data.get("epoch", "default"))))
        self.share.setEnabled(p.dcs is not None)

    def _pick_crs(self):
        try:
            from qgis.gui import QgsProjectionSelectionDialog
            from qgis.core import QgsCoordinateReferenceSystem
            d = QgsProjectionSelectionDialog(self)
            d.setCrs(QgsCoordinateReferenceSystem(self.crs.text()))
            if (exec_(d)):
                self.crs.setText(d.crs().authid())
        except Exception:       # noqa: BLE001
            theme.msg_info(self, "CloudPro", "Type an EPSG code, e.g. EPSG:2953")

    def _pick_geoid(self):
        p, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Geoid grid", "", "Grids (*.tif *.tiff *.gtx *.byn *.vrt)")
        if p:
            self.geoid.setText(p)
            self.heights.setCurrentIndex(1)

    def _apply_geo(self):
        self.mw.apply_geodetics(self.crs.text().strip(), self.heights.currentIndex() == 1, self.geoid.text().strip(),
                                self.share.isChecked(), self.epoch.currentData())

    # ---------------------------------------------------------------- survey configuration
    def _build_cfg(self):
        lay = self.t_cfg.layout()
        self.cfg_split = QtWidgets.QSplitter(Qt.Orientation.Vertical)
        self.cfg_host = QtWidgets.QWidget()
        QtWidgets.QVBoxLayout(self.cfg_host).setContentsMargins(0, 0, 0, 0)
        self.cfg_split.addWidget(self.cfg_host)
        g = QtWidgets.QGroupBox("Values CloudPro georeferences with")
        gl = QtWidgets.QGridLayout(g)
        self.src = QtWidgets.QComboBox()
        self.src.addItems(["Chain default (AlignPro if published, else as recorded)", "Recorded in the capture (no calibration)",
                           "Entered here"])
        self.src.currentIndexChanged.connect(self._src_changed)
        gl.addWidget(chrome.label("Source"), 0, 0)
        gl.addWidget(self.src, 0, 1, 1, 3)
        self.m = [dspin(-360, 360, 0, 0.001, 4, " deg") for _ in range(3)]
        self.l = [dspin(-20, 20, 0, 0.001, 3, " m") for _ in range(3)]
        self.lat = dspin(-1000, 1000, 0, 0.1, 2, " ms")
        gl.addWidget(chrome.label("Mounting roll / pitch / heading"), 1, 0)
        gl.addWidget(chrome.label("Lidar lever forward / starboard / down"), 2, 0)
        for i in range(3):
            gl.addWidget(self.m[i], 1, 1 + i)
            gl.addWidget(self.l[i], 2, 1 + i)
        gl.addWidget(chrome.label("Latency"), 3, 0)
        gl.addWidget(self.lat, 3, 1)
        h = QtWidgets.QLabel("Mounting in the mapper's convention (navaq_hesai: M = Rz(h) Ry(p) Rx(-r) Z0), lever "
                             "from the Certus reference point. The raw captures are not changed; Apply "
                             "re-georeferences with these values and keeps every edit.")
        h.setObjectName("ppHint")
        h.setWordWrap(True)
        gl.addWidget(h, 4, 0, 1, 4)
        ab = QtWidgets.QPushButton("Apply and re-georeference")
        ab.setObjectName("ppPrimary")
        ab.clicked.connect(self._apply_cfg)
        gl.addWidget(ab, 5, 3)
        self.cfg_split.addWidget(g)
        lay.addWidget(self.cfg_split)
        self._cfg_table = None

    def _src_changed(self, i):
        for w in self.m + self.l + [self.lat]:
            w.setEnabled(i == 2)

    def _fill_cfg(self):
        p = self.mw.project
        if p is None:
            return
        rows = []
        hw = {}
        if p.dcs is not None:
            hw = p.dcs.shared("hardware") or {}
        if not hw and p.lines:
            try:
                from ..core import clraw
                hw = clraw.Recording(p.lines[0]["files"][:1]).hardware
            except Exception:       # noqa: BLE001
                hw = {}
        rows.append(("HARDWARE (capture record)", ""))
        for k in ("lidar_model", "ins_model", "certus_model"):
            if hw.get(k):
                rows.append((k.replace("_", " "), hw[k]))
        for k, lab in (("ins_to_primary_antenna_m", "GNSS 1 antenna (F, S, D m)"),
                       ("ins_to_secondary_antenna_m", "GNSS 2 antenna (F, S, D m)"),
                       ("ins_to_lidar_m", "Lidar lever (F, S, D m)"), ("lidar_mount_navaq_deg", "Lidar mounting (deg)"),
                       ("certus_alignment_deg", "Certus alignment (deg)"), ("coordinate_system", "Coordinate system"),
                       ("vehicle", "Vehicle")):
            if k in hw:
                rows.append((lab, _fmt(hw[k])))
        devs = hw.get("devices") or {}
        if isinstance(devs, dict):
            for k, v in devs.items():
                rows.append(("connection: %s" % k, _fmt(v)))
        if p.dcs is not None:
            from dcs_core import handover
            try:
                tj = handover.read(p.dcs, "pospro", required=False)
            except Exception:       # noqa: BLE001
                tj = None
            rows.append(("POSPRO (trajectory.json)", "" if tj else "not published"))
            if tj:
                fr = tj.get("frame") or {}
                q = tj.get("quality") or {}
                rows += [("Frame", "%s %s, heights %s" % (fr.get("name"), fr.get("epoch"), fr.get("heights"))),
                         ("Mode / fixed", "%s, %s %% fixed" % (q.get("mode"), q.get("fixed_pct"))),
                         ("Max sigma H / V", "%s / %s m" % (q.get("max_sd_h_m"), q.get("max_sd_v_m"))),
                         ("Base station", tj.get("base_station")), ("Published", tj.get("created")),
                         ("Id", tj.get("id", "")[:23])]
                for k, v in (tj.get("levers_m") or {}).items():
                    if v:
                        rows.append(("lever %s" % k, "%s (%s)" % (v.get("value"), v.get("source"))))
            try:
                al = handover.read(p.dcs, "alignpro", required=False)
            except Exception:       # noqa: BLE001
                al = None
            rows.append(("ALIGNPRO (alignment.json)", "" if al else "not published - no calibration"))
            if al:
                m = al.get("mount_deg") or {}
                lv = al.get("lidar_lever_m") or {}
                q = al.get("quality") or {}
                rows += [("Mounting R / P / H", "%.4f / %.4f / %.4f deg (%s)" % (
                    m.get("roll", 0), m.get("pitch", 0), m.get("heading", 0), m.get("source"))),
                    ("Lidar lever", "%s m (%s)" % (lv.get("value"), lv.get("source"))),
                    ("Latency", "%.2f ms" % (1000 * float(al.get("latency_s") or 0))),
                    ("Boresight vs recorded", _fmt(al.get("boresight_deg"))),
                    ("Average error", "%s cm (recorded %s cm)" % (q.get("avg_error_cm"), q.get("avg_error_recorded_cm"))),
                    ("Published", al.get("created")), ("Id", al.get("id", "")[:23])]
        host = self.cfg_host.layout()
        if self._cfg_table is not None:
            host.removeWidget(self._cfg_table)
            self._cfg_table.deleteLater()
        self._cfg_table = _kv_table(rows)
        host.addWidget(self._cfg_table)
        ov = p.data.get("override") or {}
        self.src.setCurrentIndex({"alignpro": 0, "recorded": 1, "entered": 2}.get(ov.get("source"), 0))
        inp = p.data.get("inputs") or {}
        mount = ov.get("mount") or inp.get("mount_deg") or (hw.get("lidar_mount_navaq_deg") and [
            hw["lidar_mount_navaq_deg"].get(k, 0) for k in ("roll", "pitch", "heading")]) or [0, 0, 0]
        lever = ov.get("lever") or inp.get("lidar_lever_m") or hw.get("ins_to_lidar_m") or [0, 0, 0]
        for w, v in zip(self.m, mount):
            w.setValue(float(v))
        for w, v in zip(self.l, lever):
            w.setValue(float(v))
        self.lat.setValue(1000 * float(ov.get("latency_s", inp.get("latency_s") or 0)))
        self._src_changed(self.src.currentIndex())

    def _apply_cfg(self):
        src = ["alignpro", "recorded", "entered"][self.src.currentIndex()]
        ov = {"source": src}
        if src == "entered":
            ov.update(mount=[w.value() for w in self.m], lever=[w.value() for w in self.l],
                      latency_s=self.lat.value() / 1000.0)
        self.mw.apply_override(ov)

    # ---------------------------------------------------------------- project files
    def _build_files(self):
        lay = self.t_files.layout()
        h = QtWidgets.QLabel("Tick Points to show a file's points in the views and Track to show its drive line. "
                             "Click a row to select that file's points in the Plan view.")
        h.setObjectName("ppHint")
        h.setWordWrap(True)
        lay.addWidget(h)
        self.ftab = QtWidgets.QTableWidget(0, 7)
        self.ftab.setHorizontalHeaderLabels(["Points", "Track", "Line", "File", "Start (UTC)", "Duration", "Size"])
        self.ftab.verticalHeader().setVisible(False)
        self.ftab.horizontalHeader().setStretchLastSection(True)
        self.ftab.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.ftab.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.ftab.itemChanged.connect(self._file_item)
        self.ftab.itemSelectionChanged.connect(self._file_sel)
        lay.addWidget(self.ftab)
        row = QtWidgets.QHBoxLayout()
        for txt, fn in (("Show all", lambda: self.mw.set_files_visible(None, True)),
                        ("Hide all", lambda: self.mw.set_files_visible(None, False)),
                        ("Clear selection", self.mw.clear_selection)):
            b = QtWidgets.QPushButton(txt)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self._fbusy = False

    def _fill_files(self):
        p = self.mw.project
        self._fbusy = True
        self.ftab.setRowCount(0)
        if p is None:
            self._fbusy = False
            return
        rows = self.mw.file_table()
        self.ftab.setRowCount(len(rows))
        for i, r in enumerate(rows):
            for j, val in enumerate(["", "", "L%d" % (r["line"] + 1), os.path.basename(r["path"]),
                                     _utc(r["t0"]) if r["t0"] else "-",
                                     "%.1f s" % (r["t1"] - r["t0"]) if r["t0"] else "-",
                                     "%.1f MB" % (r["size"] / 1e6)]):
                it = QtWidgets.QTableWidgetItem(val)
                it.setData(Qt.ItemDataRole.UserRole, r["gid"])
                if j < 2:
                    it.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                    on = r["visible"] if j == 0 else r["track"]
                    it.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
                else:
                    it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                if j == 2:
                    it.setForeground(QtGui.QColor(r["colour"]))
                it.setToolTip(r["path"])
                self.ftab.setItem(i, j, it)
        self.ftab.resizeColumnsToContents()
        self._fbusy = False

    def _file_item(self, it):
        if self._fbusy or it.column() > 1:
            return
        gid = it.data(Qt.ItemDataRole.UserRole)
        on = it.checkState() == Qt.CheckState.Checked
        if it.column() == 0:
            self.mw.set_files_visible([gid], on)
        else:
            self.mw.set_file_track(gid, on)

    def _file_sel(self):
        if self._fbusy:
            return
        r = self.ftab.currentRow()
        if r >= 0:
            self.mw.select_file(self.ftab.item(r, 0).data(Qt.ItemDataRole.UserRole))


# =============================================================================
class AreaProperties(_ToolWindow):
    """The Inspection Area by numbers (right click in the Plan view)."""
    boxEdited = Signal(dict)
    stepRequested = Signal(str)
    followChanged = Signal(int)
    gotoRequested = Signal(float, float)

    def __init__(self, mw):
        super().__init__(mw, "CloudPro - Inspection Area", (440, 470))
        self._blk = False
        self._origin = (0, 0, 0)
        lay = QtWidgets.QVBoxLayout(self)
        g = QtWidgets.QGroupBox("Position and size")
        f = QtWidgets.QGridLayout(g)
        self.e = dspin(-1e8, 1e8, 0, 1, 3, " E")
        self.n = dspin(-1e8, 1e8, 0, 1, 3, " N")
        self.L = dspin(1, 5000, 60, 5, 2, " m")
        self.W = dspin(0.5, 5000, 30, 5, 2, " m")
        self.B = dspin(-360, 720, 0, 1, 2, " deg")
        for w in (self.e, self.n, self.L, self.W, self.B):
            w.valueChanged.connect(self._edited)
        f.addWidget(chrome.label("Centre"), 0, 0)
        f.addWidget(self.e, 0, 1)
        f.addWidget(self.n, 0, 2)
        f.addWidget(chrome.label("Length / width"), 1, 0)
        f.addWidget(self.L, 1, 1)
        f.addWidget(self.W, 1, 2)
        f.addWidget(chrome.label("Bearing"), 2, 0)
        f.addWidget(self.B, 2, 1)
        lay.addWidget(g)
        g = QtWidgets.QGroupBox("Stepping (Shift + arrows in the views)")
        f = QtWidgets.QGridLayout(g)
        self.step = dspin(0.01, 5000, 20, 1, 2, " m")
        self.step_len = QtWidgets.QCheckBox("Step = length")
        self.step_len.setChecked(True)
        self.step.setEnabled(False)                 # the length (less the overlap) is the step
        self.step_len.toggled.connect(lambda on: self.step.setEnabled(not on))
        self.overlap = QtWidgets.QSpinBox()
        self.overlap.setRange(0, 90)
        self.overlap.setValue(10)
        self.overlap.setSuffix(" % overlap")
        self.rot = dspin(0.01, 180, 5, 1, 2, " deg")
        f.addWidget(chrome.label("Step"), 0, 0)
        f.addWidget(self.step, 0, 1)
        f.addWidget(self.step_len, 0, 2)
        f.addWidget(chrome.label("Overlap"), 1, 0)
        f.addWidget(self.overlap, 1, 1)
        f.addWidget(chrome.label("Rotate step"), 2, 0)
        f.addWidget(self.rot, 2, 1)
        bl = QtWidgets.QGridLayout()
        for txt, key, r, c in (("Forward", "fwd", 0, 0), ("Back", "back", 1, 0), ("Right", "right", 0, 1),
                               ("Left", "left", 1, 1), ("Rotate +", "rotr", 0, 2), ("Rotate -", "rotl", 1, 2)):
            b = QtWidgets.QPushButton(txt)
            b.clicked.connect(lambda _=False, k=key: self.stepRequested.emit(k))
            bl.addWidget(b, r, c)
        f.addLayout(bl, 3, 0, 1, 3)
        self.follow = QtWidgets.QComboBox()
        self.follow.addItem("Free (straight steps)", -1)
        self.follow.setToolTip("Follow a drive line: the area steps along the line's chainage and turns with it")
        self.follow.currentIndexChanged.connect(lambda _: self.followChanged.emit(self.follow.currentData()))
        f.addWidget(chrome.label("Follow"), 4, 0)
        f.addWidget(self.follow, 4, 1, 1, 2)
        self.chain = QtWidgets.QLabel("")
        self.chain.setObjectName("ppVal")
        f.addWidget(self.chain, 5, 0, 1, 3)
        lay.addWidget(g)
        g = QtWidgets.QGroupBox("Go to / views")
        f = QtWidgets.QGridLayout(g)
        self.ge = dspin(-1e8, 1e8, 0, 1, 3, " E")
        self.gn = dspin(-1e8, 1e8, 0, 1, 3, " N")
        b = QtWidgets.QPushButton("Go")
        b.clicked.connect(lambda: self.gotoRequested.emit(self.ge.value(), self.gn.value()))
        f.addWidget(chrome.label("Centre on"), 0, 0)
        f.addWidget(self.ge, 0, 1)
        f.addWidget(self.gn, 0, 2)
        f.addWidget(b, 0, 3)
        self.vstep = dspin(0.001, 1000, 1.0, 0.1, 3, " m")
        self.vstep.setToolTip("Arrow keys pan the 2-D views by this distance; W/A/S/D/Q/E move the 3D view by it")
        f.addWidget(chrome.label("Arrow / fly step"), 1, 0)
        f.addWidget(self.vstep, 1, 1)
        lay.addWidget(g)
        lay.addStretch(1)

    def set_box(self, box, origin):
        self._blk = True
        self._origin = origin
        self.e.setValue(box["cx"] + origin[0])
        self.n.setValue(box["cy"] + origin[1])
        self.L.setValue(box["length"])
        self.W.setValue(box["width"])
        self.B.setValue(box["bearing"])
        self._blk = False

    def _edited(self, *_):
        if self._blk:
            return
        o = self._origin
        self.boxEdited.emit({"cx": self.e.value() - o[0], "cy": self.n.value() - o[1], "length": self.L.value(),
                             "width": self.W.value(), "bearing": self.B.value() % 360})

    def box_step(self):
        if self.step_len.isChecked():
            return self.L.value() * (1 - self.overlap.value() / 100.0)
        return self.step.value()

    def side_step(self):
        if self.step_len.isChecked():
            return self.W.value() * (1 - self.overlap.value() / 100.0)
        return self.step.value()

    def set_lines(self, lines):
        cur = self.follow.currentData()
        self.follow.blockSignals(True)
        self.follow.clear()
        self.follow.addItem("Free (straight steps)", -1)
        for ln in lines:
            self.follow.addItem("L%d  %s" % (ln["id"] + 1, ln.get("name", "")), ln["id"])
        self.follow.setCurrentIndex(max(0, self.follow.findData(cur)))
        self.follow.blockSignals(False)


# =============================================================================
LINES_HELP = """<h3>Lines</h3><p>One line per mapper recording (Start to Stop), with its colour.</p>
<ul><li><b>Tick</b> to show or hide that line's points in every view (hidden points are not edited either).</li>
<li><b>Right click</b> a line: show only this line, show all, edit only this line (edits and filters then
leave the other lines alone), or make the Inspection Area follow it.</li>
<li>Files and drive lines can also be toggled one by one in Project properties ▸ Project files.</li></ul>"""
CLASSES_HELP = """<h3>Classes</h3><p>The ASPRS classes present in the loaded Inspection Area, with their point
counts (accepted and rejected). During a filter preview the counts show the previewed classes.</p>
<ul><li><b>Tick</b> to show or hide a class in every view; hidden classes are not edited.</li>
<li>Colour a view by <i>Classification</i> to see them; the swatch is the class colour.</li>
<li>Classes are set by the classification filters (ground, vegetation, buildings, features) or by the
Classify edit mode.</li></ul>"""
INFO_HELP = """<h3>Info</h3><p>Use the <b>Info</b> tool (I) and click a point in any view: its coordinates, time,
line and laser, return, range, intensity, class and status (accepted, or rejected and why: manual,
automatic or original) appear here.</p><p>Below it, the Inspection Area totals: points and rejections per line.
With Select mode, the selection size is shown too.</p>"""


class InfoPanel(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(8)
        self.lines_w = QtWidgets.QListWidget()
        self.lines_w.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.classes_w = QtWidgets.QListWidget()
        self.info = QtWidgets.QPlainTextEdit()
        self.info.setReadOnly(True)
        self.info.setFont(theme.mono(11))
        for title, w, hlp, st in (("LINES", self.lines_w, LINES_HELP, 2), ("CLASSES", self.classes_w, CLASSES_HELP, 2),
                                  ("INFO", self.info, INFO_HELP, 3)):
            col = QtWidgets.QVBoxLayout()
            h = QtWidgets.QHBoxLayout()
            h.addWidget(chrome.label(title))
            h.addStretch(1)
            h.addWidget(chrome.HelpButton(title.title(), hlp, size=22))
            col.addLayout(h)
            col.addWidget(w, 1)
            lay.addLayout(col, st)
