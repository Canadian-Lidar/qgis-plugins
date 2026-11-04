"""AlignPro main window on DCS Core (the PosPro / mapper look): header with logo, project box and the
chain bar (PosPro -> AlignPro -> CloudPro), workflow steps as sensor pills, pages, toast and status
line; the Data View dock; map layers."""
import os
import time

import numpy as np
from qgis.core import Qgis, QgsCoordinateReferenceSystem
from qgis.PyQt.QtCore import QSettings, Qt, QTimer, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QFont, QGuiApplication, QTextDocument
from qgis.PyQt.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                                 QDoubleSpinBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QMenu, QPlainTextEdit,
                                 QProgressBar, QPushButton, QScrollArea, QStackedWidget, QToolButton, QVBoxLayout,
                                 QWidget)

from ..core import engine, report
from ..core.project import Project
from . import dialogs, theme
from .common import BOX_COLORS, ask, fmt, log, run_job, themed, warn
from .maplayers import GRID_ATTRS, MapLayers, local_crs
from .maptools import DragLineTool, PolygonTool, SliceCursor
from .pages import AnalyzePage, CalibPage, DataPage, InstallPage, PairsPage, QualityPage, ReportPage, WelcomePage
from .dataview import DataView
from .slices import SliceView
from .widgets import Logo, StepButton, Toast
from dcs_core.window import PluginWindow, alive
from dcs_core.stepbar import StepBar


def DarkMenu(parent=None):  # noqa: N802 - a QMenu painted by DCS Core whatever the platform style
    return theme.dark_menu(QMenu(parent))

STEPS = ["Data", "Installation", "Quality", "Pairs", "Calibration", "Analyze", "Report"]


def _scroll(widget):
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.Shape.NoFrame)
    sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    widget.setMinimumWidth(0)
    sa.setWidget(widget)
    return sa


