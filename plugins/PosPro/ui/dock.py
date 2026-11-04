"""PosPro main panel: a step-by-step workflow in the spirit of Qinertia.

    1 Data           rover files, time range, ephemeris
    2 Base stations  files or downloads, positions (PPP, CSRS-PPP, entered)
    3 Installation   vehicle, IMU model, lever arms, output point
    4 Processing     coupling mode, constellations, constraints, run
    5 Quality        summary, findings, map colouring, plots
    6 Calibration    antenna lever arms solved from the data
    7 Export         SBET/SMRMSG, text files, map layers

Styled like the Mobile Mapper dashboard (ui/theme.py).
"""
import copy
import datetime
import math
import os
import time

from qgis.core import Qgis, QgsApplication, QgsCoordinateReferenceSystem, QgsProject
from qgis.PyQt.QtCore import QDateTime, QSettings, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QColor, QFont, QIcon
from qgis.PyQt.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDateTimeEdit,
                                 QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                                 QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
                                 QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox, QPlainTextEdit,
                                 QProgressBar, QPushButton, QRadioButton, QScrollArea, QSizePolicy, QSpinBox,
                                 QStackedWidget, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget)

from ..core import csrs, engine, geo, pipeline, products, results, rinex
from ..core.project import FRAMES, IMU_PRESETS, PROJECT_SUFFIX, VEHICLE_PROFILES, Project
from . import maplayers, theme
from .tasks import WorkTask
from dcs_core.stepbar import StepBar
from dcs_core.window import PluginWindow

from .widgets import VEHICLE_PRESETS, Card, CoverageView, Logo, Section, StepButton, Toast, VehicleView
from .plots import Donut


def _hint(text):
    l = QLabel(text)
    l.setObjectName("ppHint")
    l.setWordWrap(True)
    return l


def _title(text):
    l = QLabel(text)
    l.setObjectName("ppTitle")
    return l


def _dspin(lo, hi, dec, step, suffix="", val=0.0):
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(dec)
    s.setSingleStep(step)
    if suffix:
        s.setSuffix(suffix)
    s.setValue(val)
    s.setKeyboardTracking(False)
    return s


def find_captures(job_folder, max_depth=3):
    """Mapper captures in a job folder: the .clraw files, or where there are
    none the Certus logs (.anpp). The Processing folder is skipped."""
    clraw, anpp = [], []
    base = os.path.abspath(job_folder)
    for root, dirs, fs in os.walk(base):
        depth = root[len(base):].count(os.sep)
        dirs[:] = [x for x in dirs if x.lower() != "processing" and not x.startswith(".") and depth < max_depth]
        for f in fs:
            low = f.lower()
            if low.endswith(".clraw"):
                clraw.append(os.path.join(root, f))
            elif low.endswith(".anpp"):
                anpp.append(os.path.join(root, f))
    return sorted(clraw) if clraw else sorted(anpp)


def _scroll(widget):
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.Shape.NoFrame)
    sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    widget.setMinimumWidth(0)
    sa.setWidget(widget)
    return sa


def _qdt_to_gps(q):
    d, tm = q.date(), q.time()
    t = datetime.datetime(d.year(), d.month(), d.day(), tm.hour(), tm.minute(), tm.second())
    return geo.utc_to_gps(t)


# ===========================================================================
class Page(QWidget):
    changed = pyqtSignal()

    def __init__(self, dock):
        super().__init__()
        self.dock = dock

    @property
    def project(self):
        return self.dock.project

    def refresh(self):
        pass

    def state(self):
        return "todo", ""


# ---------------------------------------------------------------------------
class DataPage(Page):
    def __init__(self, dock):
        super().__init__(dock)
        v = QVBoxLayout(self)
        v.addWidget(_title("Rover data"))
        v.addWidget(_hint("Add the Certus log (.anpp) or the mapper capture (.clraw). Split files of one "
                          "capture can be added together; they are read in name order."))
        self.files = QListWidget()
        self.files.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.files.setMinimumHeight(54)
        self.files.setMaximumHeight(90)
        v.addWidget(self.files)
        row = QHBoxLayout()
        b_add = QPushButton("Add files\u2026")
        b_rm = QPushButton("Remove")
        self.b_read = QPushButton("Read files")
        self.b_read.setObjectName("ppPrimary")
        row.addWidget(b_add)
        row.addWidget(b_rm)
        row.addStretch(1)
        row.addWidget(self.b_read)
        v.addLayout(row)
        b_add.clicked.connect(self.add_files)
        b_rm.clicked.connect(self.remove_files)
        self.b_read.clicked.connect(lambda: self.dock.run_extract(force=True))

        self.summary = QGroupBox("What the files contain")
        self.form = QFormLayout(self.summary)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        v.addWidget(self.summary)

        tg = QGroupBox("Time range to process (UTC)")
        tl = QGridLayout(tg)
        self.t0 = QDateTimeEdit()
        self.t1 = QDateTimeEdit()
        for w in (self.t0, self.t1):
            w.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
            w.setTimeSpec(Qt.TimeSpec.UTC)
            w.setCalendarPopup(True)
            w.editingFinished.connect(self.store_times)
        b_all = QPushButton("Whole capture")
        b_all.clicked.connect(self.whole)
        tl.addWidget(QLabel("Start"), 0, 0)
        tl.addWidget(self.t0, 0, 1)
        tl.addWidget(QLabel("End"), 1, 0)
        tl.addWidget(self.t1, 1, 1)
        tl.addWidget(b_all, 0, 2, 2, 1)
        v.addWidget(tg)

        ng = QGroupBox("Satellite ephemeris")
        nl = QVBoxLayout(ng)
        self.auto_nav = QCheckBox("Download broadcast ephemeris")
        self.auto_nav.toggled.connect(self.store_nav)
        nl.addWidget(self.auto_nav)
        nl.addWidget(_hint("All constellations, from the IGS (internet). The Certus records GPS ephemeris "
                           "only; without internet add a navigation file from your base."))
        self.navs = QListWidget()
        self.navs.setMaximumHeight(70)
        nl.addWidget(self.navs)
        r2 = QHBoxLayout()
        b_nav = QPushButton("Add navigation file\u2026")
        b_navrm = QPushButton("Remove")
        b_nav.clicked.connect(self.add_nav)
        b_navrm.clicked.connect(self.remove_nav)
        r2.addWidget(b_nav)
        r2.addWidget(b_navrm)
        r2.addStretch(1)
        nl.addLayout(r2)
        self.cov = CoverageView()
        cw = QWidget()
        cl = QVBoxLayout(cw)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.addWidget(self.cov)
        cl.addWidget(_hint("Mint: good. Amber: degraded (IMU samples missing, fewer than 5 satellites). "
                           "Red: no data. Dashed lines: the processing span. Hover for times."))
        v.addWidget(Section("Data coverage", cw, expanded=True))
        v.addWidget(ng)
        v.addStretch(1)

    # ------------------------------------------------------------------
    def add_files(self):
        last = QSettings().value("PosPro/last_dir", "")
        if self.project and self.project.dcs:
            last = self.project.dcs.job_folder
        fs, _ = QFileDialog.getOpenFileNames(self, "Rover files", last,
                                             "Certus / mapper (*.anpp *.clraw *.clraw.partial);;All files (*)")
        if not fs:
            return
        QSettings().setValue("PosPro/last_dir", os.path.dirname(fs[0]))
        cur = self.project.cfg["rover"]["files"]
        for f in fs:
            if f not in cur:
                cur.append(f)
        self.project.cfg["rover"]["time_start"] = self.project.cfg["rover"]["time_end"] = None
        self.dock.save()
        self.refresh()
        self.dock.run_extract()

    def remove_files(self):
        sel = {i.data(Qt.ItemDataRole.UserRole) for i in self.files.selectedItems()}
        self.project.cfg["rover"]["files"] = [f for f in self.project.cfg["rover"]["files"] if f not in sel]
        self.dock.save()
        self.refresh()

    def add_nav(self):
        fs, _ = QFileDialog.getOpenFileNames(self, "RINEX navigation files", QSettings().value("PosPro/last_dir", ""),
                                             "RINEX navigation (*.rnx *.nav *.*n *.*g *.*p *.gz);;All files (*)")
        for f in fs:
            if f not in self.project.cfg["nav_files"]:
                self.project.cfg["nav_files"].append(f)
        self.dock.save()
        self.refresh()

    def remove_nav(self):
        sel = {i.data(Qt.ItemDataRole.UserRole) for i in self.navs.selectedItems()}
        self.project.cfg["nav_files"] = [f for f in self.project.cfg["nav_files"] if f not in sel]
        self.dock.save()
        self.refresh()

    def store_nav(self, on):
        self.project.cfg["auto_ephemeris"] = bool(on)
        self.dock.save()

    def store_times(self):
        ex = self.project.cfg["rover"].get("extract") or {}
        if not ex.get("imu_t0"):
            return
        a, b = _qdt_to_gps(self.t0.dateTime()), _qdt_to_gps(self.t1.dateTime())
        if b <= a:
            return
        t0, t1 = float(ex["imu_t0"]), float(ex["imu_t1"])
        self.project.cfg["rover"]["time_start"] = a if a > t0 + 0.5 else None
        self.project.cfg["rover"]["time_end"] = b if b < t1 - 0.5 else None
        self.dock.save()
        self.dock.update_steps()

    def whole(self):
        self.project.cfg["rover"]["time_start"] = self.project.cfg["rover"]["time_end"] = None
        self.dock.save()
        self.refresh()

    # ------------------------------------------------------------------
    def refresh(self):
        p = self.project
        self.files.clear()
        self.navs.clear()
        self.dock.show_coverage(self.cov, bases=False)
        if not p:
            return
        for f in p.cfg["rover"]["files"]:
            it = QListWidgetItem(os.path.basename(f))
            it.setData(Qt.ItemDataRole.UserRole, f)
            it.setToolTip(f)
            if not os.path.exists(f):
                it.setForeground(QColor(theme.RED))
                it.setToolTip("File not found")
            self.files.addItem(it)
        for f in p.cfg["nav_files"]:
            it = QListWidgetItem(os.path.basename(f))
            it.setData(Qt.ItemDataRole.UserRole, f)
            it.setToolTip(f)
            self.navs.addItem(it)
        self.auto_nav.blockSignals(True)
        self.auto_nav.setChecked(bool(p.cfg.get("auto_ephemeris", True)))
        self.auto_nav.blockSignals(False)
        while self.form.rowCount():
            self.form.removeRow(0)
        ex = p.cfg["rover"].get("extract") or {}
        if not ex:
            self.form.addRow(QLabel("Not read yet."))
            return

        def row(k, v, warn=False):
            l = QLabel(v)
            l.setWordWrap(True)
            l.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            if warn:
                l.setStyleSheet("color: #c47a00;")
            self.form.addRow(k, l)

        try:
            t0, t1 = float(ex["imu_t0"]), float(ex["imu_t1"])
            u0, u1 = geo.gps_to_utc(t0), geo.gps_to_utc(t1)
            row("Recording", "%s  to  %s UTC  (%s)" % (u0.strftime("%Y-%m-%d %H:%M:%S"), u1.strftime("%H:%M:%S"),
                                                       geo.fmt_duration(t1 - t0)))
            for w, g in ((self.t0, p.cfg["rover"].get("time_start") or t0), (self.t1, p.cfg["rover"].get("time_end") or t1)):
                w.blockSignals(True)
                q = QDateTime()
                ut = geo.gps_to_utc(g)
                q.setTimeSpec(Qt.TimeSpec.UTC)
                q.setSecsSinceEpoch(int(geo.unix_from_utc(ut)))
                w.setMinimumDateTime(QDateTime.fromSecsSinceEpoch(int(geo.unix_from_utc(u0)), Qt.TimeSpec.UTC))
                w.setMaximumDateTime(QDateTime.fromSecsSinceEpoch(int(geo.unix_from_utc(u1)) + 1, Qt.TimeSpec.UTC))
                w.setDateTime(q)
                w.blockSignals(False)
        except (KeyError, ValueError):
            pass
        row("IMU (packet 28)", "%s samples at %.0f Hz; %s gaps" % (ex.get("imu_samples"), float(ex.get("imu_rate_hz", 0)),
                                                                 ex.get("imu_gaps", "0")),
            warn=int(ex.get("imu_gaps", "0") or 0) > 0)
        if int(ex.get("imu_before_time_valid", "0") or 0):
            row("", "%s IMU samples before GNSS time was valid were skipped" % ex.get("imu_before_time_valid"))
        if int(ex.get("imu_pairing_lag", "0") or 0):
            row("", "IMU time stamps were shifted %s sample(s) to match packet 20" % ex.get("imu_pairing_lag"), True)
        if "obs0_epochs" in ex:
            iv = float(ex.get("obs0_interval_s", 0) or 0)
            whole = abs(round(iv * 8) - iv * 8) < 1e-6 and iv > 0 and (1.0 / iv) % 1 < 1e-6
            row("GNSS (packet 60)", "%s epochs every %.3f s, %s satellites on average" % (
                ex.get("obs0_epochs"), iv, ex.get("obs0_mean_sats")))
            row("Signals", ex.get("obs0_signals", "").replace(" ", ", "))
            if iv and not whole:
                row("", "Epochs are not on whole seconds (%.0f ms). Base data are interpolated to the rover "
                        "epochs; set the Certus to 125 ms (GPS+GLONASS+Galileo) for whole-second epochs." % (iv * 1000), True)
            if "R" not in ex.get("obs0_signals", ""):
                row("", "GLONASS is off on the Certus. Turning it on adds satellites for fixing under trees.", True)
        else:
            row("GNSS (packet 60)", "none \u2013 raw observations were not recorded", True)
        row("Ephemeris (packet 61)", "%s GPS ephemerides" % ex.get("gps_ephemerides", "0"))
        if int(ex.get("crc_errors", "0") or 0):
            row("Checksum errors", ex.get("crc_errors"), True)
        hw = p.cfg["rover"].get("hardware")
        if hw:
            row("Mapper hardware", "hardware.json found in the capture (offsets can be used on the Installation page)")

    def state(self):
        p = self.project
        if not p or not p.cfg["rover"]["files"]:
            return "todo", "add rover files"
        ex = p.cfg["rover"].get("extract") or {}
        if not ex:
            return "todo", "not read yet"
        if "obs0_epochs" not in ex:
            return "error", "no raw GNSS"
        try:
            return "done", geo.fmt_duration(float(ex["imu_t1"]) - float(ex["imu_t0"]))
        except (KeyError, ValueError):
            return "done", ""


