"""The RemixPro window: recordings on the left, what to make and where on the right.

A DCS Core PluginWindow (floating by default) with the shared PluginHeader. RemixPro is a side tool, not a
numbered stage, so the header shows no 01 PosPro -> 02 AlignPro -> 03 CloudPro boxes.

Conversions run on a worker thread; the window polls its queue, shows progress, and Cancel stops the
recording being converted (its partial output is removed; recordings already done stay done).
"""
import os
import queue
import threading
import time

from qgis.PyQt.QtCore import QSettings, Qt, QTimer, QUrl
from qgis.PyQt.QtGui import QColor, QDesktopServices
from qgis.PyQt.QtWidgets import (QAbstractItemView, QCheckBox, QFileDialog, QFrame, QHBoxLayout, QHeaderView,
                                 QLabel, QLineEdit, QMenu, QPlainTextEdit, QProgressBar, QPushButton, QSplitter,
                                 QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from dcs_core import theme
from dcs_core.header import PluginHeader
from dcs_core.project import DcsProject, ProjectError
from dcs_core.widgets import KV
from dcs_core.window import PluginWindow

from ..core import __version__, make, sources
from ..core.sources import BEAMWORX, CLRAW, IE, OUTPUT_LABELS, QINERTIA

KEY = "remixpro"
COLS = ["Recording", "Kind", "Files", "Size", "Start (UTC)", "BeamworX", "Inertial Explorer", "Qinertia", ".clraw"]
OUT_COL = {BEAMWORX: 5, IE: 6, QINERTIA: 7, CLRAW: 8}
STATE_WORDS = {"current": ("✓ made", "ok"), "out of date": ("out of date", "warn"), "not made": ("–", None),
               None: ("n/a", None)}
DESCRIPTIONS = {
    BEAMWORX: "One .bwxraw per .clraw file, with its survey configuration (.xml): open in AutoPatch, AutoClean "
              "or TrajEdit. Survey lines only.",
    IE: "IMR (raw IMU, in Inertial Explorer's axes), RINEX 3.04 observations of both receivers and GPS "
        "ephemeris, and a setup note with the lever arm.",
    QINERTIA: "Generic IMU file ($qimu), the same RINEX files, and a setup note.",
    CLRAW: "Marine sonar recordings (SonarView .svlz / .svlog, TidalCal projects) to .clraw for the DCS plugins: "
           "each sonar's packets and motion on its own channel, the vehicle and GNSS data beside them.",
}


def _settings(name, default=None):
    return QSettings().value("dcs/%s/%s" % (KEY, name), default)


def _set(name, value):
    QSettings().setValue("dcs/%s/%s" % (KEY, name), value)


def _bool(v):
    return v is True or str(v).lower() in ("true", "1")


def _size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.0f %s" if unit in ("B", "KB") else "%.1f %s") % (n, unit)
        n /= 1024.0
    return ""


class RemixProWindow(PluginWindow):
    def __init__(self, iface, parent=None, guide=None):
        super().__init__(iface, KEY, "RemixPro", parent, size=(1240, 780), dock_size=620, min_size=(760, 480))
        self._guide = guide
        self.paths = []                 # what the user opened (folders and files)
        self.items = []
        self.project = None             # an open DCS project, if any
        self.out_custom = False         # the user chose the output folder
        self.worker = None
        self.q = queue.Queue()
        self.cancel = threading.Event()
        root = QWidget()
        root.setObjectName("ppRoot")
        theme.apply(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.header = PluginHeader(KEY, "RemixPro", window=self, guide=self.open_guide)
        self.header.chain.hide()        # a side tool: no stage boxes
        sub = QLabel("Files for BeamworX, Inertial Explorer and Qinertia from the mapper's recordings")
        sub.setObjectName("ppHint")
        self.header.add_extra(sub, 1)
        self._project_menu()
        lay.addWidget(self.header)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        split.addWidget(self._left())
        split.addWidget(self._right())
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        split.setSizes([820, 400])
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(12, 10, 12, 12)
        bl.addWidget(split)
        lay.addWidget(body, 1)
        self.setWidget(root)
        theme.apply_dock(self)
        theme.polish(root)
        theme.mapperize(self.right)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._poll)
        self._fill()

    # ------------------------------------------------------------------ layout
    def _project_menu(self):
        m = self.header.new_project_menu()
        m.addAction("Open a folder…  (mapper project, USB copy, download)", self.choose_folder)
        m.addAction("Add .clraw / .anpp files…", self.choose_files)
        m.addAction("Open a DCS project…", self.choose_dcs_project)
        m.addSeparator()
        self.act_rescan = m.addAction("Look again (rescan)", self.rescan)
        self.act_clear = m.addAction("Clear the list", self.clear)
        m.addSeparator()
        m.addAction("User guide (this version)", self.open_guide)
        m.addAction("About RemixPro %s" % __version__, self.about)

    def _title(self, text):
        t = QLabel(text.upper())
        t.setObjectName("ppTitle")
        theme.caps(t, 108)
        return t

    def _left(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 8, 0)
        row = QHBoxLayout()
        row.addWidget(self._title("Recordings"))
        row.addStretch(1)
        self.b_folder = QPushButton("Open folder…")
        self.b_folder.clicked.connect(self.choose_folder)
        self.b_files = QPushButton("Add files…")
        self.b_files.clicked.connect(self.choose_files)
        self.b_all = QToolButton()
        self.b_all.setText("Select")
        self.b_all.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        sm = QMenu(self.b_all)
        sm.addAction("All", lambda: self._tick(lambda it: True))
        sm.addAction("None", lambda: self._tick(lambda it: False))
        sm.addAction("Survey lines only", lambda: self._tick(lambda it: it.kind == sources.LINE))
        sm.addAction("Navigation recordings only", lambda: self._tick(lambda it: it.kind == sources.NAVIGATION))
        sm.addAction("Not made yet", lambda: self._tick(self._needs_making))
        theme.dark_menu(sm)
        self.b_all.setMenu(sm)
        for b in (self.b_folder, self.b_files, self.b_all):
            row.addWidget(b)
        v.addLayout(row)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(COLS))
        self.tree.setHeaderLabels(COLS)
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)      # keep the line number and time visible
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        h = self.tree.header()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in range(1, len(COLS)):
            h.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.itemChanged.connect(self._item_changed)
        self.tree.currentItemChanged.connect(lambda *_: self._show_details())
        v.addWidget(self.tree, 3)
        self.empty = QLabel("Open a mapper project folder, a USB copy (Mapper captures), an unzipped download, "
                            "or single .clraw / .anpp files.\n\nSurvey lines give BeamworX, Inertial Explorer and "
                            "Qinertia files; navigation recordings and Certus logs give Inertial Explorer and "
                            "Qinertia files.")
        self.empty.setObjectName("ppHint")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.empty, 3)
        v.addWidget(self._title("Selected recording"))
        self.kv = KV()
        frame = QFrame()
        frame.setObjectName("ppCard")
        fl = QVBoxLayout(frame)
        fl.addWidget(self.kv)
        v.addWidget(frame, 0)
        return w

    def _right(self):
        w = QWidget()
        w.setObjectName("ppSidebar")
        self.right = w
        v = QVBoxLayout(w)
        v.setContentsMargins(12, 0, 0, 0)
        v.addWidget(self._title("Make"))
        self.want = {}
        saved = _settings("outputs", "beamworx,inertial_explorer,qinertia,clraw")
        saved = str(saved).split(",") if saved else []
        for k in (BEAMWORX, IE, QINERTIA, CLRAW):
            cb = QCheckBox(OUTPUT_LABELS[k])
            cb.setProperty("keepcase", k == CLRAW)
            cb.setChecked(k in saved)
            cb.toggled.connect(self._outputs_changed)
            hint = QLabel(DESCRIPTIONS[k])
            hint.setObjectName("ppHint")
            hint.setWordWrap(True)
            v.addWidget(cb)
            v.addWidget(hint)
            self.want[k] = cb
        v.addSpacing(8)
        v.addWidget(self._title("Output folder"))
        orow = QHBoxLayout()
        self.out = QLineEdit()
        self.out.setPlaceholderText("chosen from the recordings")
        self.out.editingFinished.connect(self._out_edited)
        b = QPushButton("Choose…")
        b.setToolTip("Choose the output folder")
        b.clicked.connect(self.choose_out)
        orow.addWidget(self.out, 1)
        orow.addWidget(b)
        v.addLayout(orow)
        self.out_hint = QLabel("")
        self.out_hint.setObjectName("ppHint")
        self.out_hint.setWordWrap(True)
        v.addWidget(self.out_hint)
        self.force = QCheckBox("Make again even when up to date")
        v.addWidget(self.force)
        self.incomplete = QCheckBox("Include incomplete lines")
        self.incomplete.setToolTip("Lines still being recorded or copied (no continuity.json / manifest.sha256 yet)")
        v.addWidget(self.incomplete)
        v.addSpacing(6)
        self.go = QPushButton("Make files")
        self.go.setObjectName("ppPrimary")
        self.go.setMinimumHeight(38)
        self.go.clicked.connect(self.start)
        v.addWidget(self.go)
        prow = QHBoxLayout()
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.stop = QPushButton("Cancel")
        self.stop.setObjectName("ppStop")
        self.stop.clicked.connect(self.request_cancel)
        prow.addWidget(self.bar, 1)
        prow.addWidget(self.stop)
        v.addLayout(prow)
        self.status = QLabel("")
        self.status.setObjectName("ppHint")
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        v.addWidget(self._title("Log"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        v.addWidget(self.log, 1)
        self.open_out = QPushButton("Open output folder")
        self.open_out.clicked.connect(self.show_output)
        v.addWidget(self.open_out)
        return w

    # ------------------------------------------------------------------ inputs
    def choose_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Recordings: a mapper project, USB copy or download",
                                             str(_settings("last_folder", "") or ""))
        if d:
            _set("last_folder", d)
            self.project = None
            self.open_paths([d])

    def choose_files(self):
        fs, _ = QFileDialog.getOpenFileNames(self, "Recordings", str(_settings("last_folder", "") or ""),
                                             "Mapper recordings (*.clraw *.anpp);;All files (*)")
        if fs:
            _set("last_folder", os.path.dirname(fs[0]))
            self.open_paths(self.paths + fs)

    def choose_dcs_project(self):
        from dcs_core.dialogs import pick_project_file
        f = pick_project_file(self)
        if not f:
            return
        try:
            self.open_dcs_project(f)
        except (ProjectError, OSError, ValueError) as e:
            theme.msg_warn(self, "RemixPro", "That DCS project could not be opened:\n%s" % e)

    def open_dcs_project(self, path):
        """The job's recordings, outputs in Processing/<Name>/RemixPro/ (called from another plugin too)."""
        p = DcsProject.open(path)
        self.project = p
        paths = [p.job_folder] if os.path.isdir(p.job_folder) else p.capture_files()
        self.out_custom = False
        self.open_paths(paths)

    def open_paths(self, paths):
        self.paths = list(dict.fromkeys(paths))
        self.rescan()

    def clear(self):
        if self.busy:
            return
        self.paths, self.items, self.project = [], [], None
        self.out_custom = False
        self.out.clear()
        self._fill()

    def rescan(self):
        if self.busy:
            return
        self.header.set_busy(True)
        try:
            self.items = sources.scan(self.paths) if self.paths else []
        except Exception as e:      # noqa: BLE001 - shown, never fatal
            self.items = []
            self._log("Could not read the recordings: %s" % e)
        finally:
            self.header.set_busy(False)
        if not self.out_custom:
            self.out.setText(self.default_out())
        self._fill()

    def default_out(self):
        if self.project is not None:
            return os.path.join(self.project.folder, "RemixPro")
        return sources.default_output_root(self.paths) if self.paths else ""

    # ------------------------------------------------------------------ list
    def _fill(self):
        self.tree.blockSignals(True)
        self.tree.clear()
        out = self.out.text().strip()
        for it in self.items:
            row = QTreeWidgetItem([it.name, sources.KIND_LABELS[it.kind],
                                   str(len(it.clraw) or len(it.anpp) or (1 if it.sonar_file else 0)),
                                   _size(it.size), time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(it.start))
                                   if it.start else "", "", "", "", ""])
            row.setData(0, Qt.ItemDataRole.UserRole, it.key)
            row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            row.setCheckState(0, Qt.CheckState.Checked if it.complete else Qt.CheckState.Unchecked)
            tip = "\n".join([it.folder] + it.notes)
            for c in range(len(COLS)):
                row.setToolTip(c, tip)
            if not it.complete:
                row.setForeground(0, QColor(theme.AMBER))
                row.setText(1, sources.KIND_LABELS[it.kind] + " (incomplete)")
            self._set_states(row, it, out)
            self.tree.addTopLevelItem(row)
        self.tree.blockSignals(False)
        self.tree.setVisible(bool(self.items))
        self.empty.setVisible(not self.items)
        if self.items:
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        name = ""
        if self.project is not None:
            name = self.project.name
        elif self.items:
            names = {i.project for i in self.items if i.project}
            name = names.pop() if len(names) == 1 else ("%d recordings" % len(self.items))
        self.header.set_project_name(name or None)
        self._show_details()
        self._update_buttons()

    def _set_states(self, row, it, out):
        for k, c in OUT_COL.items():
            st = (make.state(out, it, k) if out else "not made") if k in it.outputs else None
            word, level = STATE_WORDS[st]
            row.setText(c, word)
            row.setForeground(c, QColor({"ok": theme.ACCENT, "warn": theme.AMBER}.get(level, theme.DIM)))

    def _refresh_states(self):
        out = self.out.text().strip()
        for i in range(self.tree.topLevelItemCount()):
            row = self.tree.topLevelItem(i)
            it = self._item_for(row)
            if it is not None:
                self._set_states(row, it, out)

    def _item_for(self, row):
        key = row.data(0, Qt.ItemDataRole.UserRole) if row is not None else None
        return next((i for i in self.items if i.key == key), None)

    def _tick(self, pred):
        self.tree.blockSignals(True)
        for i in range(self.tree.topLevelItemCount()):
            row = self.tree.topLevelItem(i)
            it = self._item_for(row)
            row.setCheckState(0, Qt.CheckState.Checked if it is not None and pred(it) else Qt.CheckState.Unchecked)
        self.tree.blockSignals(False)
        self._update_buttons()

    def _needs_making(self, it):
        out = self.out.text().strip()
        return any(make.state(out, it, k) != "current" for k in self.kinds() if k in it.outputs)

    def _item_changed(self, *_):
        self._update_buttons()

    def checked_items(self):
        out = []
        for i in range(self.tree.topLevelItemCount()):
            row = self.tree.topLevelItem(i)
            if row.checkState(0) == Qt.CheckState.Checked:
                it = self._item_for(row)
                if it is not None:
                    out.append(it)
        return out

    def _show_details(self):
        it = self._item_for(self.tree.currentItem())
        self.kv.clear()
        if it is None:
            self.kv.set("Recording", "–")
            return
        self.kv.set("Folder", it.folder)
        if it.kind == sources.SONAR:
            self.kv.set("Recording", os.path.basename(it.sonar_file))
            self.kv.set("GNSS files", ", ".join(os.path.basename(p) for p in it.rinex + it.llh + it.ppk) or
                        "none: vehicle data from the recording", None if (it.rinex or it.llh or it.ppk) else "warn")
            self.kv.set("Sonar offsets", ("; ".join("%s: %+.3f, %+.3f, %+.3f" % ((k,) + tuple(v))
                                                    for k, v in sorted(it.offsets.items())) + "  (%s)"
                                          % os.path.basename(it.offsets_source)) if it.offsets else
                        "not entered (sonar_offsets.json)", None if it.offsets else "warn")
            if it.notes:
                self.kv.set("Notes", "\n".join(it.notes))
            return
        if it.clraw:
            self.kv.set(".clraw", "%d file(s): %s" % (len(it.clraw), ", ".join(os.path.basename(p) for p in it.clraw[:3])
                                                     + (" …" if len(it.clraw) > 3 else "")))
        src, paths = it.certus_source()
        self.kv.set("Certus data", "%s (%s)" % (", ".join(os.path.basename(p) for p in paths[:2]) +
                                             (" …" if len(paths) > 2 else ""),
                                             "Certus log" if src == "anpp" else "/ins/anpp channel of the .clraw"))
        self.kv.set("Ephemeris", os.path.basename(it.ephemeris) if it.ephemeris else "none beside it",
                    None if it.ephemeris else "warn")
        lv = it.lever_primary
        self.kv.set("Primary antenna", ("%+.3f, %+.3f, %+.3f m F/S/D  (%s)" % (tuple(lv) + (it.lever_source,)))
                    if lv else "not recorded", None if lv else "warn")
        if it.kind == sources.LINE:
            self.kv.set("Survey configuration", it.config_source or "none: no BeamworX files",
                        None if it.config_source == "recorded" else "warn")
        if it.notes:
            self.kv.set("Notes", "\n".join(it.notes), None if it.complete else "warn")

    # ------------------------------------------------------------------ outputs
    def kinds(self):
        return [k for k, cb in self.want.items() if cb.isChecked()]

    def _outputs_changed(self, *_):
        _set("outputs", ",".join(self.kinds()))
        self._update_buttons()

    def choose_out(self):
        d = QFileDialog.getExistingDirectory(self, "Output folder", self.out.text().strip() or "")
        if d:
            self.out.setText(d)
            self._out_edited()

    def _out_edited(self):
        self.out_custom = bool(self.out.text().strip()) and self.out.text().strip() != self.default_out()
        self._refresh_states()
        self._update_buttons()

    def show_output(self):
        d = self.out.text().strip()
        if d and os.path.isdir(d):
            QDesktopServices.openUrl(QUrl.fromLocalFile(d))

    @property
    def busy(self):
        return self.worker is not None and self.worker.is_alive()

    def _update_buttons(self):
        busy = self.busy
        n = len(self.checked_items()) if self.items else 0
        self.go.setEnabled(not busy and n > 0 and bool(self.kinds()) and bool(self.out.text().strip()))
        self.go.setText(("Make files (%d recording%s)" % (n, "" if n == 1 else "s")).upper() if n else "MAKE FILES")
        self.stop.setEnabled(busy)
        for w in (self.b_folder, self.b_files, self.b_all, self.out, self.force, self.incomplete, self.tree):
            w.setEnabled(not busy)
        for cb in self.want.values():
            cb.setEnabled(not busy)
        self.open_out.setEnabled(os.path.isdir(self.out.text().strip()))
        out = self.out.text().strip()
        self.out_hint.setText(("In the DCS project: Processing/%s/RemixPro" % self.project.name) if self.project
                              is not None and not self.out_custom else
                              ("One folder per recording inside, with beamworx/, inertial_explorer/ and qinertia/."
                               if out else ""))

    # ------------------------------------------------------------------ running
    def start(self):
        if self.busy:
            return
        out = self.out.text().strip()
        kinds = self.kinds()
        todo = []
        for it in self.checked_items():
            k = [x for x in kinds if x in it.outputs]
            if not k:
                self._log("%s: nothing of what is ticked can be made from a %s." % (it.name, sources.KIND_LABELS[it.kind].lower()))
                continue
            if not it.complete and not self.incomplete.isChecked():
                self._log("%s: skipped, incomplete (tick Include incomplete lines to convert what is there)." % it.name)
                continue
            todo.append((it, k))
        if not todo:
            self.status.setText("Nothing to make: tick recordings and outputs.")
            return
        self.cancel.clear()
        self.log.clear()
        self._log("RemixPro %s: %d recording(s) into %s" % (__version__, len(todo), out))
        self.worker = threading.Thread(target=self._work, args=(todo, out, self.force.isChecked()), daemon=True)
        self.worker.start()
        self.timer.start()
        self.bar.setValue(0)
        self._update_buttons()

    def _work(self, todo, out, force):
        n = len(todo)
        put = self.q.put
        made = failed = 0
        for i, (it, kinds) in enumerate(todo):
            if self.cancel.is_set():
                break
            put(("step", i, n, it.name, 0.0))
            try:
                r = make.run(it, kinds, out, force=force, cancelled=self.cancel.is_set,
                             progress=lambda f, i=i, name=it.name: put(("step", i, n, name, f)))
                put(("log", make.describe_result(it, r)))
                made += 1
            except make.Cancelled:
                put(("log", ["%s: cancelled; its unfinished files were removed." % it.name]))
                break
            except Exception as e:      # noqa: BLE001 - one bad recording does not stop the others
                failed += 1
                put(("log", ["%s: FAILED: %s" % (it.name, e)]))
        put(("done", made, failed, self.cancel.is_set()))

    def _poll(self):
        last_step = None
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "step":
                    last_step = msg if self.worker is not None else None
                elif msg[0] == "log":
                    for line in msg[1]:
                        self._log(line)
                elif msg[0] == "done":
                    _, made, failed, cancelled = msg
                    last_step = None
                    self.timer.stop()
                    self.worker = None
                    self.bar.setValue(0 if cancelled else 1000)
                    self.status.setText("Cancelled." if cancelled else
                                        "Done: %d recording(s)%s." % (made, (", %d failed" % failed) if failed else ""))
                    self._refresh_states()
                    self._update_buttons()
        except queue.Empty:
            pass
        if last_step is not None:
            _, i, n, name, f = last_step
            self.bar.setValue(int(1000 * (i + f) / max(n, 1)))
            self.status.setText("%d of %d: %s  %d%%" % (i + 1, n, name, int(f * 100)))

    def request_cancel(self):
        if self.busy:
            self.cancel.set()
            self.status.setText("Cancelling…")

    def _log(self, text):
        self.log.appendPlainText(text)

    # ------------------------------------------------------------------ misc
    def open_guide(self, *_):
        if self._guide is not None:
            self._guide()

    def about(self):
        theme.msg_info(self, "RemixPro", "RemixPro %s\n\nFiles for BeamworX, Inertial Explorer and Qinertia from "
                                         "the mapper's recordings. Part of the Canadian Lidar Data Collection Systems "
                                         "plugins; needs DCS Core." % __version__)

    def cleanup(self):
        """Before the plugin unloads: stop a running conversion and the timer."""
        self.cancel.set()
        if self.worker is not None:
            self.worker.join(timeout=10)
        self.timer.stop()
