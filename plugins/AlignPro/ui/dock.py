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
from .widgets import StepButton, Toast
from dcs_core.header import PluginHeader
from dcs_core.window import PluginWindow, alive


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
        super().__init__(iface, "alignpro", "AlignPro", parent, size=(1400, 950),
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
        self._pending = []              # actions waiting for the stopped full-density read to end
        self._dense_stopped = False
        self._fine_job = False
        self._tool = None
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

        # header (DCS Core, the same in every plugin): logo, name, workflow boxes, project box, window, guide
        self.header = PluginHeader("alignpro", "AlignPro", window=self, guide=self.open_guide)
        self.header.stepClicked.connect(self._phase_clicked)
        mm = self.header.set_project_menu(DarkMenu(self.header.project_btn))
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
        mm.addAction("Open the Data View", lambda: self.show_dataview("open"))
        mm.addSeparator()
        self.a_guide = mm.addAction("User guide (this version)", self.open_guide)
        mm.addAction("Reset window", self.reset_window)
        self.pbox = self.header.project_btn
        self.wbtn = self.header.window_btn
        self.hbtn = self.header.help_btn
        self.chain = self.header.chain
        self.chain_w = self.chain               # always shown (AlignPro 1.3)
        outer.addWidget(self.header)
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
        # marine jobs (sonar recordings from the survey boat): one page instead of the Lidar workflow
        from .marinepage import MarinePage
        self._marine = False
        self._side = side
        self.marine_page = MarinePage(self)
        self._marine_scroll = _scroll(self.marine_page)
        self.stack.addWidget(self._marine_scroll)
        self.b_view = QPushButton("CHECK / FINE TUNE")
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
        for pg in self.pages + [self.pg_welcome, self.marine_page]:
            theme.mapperize(pg)
            for cb in pg.findChildren(QComboBox):
                cb.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
                cb.setMinimumContentsLength(12)
        body.addWidget(side)
        content = QVBoxLayout()
        content.setContentsMargins(10, 4, 10, 4)
        content.addWidget(self.stack, 1)
        from dcs_core.stepnav import StepNav
        self.nav = StepNav(STEPS, self.goto_page, optional={"Analyze"}, stage="alignpro")
        content.addWidget(self.nav)
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
        self.dv.autocal_requested.connect(self.autopatch)
        self.dv.loader = self._load_fine
        self.dv.area_changed.connect(self._view_area)
        self.dv.boxes_edited.connect(self._box_edited)
        self.dv.extent_requested.connect(self._map_extent)

    def _refresh_chain(self):
        if self.isVisible() and self.p is not None and self.p.dcs is not None and self.job is None:
            self.chain.refresh()
            self.pg_cal.refresh_publish()
            self.update_nav()

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
        if getattr(self, "_marine", False) and has:
            busy = self.job is not None
            self.stack.setCurrentWidget(self._marine_scroll)
            self.marine_page.setEnabled(not busy)
            for w in (self.a_folder, self.a_qgz):
                w.setEnabled(True)
            self.a_reloc.setEnabled(self.p.dcs is not None)
            for w in (self.a_reset, self.b_view, self.dv):
                w.setEnabled(False)
            self.update_nav()
            return
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
        self.dv.setEnabled(loaded and (not busy or getattr(self, "_fine_job", False)))
        self.update_nav()

    def goto_page(self, k):
        if getattr(self, "_marine", False) and self.p is not None:
            self.stack.setCurrentWidget(self._marine_scroll)
            self.marine_page.refresh()
            self.update_nav()
            return
        self.stack.setCurrentIndex(k)
        self.step_buttons[k].setChecked(True)
        if self.p is not None:
            try:
                self.pages[k].refresh()
            except Exception as e:  # noqa: BLE001
                log("page refresh: %s" % e)
        self.update_nav()

    def update_steps(self):
        if getattr(self, "_marine", False):
            self.update_header_steps()
            return
        for pg, sb in zip(self.pages, self.step_buttons):
            try:
                st, sub = pg.state()
            except Exception:  # noqa: BLE001
                st, sub = "todo", ""
            sb.set_state(st, sub)
        self.update_nav()

    # the header's steps (DCS Core 1.11.0): the seven pages in three phases
    PHASES = [("prepare", "Prepare", [0, 1, 2]), ("calibrate", "Calibrate", [3, 4]), ("report", "Report", [5, 6])]

    def _phase_clicked(self, key):
        for k, _l, pages in self.PHASES:
            if k == key and self.p is not None:
                self.goto_page(pages[0])

    def update_header_steps(self):
        from dcs_core.stepnav import crumbs
        if self.p is None:
            self.header.set_steps([])
            return
        if getattr(self, "_marine", False):
            st = self.marine_page.state()[0]
            if self.job is not None:
                st = "running"
            items, cur = crumbs([("marine", "Marine", [0])], ["Sonar patch test"], [(st, "")], 0)
            self.header.set_steps(items, cur)
            return
        states = []
        for pg in self.pages:
            try:
                states.append(pg.state())
            except Exception:       # noqa: BLE001
                states.append(("todo", ""))
        k = self.stack.currentIndex()
        if self.job is not None and k < len(states):
            states[k] = ("running", states[k][1])
        items, cur = crumbs(self.PHASES, STEPS, states, k, optional={"Analyze", "Report"})
        self.header.set_steps(items, cur)

    def update_nav(self):
        """Back / Next under the pages: is the current step done, what is next."""
        if not hasattr(self, "nav"):
            return
        self.update_header_steps()
        k = self.stack.currentIndex()
        if getattr(self, "_marine", False) and self.p is not None:
            self.nav.setVisible(False)
            return
        if self.p is None or k >= len(self.pages):
            self.nav.setVisible(False)             # the welcome page: open or create a project first
            return
        self.nav.setVisible(True)
        try:
            st, sub = self.pages[k].state()
        except Exception:           # noqa: BLE001
            st, sub = "todo", ""
        if self.job is not None:
            st = "running"
        todo = None
        if k == 0 and not self.p.lines:
            st, sub = "todo", sub or "add .clraw captures"
            todo = "add the survey lines' .clraw (ADD FILES\u2026 or ADD FOLDER\u2026), then READ FILES"
        elif k == 3:
            todo = "the lines need more overlap: drive each line both ways (see the patch-test procedure)"
        elif k == 4:
            todo = "press AUTO CALIBRATE, then APPLY ADVISED to finalize the result for CloudPro"
        elif k == 5:
            todo = "ANALYZE latency, lever arm and angles, or skip"
        elif k == 6:
            todo = "CREATE REPORT (PDF, ASCII), or skip"
        if k == 4 and st == "warn" and "apply" in sub.lower():
            sub = "press APPLY ADVISED (or FINALIZE THE APPLIED VALUES) for CloudPro"
        self.nav.update(k, st, sub, todo=todo)
        dcs = self.p.dcs
        if dcs is None:
            self.nav.set_continue("cloudpro", False, "This older stand-alone project is not part of a job.")
            return
        try:
            pst, why = dcs.state("alignpro")
        except Exception:           # noqa: BLE001
            pst, why = "none", []
        if pst == "done":
            self.nav.set_continue("cloudpro", True, "", dcs.path)
        elif pst == "stale":
            self.nav.set_continue("cloudpro", False, "Out of date (%s): calibrate again and Apply." % "; ".join(why))
        else:
            self.nav.set_continue("cloudpro", False, "Apply the calibration (step 5: APPLY ADVISED) to finalize it for CloudPro.")

    def _set_pname(self, name):
        self.header.set_project_name(name)

    def _busy(self, on):
        self.header.set_busy(on)

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
    def _when_free(self, fn, what):
        """Run fn now; while the background full-density read runs, stop it and run fn when it has ended.
        Returns False (fn not run, nothing changed) while another job runs."""
        if self.job is None:
            fn()
            return True
        if self._fine_job:
            self._pending.append(fn)
            self._dense_stopped = True
            try:
                self.job.cancel()
            except RuntimeError:
                pass
            self.status.setText("Stopping the full-density read, then %s\u2026" % what)
            return True
        self.toast("Wait for \u201c%s\u201d to finish (or stop it); %s was not done." % (
            self.job.description().replace("AlignPro: ", ""), what), "warn")
        return False

    def _run(self, title, fn, done, failed=None, warm=True):
        if self.job is not None:
            self.toast("Wait for the current job to finish (or stop it).", "warn")
            return False
        if warm:                # the error the window shows is computed here, not on the GUI thread
            p, inner = self.p, fn

            def fn(prog, canc):
                out = inner(prog, canc)
                try:
                    if p is not None and p.lines and p.P is not None and len(p.P):
                        p.current_error()
                except Exception as e:  # noqa: BLE001
                    log("error now: %s" % e)
                return out
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
                if failed is not None:
                    failed(err)
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
                if not self._marine:
                    self.pg_cal.refresh_publish()
            self.update_steps()
            pend, self._pending = self._pending, []
            for f in pend:
                f()
            if self._dense_stopped and self.job is None and self.p is not None and self.p.lines:
                self._dense_stopped = False
                self.dv.reload_region()          # the stopped full-density read, again

        def progress(v, msg):
            self.prog.setValue(int(v))
            if msg:
                self.status.setText("%s \u2013 %s" % (title, msg))

        self.job = run_job("AlignPro: " + title, fn, finished, progress)
        self.set_enabled(True)
        return True

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
        found = clraw.split_navigation([f for f in clraw.find_clraw([dcs.job_folder])
                                        if os.sep + "Processing" + os.sep not in os.path.abspath(f)])[0]
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
        """A .dcsproj (or its folder), 02_AlignPro/alignpro.json, or an older .alignpro.json. A running job is
        stopped first and the project opens when it has ended (nothing of the current project changes before)."""
        if self.job is not None:
            self._pending.append(lambda: self.open_project(path))
            self._dense_stopped = False
            try:
                self.job.cancel()
            except RuntimeError:
                pass
            self.status.setText("Stopping \u201c%s\u201d, then opening %s\u2026" % (
                self.job.description().replace("AlignPro: ", ""), os.path.basename(path)))
            return True
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
        from ..core import marine
        self._marine = p.dcs is not None and marine.is_marine(p.dcs)
        self._side.setVisible(not self._marine)
        if self._marine:
            self._set_pname(p.name)
            self.set_enabled(True)
            self.goto_page(0)
            self.message("%s: marine job (sonar recordings): the sonar patch test" % p.name)
            return True
        self.set_enabled(True)
        self._set_pname(p.name)
        self.goto_page(0)
        self._run("Reading %s" % p.name, lambda prog, canc: (p.load(prog, canc), p.find_system(), p)[-1],
                  self._loaded)
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
        QTimer.singleShot(0, self.suggest_profile)

    # -------------------------------------------------------------- system profiles
    def _apply_profile(self, secs, prof):
        """Use a system profile's Lidar values. Returns False when another job runs (nothing changed; DCS Core's
        profiles_ui then does not record the profile as applied)."""
        li = secs.get("lidar") or {}
        if not self.p or not li:
            return False
        p = self.p
        c = p.cfg
        lever = [float(x) for x in (li.get("lever_m") or c["lever"])]
        mount = [float(x) for x in (li.get("mount_deg") or c["mount"])]
        lat = float(li.get("latency_ms") if li.get("latency_ms") is not None else c.get("latency_ms", 0.0))

        def go():
            if self.p is not p:
                return
            p.log("System profile %s used: lever %.4f %.4f %.4f m, mounting %.4f %.4f %.4f, latency %.3f ms" % (
                tuple([prof["name"]] + lever + mount + [lat])))
            # the profile's mounting becomes this job's starting mounting (shared with CloudPro)
            p.share_mounting(mount, "system profile %s" % prof["name"])
            self._apply_values(lever, lat, mount)
        return bool(self._when_free(go, "applying system profile %s" % prof["name"]))

    def open_profiles(self):
        if not self.p:
            return
        from dcs_core import profiles_ui
        from ..core.project import VERSION
        profiles_ui.open_dialog(self, "AlignPro", self.p.cfg.get("system") or {}, self.p.profile_current(),
                                ("lidar",), self._apply_profile, VERSION)

    def suggest_profile(self):
        if not self.p or not self.p.lines:
            return
        from dcs_core import profiles_ui
        ans, rec = profiles_ui.suggest(self, "AlignPro", self.p.cfg.get("system") or {},
                                       self.p.profile_current(), ("lidar",), self._apply_profile,
                                       self.p.cfg.get("profiles_asked"))
        if ans == "not_applied":           # busy: nothing changed, not recorded; offered again when the job ends
            p = self.p
            self._pending.append(lambda: self.p is p and self.suggest_profile())
            self.message("The system profile was not applied while a job runs; it is offered again when the job "
                         "has finished.", "warning")
        if rec != self.p.cfg.get("profiles_asked"):
            self.p.cfg["profiles_asked"] = rec
            self.p.save()
        if ans == "open":
            self.open_profiles()

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
            try:
                with p.dcs.run("alignpro", "AlignPro", VERSION):
                    return fn(prog, canc)
            finally:
                p.end_run_log()
        return work

    def _publish_after(self, fn):
        """fn, then publish alignment.json for CloudPro (DCS projects)."""
        p = self.p

        def work(prog, canc):
            out = fn(prog, canc)
            if p.dcs is not None and p.lines:
                prog(0.95, "Finalizing alignment.json")
                p.publish()
            return out
        return work

    def focus_cloud_pair(self, target):
        """The Pairs page with CloudPro's pair (by line names, else line numbers) chosen in "From the point cloud".
        While a job is stopping for a project change this waits until the project is open."""
        if self.job is not None and self._pending:
            self._pending.append(lambda: self.focus_cloud_pair(target))
            return False
        if self.p is None:
            return False
        k = self.pages.index(self.pg_pairs)
        self.goto_page(k)
        pg = self.pg_pairs
        doc = pg._cloud_doc()
        if doc is None:
            return False

        def same(a, b):
            return [str(x).strip().lower() for x in a] == [str(x).strip().lower() for x in b]
        want_n, want_l = target.get("lines") or [], target.get("line_numbers") or []
        for i, q in enumerate(doc["pairs"]):
            if (want_n and same(q.get("lines", []), want_n)) or \
                    (want_l and None not in want_l and same(q.get("line_numbers", []), want_l)):
                pg.cloud_tbl.selectRow(i)
                pg.cloud_tbl.scrollToItem(pg.cloud_tbl.item(i, 0))
                return True
        return False

    def use_cloud_pair(self, k):
        """Use a pair CloudPro flagged as patch-test material: only its two lines, and an area of interest along the
        stretch they share (AlignPro 1.8.0)."""
        from ..core import cloudcheck
        p = self.p
        doc = cloudcheck.load(p) if p is not None else None
        if doc is None or not (0 <= k < len(doc["pairs"])):
            return
        pair = doc["pairs"][k]
        idx = cloudcheck.lines_for(p, pair)
        if len(idx) < 2:
            self.toast("%s are not among this job's lines here" % " and ".join(pair.get("lines", [])), "warn")
            return
        ring = cloudcheck.corridor(p, pair)

        def go():
            if self.p is not p:
                return
            for i, ln in enumerate(p.lines):
                ln.use = i in idx
            if ring:
                p.cfg["aoi"] = ring
                p.settings["use_aoi"] = True
            p.save()

            def fn(prog, canc):
                p.analyse()
            self._run("Pairs and surfaces", fn, lambda _: (self.refresh_all(), self.dv.reload("aoi" if ring else None),
                                                          self.toast("Using %s and the stretch they share"
                                                                     % " / ".join(pair.get("lines", [])))))
        self._when_free(go, "using the lines from the point cloud")

    def set_line_use(self, i, on):
        p = self.p

        def go():
            if self.p is not p:
                return
            p.lines[i].use = on
            p.save()

            def fn(prog, canc):
                p.analyse()
            self._run("Pairs and surfaces", fn, lambda _: self.refresh_all())
        if not self._when_free(go, "changing the lines used"):
            self.pg_data.fill_lines()          # show the line as it still is

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

    def refresh_slices(self, kind=None):
        """Boxes changed: the Data View loads the area around them (kind: the box just drawn)."""
        if self.p is not None:
            self.dv.reload(kind)

    # ------------------------------------------------------- data view area
    def _load_fine(self, region):
        """Full-density points inside the inspection boxes, read in the background."""
        if self.p is None or self.dv.region is None or self.dv.region["sig"] != region["sig"]:
            return
        if self.job is not None:
            QTimer.singleShot(500, lambda: self._load_fine(region))
            return
        p = self.p

        def fn(prog, canc):
            return p.dense_points(region["boxes"], prog, canc)

        def done(res):
            self._fine_job = False
            self.dv.set_dense(region, res)
        def failed(err):
            self._fine_job = False
            self.dv.set_dense(region, None, "stopped" if err == "Cancelled" else err.split("\n")[0])
        self._fine_job = True
        if not self._run("Full-density points", fn, done, failed, warm=False):
            self._fine_job = False

    def _view_area(self, region):
        if self.p is not None:
            try:
                self.layers.update_view_area(self.p, region)
            except Exception as e:  # noqa: BLE001
                log("view area layer: %s" % e)

    def _box_edited(self, kind):
        """A box redrawn or resized in the Data View."""
        if self.p is None:
            return
        self.p.save()
        self.layers.update_boxes(self.p)
        for pg in self.pages:
            if hasattr(pg, "refresh_boxes"):
                pg.refresh_boxes()
        self.dv.reload(kind)

    def _map_extent(self):
        """Load map extent: the QGIS map's visible area, in the project's local frame."""
        if self.p is None or not self.p.lines:
            return
        from qgis.core import QgsCoordinateTransform, QgsProject
        canvas = self.iface.mapCanvas()
        src = canvas.mapSettings().destinationCrs()
        dst = local_crs(self.p.frame)
        ext = canvas.extent()
        try:
            ext = QgsCoordinateTransform(src, dst, QgsProject.instance()).transformBoundingBox(ext)
        except Exception as e:  # noqa: BLE001
            self.message("Map extent: %s" % e, "warning")
            return
        c = ((ext.xMinimum() + ext.xMaximum()) / 2, (ext.yMinimum() + ext.yMaximum()) / 2)
        half = max(ext.width(), ext.height()) / 2
        if half > 150:
            self.toast("The map shows more than 300 m; loading the central 300 m. Zoom in for a closer area.", "warn")
        self.dv.load_area(c, half, "map extent")

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
            self.toast("Calibration done: %.4f  %.4f  %.4f   (error %.2f \u2192 %.2f cm). Not in use yet: "
                       "Apply advised & finalize." % (
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
        if not self._run("Auto calibration", self._stage_run(lambda prog, canc: self.p.calibrate(prog, canc),
                                                             "auto_calibration"), done,
                         lambda _err: self.pg_cal.busy(False)):
            self.pg_cal.busy(False)

    def apply_angles(self, angles):
        p = self.p

        def done(_):
            self.message("Applied mounting angles %.4f / %.4f / %.4f" % tuple(angles))
            self.toast("Applied %.4f  %.4f  %.4f%s \u2013 copy them to the mapper when you are happy" % (
                tuple(angles) + (" and finalized for CloudPro" if p.dcs else "",)))
            self.refresh_all()

        def go():
            if self.p is p:
                self._run("Apply and finalize", self._stage_run(self._publish_after(
                    lambda prog, canc: p.apply_mount(angles)), "apply"), done)
        return self._when_free(go, "applying the angles")

    def apply_installation(self, lever, latency, mount):
        """Apply lever arm, latency and mounting (re-georeference, surfaces, publish). Nothing changes while
        another job runs (returns False); the background full-density read is stopped first."""
        p = self.p
        return self._when_free(lambda: self.p is p and self._apply_values(lever, latency, mount),
                               "applying the values")

    def _apply_values(self, lever, latency, mount):
        p = self.p
        c = p.cfg
        c["lever"], c["latency_ms"], c["mount"] = list(lever), float(latency), list(mount)

        def fn(prog, canc):
            p.log("Applied lever %.4f %.4f %.4f m, latency %.3f ms, mounting %.4f %.4f %.4f" % (
                tuple(lever) + (latency,) + tuple(mount)))
            p.georeference()
            p.analyse()
            p.save()
        return self._run("Apply and finalize", self._stage_run(self._publish_after(fn), "apply"),
                         lambda _: (self.refresh_all(), self.toast("Applied and finalized for CloudPro" if p.dcs
                                                                   else "Applied")))

    def analyze(self, key, center, rng, steps):
        self.pg_an.running(True)

        def done(pts):
            self.pg_an.running(False)
            self.pg_an.show_result(pts)

        def failed(_err):
            self.pg_an.running(False)
        if not self._run("Analyze", lambda prog, canc: self.p.sweep(key, center, rng, steps, prog, canc), done,
                         failed):
            self.pg_an.running(False)

    def adjust_apply(self, key, value):
        p = self.p

        def done(_):
            self.message("Adjust: %s = %.4f" % (Project.PARAMS[key], value))
            self.refresh_all()

        def fn(prog, canc):
            p.set_value(key, value)
            if key == "latency_ms" or key.startswith("lever"):
                p.analyse()                  # in the job (not on the GUI thread), before publishing
                p.save()
        self._run("Adjust and finalize", self._stage_run(self._publish_after(fn), "adjust"), done)

    def settings_dialog(self):
        d = dialogs.SettingsDialog(self.p.settings, self)
        themed(d)
        if not d.exec():
            return
        self.change_settings(d.values())

    def memory_check(self, settings):
        """(level, text) for these settings on this computer: ok | warn | refuse (core/memory.py)."""
        from ..core import memory
        p = self.p
        n = len(getattr(p, "datas", None) or []) or len({l.info.get("capture", l.name) for l in p.lines}) or 6
        s = dict(p.settings)
        s.update(settings)
        return memory.check(s, n)

    def change_settings(self, values):
        """New settings: re-read the lines, re-split the passes or only rebuild the surfaces, as needed.
        Returns False when refused (the settings would not fit in this computer's memory)."""
        ch = {k for k, v in values.items() if v != self.p.settings.get(k)}
        if not ch:
            return True
        level, txt = self.memory_check(values)
        if level == "refuse":
            from ..core import memory
            cur = memory.estimate(self.p.settings, 1)["total"]
            new = memory.estimate(dict(self.p.settings, **values), 1)["total"]
            if new > cur:
                warn(self, "These point densities need %s, more than this computer can hold. They were not "
                           "applied: choose a lower density (or lower the points kept per line, points per patch "
                           "or patches in Settings)." % txt)
                return False
        elif level == "warn":
            self.message("These point densities need %s; other programs may slow down while AlignPro "
                         "calibrates." % txt, "warning")
        if self.job is not None and not self._fine_job and not (ch & dialogs.RELOAD_KEYS or ch & dialogs.PASS_KEYS):
            self.toast("Wait for the current job to finish (or stop it); the settings were not changed.", "warn")
            return False
        self.p.settings.update(values)
        self.p.save()
        if ch & dialogs.RELOAD_KEYS or ch & dialogs.PASS_KEYS:
            self.p.cfg["boxes"] = {}
            self.p.save()
            self.open_project(self.p.path)
        elif ch == {"grid_cell_m"}:
            self.layers.update_grid(self.p)
            self.refresh_status()
        else:
            p = self.p

            def go():
                def fn(prog, canc):
                    p.analyse()
                self._run("Pairs and surfaces", fn, lambda _: self.refresh_all())
            self._when_free(go, "finding the surfaces again")
        return True

    def _grid_attr(self):
        if self.p is not None and self.p.lines:
            self.layers.update_grid(self.p, self.grid_attr.currentData())

    # ================================================================ map tools
    def redraw_box(self, kind):
        if self.p is None:
            return
        crs = local_crs(self.p.frame)
        tool = DragLineTool(self.iface.mapCanvas(), crs, BOX_COLORS.get(kind, theme.AMBER))
        self._set_tool(tool)

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
                self.refresh_slices(kind)
            self._end_tool(tool)
        tool.done.connect(done)
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
        self._set_tool(tool)
        p = self.p

        def done(ring):
            self._end_tool(tool)

            def go():
                if self.p is not p:
                    return
                p.cfg["aoi"] = ring
                p.settings["use_aoi"] = True
                p.save()

                def fn(prog, canc):
                    p.analyse()
                self._run("Area of interest", fn, lambda _: (self.refresh_all(), self.dv.reload("aoi")))
            self._when_free(go, "using the area of interest")
        tool.done.connect(done)
        self.iface.mapCanvas().setMapTool(tool)
        self.toast("Click around the target on the map; right-click to finish")

    def clear_aoi(self):
        p = self.p

        def go():
            p.cfg["aoi"] = None
            p.settings["use_aoi"] = False
            p.save()

            def fn(prog, canc):
                p.analyse()
            self._run("Area of interest", fn, lambda _: (self.refresh_all(), self.dv.reload(None)))
        self._when_free(go, "clearing the area of interest")

    def _set_tool(self, tool):
        """A map tool of AlignPro's becomes the canvas tool; the previous one is restored when it ends."""
        canvas = self.iface.mapCanvas()
        cur = canvas.mapTool()
        if self._tool is not None:
            self._end_tool(self._tool, restore=False)
        if cur is not None and cur is not self._tool:
            self.prev_tool = cur
        self._tool = tool

    def _end_tool(self, tool, restore=True):
        canvas = self.iface.mapCanvas()
        try:
            if canvas.mapTool() is tool:
                canvas.unsetMapTool(tool)
            tool.release()                     # its rubber band leaves the canvas
        except RuntimeError:
            pass
        if self._tool is tool:
            self._tool = None
        if restore and self.prev_tool is not None:
            try:
                canvas.setMapTool(self.prev_tool)
            except RuntimeError:
                pass
            self.prev_tool = None

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

    # ============================================================ marine jobs
    def run_marine_solve(self):
        """The sonar patch test (core.marine.solve) on a worker thread; the result goes into cfg["marine"]."""
        p = self.p
        if p is None or p.dcs is None:
            return
        from ..core import marine
        m = p.cfg.setdefault("marine", {})
        every, power, lat = int(m.get("every", 2)), m.get("power", "medium"), bool(m.get("latency", False))

        def fn(prog, canc):
            prog(0.02, "reading the soundings")
            data = marine.load(p.dcs, every=every, power=power)
            names = {str(d): "%s%s" % (h.get("nickname") or d, " (%s)" % h["side"] if h.get("side") else "")
                     for d, h in data["heads"].items()}
            res = marine.solve(data, latency=lat, progress=lambda f: prog(0.1 + 0.9 * f, "solving"),
                               cancelled=canc, log=p.log)
            return res, names, {str(d): h.get("side") for d, h in data["heads"].items()}

        def done(out):
            res, names, looks = out
            m["result"] = res
            m["names"] = names
            m["looks"] = looks
            m.pop("applied", None)
            p.save()
            self.marine_page.refresh()
            self.toast("Patch test: %.1f cm → %.1f cm" % (res["before_cm"], res["after_cm"]))
        self._run("Sonar patch test", fn, done, warm=False)

    def run_marine_publish(self):
        p = self.p
        if p is None or p.dcs is None or not (p.cfg.get("marine") or {}).get("result"):
            return
        from ..core import marine
        m = p.cfg["marine"]
        vals = self.marine_page.values()

        def fn(prog, canc):
            from dcs_core import handover
            doc = handover.read(p.dcs, "pospro")
            heads = {int(d): {"nickname": (m.get("names") or {}).get(d), "side": (m.get("looks") or {}).get(d)}
                     for d in m["result"]["heads"]}
            for k, s in (doc.get("sensors") or {}).items():
                if k.startswith("sonar_") and int(k.split("_")[1]) in heads:
                    heads[int(k.split("_")[1])]["nickname"] = s.get("nickname")
            return marine.publish(p.dcs, m["result"], {"heads": heads, "doc": doc}, applied=vals)
        self._run("Finalize", self._stage_run(fn, "publish"),
                  lambda _: (self.marine_page.refresh(), self.toast("Sonar corrections finalized for CloudPro")),
                  warm=False)

    def publish_now(self):
        if self.p is None or self.p.dcs is None:
            return
        self._run("Finalize", self._stage_run(lambda prog, canc: self.p.publish(), "publish"),
                  lambda _: self.toast("alignment.json finalized for CloudPro"))

    def reset_project(self):
        if not ask(self, "Reset the mounting angles and lever arm to the values recorded in the .clraw files and the "
                         "latency to 0? The calibration results, the patch boxes and the line selection are "
                         "cleared too; the processing settings are kept."):
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
        self._pending = []
        try:
            if self.job is not None:
                self.job.detach()              # its callbacks must not touch this window after unload
                self.job.cancel()
        except RuntimeError:
            pass
        self.job = None
        if self._tool is not None:
            self._end_tool(self._tool, restore=False)
        self.prev_tool = None
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
