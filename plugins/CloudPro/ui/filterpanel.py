"""Automatic filters, sized for one screen quarter.

  GROUP   [group dropdown] [?] [menu: new, clone, defaults, rename, mode, import, export, delete, save]
  FILTER  [on/off] [filter dropdown] [?] [+] [-] [clone] [up] [down]
          the selected filter's settings fill the space below
  PREVIEW [live] [pause] [accept]   SCOPE [dropdown] [run]

Filters run in the order of the dropdown, each on the result of the one
before. Live preview recomputes in the background whenever a setting
changes; Accept commits it (undoable)."""
import html
import json
import os

from ..core import filters as F
from ..qt import QtCore, QtWidgets, Qt, Signal, exec_
from . import chrome, theme

SCOPES = ["Inspection Area", "Slice", "Selected lines (whole project)", "Entire project"]
MOD_TEXT = {"R": "rejects points", "C": "sets classes", "Z": "shifts heights", "R/Z/C": "re-accepts, resets shifts "
            "and classes", "R/C": "rejects and / or classifies", "C/R": "classifies or rejects", "A": "re-accepts"}


def filter_help(f):
    rows = "".join("<tr><td style='color:%s;padding-right:10px'>%s</td><td>%s</td></tr>" % (
        theme.DIM, html.escape(p.label), html.escape(p.help or _default_help(p))) for p in f.all_params())
    ref = ("<p style='color:%s'><b>Source:</b> %s</p>" % (theme.DIM, html.escape(f.ref))) if f.ref else ""
    return ("<h3 style='color:%s'>%s</h3><p><b>What it does</b> (%s): %s</p>%s"
            "<p><b>Settings</b></p><table>%s</table>" % (theme.ACCENT, html.escape(f.name),
                                                         MOD_TEXT.get(f.mod, f.mod), html.escape(f.help), ref, rows))


def _default_help(p):
    if p.key == "classes":
        return "Only points of these classes are edited (e.g. 2 for ground). Empty = all classes."
    if p.key == "lines":
        return "Only points of these lines are edited (e.g. 1,3). Empty = all lines."
    if p.kind == "choice":
        return "One of: " + ", ".join(p.choices)
    rng = ""
    if p.lo is not None and p.hi is not None:
        rng = " (%g to %g%s)" % (p.lo, p.hi, p.unit)
    return "Default %s%s%s" % (p.default, p.unit if p.kind == "float" else "", rng)


def group_help(g):
    items = "".join("<li><b>%s</b>%s – %s</li>" % (html.escape(f.name), "" if f.enabled else " (off)",
                                                    html.escape(f.help.split(". ")[0] + "."))
                    for f in g.filters)
    return ("<h3 style='color:%s'>%s</h3><p>A filter group runs its filters in order, each on the result of the "
            "previous one. Mode <b>%s</b>: %s</p><ol>%s</ol><p style='color:%s'>Preview shows the result in the views "
            "(orange = will be rejected, green = re-accepted) without changing anything; Accept or Run commits it, "
            "and Undo takes it back. Rejected points are never deleted: Reset modifications re-accepts them.</p>" % (
                theme.ACCENT, html.escape(g.name), g.mode,
                "all lines are filtered together" if g.mode == "Combined" else "each line is filtered on its own",
                items or "<li>(empty: add filters with +)</li>", theme.DIM))


