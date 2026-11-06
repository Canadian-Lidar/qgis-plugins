"""System profiles window and the "profile found for this system" question (shared by PosPro and AlignPro).

The plugin supplies:
  plugin   its name ("PosPro")
  ident    profiles.identity of the job's recordings (may be {})
  current  {section: values} as the job has them now (the sections the plugin knows)
  owned    the sections the plugin can apply and save (PosPro: antennas, rear_axle, imu, vehicle; AlignPro: lidar)
  apply_fn(sections: {section: values}, profile) -> None   puts profile values into the job
"""
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView,
                                 QLabel, QListWidget, QListWidgetItem, QPushButton, QTableWidget, QTableWidgetItem,
                                 QVBoxLayout)

from . import profiles, theme


def _diff_text(diffs):
    rows = []
    for s, key, label, unit, pv, cv in diffs:
        rows.append("<tr><td>%s</td><td><b>%s</b></td><td>%s</td></tr>" % (
            label, profiles.fmt(pv, unit), profiles.fmt(cv, unit)))
    return ("<table cellspacing=6><tr><th align=left></th><th align=left>Profile</th>"
            "<th align=left>This job</th></tr>%s</table>" % "".join(rows))


def suggest(parent, plugin, ident, current, owned, apply_fn, asked=None):
    """When a saved profile matches the job's system and differs from the job, ask whether to use it.
    asked = the plugin's record of earlier answers for this job ({profile name: updated}); a profile is not
    offered again until it changes. Returns (answer, record) with answer "applied" | "declined" | "open" | None
    (nothing to ask) and the updated record to store."""
    asked = dict(asked or {})
    p = profiles.match(ident)
    if p is None:
        return None, asked
    if asked.get(p["name"]) == p.get("updated"):
        return None, asked
    diffs = profiles.differences(p, current, owned)
    if not diffs:
        asked[p["name"]] = p.get("updated")
        return None, asked
    txt = ("<p>A saved system profile matches the system that recorded this job (%s):</p>"
           "<p><b>%s</b>, updated %s</p>%s<p>Use the profile's values for this job?</p>" % (
               profiles.describe_system(ident), p["name"], (p.get("updated") or "")[:10], _diff_text(diffs)))
    c = theme.msg_choice(parent, "%s – system profile" % plugin, txt,
                         ["Use profile values", "Show profiles…", "Keep this job's values"], default=0)
    if c == 0:
        apply_fn({s: p["sections"][s] for s in owned if s in p["sections"]}, p)
        asked[p["name"]] = p.get("updated")
        return "applied", asked
    if c == 1:
        return "open", asked
    asked[p["name"]] = p.get("updated")
    return "declined", asked