# ---------------------------------------------------------------------------
class BasePage(Page):
    COLS = ["Base", "Distance", "Interval", "Covers", "Position from"]

    def __init__(self, dock):
        super().__init__(dock)
        v = QVBoxLayout(self)
        v.addWidget(_title("Base stations"))
        v.addWidget(_hint("Tight coupling uses one base station. Pick the nearest one that covers the whole "
                          "drive (green ring: 20 km, amber 40 km, red 60 km on the map)."))
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels([c.upper() for c in self.COLS])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMinimumHeight(110)
        self.table.itemSelectionChanged.connect(self.show_selected)
        v.addWidget(self.table)
        row = QHBoxLayout()
        b_add = QPushButton("Add RINEX\u2026")
        b_dl = QPushButton("Download\u2026")
        b_dl.setToolTip("Map of the reference stations around the drive (NOAA CORS, IGS, NRCan, commercial RTK "
                        "networks, GEODNET): click one, choose the rate, download its RINEX and the precise orbits.")
        b_dl.clicked.connect(self.open_finder)
        b_rm = QPushButton("Remove")
        self.b_use = QPushButton("Use this base")
        row.addWidget(b_add)
        row.addWidget(b_dl)
        row.addWidget(b_rm)
        row.addStretch(1)
        row.addWidget(self.b_use)
        v.addLayout(row)
        b_add.clicked.connect(self.add_rinex)
        b_rm.clicked.connect(self.remove)
        self.b_use.clicked.connect(self.use_selected)

        self.edit = QGroupBox("Position of the selected base (marker)")
        g = QFormLayout(self.edit)
        self.src = QComboBox()
        for k, label in (("rinex", "RINEX header (approximate)"), ("manual", "Entered / published"),
                         ("ppp", "Built-in PPP"), ("csrs", "NRCan CSRS-PPP")):
            self.src.addItem(label, k)
        self.lat = QLineEdit()
        self.lon = QLineEdit()
        self.hgt = QLineEdit()
        for w, ph in ((self.lat, "decimal degrees"), (self.lon, "decimal degrees"),
                      (self.hgt, "ellipsoidal, m")):
            w.setPlaceholderText(ph)
        self.frame = QComboBox()
        for k, f in FRAMES.items():
            self.frame.addItem(f["label"], k)
        self.epoch = QLineEdit()
        self.epoch.setPlaceholderText("e.g. 1997.0")
        self.ant = QLineEdit()
        self.ant.setPlaceholderText("from RINEX")
        self.anth = _dspin(-10, 100, 4, 0.001, " m")
        g.addRow("Source", self.src)
        g.addRow("Latitude", self.lat)
        g.addRow("Longitude", self.lon)
        g.addRow("Height", self.hgt)
        g.addRow("Frame", self.frame)
        g.addRow("Epoch", self.epoch)
        g.addRow("Antenna type", self.ant)
        g.addRow("ARP height", self.anth)
        b_save = QPushButton("Save position")
        b_save.clicked.connect(self.save_position)
        g.addRow("", b_save)
        v.addWidget(self.edit)

        pg = QGroupBox("Compute the base position")
        pl = QVBoxLayout(pg)
        self.b_ppp = QPushButton("Built-in PPP")
        self.b_ppp.setToolTip("Static PPP with IGS precise orbits and clocks (downloaded). Gives ITRF2020 at the "
                              "observation date, transformed to NAD83(CSRS) at the same date.")
        self.b_csrs = QPushButton("Send to NRCan CSRS-PPP")
        self.email = QLineEdit()
        self.email.setPlaceholderText("CSRS-PPP user name (e-mail)")
        self.csrs_epoch = QComboBox()
        for label, val in (("New Brunswick (1997.0)", "1997-01-01"),
                           ("Federal (2010.0)", "2010-01-01"), ("Observation date", "CURR")):
            self.csrs_epoch.addItem(label, val)
        pl.addWidget(self.b_ppp)
        pl.addWidget(_hint("Precise orbits from the IGS; cached for offline use. Needs \u2265 2 h of base data "
                           "for cm level; reported at the observation date (no velocity applied)."))
        ff = QFormLayout()
        ff.addRow("E-mail", self.email)
        ff.addRow("NAD83(CSRS) epoch", self.csrs_epoch)
        pl.addLayout(ff)
        pl.addWidget(self.b_csrs)
        self.ppp_out = QLabel("")
        self.ppp_out.setWordWrap(True)
        self.ppp_out.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        pl.addWidget(self.ppp_out)
        self.b_ppp.clicked.connect(self.run_ppp)
        self.b_csrs.clicked.connect(self.run_csrs)
        self.cov = CoverageView()
        cw = QWidget()
        cl = QVBoxLayout(cw)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.addWidget(self.cov)
        cl.addWidget(_hint("Each base must cover the whole drive (red = no base data over the rover's time)."))
        v.addWidget(Section("Data coverage", cw, expanded=True))
        v.addWidget(pg)
        v.addStretch(1)

    # ------------------------------------------------------------------
    def selected(self):
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        bid = self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        return self.project.base(bid)

    def add_base_file(self, files, name=None, source="rinex"):
        info = rinex.header_info(files[0], scan_body=True)
        ent = {"name": name or info.get("marker") or os.path.basename(files[0])[:8], "files": files,
               "approx_xyz": info.get("approx_xyz"), "antenna": (info.get("antenna") or "").strip(),
               "delta_hen": list(info.get("delta_hen") or (0, 0, 0)), "interval": info.get("interval"),
               "first": info["first"].isoformat() if info.get("first") else None,
               "last": info["last"].isoformat() if info.get("last") else None,
               "pos_source": "rinex", "xyz": list(info["approx_xyz"]) if info.get("approx_xyz") else None,
               "frame": self.project.cfg["frame"]["name"], "epoch": "",
               "coverage": info.get("segments") or [], "low_sats": info.get("low_sats") or []}
        for f in files[1:]:
            try:
                more = rinex.header_info(f, scan_body=True)
                ent["coverage"] += more.get("segments") or []
                ent["low_sats"] += more.get("low_sats") or []
                if more.get("last"):
                    ent["last"] = max(ent["last"] or "", more["last"].isoformat())
            except Exception:       # noqa: BLE001
                pass
        if info.get("hatanaka"):
            ent["hatanaka"] = True
        b = self.project.add_base(ent)
        self.dock.save()
        self.refresh()
        self.dock.refresh_map_bases()
        return b

    def add_rinex(self):
        fs, _ = QFileDialog.getOpenFileNames(self, "Base station RINEX observation files",
                                             QSettings().value("PosPro/last_dir", ""),
                                             "RINEX observations (*.obs *.rnx *.crx *.*o *.*d *.gz);;All files (*)")
        if not fs:
            return
        try:
            self.add_base_file(fs)
        except Exception as e:      # noqa: BLE001
            theme.msg_warn(self, "PosPro", "Cannot read %s:\n%s" % (fs[0], e))

    def _span_days(self):
        span = self.project.rover_span()
        if not span:
            theme.msg_info(self, "PosPro", "Read the rover files first (Data page).")
            return None
        return products.days_between(span[0], span[1])

    def _download(self, title, fn):
        days = self._span_days()
        if not days:
            return

        def work(ctx):
            out = []
            for d in days:
                out.append(fn(d, ctx))
            return out

        def done(ok, res):
            if not ok:
                theme.msg_warn(self, "PosPro", "Download failed:\n%s" % res)
                return
            name = os.path.basename(res[0])[:4].upper()
            self.add_base_file(res, name)
        self.dock.start_task(title, work, done)

    def open_finder(self):
        from .basefinder import BaseFinder
        p = self.project
        days = self._span_days()
        if not days:
            return
        xyz = p.approx_xyz()
        if not xyz:
            theme.msg_info(self, "PosPro", "Read the rover files first (Data page).")
            return
        llh = geo.ecef2llh(*xyz)
        rows = results.load_rt(os.path.join(p.work_dir, "extract", "rt.bin"), 2.0) or []
        track = [(r[1], r[2]) for r in rows]
        if track:
            llh = (sum(t[0] for t in track) / len(track), sum(t[1] for t in track) / len(track), llh[2])
        dlg = BaseFinder(self, p, days, llh, track)
        dlg.exec()
        if dlg.result:
            files, st = dlg.result
            self.add_base_file(files, st.sid[:4].upper() if st else None)
            b = self.selected() or (p.cfg["bases"][-1] if p.cfg["bases"] else None)
            if st is not None and b is not None and st.extra.get("xyz_published"):
                b["note"] = "published position available from %s" % st.network
            self.dock.save()
            where = dlg.saved_to or os.path.dirname(files[0])
            self.dock.toast("%s added as a base \u2013 %d file%s saved to %s" % (
                st.sid if st else "Station", len(files), "" if len(files) == 1 else "s", where))
            self.dock.log("info", "base files saved to %s" % where)

    def download_noaa_id(self):
        sid, ok = theme.get_text(self, "NOAA CORS", "Station ID (4 characters):")
        if ok and sid.strip():
            self._download("CORS %s" % sid, lambda d, ctx: products.noaa_rinex(sid.strip(), d, lambda m: ctx.log("info", m)))

    def download_noaa_nearest(self):
        xyz = self.project.approx_xyz()
        if not xyz:
            theme.msg_info(self, "PosPro", "Read the rover files first (Data page).")
            return

        def work(ctx):
            ids = products.noaa_nearest(xyz)
            out = []
            for sid in ids:
                try:
                    st = products.noaa_station(sid)
                    x = float(st.get("x") or st.get("X"))
                    y = float(st.get("y") or st.get("Y"))
                    z = float(st.get("z") or st.get("Z"))
                    out.append((sid, geo.distance_m(xyz, (x, y, z)) / 1000.0))
                except Exception:   # noqa: BLE001
                    out.append((sid, None))
            return out

        def done(ok, res):
            if not ok:
                theme.msg_warn(self, "PosPro", "Could not reach NOAA:\n%s" % res)
                return
            items = ["%s  (%s)" % (s, "%.0f km" % d if d is not None else "?") for s, d in res]
            it, ok2 = theme.get_item(self, "Nearest NOAA CORS", "Station:", items, 0, False)
            if ok2:
                sid = it.split()[0]
                self._download("CORS %s" % sid, lambda d, ctx: products.noaa_rinex(sid, d, lambda m: ctx.log("info", m)))
        self.dock.start_task("Finding CORS stations", work, done)

    def download_igs(self):
        sid, ok = theme.get_text(self, "IGS station", "9-character station ID (e.g. FRDN00CAN):")
        if ok and len(sid.strip()) == 9:
            self._download("IGS %s" % sid, lambda d, ctx: products.igs_rinex(sid.strip(), d, lambda m: ctx.log("info", m)))

    def remove(self):
        b = self.selected()
        if b:
            self.project.remove_base(b["id"])
            self.dock.save()
            self.refresh()
            self.dock.refresh_map_bases()

    def use_selected(self):
        b = self.selected()
        if b:
            self.project.cfg["base_selected"] = b["id"]
            self.dock.save()
            self.refresh()
            self.dock.refresh_map_bases()

    def show_selected(self):
        b = self.selected()
        self.edit.setEnabled(b is not None)
        if not b:
            return
        i = self.src.findData(b.get("pos_source", "rinex"))
        self.src.setCurrentIndex(max(0, i))
        xyz = b.get("xyz")
        if xyz:
            lat, lon, h = geo.ecef2llh(*xyz)
            self.lat.setText("%.9f" % lat)
            self.lon.setText("%.9f" % lon)
            self.hgt.setText("%.4f" % h)
        else:
            self.lat.clear(); self.lon.clear(); self.hgt.clear()
        self.frame.setCurrentIndex(max(0, self.frame.findData(b.get("frame") or "NAD83(CSRS)")))
        self.epoch.setText(str(b.get("epoch") or ""))
        self.ant.setText(b.get("antenna", ""))
        self.anth.setValue(float((b.get("delta_hen") or [0])[0]))
        msg = []
        if b.get("ppp"):
            q = b["ppp"]
            msg.append("Built-in PPP (%s, %.1f h, %s): \u03c3 %.3f / %.3f / %.3f m" % (
                q.get("when", ""), q.get("duration_h", 0), ", ".join(q.get("products", [])), *q.get("sd", [0, 0, 0])))
        if b.get("csrs"):
            c = b["csrs"]
            msg.append("CSRS-PPP %s epoch %s" % (c.get("frame"), c.get("epoch")))
        if b.get("hatanaka"):
            msg.append("Compact RINEX: decompressed automatically.")
        self.ppp_out.setText("\n".join(msg))

    def save_position(self):
        b = self.selected()
        if not b:
            return
        try:
            lat, lon, h = float(self.lat.text()), float(self.lon.text()), float(self.hgt.text())
            if not (-90 <= lat <= 90 and -180 <= lon <= 180 and -500 < h < 9000):
                raise ValueError
        except ValueError:
            theme.msg_warn(self, "PosPro", "Enter latitude, longitude (decimal degrees) and ellipsoidal height.")
            return
        b["xyz"] = list(geo.llh2ecef(lat, lon, h))
        b["pos_source"] = self.src.currentData()
        b["frame"] = self.frame.currentData()
        b["epoch"] = self.epoch.text().strip()
        b["antenna"] = self.ant.text().strip()
        dh = list(b.get("delta_hen") or [0, 0, 0])
        dh[0] = self.anth.value()
        b["delta_hen"] = dh
        self.project.cfg["frame"] = {"name": b["frame"], "epoch": b["epoch"]}
        self.dock.save()
        self.refresh()
        self.dock.refresh_map_bases()

    def run_ppp(self):
        b = self.selected() or self.project.base()
        if not b:
            return

        def work(ctx):
            return pipeline.ppp_base(self.project, b, ctx)

        def done(ok, res):
            if not ok:
                theme.msg_warn(self, "PosPro", "PPP failed:\n%s" % res)
                return
            b["ppp"] = res
            xyz = res["itrf_xyz"]
            if (b.get("frame") or self.project.cfg["frame"]["name"]) == "NAD83(CSRS)":
                xyz = geo.itrf_to_nad83csrs(xyz, res["epoch"])
                b["epoch"] = "%.3f" % res["epoch"]
                b["frame"] = "NAD83(CSRS)"
            else:
                b["frame"] = "ITRF2020"
                b["epoch"] = "%.3f" % res["epoch"]
            b["xyz"] = list(xyz)
            b["pos_source"] = "ppp"
            self.project.cfg["frame"] = {"name": b["frame"], "epoch": b["epoch"]}
            self.dock.save()
            self.refresh()
            self.dock.refresh_map_bases()
            self.dock.log("info", "Base %s from PPP: %s at epoch %s (velocity not applied)." % (
                b.get("name"), b["frame"], b["epoch"]))
        self.dock.start_task("PPP of base %s" % b.get("name", ""), work, done)

    def run_csrs(self):
        b = self.selected() or self.project.base()
        if not b:
            return
        email = self.email.text().strip()
        self.project.cfg["csrs"]["email"] = email
        epoch = self.csrs_epoch.currentData()
        self.project.cfg["csrs"]["epoch"] = epoch
        self.dock.save()

        def work(ctx):
            obs = pipeline.base_obs_file(self.project, b, ctx)
            return csrs.submit(obs, email, "NAD83", epoch, log=lambda m: ctx.log("info", m),
                               is_cancelled=ctx.cancelled)

        def done(ok, res):
            if not ok:
                theme.msg_warn(self, "PosPro", "CSRS-PPP failed:\n%s" % res)
                return
            summ, zpath = res
            systems = summ["systems"]
            key = next((k for k in systems if "NAD83" in k.upper()), next(iter(systems)))
            s = systems[key]
            b["xyz"] = s["xyz"]
            b["pos_source"] = "csrs"
            b["frame"] = "NAD83(CSRS)" if "NAD83" in key.upper() else "ITRF2020"
            dec = csrs.epoch_to_decimal(s.get("epoch"))
            b["epoch"] = ("%.1f" % dec) if dec else s.get("epoch", "")
            b["csrs"] = {"frame": key, "epoch": s.get("epoch"), "sigma95": s.get("sigma95"), "zip": zpath}
            self.project.cfg["frame"] = {"name": b["frame"], "epoch": b["epoch"]}
            self.dock.save()
            self.refresh()
            self.dock.refresh_map_bases()
        self.dock.start_task("CSRS-PPP for %s" % b.get("name", ""), work, done)

    # ------------------------------------------------------------------
    def refresh(self):
        p = self.project
        self.table.setRowCount(0)
        self.dock.show_coverage(self.cov, bases=True)
        if not p:
            return
        self.email.setText(p.cfg["csrs"].get("email", ""))
        i = self.csrs_epoch.findData(p.cfg["csrs"].get("epoch", "1997-01-01"))
        self.csrs_epoch.setCurrentIndex(max(0, i))
        rover = p.approx_xyz()
        span = p.rover_span()
        src_label = {"rinex": "RINEX header", "manual": "entered", "ppp": "built-in PPP", "csrs": "CSRS-PPP"}
        sel_row = 0
        for r, b in enumerate(p.cfg["bases"]):
            self.table.insertRow(r)
            name = b.get("name", "")
            if b.get("id") == p.cfg.get("base_selected"):
                name = "\u2605 " + name
                sel_row = r
            it = QTableWidgetItem(name)
            it.setData(Qt.ItemDataRole.UserRole, b.get("id"))
            self.table.setItem(r, 0, it)
            xyz = b.get("xyz") or b.get("approx_xyz")
            d = geo.distance_m(xyz, rover) / 1000.0 if (xyz and rover) else None
            di = QTableWidgetItem("%.1f km" % d if d is not None else "?")
            if d is not None:
                di.setForeground(QColor(theme.GREEN if d <= 20 else theme.AMBER if d <= 40 else theme.RED))
            self.table.setItem(r, 1, di)
            self.table.setItem(r, 2, QTableWidgetItem("%g s" % b["interval"] if b.get("interval") else "?"))
            cov = "?"
            if span and b.get("first") and b.get("last"):
                try:
                    # RINEX epochs are GPS time
                    f = (datetime.datetime.fromisoformat(b["first"]) - geo.GPS_EPOCH).total_seconds()
                    l = (datetime.datetime.fromisoformat(b["last"]) - geo.GPS_EPOCH).total_seconds()
                    c = max(0.0, min(span[1], l) - max(span[0], f)) / max(1.0, span[1] - span[0])
                    cov = "%.0f%%" % (100 * c)
                except ValueError:
                    pass
            self.table.setItem(r, 3, QTableWidgetItem(cov))
            pos = src_label.get(b.get("pos_source", ""), b.get("pos_source", ""))
            if b.get("frame"):
                pos += " \u2013 %s %s" % (b.get("frame"), b.get("epoch") or "")
            self.table.setItem(r, 4, QTableWidgetItem(pos))
        if p.cfg["bases"]:
            self.table.selectRow(sel_row)
        self.show_selected()

    def state(self):
        p = self.project
        if not p:
            return "todo", ""
        if p.cfg["processing"].get("positioning") == "ppp":
            return "done", "not needed (PPP)"
        b = p.base()
        if not b:
            return "todo", "add a base station"
        if b.get("pos_source", "rinex") == "rinex":
            return "warn", "%s: approximate position" % b.get("name", "")
        return "done", "%s (%s)" % (b.get("name", ""), b.get("pos_source"))