class FilterPanel(QtWidgets.QWidget):
    previewRequested = Signal()
    previewOff = Signal()
    acceptRequested = Signal()
    runRequested = Signal(str)
    tuneRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.groups = F.default_groups()
        self.folder = None
        self._building = False
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(400)
        self._timer.timeout.connect(self._live)
        self._refs = [("(none)", "")]
        self._ref = ""
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(5)

        # group row
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self._key("GROUP", 52))
        self.cmb = QtWidgets.QComboBox()
        self.cmb.currentIndexChanged.connect(self._group_changed)
        row.addWidget(self.cmb, 1)
        row.addWidget(chrome.HelpButton("Filter group", lambda: group_help(self.group) if self.group else ""))
        self.btn_gmenu = chrome.IconButton("menu", "Group: new, clone, default groups, rename, mode, import, export, "
                                                   "delete, save", size=26)
        self.btn_gmenu.clicked.connect(self._group_menu)
        row.addWidget(self.btn_gmenu)
        lay.addLayout(row)

        # filter row
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self._key("FILTER", 52))
        self.chk_on = QtWidgets.QCheckBox("")
        self.chk_on.setToolTip("Run this filter (off = skipped)")
        self.chk_on.toggled.connect(self._toggle_filter)
        row.addWidget(self.chk_on)
        self.cmb_f = QtWidgets.QComboBox()
        self.cmb_f.currentIndexChanged.connect(self._filter_selected)
        row.addWidget(self.cmb_f, 1)
        row.addWidget(chrome.HelpButton("Filter", lambda: filter_help(self.current) if self.current else
                                        "Add a filter with +."))
        b = chrome.IconButton("plus", "Add a filter after this one", size=26)
        b.clicked.connect(self._add_menu)
        row.addWidget(b)
        for name, tip, fn in (("minus", "Remove this filter", self.remove_filter),
                              ("copy", "Duplicate this filter", self.clone_filter),
                              ("up", "Run earlier", lambda: self.move(-1)), ("down", "Run later", lambda: self.move(1))):
            b = chrome.IconButton(name, tip, size=26)
            b.clicked.connect(fn)
            row.addWidget(b)
        lay.addLayout(row)

        # settings
        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.form_host = QtWidgets.QWidget()
        self.form = QtWidgets.QFormLayout(self.form_host)
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.form.setVerticalSpacing(4)
        self.scroll.setWidget(self.form_host)
        lay.addWidget(self.scroll, 1)

        # preview and run
        bar = QtWidgets.QHBoxLayout()
        self.btn_prev = QtWidgets.QPushButton("Live preview")
        self.btn_prev.setCheckable(True)
        self.btn_prev.setToolTip("Show what the group would do (orange = rejected, green = re-accepted); settings "
                                 "changes update it at once unless paused. Nothing is changed until Accept.")
        self.btn_prev.toggled.connect(self._toggle_preview)
        self.btn_pause = QtWidgets.QPushButton("Pause")
        self.btn_pause.setCheckable(True)
        self.btn_pause.setToolTip("Stop updating the preview while you change several settings")
        self.btn_pause.toggled.connect(lambda on: (not on) and self._live())
        self.btn_accept = QtWidgets.QPushButton("Accept")
        self.btn_accept.setObjectName("ppPrimary")
        self.btn_accept.setEnabled(False)
        self.btn_accept.setToolTip("Commit the previewed changes (Undo takes them back)")
        self.btn_accept.clicked.connect(self.acceptRequested.emit)
        for w in (self.btn_prev, self.btn_pause, self.btn_accept):
            bar.addWidget(w)
        bar.addSpacing(8)
        bar.addWidget(self._key("SCOPE", 44))
        self.cmb_scope = QtWidgets.QComboBox()
        self.cmb_scope.addItems(SCOPES)
        self.cmb_scope.setToolTip("Where Preview and Run work: the loaded Inspection Area, the current slice, the "
                                  "visible lines over the whole project, or everything (tile by tile)")
        bar.addWidget(self.cmb_scope, 1)
        self.btn_run = QtWidgets.QPushButton("Run")
        self.btn_run.setToolTip("Run the whole group on the scope now")
        self.btn_run.clicked.connect(lambda: self.runRequested.emit(self.cmb_scope.currentText()))
        bar.addWidget(self.btn_run)
        lay.addLayout(bar)
        self.lbl_status = QtWidgets.QLabel("")
        self.lbl_status.setObjectName("ppHint")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setMaximumHeight(140)
        lay.addWidget(self.lbl_status)
        self._fill_groups()

    def _key(self, t, w):
        lab = chrome.label(t)
        lab.setFixedWidth(w)
        return lab

    # ------------------------------------------------------------- groups
    @property
    def group(self):
        i = self.cmb.currentIndex()
        return self.groups[i] if 0 <= i < len(self.groups) else None

    @property
    def current(self):
        g = self.group
        i = self.cmb_f.currentIndex()
        return g.filters[i] if g and 0 <= i < len(g.filters) else None

    def _fill_groups(self, select=0):
        self._building = True
        self.cmb.clear()
        for g in self.groups:
            self.cmb.addItem(g.name)
        self._building = False
        self.cmb.setCurrentIndex(min(select, len(self.groups) - 1))
        self._group_changed()

    def _group_changed(self, *_):
        if self._building or self.group is None:
            return
        self._fill_filters()
        self._changed()

    def _fill_filters(self, select=0):
        self._building = True
        self.cmb_f.clear()
        g = self.group
        for k, f in enumerate(g.filters):
            self.cmb_f.addItem(self._label(k, f))
            self.cmb_f.setItemData(k, f.help, Qt.ItemDataRole.ToolTipRole)
        self._building = False
        if g.filters:
            self.cmb_f.setCurrentIndex(min(select, len(g.filters) - 1))
        self._filter_selected(self.cmb_f.currentIndex())

    @staticmethod
    def _label(k, f):
        return "%d.  %s%s" % (k + 1, f.name, "" if f.enabled else "   (off)")

    def _relabel(self):
        i = self.cmb_f.currentIndex()
        if i >= 0 and self.current is not None:
            self._building = True
            self.cmb_f.setItemText(i, self._label(i, self.current))
            self._building = False

    def _group_menu(self):
        m = QtWidgets.QMenu(self)
        m.addAction("Tune the filters to the sensor precision and this data...", self.tuneRequested.emit)
        m.addSeparator()
        m.addAction("New empty group", self.new_group)
        m.addAction("Duplicate this group", self.clone_group)
        dm = m.addMenu("Add a ready-made group")
        for g in F.default_groups():
            dm.addAction(g.name, lambda gg=g: self._add_group(F.FilterGroup.from_dict(gg.to_dict())))
        m.addAction("Rename...", self._rename)
        mm = m.addMenu("Mode")
        for md, tip in (("Combined", "all lines filtered together (good positioning)"),
                        ("Individually", "each line on its own (lines that do not fit in height)")):
            a = mm.addAction("%s - %s" % (md, tip))
            a.setCheckable(True)
            a.setChecked(self.group is not None and self.group.mode == md)
            a.triggered.connect(lambda _=False, md=md: self._mode(md))
        m.addSeparator()
        m.addAction("Import group (JSON)...", self.import_group)
        m.addAction("Export group (JSON)...", self.export_group)
        m.addSeparator()
        m.addAction("Save groups with the project", self.save)
        m.addAction("Delete this group...", self.delete_group)
        for x in (m, dm, mm):
            theme.dark_menu(x)
        exec_(m, self.btn_gmenu.mapToGlobal(self.btn_gmenu.rect().bottomLeft()))

    def refresh_values(self):
        """Show the settings again after they were changed from outside (tuning)."""
        self._fill_filters(max(0, self.cmb_f.currentIndex()))

    def _add_group(self, g):
        self.groups.append(g)
        self._fill_groups(len(self.groups) - 1)

    def new_group(self):
        self._add_group(F.FilterGroup("New group %d" % (len(self.groups) + 1)))

    def clone_group(self):
        if self.group:
            g = F.FilterGroup.from_dict(self.group.to_dict())
            g.name += " (copy)"
            self._add_group(g)

    def delete_group(self):
        if len(self.groups) <= 1:
            return
        i = self.cmb.currentIndex()
        if theme.msg_question(self, "CloudPro", "Delete filter group '%s'?" % self.groups[i].name) != \
                QtWidgets.QMessageBox.StandardButton.Yes:
            return
        del self.groups[i]
        self._fill_groups(max(0, i - 1))

    def _rename(self):
        if not self.group:
            return
        t, ok = QtWidgets.QInputDialog.getText(self, "Rename group", "Name", text=self.group.name)
        if ok and t.strip():
            self.group.name = t.strip()
            self.cmb.setItemText(self.cmb.currentIndex(), self.group.name)

    def _mode(self, t):
        if self.group:
            self.group.mode = t
            self._changed()

    def import_group(self):
        p, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Import filter group", "", "Filter group (*.json)")
        if p:
            with open(p) as f:
                d = json.load(f)
            for gd in (d if isinstance(d, list) else [d]):
                self._add_group(F.FilterGroup.from_dict(gd))

    def export_group(self):
        if not self.group:
            return
        p, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Export filter group", self.group.name + ".json",
                                                     "Filter group (*.json)")
        if p:
            with open(p, "w") as f:
                json.dump(self.group.to_dict(), f, indent=1)

    def load(self, folder):
        self.folder = folder
        p = os.path.join(folder, "groups.json") if folder else None
        if p and os.path.exists(p):
            try:
                with open(p) as f:
                    d = json.load(f)
                gs = [F.FilterGroup.from_dict(x) for x in d]
                if gs:
                    self.groups = gs
                    names = {g.name for g in gs}
                    for g in F.default_groups():           # ready-made groups added in later versions
                        if g.name not in names:
                            self.groups.append(g)
            except Exception:       # noqa: BLE001
                pass
        self._fill_groups()

    def save_quiet(self):
        if self.folder:
            os.makedirs(self.folder, exist_ok=True)
            tmp = os.path.join(self.folder, "groups.json.tmp")
            with open(tmp, "w") as f:
                json.dump([g.to_dict() for g in self.groups], f, indent=1)
            os.replace(tmp, os.path.join(self.folder, "groups.json"))

    def save(self):
        if not self.folder:
            return
        self.save_quiet()                   # written to a temporary file, then renamed (no half-written file)
        self.set_status("Filter groups saved")

    # ------------------------------------------------------------- filters
    def _add_menu(self):
        m = QtWidgets.QMenu(self)
        cats = {}
        for key, cls in F.REGISTRY.items():
            cats.setdefault(cls.group, []).append(cls)
        subs = []
        for cat in F.GROUPS_ORDER:
            if cat not in cats:
                continue
            sm = m.addMenu(cat)
            subs.append(sm)
            for cls in cats[cat]:
                a = sm.addAction(cls.name)
                a.setToolTip(cls.help)
                a.triggered.connect(lambda _=False, k=cls.key: self.add_filter(k))
        for x in [m] + subs:
            theme.dark_menu(x)
            x.setToolTipsVisible(True)
        from ..qt import QtGui
        exec_(m, QtGui.QCursor.pos())

    def add_filter(self, key):
        g = self.group
        if g is None:
            return
        i = self.cmb_f.currentIndex()
        g.filters.insert(i + 1 if i >= 0 else len(g.filters), F.make(key))
        self._fill_filters(i + 1 if i >= 0 else len(g.filters) - 1)
        self._changed()

    def remove_filter(self):
        i = self.cmb_f.currentIndex()
        if i < 0:
            return
        del self.group.filters[i]
        self._fill_filters(max(0, i - 1))
        self._changed()

    def clone_filter(self):
        i = self.cmb_f.currentIndex()
        if i < 0:
            return
        f = self.group.filters[i]
        self.group.filters.insert(i + 1, F.make(f.key, **f.values))
        self._fill_filters(i + 1)
        self._changed()

    def move(self, d):
        i = self.cmb_f.currentIndex()
        j = i + d
        fl = self.group.filters if self.group else []
        if i < 0 or j < 0 or j >= len(fl):
            return
        fl[i], fl[j] = fl[j], fl[i]
        self._fill_filters(j)
        self._changed()

    def _toggle_filter(self, on):
        if self._building or self.current is None:
            return
        self.current.enabled = bool(on)
        self._relabel()
        self._changed()

    def _filter_selected(self, i):
        while self.form.rowCount():
            self.form.removeRow(0)
        f = self.current
        self._building = True
        self.chk_on.setChecked(bool(f and f.enabled))
        self.chk_on.setEnabled(f is not None)
        self._building = False
        if f is None:
            return
        for p in f.all_params():
            w = self._widget(f, p)
            lab = QtWidgets.QLabel(p.label)
            lab.setObjectName("ppKey")
            tip = p.help or _default_help(p)
            lab.setToolTip(tip)
            w.setToolTip(tip)
            self.form.addRow(lab, w)
        if f.key == "clip_reference":
            c = QtWidgets.QComboBox()
            for name, path in self._refs:
                c.addItem(name, path)
            c.addItem("Choose a raster file...", "__pick__")
            i = c.findData(self._ref)
            c.setCurrentIndex(max(0, i))
            c.currentIndexChanged.connect(lambda _i, c=c: self._ref_changed(c))
            lab = chrome.label("Reference raster")
            self.form.insertRow(0, lab, c)
        theme.fusionize(self.form_host)

    def _widget(self, f, p):
        v = f.values[p.key]

        def setv(val, f=f, p=p):
            f.values[p.key] = val
            self._changed()
        if p.kind == "float":
            w = QtWidgets.QDoubleSpinBox()
            step = p.step or 0.1
            txt = "%g" % step
            dec = len(txt.split(".")[1]) if "." in txt else 0
            w.setDecimals(max(min(dec, 4), 2))
            w.setRange(p.lo if p.lo is not None else -1e9, p.hi if p.hi is not None else 1e9)
            w.setSingleStep(step)
            w.setSuffix(p.unit)
            w.setValue(float(v))
            w.setKeyboardTracking(False)
            w.valueChanged.connect(lambda x: setv(float(x)))
        elif p.kind == "int":
            w = QtWidgets.QSpinBox()
            w.setRange(int(p.lo if p.lo is not None else -10 ** 9), int(p.hi if p.hi is not None else 10 ** 9))
            w.setValue(int(v))
            w.setKeyboardTracking(False)
            w.valueChanged.connect(lambda x: setv(int(x)))
        elif p.kind == "bool":
            w = QtWidgets.QCheckBox()
            w.setChecked(bool(v))
            w.toggled.connect(lambda x: setv(bool(x)))
        elif p.kind == "choice":
            w = QtWidgets.QComboBox()
            w.addItems(p.choices)
            w.setCurrentText(str(v))
            w.currentTextChanged.connect(lambda x: setv(str(x)))
        else:
            w = QtWidgets.QLineEdit(str(v))
            w.editingFinished.connect(lambda w=w: setv(w.text().strip()))
        return w

    # ------------------------------------------------------------- preview
    def _changed(self):
        if self.btn_prev.isChecked() and not self.btn_pause.isChecked():
            self._timer.start()

    def _live(self):
        if self.btn_prev.isChecked():
            self.previewRequested.emit()

    def _toggle_preview(self, on):
        self.btn_accept.setEnabled(on)
        if on:
            self.previewRequested.emit()
        else:
            self.previewOff.emit()

    def preview_done(self):
        self.btn_prev.blockSignals(True)
        self.btn_prev.setChecked(False)
        self.btn_prev.blockSignals(False)
        self.btn_accept.setEnabled(False)

    def set_status(self, txt):
        lines = txt.split("\n")
        self.lbl_status.setText("\n".join(lines[:8]) + ("\n..." if len(lines) > 8 else ""))
        self.lbl_status.setToolTip(txt)

    # ------------------------------------------------------------- reference raster
    def set_raster_layers(self, items):
        self._refs = [("(none)", "")] + list(items)

    def _ref_changed(self, c):
        d = c.currentData()
        if d == "__pick__":
            p, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Reference raster", "",
                                                         "Rasters (*.tif *.tiff *.asc *.vrt *.img)")
            if p:
                self._refs.append((os.path.basename(p), p))
                self._ref = p
            self._filter_selected(self.cmb_f.currentIndex())
            return
        self._ref = d or ""
        self._changed()

    def reference(self):
        return self._ref