class ProfilesDialog(QDialog):
    """Save the job's values as a system profile, or apply a saved profile to the job."""

    def __init__(self, parent, plugin, ident, current, owned, apply_fn, version=""):
        super().__init__(parent)
        self.plugin, self.ident, self.current, self.owned, self.apply_fn = plugin, ident or {}, current, owned, apply_fn
        self.by = ("%s %s" % (plugin, version)).strip()
        self.setWindowTitle("%s – system profiles" % plugin)
        self.resize(940, 560)
        v = QVBoxLayout(self)
        intro = QLabel("A system profile keeps the checked offsets and mounting of one mapper system, so the "
                       "next job from it starts with them. This job's system: <b>%s</b>." %
                       profiles.describe_system(self.ident))
        intro.setWordWrap(True)
        v.addWidget(intro)
        h = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(QLabel("Saved profiles"))
        self.list = QListWidget()
        self.list.currentItemChanged.connect(lambda *_: self._show())
        left.addWidget(self.list, 1)
        row = QHBoxLayout()
        self.b_new = QPushButton("New…")
        self.b_new.setToolTip("A new profile for this job's system, with this job's values")
        self.b_new.clicked.connect(self.new_profile)
        self.b_del = QPushButton("Delete")
        self.b_del.clicked.connect(self.delete_profile)
        row.addWidget(self.b_new)
        row.addWidget(self.b_del)
        left.addLayout(row)
        row2 = QHBoxLayout()
        b_exp = QPushButton("Export…")
        b_exp.clicked.connect(self.export_profile)
        b_imp = QPushButton("Import…")
        b_imp.clicked.connect(self.import_profile)
        row2.addWidget(b_exp)
        row2.addWidget(b_imp)
        left.addLayout(row2)
        h.addLayout(left, 1)
        right = QVBoxLayout()
        self.title = QLabel("")
        self.title.setWordWrap(True)
        right.addWidget(self.title)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["", "Profile", "This job"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        right.addWidget(self.table, 1)
        self.checks = {}
        crow = QHBoxLayout()
        crow.addWidget(QLabel("Sections:"))
        for s in owned:
            cb = QCheckBox(profiles.SECTION_TITLES[s])
            cb.setChecked(True)
            self.checks[s] = cb
            crow.addWidget(cb)
        crow.addStretch(1)
        right.addLayout(crow)
        h.addLayout(right, 3)
        v.addLayout(h, 1)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        btns = QHBoxLayout()
        self.b_save = QPushButton("Save this job's values to the profile")
        self.b_save.setToolTip("The checked sections of the selected profile take this job's values; "
                               "the other sections are kept")
        self.b_save.clicked.connect(self.save_to_profile)
        self.b_apply = QPushButton("Use the profile's values for this job")
        self.b_apply.clicked.connect(self.apply_profile)
        b_close = QPushButton("Close")
        b_close.clicked.connect(self.accept)
        btns.addWidget(self.b_save)
        btns.addStretch(1)
        btns.addWidget(self.b_apply)
        btns.addWidget(b_close)
        v.addLayout(btns)
        self.applied = None
        self.reload()

    # ------------------------------------------------------------------
    def reload(self, select=None):
        self.list.clear()
        hit = profiles.match(self.ident)
        for p in profiles.list_profiles():
            label = p["name"]
            if hit is not None and p["name"] == hit["name"]:
                label += "   (this system)"
            it = QListWidgetItem(label)
            it.setData(Qt.ItemDataRole.UserRole, p["name"])
            self.list.addItem(it)
            if p["name"] == (select or (hit or {}).get("name")):
                self.list.setCurrentItem(it)
        if self.list.currentItem() is None and self.list.count():
            self.list.setCurrentRow(0)
        self._show()

    def selected(self):
        it = self.list.currentItem()
        return profiles.load(it.data(Qt.ItemDataRole.UserRole)) if it else None

    def _sections(self):
        return [s for s, cb in self.checks.items() if cb.isChecked()]

    def _show(self):
        p = self.selected()
        have = p is not None
        for b in (self.b_del, self.b_save, self.b_apply):
            b.setEnabled(have)
        self.table.setRowCount(0)
        if not have:
            self.title.setText("No profile yet. Press <b>New…</b> to save this job's values as the profile of "
                               "its system.")
            return
        self.title.setText("<b>%s</b> – %s; updated %s%s" % (
            p["name"], profiles.describe_system(p.get("system")), (p.get("updated") or "")[:16].replace("T", " "),
            ("<br>%s" % p["notes"]) if p.get("notes") else ""))
        amber = QColor(theme.AMBER) if hasattr(theme, "AMBER") else QColor("#F5A623")
        for s in profiles.SECTIONS:
            pv = (p.get("sections") or {}).get(s)
            cv = self.current.get(s)
            if pv is None and cv is None:
                continue
            r = self.table.rowCount()
            self.table.insertRow(r)
            hdr = QTableWidgetItem(profiles.SECTION_TITLES[s] + ("" if s in self.owned else "  (kept by the other plugin)"))
            f = hdr.font()
            f.setBold(True)
            hdr.setFont(f)
            self.table.setItem(r, 0, hdr)
            self.table.setItem(r, 1, QTableWidgetItem(("from %s" % pv.get("by")) if pv and pv.get("by") else
                                                      ("" if pv else "not in the profile")))
            self.table.setItem(r, 2, QTableWidgetItem("" if cv is not None else "–"))
            for key, label, unit in profiles.FIELDS[s]:
                a = (pv or {}).get(key)
                b = (cv or {}).get(key)
                if a is None and b is None:
                    continue
                r = self.table.rowCount()
                self.table.insertRow(r)
                lab = "    %s%s" % (label, (" (%s)" % unit) if unit else "")
                self.table.setItem(r, 0, QTableWidgetItem(lab))
                ia, ib = QTableWidgetItem(profiles.fmt(a, unit)), QTableWidgetItem(profiles.fmt(b, unit))
                if pv is not None and cv is not None and not profiles._same(a, b, unit):
                    ia.setForeground(amber)
                    ib.setForeground(amber)
                self.table.setItem(r, 1, ia)
                self.table.setItem(r, 2, ib)
        self.table.resizeRowsToContents()

    # ------------------------------------------------------------------
    def new_profile(self):
        sugg = self.ident.get("unit_name") or ""
        if self.ident.get("unit_serial"):
            sugg = ("%s (%s)" % (sugg, self.ident["unit_serial"])).strip() if sugg else self.ident["unit_serial"]
        name = theme.get_text(self, "New system profile", "Name of the system (e.g. the van it is on):",
                              text=sugg or "Mapper system") if hasattr(theme, "get_text") else None
        if isinstance(name, tuple):
            name = name[0] if name[1] else None
        name = (name or "").strip()
        if not name:
            return
        if profiles.load(name) is not None:
            theme.msg_warn(self, self.plugin, "A profile named %s exists already; select it and press "
                           "Save this job's values to the profile, or choose another name." % name)
            return
        p = profiles.new(name, self.ident)
        profiles.update_sections(p, {s: self.current[s] for s in self._sections() if self.current.get(s)},
                                 by=self.by)
        profiles.save(p)
        self.status.setText("Saved %s with this job's %s." % (name, ", ".join(
            profiles.SECTION_TITLES[s].lower() for s in self._sections() if self.current.get(s)) or "values"))
        self.reload(select=name)

    def save_to_profile(self):
        p = self.selected()
        if p is None:
            return
        secs = [s for s in self._sections() if self.current.get(s)]
        if not secs:
            return
        diffs = profiles.differences(p, self.current, secs)
        if diffs and theme.msg_choice(
                self, self.plugin, "<p>Replace these values in <b>%s</b> with this job's?</p>%s" % (
                    p["name"], _diff_text(diffs)), ["Replace", "Cancel"]) != 0:
            return
        if self.ident and not p.get("system"):
            p["system"] = dict(self.ident)
        profiles.update_sections(p, {s: self.current[s] for s in secs}, by=self.by)
        profiles.save(p)
        self.status.setText("%s updated (%s)." % (p["name"], ", ".join(profiles.SECTION_TITLES[s].lower()
                                                                       for s in secs)))
        self.reload(select=p["name"])

    def apply_profile(self):
        p = self.selected()
        if p is None:
            return
        secs = {s: p["sections"][s] for s in self._sections() if s in (p.get("sections") or {})}
        if not secs:
            self.status.setText("%s has none of the checked sections." % p["name"])
            return
        ps = (p.get("system") or {})
        if self.ident and ps and not profiles.match(self.ident, [p]):
            if theme.msg_choice(self, self.plugin,
                                "<p><b>%s</b> was made for another system (%s); this job was recorded by %s.</p>"
                                "<p>Use its values anyway?</p>" % (p["name"], profiles.describe_system(ps),
                                                                    profiles.describe_system(self.ident)),
                                ["Use them", "Cancel"], default=1) != 0:
                return
        self.apply_fn(secs, p)
        self.applied = p["name"]
        self.current = dict(self.current)
        self.current.update({k: v for k, v in secs.items()})
        self.status.setText("This job now uses the %s of %s." % (
            ", ".join(profiles.SECTION_TITLES[s].lower() for s in secs), p["name"]))
        self._show()

    def delete_profile(self):
        p = self.selected()
        if p is None:
            return
        if theme.msg_choice(self, self.plugin, "Delete the system profile %s from this computer?" % p["name"],
                            ["Delete", "Cancel"], default=1) != 0:
            return
        profiles.delete(p["name"])
        self.reload()

    def export_profile(self):
        p = self.selected()
        if p is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export system profile", p["name"] + ".json",
                                              "System profile (*.json)")
        if path:
            profiles.export(p["name"], path)
            self.status.setText("Exported to %s" % path)

    def import_profile(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import system profile", "", "System profile (*.json)")
        if not path:
            return
        try:
            p = profiles.import_file(path)
        except (OSError, ValueError) as e:
            theme.msg_warn(self, self.plugin, str(e))
            return
        self.status.setText("Imported %s" % p["name"])
        self.reload(select=p["name"])


def open_dialog(parent, plugin, ident, current, owned, apply_fn, version=""):
    d = ProfilesDialog(parent, plugin, ident, current, owned, apply_fn, version)
    try:
        theme.mark(d)
    except Exception:       # noqa: BLE001
        pass
    d.exec()
    d.deleteLater()
    return d.applied