class AlignProDock(PluginWindow):
    """AlignPro's window: DCS Core's standard plugin window (floating by default, docked on request)."""

    def __init__(self, iface, parent=None):
        super().__init__(iface, "alignpro", "AlignPro \u2013 Lidar Boresight Calibration", parent, size=(1400, 950),
                         dock_size=720, object_name="AlignProDock")
        self.iface = iface
        self.p = None
        self.job = None
        self.avg_error = None
        self.layers = MapLayers(iface)
        self.cursor = SliceCursor(iface.mapCanvas())
        self.prev_tool = None
        self.combi_row = 0
        self._log_lines = []
        self._build()
        self._build_dataview(parent)
        self.set_enabled(False)
        QTimer.singleShot(0, self.reopen_last)

    # ================================================================ layout
    def _build(self):
        root = QWidget()
        theme.apply(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

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
        t2 = QLabel("AlignPro \u00b7 Lidar Boresight Calibration")
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
        mm = DarkMenu(self.pbox)
        mm.addAction("New project\u2026", self.new_project)
        mm.addAction("Open project\u2026", self.open_dialog)
        self.recent_menu = theme.dark_menu(QMenu("Recent projects", mm))
        mm.addMenu(self.recent_menu)
        self.recent_menu.aboutToShow.connect(self._fill_recent)
        mm.addSeparator()
        self.a_folder = mm.addAction("Open the project folder", self.explore)
        self.a_reloc = mm.addAction("Raw data moved? Relocate the job folder\u2026", self.relocate_raw)
        self.a_qgz = mm.addAction("Save the QGIS project in the project folder", self.save_qgis_project)
        self.a_reset = mm.addAction("Reset AlignPro to the recorded values\u2026", self.reset_project)
        mm.addAction("Show the log", self.show_log)
        mm.addSeparator()
        self.a_guide = mm.addAction("User guide (this version)", self.open_guide)
        self.pbox.setMenu(mm)
        head.addWidget(self.pbox)
        self.wbtn = QToolButton()
        self.wbtn.setObjectName("ppWinBtn")
        self.wbtn.setText("\u2922")
        self.wbtn.setFixedSize(34, 34)
        self.attach_menu(self.wbtn, [("Open the Data View", lambda: self.show_dataview("open"))])
        self.hbtn = QToolButton()
        self.hbtn.setObjectName("ppWinBtn")
        self.hbtn.setText("?")
        self.hbtn.setToolTip("AlignPro user guide (PDF for this version)")
        self.hbtn.setFixedSize(34, 34)
        self.hbtn.clicked.connect(self.open_guide)
        head.addWidget(self.hbtn)
        head.addWidget(self.wbtn)
        self._set_pname("No project")
        outer.addWidget(hw)
        # the chain bar: PosPro -> AlignPro -> CloudPro on this project (DCS Core)
        cw = QWidget()
        cw.setObjectName("ppChain")
        cl = QHBoxLayout(cw)
        cl.setContentsMargins(12, 4, 12, 6)
        self.chain = StepBar("alignpro")
        cl.addWidget(self.chain)
        outer.addWidget(cw)
        self.chain_w = cw
        cw.setVisible(False)
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
        self.pg_data, self.pg_inst, self.pg_qc = DataPage(self), InstallPage(self), QualityPage(self)
        self.pg_pairs, self.pg_cal, self.pg_an, self.pg_rep = (PairsPage(self), CalibPage(self), AnalyzePage(self),
                                                               ReportPage(self))
        self.pages = [self.pg_data, self.pg_inst, self.pg_qc, self.pg_pairs, self.pg_cal, self.pg_an, self.pg_rep]
        self.step_buttons = []
        grp = QButtonGroup(self)
        grp.setExclusive(True)
        for i, (pg, t) in enumerate(zip(self.pages, STEPS)):
            sb = StepButton(i + 1, t)
            grp.addButton(sb, i)
            sl.addWidget(sb)
            self.step_buttons.append(sb)
            sb.clicked.connect(lambda _=False, k=i: self.goto_page(k))
            self.stack.addWidget(_scroll(pg))
        self.pg_welcome = WelcomePage(self)
        self.stack.addWidget(_scroll(self.pg_welcome))
        self.b_view = QPushButton("DATA VIEW \u00b7 FINE TUNE")
        self.b_view.setObjectName("ppPrimary")
        self.b_view.setToolTip("Plan, profile and section of the points, re-drawn live while you adjust roll, pitch, "
                               "heading, lever arm and latency")
        self.b_view.clicked.connect(lambda: self.show_dataview("float"))
        sl.addSpacing(6)
        sl.addWidget(self.b_view)
        sl.addStretch(1)
        # live figures under the steps, as the mapper's sensor pills
        self.kpi = QLabel("")
        self.kpi.setObjectName("ppHint")
        self.kpi.setWordWrap(True)
        self.kpi.setTextFormat(Qt.TextFormat.RichText)
        sl.addWidget(self.kpi)
        k = QLabel("MAP GRID")
        k.setObjectName("ppKey")
        sl.addWidget(k)
        self.grid_attr = QComboBox()
        for key, (t, *_r) in GRID_ATTRS.items():
            self.grid_attr.addItem(t, key)
        self.grid_attr.setCurrentIndex(list(GRID_ATTRS).index("separation"))
        self.grid_attr.currentIndexChanged.connect(self._grid_attr)
        sl.addWidget(self.grid_attr)
        for pg in self.pages + [self.pg_welcome]:
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
        self.prog.setMaximumWidth(160)
        self.prog.setVisible(False)
        self.b_cancel = QPushButton("STOP")
        self.b_cancel.setObjectName("ppStop")
        self.b_cancel.setVisible(False)
        self.b_cancel.clicked.connect(lambda: self.job and self.job.cancel())
        foot.addWidget(self.status, 1)
        foot.addWidget(self.prog)
        foot.addWidget(self.b_cancel)
        outer.addLayout(foot)
        self.setWidget(root)
        theme.apply_dock(self)
        theme.polish(root)
        theme.mark(root)
        self.setMinimumWidth(690)
        self.stack.setCurrentIndex(len(self.pages))
        self.pg_welcome.refresh()

    def _build_dataview(self, parent):
        self.view_dock = PluginWindow(self.iface, "alignpro_dataview", "AlignPro \u2013 Data View & Fine Tuning", parent,
                                      size=(1700, 1000), dock_size=420, object_name="AlignProDataViewDock")
        self.dv = DataView()
        theme.apply(self.dv)
        theme.apply_dock(self.view_dock)
        vb = QToolButton()
        vb.setObjectName("ppWinBtn")
        vb.setText("\u2922")
        vb.setFixedSize(30, 30)
        self.view_dock.attach_menu(vb)
        self.dv.bar.addWidget(vb)
        self.view_dock.setWidget(self.dv)
        theme.polish(self.dv)
        theme.mapperize(self.dv)
        theme.mark(self.dv)
        self.dv.apply_requested.connect(self.apply_installation)

    def _refresh_chain(self):
        if self.isVisible() and self.p is not None and self.p.dcs is not None and self.job is None:
            self.chain.refresh()
            self.pg_cal.refresh_publish()

    def show_dataview(self, how="open"):
        """open: where it was left (floating the first time); float, max, or dock (at the bottom)."""
        d = self.view_dock
        if not alive(d):
            return
        d.show_window()
        if how == "dock":
            d.dock_at("bottom")
        elif how == "max":
            if d.window_mode() != "max":
                d.maximise()
        elif how == "float" and d.window_mode() != "float":
            d.float_window()

    # ============================================================ state
    def set_enabled(self, on):
        has = self.p is not None
        loaded = has and bool(self.p.lines)
        busy = self.job is not None
        for i, (pg, b) in enumerate(zip(self.pages, self.step_buttons)):
            ok = (i == 0 and has) or loaded
            b.setEnabled(ok)
        if not has:
            self.pg_welcome.refresh()
            self.stack.setCurrentIndex(len(self.pages))
            for b in self.step_buttons:
                b.setChecked(False)
        elif self.stack.currentIndex() == len(self.pages):
            self.stack.setCurrentIndex(0)
            self.step_buttons[0].setChecked(True)
        self.stack.setEnabled(not busy or not has)
        self.a_folder.setEnabled(has)
        self.a_reloc.setEnabled(has and self.p.dcs is not None)
        self.a_qgz.setEnabled(has)
        self.a_reset.setEnabled(loaded and not busy)
        self.b_view.setEnabled(loaded and not busy)
        self.dv.setEnabled(loaded and not busy)

    def goto_page(self, k):
        self.stack.setCurrentIndex(k)
        self.step_buttons[k].setChecked(True)
        if self.p is not None:
            try:
                self.pages[k].refresh()
            except Exception as e:  # noqa: BLE001
                log("page refresh: %s" % e)

    def update_steps(self):
        for pg, sb in zip(self.pages, self.step_buttons):
            try:
                st, sub = pg.state()
            except Exception:  # noqa: BLE001
                st, sub = "todo", ""
            sb.set_state(st, sub)

    def _set_pname(self, name):
        self.pbox.setText(name.upper() + "  \u25be")
        f = self.pbox.font()
        f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 110)
        self.pbox.setFont(f)

    def _busy(self, on):
        self.pbox.setProperty("busy", bool(on))
        self.pbox.style().unpolish(self.pbox)
        self.pbox.style().polish(self.pbox)

    def toast(self, text, level="ok"):
        self.toast_w.show_message(text, level)

    def message(self, txt, level="info"):
        self._log_lines.append("%s %s %s" % (time.strftime("%H:%M:%S"), level.upper(), txt))
        self._log_lines = self._log_lines[-2000:]
        if level in ("warning", "error"):
            self.toast(txt if len(txt) < 300 else txt[:300] + "\u2026", "warn" if level == "warning" else "bad")
            self.iface.messageBar().pushMessage("AlignPro", txt, Qgis.MessageLevel.Warning if level == "warning"
                                                else Qgis.MessageLevel.Critical, 8)
        self.status.setText(txt if len(txt) < 200 else txt[:200] + "\u2026")

    def open_guide(self, *_):
        """The user guide PDF for the installed AlignPro version."""
        from ..guide import open_guide
        return open_guide(self.iface, self)

    def show_log(self):
        d = QDialog(self)
        d.setWindowTitle("AlignPro log")
        lay = QVBoxLayout(d)
        t = QPlainTextEdit("\n".join(self._log_lines + (self.p.cfg.get("log", []) if self.p else [])))
        t.setReadOnly(True)
        lay.addWidget(t)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(d.reject)
        lay.addWidget(bb)
        d.resize(780, 480)
        themed(d)
        d.exec()

    # ================================================================ jobs
    def _run(self, title, fn, done):
        if self.job is not None:
            self.toast("Wait for the current job to finish (or stop it).", "warn")
            return
        self.prog.setValue(0)
        self.prog.setVisible(True)
        self.b_cancel.setVisible(True)
        self.status.setText(title + "\u2026")
        self._busy(True)

        def finished(res, err):
            self.job = None
            self._busy(False)
            self.prog.setVisible(False)
            self.b_cancel.setVisible(False)
            self.set_enabled(True)
            if err:
                if err != "Cancelled":
                    log(err)
                    self.message("%s: %s" % (title, err.split("\n")[0]), "error")
                else:
                    self.message("%s: stopped" % title)
            else:
                self.status.setText("%s: done" % title)
                done(res)
            if self.p is not None and self.p.dcs is not None:
                self.chain.refresh()
                self.pg_cal.refresh_publish()
            self.update_steps()

        def progress(v, msg):
            self.prog.setValue(int(v))
            if msg:
                self.status.setText("%s \u2013 %s" % (title, msg))

        self.job = run_job("AlignPro: " + title, fn, finished, progress)
        self.set_enabled(True)

    # ============================================================ project
    def _fill_recent(self):
        self.recent_menu.clear()
        rec = dialogs.recent_projects()
        if not rec:
            a = self.recent_menu.addAction("(none)")
            a.setEnabled(False)
        for path in rec:
            nm = os.path.basename(path).replace(".dcsproj", "").replace(".alignpro.json", "")
            self.recent_menu.addAction(nm + "   \u2014   " + os.path.dirname(path), lambda p=path: self.open_project(p))

    def reopen_last(self):
        rec = dialogs.recent_projects()
        if rec and self.p is None and self.job is None:
            self.open_project(rec[0])

    def new_project(self):
        """A new DCS project in the job folder (Processing/<Name>/); AlignPro works in 02_AlignPro."""
        from dcs_core import dialogs as dd
        from qgis.PyQt.QtWidgets import QMessageBox
        dcs = dd.new_project(self, "AlignPro")
        if dcs is None:
            return
        from ..core import clraw
        found = [f for f in clraw.find_clraw([dcs.job_folder])
                 if os.sep + "Processing" + os.sep not in os.path.abspath(f)]
        if found and not dcs.capture_files():
            names = "\n".join(os.path.relpath(f, dcs.job_folder) for f in found[:12])
            more = "\n\u2026 and %d more" % (len(found) - 12) if len(found) > 12 else ""
            r = theme.msg_question(self, "AlignPro", "Found %d capture file(s) in the job folder:\n\n%s%s\n\n"
                                                     "Use them for this project?" % (len(found), names, more))
            if r == QMessageBox.StandardButton.Yes:
                dcs.set_capture_files(found, "AlignPro")
        elif not found:
            theme.msg_info(self, "AlignPro", "No .clraw captures were found in %s. Add them on the Data page."
                           % dcs.job_folder)
        Project.from_dcs(dcs)
        self.toast("Project %s created in %s" % (dcs.name, dcs.folder))
        self.open_project(dcs.path)

    def open_dialog(self):
        from dcs_core.project import SUFFIX
        f, _ = QFileDialog.getOpenFileName(self, "Open project", QSettings().value("AlignPro/last_dir", ""),
                                           "Processing project (*%s);;Older AlignPro project (*.alignpro.json)"
                                           % SUFFIX)
        if f:
            self.open_project(f)

    def open_project(self, path):
        """A .dcsproj (or its folder), 02_AlignPro/alignpro.json, or an older .alignpro.json."""
        try:
            p = Project.open_any(path)
        except Exception as e:  # noqa: BLE001
            warn(self, "Cannot open %s:\n%s" % (path, e))
            return False
        if p.dcs is not None and p.dcs.missing_captures():
            from dcs_core import dialogs as dd
            dd.check_raw(self, p.dcs, "AlignPro")
        dialogs.add_recent(p.project_path)
        QSettings().setValue("AlignPro/last_dir", os.path.dirname(p.project_folder))
        if self.p is not None:
            self.layers.remove_all()
        self.p = p
        self.chain.set_project(p.dcs)
        self.chain_w.setVisible(p.dcs is not None)
        self.set_enabled(True)
        self._set_pname(p.name)
        self.goto_page(0)
        self._run("Reading %s" % p.name, lambda prog, canc: (p.load(prog, canc), p)[1], self._loaded)
        if self.job is None:
            self.p = None
        return True

    def _loaded(self, p):
        self.p = p
        self.message("%s: %d lines, %d planar patches, trajectory %s" % (
            p.name, len(p.lines), len(p.P) if p.P is not None else 0, p.traj.source))
        for w in p.warnings:
            self.message(w, "warning")
        self.dv.set_project(p, keep=False)
        self.refresh_all(zoom=True, view=False)
        self.show_dataview("dock")

    def relocate_raw(self):
        if not self.p or not self.p.dcs:
            theme.msg_info(self, "AlignPro", "This is an older stand-alone AlignPro project; there is no job folder "
                                             "to relocate. Add the files again on the Data page.")
            return
        d = QFileDialog.getExistingDirectory(self, "New location of the job folder", self.p.dcs.job_folder)
        if not d:
            return
        still = self.p.dcs.relocate(d, "AlignPro")
        if still:
            theme.msg_warn(self, "AlignPro", "Not found under %s:\n%s" % (d, "\n".join(still[:6])))
            return
        self.toast("Raw data found in %s" % d)
        self.open_project(self.p.project_path)

    def save_qgis_project(self):
        if not self.p:
            return
        from qgis.core import QgsProject
        from qgis.PyQt.QtWidgets import QMessageBox
        qp = QgsProject.instance()
        target = self.p.dcs.qgis_project_path() if self.p.dcs else os.path.join(self.p.folder, self.p.name + ".qgz")
        if qp.fileName() and os.path.abspath(qp.fileName()) != os.path.abspath(target):
            r = theme.msg_question(self, "AlignPro", "The QGIS project is %s.\n\nSave it as %s instead?" % (
                qp.fileName(), target))
            if r != QMessageBox.StandardButton.Yes:
                return
        qp.setFileName(target)
        if qp.write():
            self.toast("QGIS project saved: %s" % target)
        else:
            theme.msg_warn(self, "AlignPro", "Could not save %s" % target)

    # ------------------------------------------------------------ the stage run (DCS)
    def _stage_run(self, fn, what):
        """fn inside the AlignPro stage run of the shared project (stage lock, running state, a failed
        run keeps the earlier results). Without a DCS project fn just runs."""
        p = self.p

        def work(prog, canc):
            if p.dcs is None:
                return fn(prog, canc)
            p.start_run_log(what)
            from ..core.project import VERSION
            with p.dcs.run("alignpro", "AlignPro", VERSION):
                return fn(prog, canc)
        return work

    def _publish_after(self, fn):
        """fn, then publish alignment.json for CloudPro (DCS projects)."""
        p = self.p

        def work(prog, canc):
            out = fn(prog, canc)
            if p.dcs is not None and p.lines:
                prog(0.95, "Publishing alignment.json")
                p.publish()
            return out
        return work

    def set_line_use(self, i, on):
        self.p.lines[i].use = on
        self.p.save()

        def fn(prog, canc):
            self.p.analyse()
        self._run("Pairs and surfaces", fn, lambda _: self.refresh_all())

    # ============================================================ refresh
    def refresh_all(self, zoom=False, view=True):
        if self.p is None:
            return
        self.layers.update_all(self.p, zoom)
        self.refresh_status()
        for pg in self.pages:
            try:
                pg.refresh()
            except Exception as e:  # noqa: BLE001
                log("refresh %s: %s" % (type(pg).__name__, e))
        if view:
            self.dv.set_project(self.p, keep=True)
        self.update_steps()
        self.set_enabled(True)

    def refresh_status(self):
        p = self.p
        try:
            self.avg_error = p.current_error()
        except Exception as ex:  # noqa: BLE001
            log(ex)
            self.avg_error = None
        e = self.avg_error
        col = theme.GREEN if e is not None and e < 1.0 else (theme.AMBER if e is not None and e < 2.5 else theme.RED)
        gs = getattr(p, "grid_summary", {}) or {}
        src = (getattr(p, "traj_info", {}) or {}).get("source", "realtime")
        tname = {"pospro": "POSPRO", "sbet": "SBET FILE"}.get(src, "REAL-TIME")
        m = p.cfg["mount"]
        self.kpi.setText(
            "<span style='font-family:%s; font-size:11px; color:%s; font-weight:600; letter-spacing:1px'>AVG ERROR</span>"
            "<br><span style='font-family:%s; font-size:18px; color:%s'>%s cm</span><br>"
            "<span style='font-family:%s; font-size:11px; color:%s'>SDEV %s \u00b7 SEP %s cm<br>%s \u00b7 LOCAL ENU<br>"
            "R %.4f<br>P %.4f<br>H %.4f</span>" % (
                theme.UI, theme.DIM, theme.MONO, col, fmt(e), theme.MONO, theme.TEXT, fmt(gs.get("avg_sdev_cm")),
                fmt(gs.get("avg_sep_cm")), tname, m[0], m[1], m[2]))
        self.kpi.setToolTip("Mean point-to-plane distance over the planar patches at the current angles; grid "
                            "standard deviation and line separation of the lowest surface per cell; trajectory; "
                            "current mounting angles.")

    def refresh_slices(self):
        """Boxes changed: show the current box again in the Data View."""
        if self.p is not None:
            self.dv._preset()

    def set_combi(self, row):
        """A combination row was selected: try its angles in the Data View."""
        r = (self.p.cfg.get("results") or {}) if self.p else {}
        rows = r.get("combis") or []
        if 0 <= row < len(rows) and rows[row].get("ok"):
            c = self.p.cfg
            self.dv.set_values([rows[row][k] for k in ("roll", "pitch", "heading")], c["lever"], c["latency_ms"])

    # ============================================================ actions
    def autopatch(self):
        self.goto_page(4)
        self.pg_cal.busy(True)

        def done(res):
            self.pg_cal.busy(False)
            a, s = res["advised"], res["sdev"]
            self.message("Auto calibration: advised %.4f / %.4f / %.4f (SDEV %.4f / %.4f / %.4f), error %.2f \u2192 %.2f cm" % (
                a["roll"], a["pitch"], a["heading"], s["roll"], s["pitch"], s["heading"],
                res["error_before_cm"], res["error_after_cm"]))
            self.toast("Calibration done: %.4f  %.4f  %.4f   (error %.2f \u2192 %.2f cm). Apply to publish it." % (
                a["roll"], a["pitch"], a["heading"], res["error_before_cm"], res["error_after_cm"]))
            for w in res.get("warnings", []):
                self.message(w, "warning")
            for pg in (self.pg_data, self.pg_pairs, self.pg_cal):
                pg.refresh()
            self.chain.refresh()
            # show the result straight away: advised as live trial values over the applied ones in grey
            self.dv.compare.setCurrentIndex(2)
            self.dv.load_values("advised")
            self.show_dataview("float")
        self._run("Auto calibration", self._stage_run(lambda prog, canc: self.p.calibrate(prog, canc),
                                                      "auto_calibration"), done)
        if self.job is None:
            self.pg_cal.busy(False)

    def apply_angles(self, angles):
        def done(_):
            self.message("Applied mounting angles %.4f / %.4f / %.4f" % tuple(angles))
            self.toast("Applied %.4f  %.4f  %.4f%s \u2013 copy them to the mapper when you are happy" % (
                tuple(angles) + (" and published for CloudPro" if self.p.dcs else "",)))
            self.refresh_all()
        self._run("Apply and publish", self._stage_run(self._publish_after(
            lambda prog, canc: self.p.apply_mount(angles)), "apply"), done)

    def apply_installation(self, lever, latency, mount):
        c = self.p.cfg
        c["lever"], c["latency_ms"], c["mount"] = list(lever), float(latency), list(mount)

        def fn(prog, canc):
            self.p.log("Applied lever %.4f %.4f %.4f m, latency %.3f ms, mounting %.4f %.4f %.4f" % (
                tuple(lever) + (latency,) + tuple(mount)))
            self.p.georeference()
            self.p.analyse()
            self.p.save()
        self._run("Apply and publish", self._stage_run(self._publish_after(fn), "apply"),
                  lambda _: (self.refresh_all(), self.toast("Applied and published for CloudPro" if self.p.dcs
                                                            else "Applied")))

    def analyze(self, key, center, rng, steps):
        self.pg_an.running(True)

        def done(pts):
            self.pg_an.running(False)
            self.pg_an.show_result(pts)
        self._run("Analyze", lambda prog, canc: self.p.sweep(key, center, rng, steps, prog, canc), done)
        if self.job is None:
            self.pg_an.running(False)
        else:
            self.job.taskTerminated.connect(lambda: self.pg_an.running(False))

    def adjust_apply(self, key, value):
        def done(_):
            self.message("Adjust: %s = %.4f" % (Project.PARAMS[key], value))
            if key == "latency_ms" or key.startswith("lever"):
                self.p.analyse()
            self.refresh_all()
        self._run("Adjust and publish", self._stage_run(self._publish_after(
            lambda prog, canc: self.p.set_value(key, value)), "adjust"), done)

    def settings_dialog(self):
        d = dialogs.SettingsDialog(self.p.settings, self)
        themed(d)
        if not d.exec():
            return
        ch = d.changed()
        if not ch:
            return
        self.p.settings.update(d.values())
        self.p.save()
        if ch & dialogs.RELOAD_KEYS or ch & dialogs.PASS_KEYS:
            self.p.cfg["boxes"] = {}
            self.p.save()
            self.open_project(self.p.path)
        elif ch == {"grid_cell_m"}:
            self.layers.update_grid(self.p)
            self.refresh_status()
        else:
            def fn(prog, canc):
                self.p.analyse()
            self._run("Pairs and surfaces", fn, lambda _: self.refresh_all())

    def _grid_attr(self):
        if self.p is not None and self.p.lines:
            self.layers.update_grid(self.p, self.grid_attr.currentData())

    # ================================================================ map tools
    def redraw_box(self, kind):
        if self.p is None:
            return
        crs = local_crs(self.p.frame)
        tool = DragLineTool(self.iface.mapCanvas(), crs, BOX_COLORS.get(kind, theme.AMBER))
        self._tool = tool

        def done(a, b):
            boxes = self.p.cfg.setdefault("boxes", {})
            old = boxes.get(kind, {})
            width = old.get("width", {"roll": 1.0, "pitch": 1.0, "heading": 4.0}.get(kind, 1.0))
            box = {"p0": list(a), "p1": list(b), "width": width,
                   "view": old.get("view", "plan" if kind == "heading" else "profile"),
                   "zmin": old.get("zmin"), "zmax": old.get("zmax")}
            if kind == "profile":
                self._profile(box)
            else:
                boxes[kind] = box
                self.p.save()
                self.layers.update_boxes(self.p)
                self.refresh_slices()
            self.iface.mapCanvas().unsetMapTool(tool)
            if self.prev_tool:
                self.iface.mapCanvas().setMapTool(self.prev_tool)
        tool.done.connect(done)
        self.prev_tool = self.iface.mapCanvas().mapTool()
        self.iface.mapCanvas().setMapTool(tool)
        self.toast("Drag the %s box on the map: press at the start, release at the end" % kind)

    def _profile(self, box):
        d = QDialog(self)
        d.setWindowTitle("AlignPro \u2013 Profile")
        d.resize(960, 440)
        lay = QVBoxLayout(d)
        row = QHBoxLayout()
        wsp = QDoubleSpinBox()
        wsp.setRange(0.1, 50)
        wsp.setValue(1.0)
        wsp.setSuffix(" m")
        plan = QCheckBox("Plan view")
        k = QLabel("CORRIDOR WIDTH")
        k.setObjectName("ppKey")
        row.addWidget(k)
        row.addWidget(wsp)
        row.addWidget(plan)
        row.addStretch(1)
        lay.addLayout(row)
        v = SliceView("profile")
        v.btn.hide()
        lay.addWidget(v, 1)

        def upd():
            box["width"] = wsp.value()
            data = self.p.slice(box)
            pl = plan.isChecked()
            v.set_points([(self.p.lines[i].color, al, ac if pl else z) for i, al, ac, z in data], pl)
        wsp.valueChanged.connect(upd)
        plan.toggled.connect(upd)
        themed(d)
        upd()
        d.show()

    def auto_boxes(self):
        self.p.cfg["boxes"] = engine.auto_boxes(self.p.lines, self.p.P)
        self.p.save()
        self.layers.update_boxes(self.p)
        self.refresh_slices()

    def _slice_cursor(self, kind, along):
        b = (self.p.cfg.get("boxes") or {}).get(kind) if self.p else None
        if not b:
            return
        p0, p1 = np.array(b["p0"]), np.array(b["p1"])
        L = np.linalg.norm(p1 - p0)
        if L < 1e-6:
            return
        pt = p0 + (p1 - p0) / L * along
        self.cursor.show(float(pt[0]), float(pt[1]), local_crs(self.p.frame), kind)
        QTimer.singleShot(1500, self.cursor.hide)

    def draw_aoi(self):
        tool = PolygonTool(self.iface.mapCanvas())
        self._tool = tool

        def done(ring):
            self.p.cfg["aoi"] = ring
            self.p.settings["use_aoi"] = True
            self.p.save()
            self.iface.mapCanvas().unsetMapTool(tool)

            def fn(prog, canc):
                self.p.analyse()
            self._run("Area of interest", fn, lambda _: self.refresh_all())
        tool.done.connect(done)
        self.iface.mapCanvas().setMapTool(tool)
        self.toast("Click around the target on the map; right-click to finish")

    def clear_aoi(self):
        self.p.cfg["aoi"] = None
        self.p.settings["use_aoi"] = False
        self.p.save()

        def fn(prog, canc):
            self.p.analyse()
        self._run("Area of interest", fn, lambda _: self.refresh_all())

    # ================================================================ output
    def make_report(self, pdf_path, header, shots, clip):
        images = []
        if shots:
            d = self.p.work
            mp = os.path.join(d, "shot_map.png")
            self.iface.mapCanvas().saveAsImage(mp)
            images.append(("Chart view", mp))
            for k, pm in self.dv.grab_views().items():
                fp = os.path.join(d, "shot_view_%s.png" % k)
                pm.save(fp)
                images.append(("Data view \u2013 %s" % k, fp))
            boxes = self.p.cfg.get("boxes") or {}
            for k in ("roll", "pitch", "heading"):
                b = boxes.get(k)
                if not b:
                    continue
                fp = os.path.join(d, "shot_%s.png" % k)
                tmp = SliceView(k)
                tmp.btn.hide()
                plan = b.get("view") == "plan"
                data = self.p.slice(b)
                tmp.set_points([(self.p.lines[j].color, al, ac if plan else z) for j, al, ac, z in data], plan,
                               "applied angles")
                tmp.resize(1400, 620)
                tmp.grab().save(fp)
                tmp.deleteLater()
                images.append(("%s box" % k.capitalize(), fp))
        html, txt = report.html_report(self.p, images)
        if not header:
            txt = report.ascii_report(self.p, header=False)
        txt_path = os.path.splitext(pdf_path)[0] + ".txt"
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(txt)
        try:
            _write_pdf(html, pdf_path)
        except Exception as e:  # noqa: BLE001
            warn(self, "PDF not written (%s). The ASCII report is at %s" % (e, txt_path))
            return False
        if clip:
            QGuiApplication.clipboard().setText(txt)
        self.message("Report written: %s" % pdf_path)
        self.update_steps()
        if ask(self, "Report written:\n%s\n\nOpen the PDF?" % pdf_path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(pdf_path))
        return True

    def export_las(self):
        crs_def = "EPSG:2953"
        hw = self.p.lines[0].info.get("hardware") or {}
        cs = hw.get("coordinate_system") or {}
        if cs.get("definition"):
            crs_def = cs["definition"]
        path, _ = QFileDialog.getSaveFileName(self, "Export corrected points", os.path.join(
            self.p.folder, "%s_corrected.las" % self.p.name), "LAS (*.las)")
        if not path:
            return
        if not QgsCoordinateReferenceSystem(crs_def).isValid():
            warn(self, "Coordinate system %s is not valid." % crs_def)
            return

        def fn(prog, canc):
            from osgeo import osr
            from ..core.las import write_las
            src = osr.SpatialReference()
            src.ImportFromEPSG(4979)
            src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            dst = osr.SpatialReference()
            dst.SetFromUserInput(crs_def)
            dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            tr = osr.CoordinateTransformation(src, dst)
            xs, ys, zs, it, sid, tt = [], [], [], [], [], []
            used = [ln for ln in self.p.lines if ln.use]
            for k, ln in enumerate(used):
                lat, lon, h = self.p.frame.to_geodetic(ln.x)
                pts = np.column_stack([np.degrees(lon), np.degrees(lat), h])
                out = np.array(tr.TransformPoints(pts.tolist()))
                xs.append(out[:, 0])
                ys.append(out[:, 1])
                zs.append(out[:, 2])
                it.append(ln.data["i"][ln.idx])
                sid.append(np.full(len(out), ln.info["line"]))
                tt.append(ln.data["t"][ln.idx] - 315964800.0 + 18 - 1e9)   # adjusted standard GPS time
                prog((k + 1) / len(used), ln.name)
            write_las(path, np.concatenate(xs), np.concatenate(ys), np.concatenate(zs), np.concatenate(it),
                      np.concatenate(sid), np.concatenate(tt), dst.GetAuthorityCode(None))
            return path
        self._run("Export LAS", fn, lambda p: self.toast("Exported %s (ellipsoidal heights, %s)" % (
            os.path.basename(p), crs_def)))

    def explore(self):
        if self.p is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.p.project_folder))

    def publish_now(self):
        if self.p is None or self.p.dcs is None:
            return
        self._run("Publish", self._stage_run(lambda prog, canc: self.p.publish(), "publish"),
                  lambda _: self.toast("alignment.json published for CloudPro"))

    def reset_project(self):
        if not ask(self, "Reset the project to the angles, lever arm and settings recorded in the .clraw files?"):
            return
        self._run("Reset", lambda prog, canc: self.p.reset(), lambda _: self.refresh_all())

    def cleanup(self):
        """Called by the plugin's unload (also when QGIS closes): stop everything AlignPro started and
        release the Data View window safely (dcs_core.window)."""
        for t in (getattr(self, "_chain_timer", None), getattr(getattr(self, "dv", None), "_timer", None)):
            try:
                if t is not None:
                    t.stop()
            except RuntimeError:
                pass
        try:
            if self.job is not None:
                self.job.cancel()
        except RuntimeError:
            pass
        try:
            canvas = self.iface.mapCanvas()
            tool = canvas.mapTool()
            if tool is not None and tool is getattr(self, "_tool", None):
                canvas.unsetMapTool(tool)
        except RuntimeError:
            pass
        try:
            self.cursor.hide()
        except RuntimeError:
            pass
        try:
            self.layers.remove_all()
        except RuntimeError:
            pass
        v = getattr(self, "view_dock", None)
        if alive(v):
            v.release_window()
        self.view_dock = None


def _write_pdf(html, path):
    from qgis.PyQt.QtCore import QMarginsF, QSizeF
    from qgis.PyQt.QtGui import QPageLayout, QPageSize, QPdfWriter
    doc = QTextDocument()
    doc.setDefaultFont(QFont("Arial", 9))
    doc.setBaseUrl(QUrl.fromLocalFile(os.path.dirname(path) + os.sep))
    doc.setHtml(html)
    w = QPdfWriter(path)
    w.setPageLayout(QPageLayout(QPageSize(QPageSize.PageSizeId.A4), QPageLayout.Orientation.Portrait,
                                QMarginsF(15, 15, 15, 15), QPageLayout.Unit.Millimeter))
    w.setResolution(150)
    doc.setPageSize(QSizeF(w.pageLayout().paintRectPixels(w.resolution()).size()))
    doc.print(w) if hasattr(doc, "print") else doc.print_(w)