# ---------------------------------------------------------------------------
class InstallPage(Page):
    """Laid out as the mapper's Settings > Hardware: mounting drawing,
    positioning and motion (Certus, GPS1, GPS2), survey sensor, vehicle."""

    AXES = ("Forward (m)", "Starboard (m)", "Down (m)")

    def __init__(self, dock):
        super().__init__(dock)
        self._loading = True
        v = QVBoxLayout(self)
        v.addWidget(_title("Installation"))
        v.addWidget(_hint("Offsets in metres from the Certus reference point in the vehicle frame: Forward +, "
                          "Starboard +, Down + \u2013 the same frame and values as the mapper's Settings > Hardware. "
                          "They are filled from the capture when the rover files are read."))

        # ---- mounting drawing ----
        dw = QWidget()
        dl = QVBoxLayout(dw)
        dl.setContentsMargins(0, 0, 0, 0)
        f = QFormLayout()
        self.vtype = QComboBox()
        for k, o in VEHICLE_PRESETS.items():
            self.vtype.addItem(o["label"], k)
        self.vtype.currentIndexChanged.connect(self.store)
        f.addRow("Vehicle", self.vtype)
        dl.addLayout(f)
        self.view = VehicleView()
        dl.addWidget(self.view)
        v.addWidget(Section("Mounting drawing", dw, expanded=True))

        # ---- positioning and motion ----
        pw = QWidget()
        pl = QVBoxLayout(pw)
        pl.setContentsMargins(0, 0, 0, 0)
        f = QFormLayout()
        self.imu = QComboBox()
        for k, dd in IMU_PRESETS.items():
            self.imu.addItem(dd["label"], k)
        self.imu.currentIndexChanged.connect(self.store)
        f.addRow("Positioning motion sensor", self.imu)
        self.angles = QLabel("\u2013")
        self.angles.setObjectName("ppVal")
        self.angles.setWordWrap(True)
        f.addRow("Mounting angles", self.angles)
        pl.addLayout(f)
        pl.addWidget(_hint("Roll + starboard down, pitch + bow up, heading + clockwise. The Certus applies its "
                           "alignment itself, so its output is already in the vehicle frame (shown for checking)."))
        self.rows, self.cmp = {}, {}
        pl.addLayout(self._offset_grid((("lever_antenna", "Primary GPS (GPS1)"),
                                        ("lever_antenna2", "Secondary GPS (GPS2)"))))
        self.use2 = QCheckBox("Use the secondary antenna in tight coupling")
        self.use2.setToolTip("The Certus records raw observations from both antennas. With both, heading stays "
                             "observable when parked or driving straight, and GNSS gaps are bridged better.")
        self.use2.toggled.connect(self.store)
        pl.addWidget(self.use2)
        mr = QHBoxLayout()
        self.b_meas2 = QPushButton("Measure GPS2 from the data")
        self.b_meas2.setToolTip("Carrier-phase baseline from GPS1 to GPS2 in the recorded observations, turned into "
                                "the vehicle frame with the Certus attitude and added to GPS1. Works parked or "
                                "driving; needs a few minutes with both antennas in the open.")
        self.b_meas2.clicked.connect(self.measure_gps2)
        mr.addWidget(self.b_meas2)
        mr.addStretch(1)
        pl.addLayout(mr)
        self.meas2 = _hint("")
        self.meas2.setVisible(False)
        pl.addWidget(self.meas2)
        v.addWidget(Section("Positioning and motion", pw, expanded=True))

        # ---- survey sensor / output point ----
        sw = QWidget()
        sl = QVBoxLayout(sw)
        sl.setContentsMargins(0, 0, 0, 0)
        f = QFormLayout()
        self.out_name = QLineEdit()
        self.out_name.setPlaceholderText("e.g. Lidar")
        self.out_name.editingFinished.connect(self.store)
        f.addRow("Name", self.out_name)
        sl.addLayout(f)
        sl.addLayout(self._offset_grid((("lever_output", "Sensor offsets"),)))
        sl.addWidget(_hint("The trajectory is exported at this point. Leave it at 0 to export at the Certus."))
        v.addWidget(Section("Survey sensor / output point", sw, expanded=True))

        # ---- vehicle ----
        vw = QWidget()
        vl = QVBoxLayout(vw)
        vl.setContentsMargins(0, 0, 0, 0)
        f = QFormLayout()
        self.vehicle = QComboBox()
        for k, dd in VEHICLE_PROFILES.items():
            self.vehicle.addItem(dd["label"], k)
        self.vehicle.currentIndexChanged.connect(self.store)
        f.addRow("Motion constraints", self.vehicle)
        self.toff = _dspin(-500, 500, 3, 0.5, " ms")
        self.toff.valueChanged.connect(self.store)
        f.addRow("IMU time offset", self.toff)
        vl.addLayout(f)
        vl.addLayout(self._offset_grid((("lever_nhc", "Rear axle centre"),)))
        vl.addWidget(_hint("The wheeled-vehicle constraint says the rear axle does not slide sideways or lift. "
                           "If the Certus is not above the axle, enter where the axle centre is: in a turn at "
                           "0.3 rad/s each metre of offset is 0.3 m/s of sideways speed at the Certus, three times "
                           "what the constraint allows. Measure it once per vehicle (tape to 5 cm is enough)."))
        v.addWidget(Section("Vehicle", vw, expanded=False))

        br = QHBoxLayout()
        self.b_hw = QPushButton("Use offsets from the capture")
        self.b_hw.clicked.connect(lambda: self.use_hardware(ask=True))
        br.addWidget(self.b_hw)
        br.addStretch(1)
        v.addLayout(br)
        self.src = _hint("")
        v.addWidget(self.src)
        v.addStretch(1)
        self._loading = False

    def _offset_grid(self, rows):
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        for c, t in enumerate(self.AXES, start=1):
            l = QLabel(t.upper())
            l.setObjectName("ppKey")
            l.setAlignment(Qt.AlignmentFlag.AlignCenter)
            grid.addWidget(l, 0, c)
        r = 1
        for key, label in rows:
            kl = QLabel(label.upper())
            kl.setObjectName("ppKey")
            grid.addWidget(kl, r, 0)
            sp = []
            for c in range(3):
                s = _dspin(-50, 50, 3, 0.001, "")
                s.setMinimumWidth(70)
                s.valueChanged.connect(self.store)
                grid.addWidget(s, r, c + 1)
                sp.append(s)
            self.rows[key] = sp
            cl = QLabel("")
            cl.setObjectName("ppHint")
            cl.setWordWrap(True)
            cl.setVisible(False)
            grid.addWidget(cl, r + 1, 1, 1, 3)
            self.cmp[key] = cl
            r += 2
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)
        grid.setColumnStretch(3, 1)
        return grid

    def refresh(self):
        p = self.project
        if not p:
            return
        self._loading = True
        inst = p.cfg["installation"]
        for k, sp in self.rows.items():
            for c in range(3):
                sp[c].setValue(float((inst.get(k) or [0, 0, 0])[c]))
        self.out_name.setText(inst.get("output_name", ""))
        self.toff.setValue(float(inst.get("time_offset_ms", 0)))
        self.use2.setChecked(bool(inst.get("use_antenna2", True)))
        ex = p.cfg["rover"].get("extract") or {}
        self.use2.setEnabled(int(ex.get("obs1_epochs", 0) or 0) > 0 or not ex)
        veh = p.cfg["vehicle"]
        self.vehicle.setCurrentIndex(max(0, self.vehicle.findData(veh.get("profile", "car"))))
        self.vtype.setCurrentIndex(max(0, self.vtype.findData(veh.get("type", "van"))))
        self.imu.setCurrentIndex(max(0, self.imu.findData(p.cfg["imu"].get("preset", "certus_evo"))))
        cap = pipeline.capture_offsets(p)
        self.b_hw.setVisible(bool(cap["source"]))
        self.src.setText(("Offsets from: %s" % inst["source"]) if inst.get("source") else
                         "Offsets entered by hand.")
        self._loading = False
        self.draw()

    def store(self, *_):
        if self._loading or not self.project:
            return
        inst = self.project.cfg["installation"]
        for k, sp in self.rows.items():
            inst[k] = [s.value() for s in sp]
        inst["output_name"] = self.out_name.text().strip() or "Output point"
        inst["time_offset_ms"] = self.toff.value()
        inst["use_antenna2"] = self.use2.isChecked()
        veh = self.project.cfg["vehicle"]
        veh["profile"] = self.vehicle.currentData()
        if self.vtype.currentData() != veh.get("type"):
            veh["type"] = self.vtype.currentData()
            veh["outline"] = {}
        self.project.cfg["imu"]["preset"] = self.imu.currentData()
        self.dock.save()
        self.draw()
        self.dock.update_steps()

    def _compare(self, key, entered, ref, what):
        lab = self.cmp[key]
        if ref is None:
            lab.setVisible(False)
            return
        dmax = max(abs(a - b) for a, b in zip(entered, ref))
        ok = dmax < 0.01
        lab.setText("%s: %.3f, %.3f, %.3f m  %s" % (what, ref[0], ref[1], ref[2],
                    "\u2713 matches" if ok else "\u2260 differs by %.0f mm" % (dmax * 1000)))
        lab.setStyleSheet("color: %s;" % (theme.GREEN if ok else theme.AMBER))
        lab.setVisible(True)

    def draw(self):
        p = self.project
        inst = p.cfg["installation"]
        veh = p.cfg["vehicle"]
        outline = dict(VEHICLE_PRESETS.get(veh.get("type") or "van", VEHICLE_PRESETS["van"]))
        outline.update(veh.get("outline") or {})
        pts = {"gps1": tuple(inst["lever_antenna"]) + ("GPS1",)}
        if any(inst.get("lever_antenna2") or [0, 0, 0]):
            pts["gps2"] = tuple(inst["lever_antenna2"]) + ("GPS2",)
        if any(inst["lever_output"]):
            pts["output"] = tuple(inst["lever_output"]) + (inst.get("output_name") or "Output",)
        if any(inst["lever_nhc"]):
            pts["axle"] = tuple(inst["lever_nhc"]) + ("Axle",)
        self.view.set_points(pts, outline)
        cap = pipeline.capture_offsets(p)
        self._compare("lever_antenna", inst["lever_antenna"], cap["certus_antenna"], "Certus reports")
        hw = p.cfg["rover"].get("hardware") or {}
        if cap["certus_antenna2"] is not None:
            self._compare("lever_antenna2", inst["lever_antenna2"], cap["certus_antenna2"], "Certus reports")
        elif hw:
            self._compare("lever_antenna2", inst["lever_antenna2"], cap["antenna2"], "Mapper capture")
        elif cap["certus_dual"] == "automatic":
            ex = p.cfg["rover"].get("extract") or {}
            lab = self.cmp["lever_antenna2"]
            lab.setText("Certus dual antenna: automatic (%s) \u2013 the Certus works the offset out itself, so it "
                        "has no GPS2 offset to read. Use \u201cMeasure GPS2 from the data\u201d."
                        % ex.get("certus_dual_orientation", "?"))
            lab.setStyleSheet("color: %s;" % theme.DIM)
            lab.setVisible(True)
        else:
            self.cmp["lever_antenna2"].setVisible(False)
        self._compare("lever_output", inst["lever_output"], cap["lidar"] if hw else None, "Mapper Lidar offset")
        self._compare("lever_nhc", inst["lever_nhc"], cap["axle"], "Mapper rear axle")
        al = cap["alignment"]
        self.angles.setText("Roll %.2f\u00b0 \u00b7 Pitch %.2f\u00b0 \u00b7 Heading %.2f\u00b0" % tuple(al)
                            if al else "not in the capture")

    def measure_gps2(self):
        p = self.project
        if not p:
            return
        if not any(abs(x) > 1e-6 for x in p.cfg["installation"]["lever_antenna"]):
            theme.msg_info(self, "PosPro", "Enter (or read from the capture) the GPS1 offset first; GPS2 is "
                                           "measured from it.")
            return
        work_p = self.dock._work_copy()

        def work(ctx):
            return pipeline.measure_antenna2(work_p, ctx)

        def done(ok, res):
            if not ok:
                theme.msg_warn(self, "PosPro", "GPS2 could not be measured:\n\n%s" % str(res).splitlines()[0])
                return
            v, sd = res["value"], res["sd"]
            cur = p.cfg["installation"]["lever_antenna2"]
            diff = max(abs(a - b) for a, b in zip(v, cur)) if any(cur) else None
            self.meas2.setText("Measured GPS2: %.3f, %.3f, %.3f m (\u00b1 %.3f, %.3f, %.3f), antennas %.3f m apart, "
                               "%d fixed epochs (%.0f%%)%s" % (tuple(v) + tuple(sd) + (
                                   res["length"], res["epochs"], res["fixed_pct"],
                                   "" if diff is None else "; differs from the entered value by %.0f mm" % (diff * 1000))))
            self.meas2.setVisible(True)
            r = theme.msg_question(self, "PosPro", "GPS2 measured from the data:\n\n"
                                   "Forward %.3f m, Starboard %.3f m, Down %.3f m\n"
                                   "(\u00b1 %.3f, %.3f, %.3f m; antennas %.3f m apart)\n\nUse these values?"
                                   % (tuple(v) + tuple(sd) + (res["length"],)))
            if r == QMessageBox.StandardButton.Yes:
                p.cfg["installation"]["lever_antenna2"] = v
                src = p.cfg["installation"].get("source") or ""
                if "GPS2 measured" not in src:
                    p.cfg["installation"]["source"] = (src + " + " if src else "") + "GPS2 measured from the data"
                self.dock.save()
                self.refresh()
        self.dock.start_task("Measuring GPS2", work, done)

    def use_hardware(self, ask=False, quiet=False):
        """Fill the installation from the capture. Returns the list of what
        was set."""
        p = self.project
        cap = pipeline.capture_offsets(p)
        inst = p.cfg["installation"]
        msg = []
        if cap["antenna"] is not None:
            inst["lever_antenna"] = cap["antenna"]
            msg.append("GPS1 %.3f, %.3f, %.3f" % tuple(cap["antenna"]))
        if cap["antenna2"] is not None:
            inst["lever_antenna2"] = cap["antenna2"]
            msg.append("GPS2 %.3f, %.3f, %.3f" % tuple(cap["antenna2"]))
        if cap["lidar"] is not None and any(cap["lidar"]):
            inst["lever_output"] = cap["lidar"]
            inst["output_name"] = "Lidar"
            msg.append("Lidar %.3f, %.3f, %.3f" % tuple(cap["lidar"]))
        if cap["axle"] is not None:
            inst["lever_nhc"] = cap["axle"]
            msg.append("rear axle %.3f, %.3f, %.3f" % tuple(cap["axle"]))
        veh = cap["vehicle"] or {}
        if veh.get("type") in VEHICLE_PRESETS:
            p.cfg["vehicle"]["type"] = veh["type"]
            p.cfg["vehicle"]["outline"] = {k: v for k, v in veh.items() if k.endswith("_m")}
            p.cfg["vehicle"]["profile"] = "truck" if veh["type"] == "truck" else "car"
        if not msg:
            if ask:
                theme.msg_info(self, "PosPro", "The capture holds no offsets (no mapper hardware.json and no "
                                               "Certus installation packet). Enter them by hand.")
            return []
        inst["source"] = cap["source"]
        self.dock.save()
        self.refresh()
        if not quiet:
            theme.msg_info(self, "PosPro", "Offsets set from %s:\n\n%s" % (cap["source"], "\n".join(msg)))
        return msg

    def state(self):
        p = self.project
        if not p:
            return "todo", ""
        la = p.cfg["installation"]["lever_antenna"]
        if not any(abs(v) > 1e-6 for v in la):
            return "warn", "antenna offset is zero"
        return "done", "antenna %.2f, %.2f, %.2f m" % tuple(la)


# ---------------------------------------------------------------------------
class ProcessPage(Page):
    def __init__(self, dock):
        super().__init__(dock)
        v = QVBoxLayout(self)
        v.addWidget(_title("Processing"))
        mg = QGroupBox("Method")
        ml = QVBoxLayout(mg)
        self.mode = QButtonGroup(self)
        for k, label, tip in (("tc", "Tightly coupled (recommended)",
                               "Raw carrier phase and code from rover and base go straight into the inertial filter. "
                               "Keeps fixing with fewer than 4 satellites and repairs cycle slips with the IMU."),
                              ("lc", "Loosely coupled",
                               "GNSS positions (RTKLIB) are computed first and then blended with the IMU. "
                               "Needed for PPP without a base."),
                              ("gnss", "GNSS only (no IMU)", "RTKLIB kinematic solution for comparison."),
                              ("rt", "Real-time solution (no base station)",
                               "The Certus's own real-time trajectory, cleaned in Trajectory Review. For when there is "
                               "no base station or RTK was used in the field.")):
            rb = QRadioButton(label)
            rb.setProperty("key", k)
            rb.setToolTip(tip)
            self.mode.addButton(rb)
            ml.addWidget(rb)
        self.pos = QComboBox()
        self.pos.addItem("Differential (base station, PPK)", "ppk")
        self.pos.addItem("Precise point positioning (no base)", "ppp")
        self.passes = QComboBox()
        self.passes.addItem("Forward and backward, combined", "both")
        self.passes.addItem("Forward only (quick check)", "forward")
        f = QFormLayout()
        f.addRow("Positioning", self.pos)
        f.addRow("Passes", self.passes)
        ml.addLayout(f)
        v.addWidget(mg)

        gg = QGroupBox("GNSS")
        gl = QGridLayout(gg)
        self.sys = {}
        for c, (k, label) in enumerate((("G", "GPS"), ("R", "GLONASS"), ("E", "Galileo"), ("C", "BeiDou"), ("J", "QZSS"))):
            cb = QCheckBox(label)
            self.sys[k] = cb
            gl.addWidget(cb, c // 3, c % 3)
        self.elmask = _dspin(0, 45, 0, 1, "\u00b0")
        self.ratio = _dspin(1.5, 10, 1, 0.1)
        self.ar = QCheckBox("Fix integer ambiguities")
        self.nf = QComboBox()
        self.nf.addItem("Dual frequency", 2)
        self.nf.addItem("L1 only", 1)
        self.atm = QComboBox()
        self.atm.addItem("Automatic (long baselines)", "auto")
        self.atm.addItem("Always estimate", "on")
        self.atm.addItem("Off (short baselines)", "off")
        self.atm.setToolTip("Estimates the difference in ionosphere (per satellite) and troposphere between rover "
                            "and base. Automatic turns it on beyond 10 km or 150 m height difference, where it "
                            "keeps ambiguities fixable.")
        self.snr = QCheckBox("Weight by signal strength")
        self.snr.setToolTip("Weak signals (trees, buildings, multipath) count less than strong ones.")
        f4 = QFormLayout()
        f4.addRow("Elevation mask", self.elmask)
        f4.addRow("Ratio test", self.ratio)
        f4.addRow(self.ar)
        f4.addRow("Frequencies", self.nf)
        f4.addRow("Atmosphere", self.atm)
        f4.addRow(self.snr)
        gl.addLayout(f4, 2, 0, 1, 3)
        v.addWidget(gg)

        ig = QGroupBox("Inertial")
        il = QGridLayout(ig)
        self.nhc = QCheckBox("Wheeled vehicle constraint")
        self.nhc.setToolTip("The vehicle does not slide sideways or jump: keeps the solution tight in GNSS gaps.")
        self.zupt = QCheckBox("Zero velocity when parked")
        self.rate = QComboBox()
        for label, hz in (("IMU rate (200 Hz)", 0), ("100 Hz", 100), ("50 Hz", 50), ("20 Hz", 20), ("10 Hz", 10)):
            self.rate.addItem(label, hz)
        il.addWidget(self.nhc, 0, 0, 1, 2)
        il.addWidget(self.zupt, 1, 0, 1, 2)
        il.addWidget(QLabel("Trajectory rate"), 2, 0)
        il.addWidget(self.rate, 2, 1)
        v.addWidget(ig)

        adv = QWidget()
        al = QFormLayout(adv)
        self.align = QComboBox()
        self.align.addItem("Automatic (real-time attitude, else from motion)", 0)
        self.align.addItem("Real-time attitude from packet 20", 1)
        self.align.addItem("From motion (GNSS course, drive straight)", 2)
        self.align_speed = _dspin(1, 30, 1, 0.5, " m/s")
        self.gate = _dspin(2, 100, 1, 1)
        self.basedt = _dspin(1, 300, 0, 5, " s")
        self.extra = QPlainTextEdit()
        self.extra.setPlaceholderText("expert settings, one per line: key = value\n(engine keys or RTKLIB pos1-/pos2-/stats- keys)")
        self.extra.setMaximumHeight(70)
        al.addRow("Alignment", self.align)
        al.addRow("Alignment speed", self.align_speed)
        al.addRow("Outlier gate (\u03c7\u00b2)", self.gate)
        al.addRow("Max base gap", self.basedt)
        al.addRow("Expert", self.extra)
        v.addWidget(Section("Advanced", adv))

        self.b_run = QPushButton("Process")
        self.b_run.setObjectName("ppPrimary")
        self.b_run.setMinimumHeight(36)
        self.b_run.clicked.connect(self.dock.run_processing)
        v.addWidget(self.b_run)
        self.steps = QLabel("")
        self.steps.setWordWrap(True)
        v.addWidget(self.steps)
        v.addStretch(1)
        for w in [self.pos, self.passes, self.nf, self.rate, self.align, self.atm]:
            w.currentIndexChanged.connect(self.store)
        for w in [self.elmask, self.ratio, self.align_speed, self.gate, self.basedt]:
            w.valueChanged.connect(self.store)
        for w in list(self.sys.values()) + [self.ar, self.nhc, self.zupt, self.snr]:
            w.toggled.connect(self.store)
        self.mode.buttonClicked.connect(self.store)
        self.extra.textChanged.connect(self.store)
        self._loading = False

    def refresh(self):
        p = self.project
        if not p:
            return
        self._loading = True
        c = p.cfg["processing"]
        for b in self.mode.buttons():
            b.setChecked(b.property("key") == c.get("mode", "tc"))
        self.pos.setCurrentIndex(max(0, self.pos.findData(c.get("positioning", "ppk"))))
        self.passes.setCurrentIndex(max(0, self.passes.findData(c.get("passes", "both"))))
        for k, cb in self.sys.items():
            cb.setChecked(k in c.get("systems", "GREJ"))
        self.elmask.setValue(float(c.get("elmask", 12)))
        self.ratio.setValue(float(c.get("ar_ratio", 3)))
        self.ar.setChecked(bool(c.get("ar", True)))
        self.nf.setCurrentIndex(max(0, self.nf.findData(int(c.get("frequencies", 2)))))
        self.atm.setCurrentIndex(max(0, self.atm.findData(c.get("atmosphere", "auto"))))
        self.snr.setChecked(bool(c.get("snr_weighting", True)))
        self.nhc.setChecked(bool(c.get("nhc", True)))
        self.zupt.setChecked(bool(c.get("zupt", True)))
        self.rate.setCurrentIndex(max(0, self.rate.findData(int(c.get("output_rate_hz", 0) or 0))))
        self.align.setCurrentIndex(max(0, self.align.findData(int(c.get("align_mode", 0)))))
        self.align_speed.setValue(float(c.get("align_speed", 4)))
        self.gate.setValue(float(c.get("gate_chi2", 9)))
        self.basedt.setValue(float(c.get("base_max_dt", 60)))
        self.extra.setPlainText("\n".join("%s = %s" % kv for kv in (c.get("advanced") or {}).items()))
        self._loading = False
        self.dock.update_steps()

    def store(self, *_):
        if self._loading or not self.project:
            return
        c = self.project.cfg["processing"]
        for b in self.mode.buttons():
            if b.isChecked():
                c["mode"] = b.property("key")
        c["positioning"] = self.pos.currentData()
        c["passes"] = self.passes.currentData()
        c["systems"] = "".join(k for k, cb in self.sys.items() if cb.isChecked()) or "G"
        c["elmask"] = self.elmask.value()
        c["ar_ratio"] = self.ratio.value()
        c["ar"] = self.ar.isChecked()
        c["frequencies"] = self.nf.currentData()
        c["atmosphere"] = self.atm.currentData()
        c["snr_weighting"] = self.snr.isChecked()
        c["nhc"] = self.nhc.isChecked()
        c["zupt"] = self.zupt.isChecked()
        c["output_rate_hz"] = self.rate.currentData()
        c["align_mode"] = self.align.currentData()
        c["align_speed"] = self.align_speed.value()
        c["gate_chi2"] = self.gate.value()
        c["base_max_dt"] = self.basedt.value()
        adv = {}
        for line in self.extra.toPlainText().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, val = line.split("=", 1)
                if k.strip():
                    adv[k.strip()] = val.strip()
        c["advanced"] = adv
        self.dock.save()
        self.dock.update_steps()

    def state(self):
        p = self.project
        if not p:
            return "todo", ""
        r = p.cfg.get("results") or {}
        if r.get("combine") or (r.get("mode") == "gnss" and r.get("gnss")):
            return "done", "processed %s" % r.get("finished", "")[:16]
        return "todo", {"tc": "tightly coupled", "lc": "loosely coupled", "gnss": "GNSS only"}.get(
            p.cfg["processing"].get("mode"), "")


# ---------------------------------------------------------------------------
class QualityPage(Page):
    def __init__(self, dock):
        super().__init__(dock)
        v = QVBoxLayout(self)
        v.addWidget(_title("Quality"))
        top = QHBoxLayout()
        self.donut = Donut()
        self.donut.setFixedSize(150, 150)
        top.addWidget(self.donut)
        self.legend = QLabel("")
        self.legend.setTextFormat(Qt.TextFormat.RichText)
        top.addWidget(self.legend, 1)
        v.addLayout(top)
        grid = QGridLayout()
        self.cards = {}
        for i, (k, label) in enumerate((("fixed", "fixed ambiguities"), ("ins", "bridged by the IMU only"),
                                         ("sep", "fwd/bwd separation (95%)"), ("sd", "largest 3D \u03c3"),
                                         ("gap", "longest GNSS gap"), ("time", "processing time"))):
            c = Card(label)
            self.cards[k] = c
            grid.addWidget(c, i // 2, i % 2)
        v.addLayout(grid)
        cg = QHBoxLayout()
        _cl = QLabel("COLOUR THE MAP BY")
        _cl.setObjectName("ppKey")
        cg.addWidget(_cl)
        self.colour = QComboBox()
        self.colour.addItem("Solution type", "status")
        self.colour.addItem("Estimated accuracy (3D \u03c3)", "accuracy")
        self.colour.addItem("Forward/backward separation", "separation")
        self.colour.currentIndexChanged.connect(lambda *_: self.dock.recolour(self.colour.currentData()))
        cg.addWidget(self.colour, 1)
        v.addLayout(cg)
        v.addWidget(_hint("Findings – click one to see it on the map and in the plots."))
        self.find = QListWidget()
        self.find.setWordWrap(True)
        self.find.itemClicked.connect(self.goto)
        v.addWidget(self.find, 1)
        b = QPushButton("Open the quality plots")
        b.clicked.connect(self.dock.show_plots)
        v.addWidget(b)

    def refresh(self):
        p = self.project
        qc = self.dock.qc
        self.find.clear()
        if not p or not qc:
            self.donut.set_parts([])
            self.legend.setText("No results yet.")
            for c in self.cards.values():
                c.set("\u2013")
            return
        parts = results.status_shares(qc)
        self.donut.set_parts(parts)
        self.legend.setText("<br>".join('<span style="color:%s">\u25cf</span> %s %.1f%%' % (c, l, v) for l, v, c in parts))
        r = p.cfg.get("results") or {}
        cmb = r.get("combine") or {}
        fixed = next((v for l, v, c in parts if l == "Fixed"), 0.0)
        ins = next((v for l, v, c in parts if l == "INS only"), 0.0)
        self.cards["fixed"].set("%.1f%%" % fixed, "ok" if fixed >= 95 else "warn" if fixed >= 75 else "bad")
        self.cards["ins"].set("%.1f%%" % ins)
        try:
            s95 = float(cmb.get("sep_h_p95_m", "nan"))
            self.cards["sep"].set("%.3f m" % s95, "ok" if s95 < 0.05 else "warn" if s95 < 0.2 else "bad")
        except ValueError:
            self.cards["sep"].set("\u2013")
        sd = max(qc["sd_3d"]) if qc["sd_3d"] else 0
        self.cards["sd"].set("%.3f m" % sd, "ok" if sd < 0.1 else "warn" if sd < 0.5 else "bad")
        gaps = results._runs(qc["gps_time"], [s == 1 for s in qc["status"]])
        g = max((b - a for a, b in gaps), default=0)
        self.cards["gap"].set("%.0f s" % g)
        self.cards["time"].set("%s s" % r.get("elapsed_s", "\u2013"))
        icons = {"ok": "\u2714", "warn": "\u26a0", "bad": "\u2716", "info": "\u2139"}
        cols = {"ok": theme.GREEN, "warn": theme.AMBER, "bad": theme.RED, "info": None}
        for lvl, text, t0, t1 in results.findings(qc, r.get("ins")):
            if t0 is not None:
                text += "  (%s UTC)" % geo.gps_to_utc(t0).strftime("%H:%M:%S")
            it = QListWidgetItem("%s  %s" % (icons.get(lvl, ""), text))
            if cols.get(lvl):
                it.setForeground(QColor(cols[lvl]))
            it.setData(Qt.ItemDataRole.UserRole, (t0, t1))
            self.find.addItem(it)

    def goto(self, item):
        t = item.data(Qt.ItemDataRole.UserRole)
        if t and t[0] is not None:
            self.dock.goto_time(t[0], t[1])

    def state(self):
        qc = self.dock.qc
        if not qc:
            return "todo", "after processing"
        parts = dict((l, v) for l, v, c in results.status_shares(qc))
        f = parts.get("Fixed", 0)
        return ("done" if f >= 95 else "warn"), "%.1f%% fixed" % f


# ---------------------------------------------------------------------------
class ReviewPage(Page):
    """Trajectory Review: find the parts of the trajectory that miss the
    quality limits and decide what to do with each (see core/review.py)."""

    COLS = ["#", "Found by", "Start (UTC)", "Duration", "Worst", "Decision"]

    def __init__(self, dock):
        super().__init__(dock)
        self.tool = None
        self.items = []
        v = QVBoxLayout(self)
        v.addWidget(_title("Trajectory Review"))
        v.addWidget(_hint("Find where the trajectory misses your limits, look at each place on the map and in the "
                          "plots, then decide: exclude the GNSS there and let the IMU carry it (processed again), "
                          "mark it poor so later stages know, accept it, or \u2013 for real-time and GNSS-only "
                          "trajectories \u2013 interpolate across it."))
        # ---- limits ----
        lw = QWidget()
        g = QGridLayout(lw)
        g.setContentsMargins(0, 0, 0, 0)
        self.use, self.val = {}, {}
        rows = (("solution", "Solution worse than", None),
                ("sd_h", "Horizontal \u03c3 above", (" m", 3, 0.001, 10.0, 0.01)),
                ("sd_v", "Vertical \u03c3 above", (" m", 3, 0.001, 10.0, 0.01)),
                ("sep_h", "Fwd/bwd separation above", (" m", 3, 0.001, 10.0, 0.01)),
                ("imu_only", "IMU only longer than", (" s", 1, 0.5, 600.0, 1.0)),
                ("heading", "Heading \u03c3 above", ("\u00b0", 2, 0.01, 10.0, 0.05)))
        keymap = {"solution": ("use_solution", "min_solution"), "sd_h": ("use_sd_h", "sd_h_m"),
                  "sd_v": ("use_sd_v", "sd_v_m"), "sep_h": ("use_sep_h", "sep_h_m"),
                  "imu_only": ("use_imu_only", "imu_only_s"), "heading": ("use_heading", "heading_sd_deg")}
        self.keymap = keymap
        for r, (k, label, spec) in enumerate(rows):
            cb = QCheckBox(label)
            cb.toggled.connect(self.store)
            g.addWidget(cb, r, 0)
            if spec is None:
                w = QComboBox()
                for key, lab in (("fixed", "fixed"), ("float", "float"), ("dgnss", "DGNSS"), ("any", "single")):
                    w.addItem(lab, key)
                w.currentIndexChanged.connect(self.store)
            else:
                suf, dec, lo, hi, step = spec
                w = _dspin(lo, hi, dec, step, suf)
                w.valueChanged.connect(self.store)
            g.addWidget(w, r, 1)
            self.use[k], self.val[k] = cb, w
        r = len(rows)
        g.addWidget(QLabel("Widen each by (before / after)"), r, 0)
        ext = QHBoxLayout()
        self.eb = _dspin(0, 60, 1, 0.5, " s")
        self.ea = _dspin(0, 60, 1, 0.5, " s")
        for s in (self.eb, self.ea):
            s.valueChanged.connect(self.store)
            ext.addWidget(s)
        g.addLayout(ext, r, 1)
        g.addWidget(QLabel("\u03c3 given to poor parts"), r + 1, 0)
        self.poor_sd = _dspin(0.01, 10, 3, 0.05, " m")
        self.poor_sd.valueChanged.connect(self.store)
        g.addWidget(self.poor_sd, r + 1, 1)
        g.setColumnStretch(1, 1)
        v.addWidget(Section("Limits", lw, expanded=True))
        br = QHBoxLayout()
        self.b_detect = QPushButton("Find anomalies")
        self.b_detect.setObjectName("ppPrimary")
        self.b_detect.clicked.connect(self.detect)
        br.addWidget(self.b_detect)
        br.addStretch(1)
        for mode, label in (("rectangle", "Rectangle"), ("polygon", "Polygon"), ("lasso", "Lasso")):
            b = QPushButton(label)
            b.setToolTip("Select part of the trajectory on the map with a %s; it is added to the list." % label.lower())
            b.clicked.connect(lambda _=False, m=mode: self.select_on_map(m))
            br.addWidget(b)
        b = QPushButton("Plot view")
        b.setToolTip("Add the time range shown in the quality plots")
        b.clicked.connect(self.select_plot_range)
        br.addWidget(b)
        v.addLayout(br)
        # ---- list ----
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMinimumHeight(220)
        self.table.itemSelectionChanged.connect(self.selection_changed)
        self.table.cellDoubleClicked.connect(lambda *_: self.zoom())
        v.addWidget(self.table, 1)
        ar = QGridLayout()
        self.b_zoom = QPushButton("Zoom to")
        self.b_zoom.clicked.connect(self.zoom)
        self.b_excl = QPushButton("Exclude GNSS")
        self.b_excl.setToolTip("Do not use GNSS in these windows; the IMU carries the trajectory. Needs processing "
                               "again.")
        self.b_excl.clicked.connect(lambda: self.decide("exclude"))
        self.b_poor = QPushButton("Mark poor")
        self.b_poor.setToolTip("Keep it, but raise its \u03c3 to the value above (SMRMSG, quality, handover) so "
                               "later stages know not to trust it.")
        self.b_poor.clicked.connect(lambda: self.decide("poor"))
        self.b_interp = QPushButton("Interpolate")
        self.b_interp.setToolTip("Straight line between the good samples on both sides. Only for real-time and "
                                 "GNSS-only trajectories: with the IMU, excluding the GNSS is better.")
        self.b_interp.clicked.connect(lambda: self.decide("interpolate"))
        self.b_accept = QPushButton("Accept")
        self.b_accept.clicked.connect(lambda: self.decide("accept"))
        self.b_clear = QPushButton("Clear decision")
        self.b_clear.clicked.connect(self.clear_decision)
        for i, b in enumerate((self.b_zoom, self.b_excl, self.b_poor, self.b_interp, self.b_accept, self.b_clear)):
            ar.addWidget(b, i // 3, i % 3)
        v.addLayout(ar)
        hr = QHBoxLayout()
        self.interp_h = QCheckBox("Horizontal")
        self.interp_v = QCheckBox("Vertical")
        self.interp_h.setChecked(True)
        self.interp_v.setChecked(True)
        self.interp_lab = QLabel("Interpolate:")
        hr.addWidget(self.interp_lab)
        hr.addWidget(self.interp_h)
        hr.addWidget(self.interp_v)
        hr.addStretch(1)
        v.addLayout(hr)
        self.pending = QLabel("")
        self.pending.setWordWrap(True)
        self.pending.setObjectName("ppHint")
        v.addWidget(self.pending)
        er = QHBoxLayout()
        self.b_reproc = QPushButton("Process again")
        self.b_reproc.setObjectName("ppPrimary")
        self.b_reproc.clicked.connect(self.dock.run_processing)
        er.addWidget(self.b_reproc)
        er.addStretch(1)
        for label, fn in (("Undo", self.undo), ("Reset to original", self.reset), ("Edit log", self.show_log)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            er.addWidget(b)
        v.addLayout(er)
        self._loading = False

    # ------------------------------------------------------------- state
    def refresh(self):
        p = self.project
        if not p:
            return
        from ..core import review
        r = review.state(p)
        th = r["thresholds"]
        self._loading = True
        for k, (uk, vk) in self.keymap.items():
            self.use[k].setChecked(bool(th[uk]))
            w = self.val[k]
            if isinstance(w, QComboBox):
                w.setCurrentIndex(max(0, w.findData(th[vk])))
            else:
                w.setValue(float(th[vk]))
            w.setEnabled(bool(th[uk]))
        self.eb.setValue(float(th["extend_before_s"]))
        self.ea.setValue(float(th["extend_after_s"]))
        self.poor_sd.setValue(float(th["poor_sd_m"]))
        self._loading = False
        mode = p.cfg["processing"].get("mode")
        can_interp = mode in ("gnss", "rt")
        for w in (self.b_interp, self.interp_h, self.interp_v, self.interp_lab):
            w.setVisible(can_interp)
        self.b_excl.setVisible(mode in ("tc", "lc"))
        self.b_excl.setEnabled(mode in ("tc", "lc"))
        self.fill()

    def store(self, *_):
        if self._loading or not self.project:
            return
        from ..core import review
        th = review.state(self.project)["thresholds"]
        for k, (uk, vk) in self.keymap.items():
            th[uk] = self.use[k].isChecked()
            w = self.val[k]
            th[vk] = w.currentData() if isinstance(w, QComboBox) else w.value()
            w.setEnabled(th[uk])
        th["extend_before_s"], th["extend_after_s"] = self.eb.value(), self.ea.value()
        th["poor_sd_m"] = self.poor_sd.value()
        self.dock.save()

    def fill(self):
        from ..core import review
        p = self.project
        r = review.state(p)
        items = []
        for a in r["anomalies"]:
            e = review.edit_for(p, a["t0"], a["t1"])
            items.append({"id": a["id"], "t0": a["t0"], "t1": a["t1"], "what": review.KIND_LABEL.get(a["kind"], a["kind"]),
                          "worst": review.describe_max(a), "edit": e})
        # edits that no anomaly covers (map selections, or found before different limits)
        for e in r["edits"]:
            if not any(it["edit"] is e for it in items):
                items.append({"id": e["id"], "t0": e["t0"], "t1": e["t1"],
                              "what": review.KIND_LABEL.get(e.get("source"), e.get("source", "")), "worst": "",
                              "edit": e})
        items.sort(key=lambda x: x["t0"])
        self.items = items
        self.table.setRowCount(len(items))
        cols = {"exclude": theme.BLUE, "poor": theme.AMBER, "interpolate": theme.VIOLET, "accept": theme.GREEN}
        for i, it in enumerate(items):
            e = it["edit"]
            vals = [it["id"], it["what"], geo.gps_to_utc(it["t0"]).strftime("%H:%M:%S"), "%.1f s" % (it["t1"] - it["t0"]),
                    it["worst"], review.ACTION_LABEL[e["action"]] if e else "open"]
            for c, val in enumerate(vals):
                ti = QTableWidgetItem(str(val))
                if c == 5:
                    ti.setForeground(QColor(cols.get(e["action"], theme.TEXT) if e else theme.RED))
                self.table.setItem(i, c, ti)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        n_open = sum(1 for it in items if not it["edit"])
        msg = []
        if review.exclusions_pending(p):
            msg.append("GNSS exclusions changed since the last processing \u2013 press Process again to apply them.")
            self.pending.setStyleSheet("color: %s;" % theme.AMBER)
        else:
            self.pending.setStyleSheet("")
        if items:
            msg.append("%d listed, %d still open." % (len(items), n_open))
        self.pending.setText(" ".join(msg))
        self.b_reproc.setVisible(review.exclusions_pending(p))
        self._draw_map()
        self.selection_changed()

    def _draw_map(self):
        if not self.dock.qc:
            return
        rows = [{"id": it["id"], "t0": it["t0"], "t1": it["t1"], "what": it["what"],
                 "state": it["edit"]["action"] if it["edit"] else "open"} for it in self.items]
        try:
            maplayers.review_layer(self.project, self.dock.qc, rows)
        except Exception as e:      # noqa: BLE001
            self.dock.log("warning", "review layer: %s" % e)

    def selected_items(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.items[r] for r in rows if r < len(self.items)]

    def selection_changed(self):
        n = len(self.selected_items())
        for b in (self.b_zoom, self.b_accept, self.b_poor, self.b_clear):
            b.setEnabled(n > 0)
        mode = self.project.cfg["processing"].get("mode") if self.project else ""
        self.b_excl.setEnabled(n > 0 and mode in ("tc", "lc"))
        self.b_interp.setEnabled(n > 0 and mode in ("gnss", "rt"))

    # ------------------------------------------------------------- actions
    def detect(self):
        from ..core import review
        if not self.dock.qc:
            theme.msg_info(self, "PosPro", "Process the project first.")
            return
        a = review.detect(self.project)
        self.dock.save()
        self.fill()
        self.dock.toast("%d anomal%s found" % (len(a), "y" if len(a) == 1 else "ies"),
                        "ok" if not a else "warn")

    def zoom(self):
        its = self.selected_items()
        if its:
            self.dock.goto_time(min(i["t0"] for i in its), max(i["t1"] for i in its))

    def decide(self, action):
        from ..core import review
        its = self.selected_items()
        if not its:
            return
        for it in its:
            if it["edit"] is not None:
                review.state(self.project)["edits"] = [e for e in review.state(self.project)["edits"]
                                                       if e is not it["edit"]]
            review.add_edit(self.project, it["t0"], it["t1"], action,
                            source=next((k for k, lab in review.KIND_LABEL.items() if lab == it["what"]), "manual"),
                            horizontal=self.interp_h.isChecked(), vertical=self.interp_v.isChecked())
        self.dock.save()
        self.after_edit(action in ("poor", "interpolate"))

    def clear_decision(self):
        from ..core import review
        for it in self.selected_items():
            if it["edit"] is not None:
                review.remove_edit(self.project, it["edit"]["id"])
        self.dock.save()
        self.after_edit(True)

    def undo(self):
        from ..core import review
        if review.undo(self.project):
            self.dock.save()
            self.after_edit(True)

    def reset(self):
        from ..core import review
        if theme.msg_question(self, "PosPro", "Remove every review decision and go back to the trajectory as "
                                              "processed?") != QMessageBox.StandardButton.Yes:
            return
        review.reset(self.project)
        self.dock.save()
        self.after_edit(True)

    def after_edit(self, reapply):
        """Poor / interpolate change the results at once (from the untouched
        copy); exclusions wait for Process again."""
        from ..core import review
        if not reapply:
            self.fill()
            self.dock.update_steps()
            return
        p = self.project
        work_p = self.dock._work_copy()

        def work(ctx):
            n = review.apply(work_p)
            if work_p.dcs is not None and os.path.exists(os.path.join(work_p.work_dir, "results", "trajectory.bin")):
                with work_p.dcs.run("pospro", "PosPro", pipeline.plugin_version()):
                    pipeline.publish_handover(work_p, ctx)
            return n

        def done(ok, res):
            if p.dcs is not None:
                p.dcs.reload()
                self.dock.chain.refresh()
            if not ok:
                theme.msg_warn(self, "PosPro", "Could not apply the review edits:\n%s" % res)
                return
            self.dock.qc = results.load_qc(os.path.join(p.work_dir, "results", "qc.csv"))
            self.dock.show_trajectory()
            if self.dock.plugin.plots:
                self.dock.plugin.plots.set_data(self.dock.qc)
            self.dock.pages[4].refresh()
            self.fill()
            self.dock.update_steps()
        if self.dock.start_task("Applying review edits", work, done) is None:
            self.fill()

    def select_on_map(self, mode):
        from .selecttool import SelectTool
        if not self.dock.qc:
            theme.msg_info(self, "PosPro", "Process the project first.")
            return
        canvas = self.dock.iface.mapCanvas()
        self.tool = SelectTool(canvas, mode)
        self.tool.selected.connect(self.map_selected)
        canvas.setMapTool(self.tool)
        self.dock.toast({"rectangle": "Drag a rectangle over the trajectory",
                         "polygon": "Click the corners; right-click or double-click to finish",
                         "lasso": "Drag around the part of the trajectory"}[mode])

    def map_selected(self, geom):
        from qgis.core import QgsCoordinateTransform, QgsGeometry, QgsPointXY, QgsProject
        from ..core import review
        canvas = self.dock.iface.mapCanvas()
        tr = QgsCoordinateTransform(maplayers._crs(self.project), canvas.mapSettings().destinationCrs(),
                                    QgsProject.instance())
        qc = self.dock.qc
        flags = []
        for lat, lon in zip(qc["lat_deg"], qc["lon_deg"]):
            try:
                pt = tr.transform(QgsPointXY(lon, lat))
            except Exception:       # noqa: BLE001
                flags.append(False)
                continue
            flags.append(geom.contains(QgsGeometry.fromPointXY(pt)))
        runs = review._runs(qc["gps_time"], flags)
        if not runs:
            self.dock.toast("No part of the trajectory inside the selection", "warn")
            return
        for a, b, _m in runs:
            review.add_selection(self.project, max(qc["gps_time"][0], a - 0.5), min(qc["gps_time"][-1], b + 0.5))
        self.dock.save()
        self.fill()
        self.dock.toast("%d part%s of the trajectory added" % (len(runs), "" if len(runs) == 1 else "s"))

    def select_plot_range(self):
        plots = self.dock.plugin.plots
        pl = next(iter(plots.plots.values()), None) if plots else None
        if pl is None or pl.x0 is None or pl.x1 is None:
            theme.msg_info(self, "PosPro", "Open the quality plots and zoom to the part you want, then press this "
                                           "again.")
            return
        from ..core import review
        review.add_selection(self.project, pl.x0, pl.x1)
        self.dock.save()
        self.fill()

    def show_log(self):
        from ..core import review
        lines = review.state(self.project)["log"] if self.project else []
        d = QDialog(self)
        d.setWindowTitle("Trajectory Review log")
        lay = QVBoxLayout(d)
        t = QPlainTextEdit("\n".join(lines) or "No edits yet.")
        t.setReadOnly(True)
        lay.addWidget(t)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(d.reject)
        lay.addWidget(bb)
        d.resize(700, 420)
        d.exec()

    def state(self):
        from ..core import review
        p = self.project
        if not p or not self.dock.qc:
            return "todo", "after processing"
        r = review.state(p)
        if review.exclusions_pending(p):
            return "warn", "process again"
        if not r["anomalies"] and not r["edits"]:
            return "todo", "find anomalies"
        n_open = sum(1 for a in r["anomalies"] if not review.edit_for(p, a["t0"], a["t1"]))
        if n_open:
            return "warn", "%d open" % n_open
        return "done", "%d reviewed" % (len(r["anomalies"]) + len([e for e in r["edits"] if e.get("source") == "manual"]))


# ---------------------------------------------------------------------------
class CalibPage(Page):
    """Antenna lever arms solved from the drive."""
    LEVER_ROWS = (("lever_antenna", "Primary antenna"), ("lever_antenna2", "Secondary antenna"))

    def __init__(self, dock):
        super().__init__(dock)
        v = QVBoxLayout(self)
        v.addWidget(_title("Calibration"))
        v.addWidget(_hint("Works on data you have already processed. Measure the offsets as well as you can "
                          "first; the solver then checks and refines them."))
        lg = QGroupBox("Antenna lever arms")
        ll = QVBoxLayout(lg)
        ll.addWidget(_hint("The tightly coupled filter estimates the IMU-to-antenna offsets together with the "
                           "trajectory. They become observable when the vehicle turns and changes speed: drive "
                           "a few figure-eights or loops in open sky. Horizontal offsets come out to about a "
                           "centimetre; the vertical offset is weaker, so keep the tape measure for that one."))
        self.flow1 = QLabel("MEASURE  \u2192  PROCESS  \u2192  SOLVE  \u2192  COMPARE  \u2192  APPLY  \u2192  PROCESS AGAIN")
        self.flow1.setObjectName("ppKey")
        ll.addWidget(self.flow1)
        self.b_lever = QPushButton("Solve lever arms")
        self.b_lever.setObjectName("ppPrimary")
        self.b_lever.clicked.connect(self.run_levers)
        ll.addWidget(self.b_lever)
        self.lt = QTableWidget(0, 5)
        self.lt.setHorizontalHeaderLabels(["OFFSET", "ENTERED", "SOLVED", "\u00b1 1\u03c3", "CHANGE"])
        self.lt.verticalHeader().setVisible(False)
        self.lt.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.lt.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        ll.addWidget(self.lt)
        self.lever_note = _hint("")
        ll.addWidget(self.lever_note)
        self.b_lever_apply = QPushButton("Apply solved offsets")
        self.b_lever_apply.clicked.connect(self.apply_levers)
        ll.addWidget(self.b_lever_apply)
        v.addWidget(lg)
        v.addStretch(1)

    def refresh(self):
        p = self.project
        if not p:
            return
        self.show_levers(p.cfg["calibration"].get("levers") or {})

    def show_levers(self, res):
        inst = self.project.cfg["installation"]
        rows = []
        for key, label in self.LEVER_ROWS:
            r = res.get(key)
            for i, ax in enumerate(("X fwd", "Y right", "Z down")):
                ent = (inst.get(key) or [0, 0, 0])[i]
                name = "%s %s" % ("GNSS 1" if key == "lever_antenna" else "GNSS 2", ax.split()[0])
                if r:
                    val, sd = r["value"][i], r["sd"][i]
                    rows.append((name, "%.3f" % ent, "%.3f" % val, "%.3f" % sd, "%+.3f" % (val - ent),
                                 abs(val - ent) > max(3 * sd, 0.02)))
                elif key == "lever_antenna":
                    rows.append((name, "%.3f" % ent, "\u2013", "\u2013", "\u2013", False))
        self.lt.setRowCount(len(rows))
        self.lt.setFixedHeight(40 + 32 * max(3, len(rows)))
        for r_, row in enumerate(rows):
            for c in range(5):
                it = QTableWidgetItem(row[c])
                if c == 4 and row[5]:
                    it.setForeground(QColor(theme.AMBER))
                self.lt.setItem(r_, c, it)
        self.b_lever_apply.setEnabled(bool(res.get("lever_antenna")))
        if res.get("lever_antenna"):
            big = [r for r in rows if r[5]]
            self.lever_note.setText(("Solved %s. " % res.get("finished", "")) +
                                    ("Amber changes are larger than three standard deviations: re-measure those "
                                     "offsets or apply the solved values and process again." if big else
                                     "The solved offsets agree with the entered ones within their uncertainty."))
        else:
            self.lever_note.setText("")

    def run_levers(self):
        p = self.project
        if p.cfg["processing"].get("mode") != "tc":
            theme.msg_info(self, "PosPro", "The lever-arm solver uses tight coupling: choose it on the "
                                                    "Processing page first.")
            return
        work_p = self.dock._work_copy()

        def work(ctx):
            return pipeline.solve_levers(work_p, ctx)

        def done(ok, res):
            if not ok:
                theme.msg_warn(self, "PosPro", "Lever-arm solution stopped:\n\n%s" % res)
                return
            p.cfg["calibration"]["levers"] = {k: v for k, v in res.items() if k != "passes"}
            self.dock.save()
            self.show_levers(p.cfg["calibration"]["levers"])
            self.dock.toast("Lever arms solved \u2013 compare them with the entered values.", "ok")
        self.dock.start_task("Solving lever arms", work, done)

    def apply_levers(self):
        p = self.project
        res = p.cfg["calibration"].get("levers") or {}
        txt = []
        for key, label in self.LEVER_ROWS:
            if res.get(key):
                p.cfg["installation"][key] = [round(x, 4) for x in res[key]["value"]]
                txt.append("%s: %.3f, %.3f, %.3f m" % ((label,) + tuple(res[key]["value"])))
        if not txt:
            return
        self.dock.save()
        self.dock.pages[2].refresh()
        self.show_levers(res)
        theme.msg_info(self, "PosPro", "Applied:\n" + "\n".join(txt) +
                                "\n\nProcess the trajectory again so it uses them, and update the Certus and the "
                                "mapper Settings if you keep them.")

    def state(self):
        p = self.project
        if not p:
            return "todo", ""
        if (p.cfg["calibration"].get("levers") or {}).get("lever_antenna"):
            return "done", "lever arms solved"
        return "todo", "antenna lever arms"


# ---------------------------------------------------------------------------
class ExportPage(Page):
    COLS = [("date_time_utc", "UTC date, time"), ("time_sow", "GPS seconds of week"), ("time_gps", "GPS seconds"),
            ("unix", "Unix time"), ("lat", "Latitude"), ("lon", "Longitude"), ("h", "Ellipsoidal height"),
            ("easting", "Easting"), ("northing", "Northing"), ("roll", "Roll"), ("pitch", "Pitch"),
            ("heading", "Heading"), ("vn", "Velocity N"), ("ve", "Velocity E"), ("vd", "Velocity D"),
            ("speed", "Speed"), ("sd_n", "\u03c3 North"), ("sd_e", "\u03c3 East"), ("sd_d", "\u03c3 Down"),
            ("sd_roll", "\u03c3 Roll"), ("sd_pitch", "\u03c3 Pitch"), ("sd_heading", "\u03c3 Heading"),
            ("status", "Solution type"), ("nsat", "Satellites"), ("sep_h", "Separation H"),
            ("sep_v", "Separation V")]

    def __init__(self, dock):
        super().__init__(dock)
        v = QVBoxLayout(self)
        v.addWidget(_title("Export"))
        f = QFormLayout()
        row = QHBoxLayout()
        self.folder = QLineEdit()
        b = QPushButton("\u2026")
        b.setFixedWidth(30)
        b.clicked.connect(self.pick_folder)
        row.addWidget(self.folder)
        row.addWidget(b)
        f.addRow("Folder", row)
        self.point = QComboBox()
        f.addRow("Position of", self.point)
        self.rate = QComboBox()
        for label, hz in (("Every sample", 0), ("200 Hz", 200), ("100 Hz", 100), ("50 Hz", 50), ("10 Hz", 10), ("1 Hz", 1)):
            self.rate.addItem(label, hz)
        f.addRow("Rate", self.rate)
        self.crop = QCheckBox("Data-page time range only")
        f.addRow("", self.crop)
        v.addLayout(f)
        self.sbet = QCheckBox("SBET + SMRMSG")
        v.addWidget(self.sbet)
        v.addWidget(_hint("SBET trajectory and SMRMSG accuracy files: sbet_YYYYMMDD.out and smrmsg_YYYYMMDD.out."))
        self.ascii = QCheckBox("Text file")
        v.addWidget(self.ascii)
        tw = QWidget()
        tl = QVBoxLayout(tw)
        tl.setContentsMargins(18, 0, 0, 0)
        self.cols = QListWidget()
        self.cols.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.cols.setMaximumHeight(150)
        for k, label in self.COLS:
            it = QListWidgetItem(label)
            it.setData(Qt.ItemDataRole.UserRole, k)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Unchecked)
            self.cols.addItem(it)
        tl.addWidget(_hint("Tick the columns; drag to reorder."))
        tl.addWidget(self.cols)
        f3 = QFormLayout()
        self.sep = QComboBox()
        for label, s in (("Comma", ","), ("Tab", "\t"), ("Space", " "), ("Semicolon", ";")):
            self.sep.addItem(label, s)
        f3.addRow("Separator", self.sep)
        try:
            from qgis.gui import QgsProjectionSelectionWidget
            self.crs = QgsProjectionSelectionWidget()
            self.crs.setCrs(QgsCoordinateReferenceSystem("EPSG:2953"))
        except Exception:          # noqa: BLE001
            self.crs = QLineEdit("EPSG:2953")
        f3.addRow("Easting/northing in", self.crs)
        tl.addLayout(f3)
        v.addWidget(tw)
        self.ascii.toggled.connect(tw.setVisible)
        tw.setVisible(False)
        self.layers = QCheckBox("Trajectory layer (GeoPackage)")
        v.addWidget(self.layers)
        self.gpx = QCheckBox("GPX track (1 s)")
        v.addWidget(self.gpx)
        self.b_export = QPushButton("Export")
        self.b_export.setObjectName("ppPrimary")
        self.b_export.clicked.connect(self.run)
        v.addWidget(self.b_export)
        self.out = QLabel("")
        self.out.setWordWrap(True)
        self.out.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.out)
        v.addWidget(_hint("Heights are ellipsoidal in the base station's frame. SBET velocities are north, "
                          "east, down with wander angle 0; angles in radians, as Applanix defines."))
        v.addStretch(1)

    def pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Export folder", self.folder.text() or self.project.folder)
        if d:
            self.folder.setText(d)

    def refresh(self):
        p = self.project
        if not p:
            return
        e = p.cfg["export"]
        self.folder.setText(e.get("folder") or os.path.join(p.folder, "export"))
        inst = p.cfg["installation"]
        self.point.clear()
        self.point.addItem("IMU centre", "imu")
        if any(inst["lever_output"]):
            self.point.addItem(inst.get("output_name") or "Output point", "output")
        self.point.addItem("GNSS antenna", "antenna")
        self.point.setCurrentIndex(max(0, self.point.findData(e.get("point", "imu"))))
        self.rate.setCurrentIndex(max(0, self.rate.findData(int(e.get("rate_hz", 0) or 0))))
        self.sbet.setChecked(bool(e.get("sbet", True)))
        self.ascii.setChecked(bool(e.get("ascii", False)))
        self.layers.setChecked(bool(e.get("gpkg", False)))
        self.gpx.setChecked(bool(e.get("gpx", False)))
        want = (e.get("ascii_cols") or "").split(",")
        for i in range(self.cols.count()):
            it = self.cols.item(i)
            it.setCheckState(Qt.CheckState.Checked if it.data(Qt.ItemDataRole.UserRole) in want else Qt.CheckState.Unchecked)
        self.sep.setCurrentIndex(max(0, self.sep.findData(e.get("ascii_sep", ","))))

    def run(self):
        p = self.project
        e = p.cfg["export"]
        e["folder"] = self.folder.text().strip()
        e["point"] = self.point.currentData()
        e["rate_hz"] = self.rate.currentData()
        e["sbet"] = self.sbet.isChecked()
        e["ascii"] = self.ascii.isChecked()
        e["gpkg"] = self.layers.isChecked()
        e["gpx"] = self.gpx.isChecked()
        cols = [self.cols.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.cols.count())
                if self.cols.item(i).checkState() == Qt.CheckState.Checked]
        e["ascii_cols"] = ",".join(cols)
        e["ascii_sep"] = self.sep.currentData()
        self.dock.save()
        lever = {"imu": [0, 0, 0], "output": p.cfg["installation"]["lever_output"],
                 "antenna": p.cfg["installation"]["lever_antenna"]}[e["point"]]
        proj = None
        if e["ascii"] and ("easting" in cols or "northing" in cols):
            crs = self.crs.crs() if hasattr(self.crs, "crs") else QgsCoordinateReferenceSystem(self.crs.text())
            proj = crs.toProj()
            if not any(k in proj for k in ("+proj=tmerc", "+proj=utm", "+proj=sterea")):
                theme.msg_warn(self, "PosPro", "Easting/northing: choose a transverse Mercator, UTM or "
                                                    "stereographic system (e.g. EPSG:2953).")
                return
        span = p.rover_span() if self.crop.isChecked() else None
        out_dir = e["folder"] or os.path.join(p.folder, "export")

        def work(ctx):
            files = pipeline.export(p, ctx, out_dir, e["rate_hz"], lever, e["sbet"],
                                    e["ascii_cols"] if e["ascii"] and cols else None, e["ascii_sep"], proj,
                                    span[0] if span else None, span[1] if span else None)
            return files

        def done(ok, res):
            if not ok:
                theme.msg_warn(self, "PosPro", "Export failed:\n%s" % res)
                return
            if e["gpkg"]:
                path = self.dock.save_layer_gpkg(out_dir)
                if path:
                    res.append(path)
            if e.get("gpx") and self.dock.qc:
                try:
                    res.append(results.write_gpx(self.dock.qc, os.path.join(out_dir, "%s_trajectory.gpx" % p.name),
                                                 p.name))
                except OSError as ex:
                    self.dock.log("warning", "GPX: %s" % ex)
            note = os.path.join(out_dir, "%s_export_note.txt" % p.name)
            try:
                with open(note, "w", encoding="utf-8") as fh:
                    fh.write(self.dock.export_note(e, lever))
                res.append(note)
            except OSError:
                pass
            self.out.setText("Written:\n" + "\n".join(res))
        self.dock.start_task("Export", work, done)

    def state(self):
        p = self.project
        if not p or not (p.cfg.get("results") or {}).get("combine"):
            return "todo", "after processing"
        return "todo", "SBET, text, layers"


# ===========================================================================
class PosProDock(PluginWindow):
    """PosPro's window: DCS Core's standard plugin window (floating by default, docked on request)."""

    def __init__(self, iface, plugin):
        super().__init__(iface, "pospro", "PosPro", size=(1400, 950), dock_size=720, object_name="PosProDock")
        self.iface = iface
        self.plugin = plugin
        self.project = None
        self.qc = None
        self.task = None
        self._log_lines = []

        root = QWidget()
        theme.apply(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # header: logo, brand, project box (as the mapper dashboard)
        hw = QWidget()
        hw.setObjectName("ppHeader")
        head = QHBoxLayout(hw)
        head.setContentsMargins(12, 8, 12, 8)
        head.setSpacing(12)
        head.addWidget(Logo(42))
        brand = QVBoxLayout()
        brand.setSpacing(1)
        from dcs_core import menu as _dcs_menu
        t1 = QLabel(_dcs_menu.GROUP)
        t1.setObjectName("ppBrandTitle")
        t2 = QLabel("PosPro")
        t2.setObjectName("ppBrandSub")
        for t_ in (t1, t2):
            theme.caps(t_, 107)
        brand.addWidget(t1)
        brand.addWidget(t2)
        head.addLayout(brand)
        head.addStretch(1)
        self.pbox = QToolButton()
        self.pbox.setObjectName("ppProject")
        self.pbox.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.pbox.setMinimumWidth(170)
        mm = QMenu(self.pbox)
        mm.addAction("New project\u2026", self.new_project)
        mm.addAction("Open project\u2026", self.open_project)
        mm.addSeparator()
        mm.addAction("Open the project folder", self.open_folder)
        mm.addAction("Raw data moved? Relocate the job folder\u2026", self.relocate_raw)
        mm.addAction("Save the QGIS project in the project folder", self.save_qgis_project)
        mm.addAction("Show the log", self.show_log)
        mm.addAction("Engine version", self.engine_info)
        mm.addSeparator()
        mm.addAction("User guide (this version)", self.open_guide)
        self.pbox.setMenu(mm)
        head.addWidget(self.pbox)
        # the Window menu (DCS Core): floating, maximise, dock left / right / top / bottom, reset
        self.wbtn = QToolButton()
        self.wbtn.setObjectName("ppWinBtn")
        self.wbtn.setText("\u2922")
        self.wbtn.setFixedSize(34, 34)
        self.attach_menu(self.wbtn)
        head.addWidget(self.wbtn)
        self.helpbtn = QToolButton()
        self.helpbtn.setObjectName("ppWinBtn")
        self.helpbtn.setText("?")
        self.helpbtn.setToolTip("User guide for this version of PosPro (PDF)")
        self.helpbtn.setFixedSize(34, 34)
        self.helpbtn.clicked.connect(self.open_guide)
        head.addWidget(self.helpbtn)
        self.pname = self.pbox
        self._set_pname("No project")
        outer.addWidget(hw)
        # the chain bar: PosPro -> AlignPro -> CloudPro on this project
        cw = QWidget()
        cw.setObjectName("ppChain")
        cl = QHBoxLayout(cw)
        cl.setContentsMargins(12, 4, 12, 6)
        self.chain = StepBar("pospro")
        cl.addWidget(self.chain)
        outer.addWidget(cw)
        self._chain_timer = QTimer(self)
        self._chain_timer.setInterval(5000)
        self._chain_timer.timeout.connect(self._refresh_chain)
        self._chain_timer.start()

        body = QHBoxLayout()
        body.setSpacing(0)
        side = QWidget()
        side.setObjectName("ppSidebar")
        side.setFixedWidth(186)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(4, 8, 4, 8)
        sl.setSpacing(4)
        self.stack = QStackedWidget()
        self.pages = [DataPage(self), BasePage(self), InstallPage(self), ProcessPage(self), QualityPage(self),
                      ReviewPage(self),
                      CalibPage(self), ExportPage(self)]
        titles = ["Data", "Base", "Installation", "Processing", "Quality", "Review", "Calibration", "Export"]
        self.step_buttons = []
        grp = QButtonGroup(self)
        grp.setExclusive(True)
        for i, (pg, t) in enumerate(zip(self.pages, titles)):
            sb = StepButton(i + 1, t)
            grp.addButton(sb, i)
            sl.addWidget(sb)
            self.step_buttons.append(sb)
            sb.clicked.connect(lambda _=False, k=i: self.goto_page(k))
            self.stack.addWidget(_scroll(pg))
        sl.addStretch(1)
        for pg in self.pages:
            theme.mapperize(pg)
            for cb in pg.findChildren(QComboBox):
                cb.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
                cb.setMinimumContentsLength(12)
        body.addWidget(side)
        content = QVBoxLayout()
        content.setContentsMargins(10, 4, 10, 4)
        content.addWidget(self.stack, 1)
        body.addLayout(content, 1)
        outer.addLayout(body, 1)

        self.toast_w = Toast()
        tw_ = QHBoxLayout()
        tw_.setContentsMargins(10, 0, 10, 0)
        tw_.addWidget(self.toast_w)
        outer.addLayout(tw_)
        foot = QHBoxLayout()
        foot.setContentsMargins(10, 4, 10, 6)
        self.status = QLabel("")
        self.status.setObjectName("ppStatus")
        self.status.setWordWrap(True)
        self.prog = QProgressBar()
        self.prog.setMaximumWidth(140)
        self.prog.setVisible(False)
        self.b_cancel = QPushButton("STOP")
        self.b_cancel.setObjectName("ppStop")
        self.b_cancel.setVisible(False)
        self.b_cancel.clicked.connect(self.cancel)
        foot.addWidget(self.status, 1)
        foot.addWidget(self.prog)
        foot.addWidget(self.b_cancel)
        outer.addLayout(foot)
        self.setWidget(root)
        theme.apply_dock(self)
        theme.polish(root)
        self.setMinimumWidth(690)
        self.goto_page(0)
        self.set_enabled(False)
        QTimer.singleShot(0, self.reopen_last)

    # ------------------------------------------------------------------
    def set_enabled(self, on):
        self.stack.setEnabled(on)
        for b in self.step_buttons:
            b.setEnabled(on)

    def goto_page(self, k):
        self.stack.setCurrentIndex(k)
        self.step_buttons[k].setChecked(True)
        if self.project:
            self.pages[k].refresh()

    def update_steps(self):
        for pg, sb in zip(self.pages, self.step_buttons):
            try:
                st, sub = pg.state()
            except Exception:       # noqa: BLE001
                st, sub = "todo", ""
            sb.set_state(st, sub)

    def save(self):
        if self.project:
            self.project.save()
        self.update_steps()

    def _set_pname(self, name):
        self.pbox.setText(name.upper() + "  \u25be")
        f = self.pbox.font()
        f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 110)
        self.pbox.setFont(f)

    def toast(self, text, level="ok"):
        self.toast_w.show_message(text, level)

    def log(self, level, text):
        if level in ("warning", "error"):
            self.toast(text if len(text) < 300 else text[:300] + "\u2026", "warn" if level == "warning" else "bad")
        self._log_lines.append("%s %s %s" % (time.strftime("%H:%M:%S"), level.upper(), text))
        self._log_lines = self._log_lines[-2000:]
        if level in ("warning", "error"):
            self.iface.messageBar().pushMessage("PosPro", text, Qgis.MessageLevel.Warning if level == "warning"
                                                else Qgis.MessageLevel.Critical, 8)
        if level != "info" or len(text) < 140:
            self.status.setText(text)

    def show_log(self):
        d = QDialog(self)
        d.setWindowTitle("PosPro log")
        lay = QVBoxLayout(d)
        t = QPlainTextEdit("\n".join(self._log_lines))
        t.setReadOnly(True)
        lay.addWidget(t)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(d.reject)
        lay.addWidget(bb)
        d.resize(780, 480)
        d.exec()

    def open_guide(self):
        from ..plugin import guide_path, open_guide
        if not guide_path():
            theme.msg_warn(self, "PosPro", "The user guide is missing from this installation; reinstall PosPro.")
            return
        open_guide()

    def engine_info(self):
        v, r = engine.version()
        theme.msg_info(self, "PosPro", "Engine %s\nRTKLIB %s\n%s" % (v or "not found", r or "", engine.engine_path()))

    def open_folder(self):
        if self.project:
            from qgis.PyQt.QtCore import QUrl
            from qgis.PyQt.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.project.project_folder))

    def _refresh_chain(self):
        if self.isVisible() and self.project is not None and self.project.dcs is not None:
            self.chain.refresh()

    def relocate_raw(self):
        if not self.project or not self.project.dcs:
            theme.msg_info(self, "PosPro", "This is an older stand-alone PosPro project; there is no job folder "
                                           "to relocate. Use Add files on the Data page.")
            return
        d = QFileDialog.getExistingDirectory(self, "New location of the job folder", self.project.dcs.job_folder)
        if not d:
            return
        still = self.project.dcs.relocate(d, "PosPro")
        if still:
            theme.msg_warn(self, "PosPro", "Not found under %s:\n%s" % (d, "\n".join(still[:6])))
            return
        self._remap_rover_files()
        self.toast("Raw data found in %s" % d)

    def _remap_rover_files(self):
        """After a relocation, point PosPro's rover file list at the files the
        shared project now resolves."""
        p = self.project
        now = {os.path.basename(f): f for f in p.dcs.capture_files()}
        files_ = p.cfg["rover"]["files"]
        p.cfg["rover"]["files"] = [now.get(os.path.basename(f), f) for f in files_]
        p.save()
        if self.project is p and getattr(self, "chain", None) is not None:
            self.pages[0].refresh()

    def save_qgis_project(self):
        if not self.project:
            return
        from qgis.core import QgsProject
        qp = QgsProject.instance()
        target = self.project.dcs.qgis_project_path() if self.project.dcs else \
            os.path.join(self.project.folder, self.project.name + ".qgz")
        if qp.fileName() and os.path.abspath(qp.fileName()) != os.path.abspath(target):
            r = theme.msg_question(self, "PosPro", "The QGIS project is %s.\n\nSave it as %s instead?" % (
                qp.fileName(), target))
            if r != QMessageBox.StandardButton.Yes:
                return
        qp.setFileName(target)
        if qp.write():
            self.toast("QGIS project saved: %s" % target)
        else:
            theme.msg_warn(self, "PosPro", "Could not save %s" % target)

    # ------------------------------------------------------------------
    def new_project(self):
        from dcs_core import dialogs
        dcs = dialogs.new_project(self, "PosPro")
        if dcs is None:
            return
        p = Project.from_dcs(dcs)
        found = find_captures(dcs.job_folder)
        if found:
            names = "\n".join(os.path.relpath(f, dcs.job_folder) for f in found[:12])
            more = "\n\u2026 and %d more" % (len(found) - 12) if len(found) > 12 else ""
            r = theme.msg_question(self, "PosPro", "Found %d capture file(s) in the job folder:\n\n%s%s\n\n"
                                                   "Use them as the rover files?" % (len(found), names, more))
            if r == QMessageBox.StandardButton.Yes:
                p.cfg["rover"]["files"] = found
                p.save()
        self.set_project(p)
        self.toast("Project %s created in %s" % (dcs.name, dcs.folder))

    def open_project(self):
        from dcs_core.project import SUFFIX
        f, _ = QFileDialog.getOpenFileName(self, "Open project", QSettings().value("PosPro/last_dir", ""),
                                           "Processing project (*%s);;Older PosPro project (*%s)"
                                           % (SUFFIX, PROJECT_SUFFIX))
        if f:
            self.open_path(f)

    def open_path(self, path):
        try:
            p = Project.open_any(path)
        except Exception as e:      # noqa: BLE001
            theme.msg_warn(self, "PosPro", "Cannot open the project:\n%s" % e)
            return False
        if p.dcs is not None:
            from dcs_core import dialogs
            if p.dcs.missing_captures():
                dialogs.check_raw(self, p.dcs, "PosPro")
            if any(not os.path.exists(f) for f in p.cfg["rover"]["files"]):
                self.project = p
                self._remap_rover_files()
        self.set_project(p)
        return True

    def reopen_last(self):
        last = QSettings().value("PosPro/last_project", "")
        if last and os.path.exists(last) and not self.project:
            try:
                self.set_project(Project.open_any(last))
            except Exception:           # noqa: BLE001
                pass

    def set_project(self, p):
        self.project = p
        QSettings().setValue("PosPro/last_project", p.dcs.path if p.dcs else p.path)
        QSettings().setValue("PosPro/last_dir", os.path.dirname(p.project_folder))
        self.chain.set_project(p.dcs)
        self.chain.setVisible(p.dcs is not None)
        self._set_pname(p.name)
        self.set_enabled(True)
        self.qc = results.load_qc(os.path.join(p.work_dir, "results", "qc.csv"))
        for pg in self.pages:
            pg.refresh()
        self.update_steps()
        self.refresh_map_bases()
        self.show_rt_track()
        if self.qc:
            self.show_trajectory()
        self.goto_page(self.stack.currentIndex())

    # ------------------------------------------------------------------
    def start_task(self, title, fn, on_done):
        if self.task is not None:
            theme.msg_info(self, "PosPro", "Wait for the current job to finish (or cancel it).")
            return None
        t = WorkTask("PosPro: %s" % title, fn)
        t.message.connect(self.log)
        t.stage.connect(lambda s: self.status.setText("%s \u2013 %s" % (title, s)))
        t.progressChanged.connect(lambda v: self.prog.setValue(int(v)))

        def finished(ok, res):
            self.task = None
            self.pbox.setProperty("busy", False)
            self.pbox.style().unpolish(self.pbox)
            self.pbox.style().polish(self.pbox)
            self.prog.setVisible(False)
            self.b_cancel.setVisible(False)
            self.status.setText("%s: %s" % (title, "done" if ok else "stopped"))
            if not ok:
                self.log("error", "%s: %s" % (title, str(res).splitlines()[0] if res else "stopped"))
            on_done(ok, res)
            self.update_steps()
        t.done.connect(finished)
        self.task = t
        self.pbox.setProperty("busy", True)
        self.pbox.style().unpolish(self.pbox)
        self.pbox.style().polish(self.pbox)
        self.prog.setValue(0)
        self.prog.setVisible(True)
        self.b_cancel.setVisible(True)
        self.status.setText(title + "\u2026")
        QgsApplication.taskManager().addTask(t)
        return t

    def _work_copy(self):
        """The background job works on its own copy of the settings, so the
        panel can keep saving the project while it runs."""
        return copy.deepcopy(self.project)

    def _merge_back(self, work_p):
        src, dst = work_p.cfg, self.project.cfg
        for k in ("extract", "extract_files", "hardware"):
            dst["rover"][k] = src["rover"].get(k)
        dst["results"] = src.get("results") or {}
        dst["frame"] = src.get("frame") or dst.get("frame")
        dst.setdefault("review", {})["excluded_in_run"] = (src.get("review") or {}).get("excluded_in_run", [])

    def cancel(self):
        if self.task:
            self.task.cancel()

    def run_extract(self, force=False):
        if not self.project or not self.project.cfg["rover"]["files"]:
            return

        work_p = self._work_copy()

        def work(ctx):
            return pipeline.extract(work_p, ctx, force)

        def done(ok, res):
            if ok:
                self._merge_back(work_p)
                self.save()
                self.pages[0].refresh()
                self.show_rt_track()
                self.refresh_map_bases()
                inst = self.project.cfg["installation"]
                if not inst.get("source") and not any(abs(x) > 1e-9 for x in inst["lever_antenna"]):
                    got = self.pages[2].use_hardware(quiet=True)
                    if got:
                        self.toast("Installation offsets filled from the capture (%s)" % inst.get("source"))
                self.pages[2].refresh()
        self.start_task("Reading rover files", work, done)

    def run_processing(self):
        p = self.project
        problems = []
        if not (p.cfg["rover"].get("extract") or {}):
            problems.append("Read the rover files (Data).")
        if p.cfg["processing"].get("positioning") != "ppp" and p.cfg["processing"].get("mode") != "rt":
            b = p.base()
            if not b:
                problems.append("Add a base station (Base stations).")
            elif b.get("pos_source", "rinex") == "rinex":
                r = theme.msg_question(self, "PosPro", "The base position comes from the RINEX header and may be "
                                         "metres off; every point of the trajectory inherits that error.\n\n"
                                         "Process anyway?")
                if r != QMessageBox.StandardButton.Yes:
                    return
        if problems:
            theme.msg_info(self, "PosPro", "\n".join(problems))
            return
        if self.task is not None:
            theme.msg_info(self, "PosPro", "Wait for the current job to finish (or cancel it).")
            return
        self.save()
        run = None
        if p.dcs is not None:
            from dcs_core.locks import Locked
            run = p.dcs.run("pospro", "PosPro", pipeline.plugin_version())
            try:
                run.__enter__()
            except Locked as e:
                theme.msg_warn(self, "PosPro", str(e))
                return
            self.chain.refresh()
        work_p = self._work_copy()

        def work(ctx):
            summary = pipeline.run_all(work_p, ctx.sub(0, 0.93))
            if work_p.dcs is not None:
                summary["handover"] = pipeline.publish_handover(work_p, ctx.sub(0.93, 1.0))
            return summary

        def done(ok, res):
            if run is not None:
                run.__exit__(None if ok else RuntimeError, None if ok else RuntimeError(str(res)[:400]), None)
                p.dcs.reload()
                self.chain.refresh()
            if not ok:
                theme.msg_warn(self, "PosPro", "Processing stopped:\n\n%s" % res)
                return
            self._merge_back(work_p)
            self.save()
            self.qc = results.load_qc(os.path.join(p.work_dir, "results", "qc.csv"))
            self.show_trajectory()
            self.pages[4].refresh()
            self.pages[5].refresh()
            self.goto_page(4)
            if self.plugin.plots:
                self.plugin.plots.set_data(self.qc)
        self.start_task("Processing", work, done)

    # ------------------------------------------------------------------
    def show_coverage(self, view, bases=False):
        p = self.project
        if not p or not p.work_dir:
            view.set_data([], 0, 1)
            return
        path = os.path.join(p.work_dir, "extract", "coverage.csv")
        try:
            key = (path, os.path.getmtime(path))
        except OSError:
            view.set_data([], 0, 1)
            return
        if getattr(self, "_cov_key", None) != key:
            ex = p.cfg["rover"].get("extract") or {}
            try:
                rate = int(round(float(ex.get("imu_rate_hz") or 0))) or None
            except ValueError:
                rate = None
            self._cov = results.load_coverage(path, rate)
            self._cov_key = key
        c = self._cov
        if not c:
            view.set_data([], 0, 1)
            return
        rows = list(c["rows"])
        if bases:
            for b in p.cfg["bases"]:
                rows.append(("Base " + str(b.get("name", "?"))[:8], results.base_runs(b, c["t0"], c["t1"])))
        view.set_data(rows, c["t0"], c["t1"], p.rover_span())

    def refresh_map_bases(self):
        if self.project and self.project.cfg["bases"]:
            try:
                maplayers.bases_layer(self.project)
            except Exception as e:      # noqa: BLE001
                self.log("warning", "base layer: %s" % e)

    def show_rt_track(self):
        p = self.project
        rows = results.load_rt(os.path.join(p.work_dir, "extract", "rt.bin"), 1.0)
        if rows:
            lyr = maplayers.rt_layer(p, rows)
            if not self.qc:
                maplayers.zoom_to(self.iface, lyr)

    def show_trajectory(self):
        if not self.qc:
            return
        lyr = maplayers.trajectory_layers(self.project, self.qc, self.pages[4].colour.currentData() or "status")
        self.traj_layer = lyr
        maplayers.zoom_to(self.iface, lyr)

    def recolour(self, how):
        lyr = getattr(self, "traj_layer", None)
        if lyr is not None:
            try:
                maplayers.style_trajectory(lyr, how)
            except RuntimeError:
                self.show_trajectory()

    def show_plots(self):
        self.plugin.show_plots(self.qc)

    def goto_time(self, t0, t1=None):
        self.plugin.goto_time(t0, t1)

    def save_layer_gpkg(self, out_dir):
        from qgis.core import QgsVectorFileWriter
        lyr = getattr(self, "traj_layer", None)
        if lyr is None:
            return None
        path = os.path.join(out_dir, "%s_trajectory.gpkg" % self.project.name)
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = "GPKG"
        opts.layerName = "trajectory"
        res = QgsVectorFileWriter.writeAsVectorFormatV3(lyr, path, QgsProject.instance().transformContext(), opts)
        return path if res[0] == QgsVectorFileWriter.WriterError.NoError else None

    def export_note(self, e, lever):
        p = self.project
        b = p.base()
        fr = p.cfg["frame"]
        span = p.rover_span()
        lines = ["PosPro export", "=============", "",
                 "Project      %s" % p.name,
                 "Processed    %s (%s, %s)" % ((p.cfg.get("results") or {}).get("finished", ""),
                                               p.cfg["processing"].get("mode"), p.cfg["processing"].get("passes")),
                 "Frame        %s, epoch %s (that of the base position)" % (fr.get("name"), fr.get("epoch")),
                 "Base         %s, position from %s" % (b.get("name") if b else "-", b.get("pos_source") if b else "-"),
                 "Heights      ellipsoidal",
                 "Time         GPS; SBET time is GPS seconds of week %s" % (geo.gps_week_sow(span[0])[0] if span else ""),
                 "Point        %s, offset from IMU (X fwd, Y right, Z down) %.3f %.3f %.3f m" % (
                     e.get("point"), lever[0], lever[1], lever[2]),
                 "Attitude     roll, pitch, heading of the Certus aligned (vehicle) frame",
                 "",
                 "Use the same reference point in the point-cloud processing as in this export."]
        return "\n".join(lines) + "\n"
