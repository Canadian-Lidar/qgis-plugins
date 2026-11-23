"""PosPro's pages for marine jobs (sonar recordings from the survey boat), on the same steps as a Lidar job:
Data · Installation · Base · Processing · Quality · Calibration · Review · Export. The sidebar, Back / Next and
the header phases are the Lidar ones; each page shows the marine content of its step."""
import datetime
import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QAbstractItemView, QCheckBox, QDoubleSpinBox, QFormLayout,
                                 QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableWidget, QTableWidgetItem,
                                 QVBoxLayout, QWidget)

from ..core import marine

POSITION_CHOICES = [("auto", "Best available: PPK, else the Reach log, else the autopilot"),
                    ("ppk", "PPK (Reach rover against the base)"), ("llh", "Reach real-time solution (.LLH)"),
                    ("mavlink", "Autopilot position (MAVLink)")]
GPS_EPOCH_UNIX = 315964800.0
LEAP = 18.0


def _title(text):
    t = QLabel(text)
    t.setObjectName("ppTitle")
    return t


def _section(text):
    t = QLabel(text.upper())
    t.setObjectName("ppSection")
    t.setStyleSheet("font-weight: 700; letter-spacing: 1px; padding-top: 8px;")
    return t


def _key(text):
    k = QLabel(text)
    k.setObjectName("ppKey")
    return k


def _val(text=""):
    v = QLabel(text)
    v.setObjectName("ppVal")
    v.setWordWrap(True)
    v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return v


def _utc(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _table(headers):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(True)
    return t


def _set_rows(t, rows):
    t.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, v in enumerate(row):
            if isinstance(v, QWidget):
                t.setCellWidget(r, c, v)
            else:
                t.setItem(r, c, QTableWidgetItem("" if v is None else str(v)))
    t.resizeRowsToContents()
    h = t.horizontalHeader().height() + sum(t.rowHeight(r) for r in range(t.rowCount())) + 6
    t.setFixedHeight(min(360, max(60, h)))


class MPage(QWidget):
    """Base of the marine pages: the job's recordings, read once per refresh of the Data page."""

    def __init__(self, dock):
        super().__init__()
        self.dock = dock
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(4, 4, 4, 4)

    @property
    def project(self):
        return self.dock.project

    def cfg(self):
        return self.project.cfg.setdefault("marine", {}) if self.project else {}

    def recs(self):
        return self.dock.marine_recordings()

    def summary(self):
        p = self.project
        return (p.cfg.get("results") or {}).get("marine") if p else None

    def refresh(self):
        pass

    def state(self):
        return "todo", ""


# ------------------------------------------------------------------------------------------------- 1 Data
class MDataPage(MPage):
    """Laid out as the mobile mapping Data page: the recordings (rover data), what they contain, the time range,
    the data coverage and the satellite ephemeris (the same settings and files as mobile mapping)."""

    def __init__(self, dock):
        super().__init__(dock)
        import types
        from qgis.PyQt.QtWidgets import QDateTimeEdit, QGridLayout, QGroupBox, QListWidget
        from . import dock as D
        from .widgets import CoverageView, Section
        self._D = D
        v = self.lay
        v.setContentsMargins(0, 0, 0, 0)
        v.addWidget(D._title("Rover data"))
        v.addWidget(D._hint("The job's sonar recordings (.clraw made by RemixPro from SonarView and the Reach files). "
                            "Each holds every sonar head, the autopilot and, when they were found, the Reach rover "
                            "and base files. Untick a recording to leave it out of the job (PosPro, AlignPro and "
                            "CloudPro); add recordings kept in other folders, such as other days, or SonarView .svlz / "
                            ".svlog recordings (RemixPro converts them into the job's day folder)."))
        self.files = QListWidget()
        self.files.setMinimumHeight(90)
        self.files.setMaximumHeight(180)
        self.files.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.files.itemChanged.connect(self._ticked)
        v.addWidget(self.files)
        self.overlap = QLabel("")
        self.overlap.setWordWrap(True)
        self.overlap.setStyleSheet("color: %s;" % D.theme.RED)
        self.overlap.hide()
        v.addWidget(self.overlap)
        rr = QHBoxLayout()
        b_add = QPushButton("Add recordings\u2026")
        b_add.setToolTip("Add recordings from another folder (another day of the survey): .clraw, or SonarView "
                         ".svlz / .svlog, which RemixPro converts into the job's day folder first")
        b_add.clicked.connect(self._add_recordings)
        b_rm = QPushButton("Remove")
        b_rm.setToolTip("Leave the selected recordings out of the job (added ones are removed from the list)")
        b_rm.clicked.connect(self._remove_recordings)
        rr.addWidget(b_add)
        rr.addWidget(b_rm)
        rr.addStretch(1)
        v.addLayout(rr)
        self.summary = QGroupBox("What the files contain")
        self.form = QFormLayout(self.summary)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        v.addWidget(self.summary)
        tg = QGroupBox("Time range to process (UTC)")
        tl = QGridLayout(tg)
        self.t0, self.t1 = QDateTimeEdit(), QDateTimeEdit()
        for w in (self.t0, self.t1):
            w.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
            D._edit_utc(w)
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
        nl.addWidget(D._hint("All constellations, from the IGS (internet). Without internet, the navigation files "
                             "recorded by the Reach (in the recordings, beside the base) are used; add others here."))
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
        cl.addWidget(D._hint("Mint: data. Red: none. Dashed lines: the processing span. Hover for times."))
        v.addWidget(Section("Data coverage", cw, expanded=True))
        v.addWidget(ng)
        v.addStretch(1)
        # the same handlers as the mobile mapping Data page (cfg nav_files, auto_ephemeris, rover time range)
        for name in ("add_nav", "remove_nav", "store_nav", "store_times", "whole"):
            setattr(self, name, types.MethodType(getattr(D.DataPage, name), self))
        b_nav.clicked.disconnect()
        b_navrm.clicked.disconnect()
        b_all.clicked.disconnect()
        b_nav.clicked.connect(self.add_nav)
        b_navrm.clicked.connect(self.remove_nav)
        b_all.clicked.connect(self.whole)
        self.auto_nav.toggled.disconnect()
        self.auto_nav.toggled.connect(self.store_nav)
        for w in (self.t0, self.t1):
            w.editingFinished.disconnect()
            w.editingFinished.connect(self.store_times)

    # -- the recordings used (DCS Core keeps the selection: every plugin reads it)
    def _store_selection(self, added=None, excluded=None):
        dcs = self.project.dcs if self.project else None
        if dcs is None:
            return
        try:
            dcs.set_sonar_selection(added=added, excluded=excluded, plugin="PosPro")
        except Exception as e:      # noqa: BLE001
            self._D.theme.msg_warn(self, "PosPro", "Cannot change the recordings: %s" % e)
            return
        self.dock.marine_selection_changed()
        self.refresh()

    def _ticked(self, item):
        if getattr(self, "_filling", False):
            return
        dcs = self.project.dcs
        paths = item.data(Qt.ItemDataRole.UserRole) or []
        keys = {os.path.normcase(os.path.abspath(p)) for p in paths}
        off = [f for f in dcs.sonar_excluded() if os.path.normcase(os.path.abspath(f)) not in keys]
        if item.checkState() != Qt.CheckState.Checked:
            off += list(paths)
        self._store_selection(excluded=off)

    def _add_recordings(self):
        from qgis.PyQt.QtWidgets import QFileDialog
        dcs = self.project.dcs
        start = os.path.dirname(dcs.job_folder) if dcs else ""
        fs, _ = QFileDialog.getOpenFileNames(self, "Add sonar recordings", start,
                                             "Sonar recordings (*.clraw *.svlz *.svlog);;RemixPro recordings (*.clraw)"
                                             ";;SonarView recordings (*.svlz *.svlog)")
        if not fs:
            return
        sv = [f for f in fs if f.lower().endswith(marine.SONARVIEW)]
        if sv:
            self._convert_sonarview(sv, [f for f in fs if f not in sv])
            return
        self._add_clraw(fs)

    def _convert_sonarview(self, sv, other):
        """SonarView recordings (PosPro 2.11.0): RemixPro makes their .clraw in the job's day folder, then they are
        added like any other recording."""
        dcs = self.project.dcs

        def work(ctx):
            ctx.progress(0.0, "converting the SonarView recordings with RemixPro")
            return marine.convert_sonarview(dcs, sv, ctx)

        def done(ok, res):
            if not ok:
                if res and "RemixPro" in str(res):
                    self._D.theme.msg_warn(self, "PosPro", str(res).splitlines()[0])
                return
            for n in res.get("notes", []):
                self.dock.log("info", n)
            if res.get("failed"):
                self._D.theme.msg_warn(self, "PosPro", "Not converted:\n" + "\n".join(
                    "%s: %s" % (os.path.basename(p), why) for p, why in res["failed"]))
            self.dock.log("info", "RemixPro made %d .clraw from %d SonarView file(s)" % (len(res["files"]), len(sv)))
            if res["files"] or other:
                self._add_clraw(list(res["files"]) + list(other))
        self.dock.start_task("Converting SonarView recordings", work, done)

    def _add_clraw(self, fs):
        from dcs_core import clraw
        dcs = self.project.dcs
        good = [f for f in fs if clraw.is_sonar(f)]
        if len(good) < len(fs):
            self._D.theme.msg_warn(self, "PosPro", "%d of the files are not sonar recordings and were not added."
                                   % (len(fs) - len(good)))
        if not good:
            return
        keys = {os.path.normcase(os.path.abspath(f)) for f in good}
        added = dcs.sonar_added() + [f for f in good if os.path.normcase(os.path.abspath(f)) not in
                                     {os.path.normcase(os.path.abspath(a)) for a in dcs.sonar_added()}]
        off = [f for f in dcs.sonar_excluded() if os.path.normcase(os.path.abspath(f)) not in keys]
        self._store_selection(added=added, excluded=off)

    def _remove_recordings(self):
        dcs = self.project.dcs
        items = self.files.selectedItems()
        if not items:
            return
        added = list(dcs.sonar_added())
        off = list(dcs.sonar_excluded())
        for it in items:
            paths = it.data(Qt.ItemDataRole.UserRole) or []
            keys = {os.path.normcase(os.path.abspath(p)) for p in paths}
            if it.data(Qt.ItemDataRole.UserRole + 1):          # added from elsewhere: forget it
                added = [a for a in added if os.path.normcase(os.path.abspath(a)) not in keys]
                off = [o for o in off if os.path.normcase(os.path.abspath(o)) not in keys]
            else:                                               # found in the job folder: leave it out
                off += [p for p in paths if os.path.normcase(os.path.abspath(p)) not in
                        {os.path.normcase(os.path.abspath(o)) for o in off}]
        self._store_selection(added=added, excluded=off)

    def add_nav(self):                  # replaced in __init__ by the mobile mapping page's handlers
        pass

    remove_nav = store_nav = store_times = whole = add_nav

    def refresh(self):
        from qgis.PyQt.QtWidgets import QListWidgetItem
        D = self._D
        p = self.project
        self.files.clear()
        self.navs.clear()
        while self.form.rowCount():
            self.form.removeRow(0)
        if p is None:
            return
        if not p.cfg["rover"].get("marine_span") or "marine_runs" not in p.cfg["rover"]:
            self.dock.marine_selection_changed()
        self.dock.show_coverage(self.cov, bases=False)
        self._filling = True
        spans = {}
        for r, paths, used, added in self.dock.marine_recordings_all():
            name = r.name if not isinstance(r, Exception) else os.path.basename(paths[0])
            label = name
            if not isinstance(r, Exception):
                a, b = r.span()
                label = "%s    %s - %s UTC" % (name, _utc(a)[:16], _utc(b)[11:16])
                if used:
                    spans[name] = (a, b)
            if added:
                label += "    (added: %s)" % os.path.dirname(paths[0])
            it = QListWidgetItem(label)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if used else Qt.CheckState.Unchecked)
            it.setData(Qt.ItemDataRole.UserRole, list(paths))
            it.setData(Qt.ItemDataRole.UserRole + 1, bool(added))
            it.setToolTip("\n".join(paths) if not isinstance(r, Exception) else str(r))
            self.files.addItem(it)
        self._filling = False
        ov = marine.overlaps(spans)
        self.overlap.setText("Recordings covering the same time: %s. Untick one of each: the boat cannot be in two "
                             "places at once." % "; ".join("%s and %s" % (a, b) for a, b, _d in ov) if ov else "")
        self.overlap.setVisible(bool(ov))
        for r in self.recs():
            if isinstance(r, Exception):
                self.form.addRow(_key("Recording"), _val(str(r)))
                continue
            t0, t1 = r.span()
            have = []
            if r.rinex("rover") and r.rinex("base"):
                have.append("rover + base RINEX (PPK)")
            elif r.rinex("rover"):
                have.append("rover RINEX (no base)")
            if r.files[0]._m.topic_id("/gnss/llh") is not None:
                have.append("Reach log")
            have.append("autopilot")
            parts = r.metadata.get("source_file")
            n_parts = len(parts) if isinstance(parts, list) else 1
            self.form.addRow(_key(r.name), _val("%s  (%.0f min, %d part%s)" % (
                _utc(t0), (t1 - t0) / 60.0, n_parts, "" if n_parts == 1 else "s")))
            self.form.addRow(_key("Sonar heads"), _val(", ".join("%s (looks %s)" % (h["nickname"], h.get("looks") or "?")
                                                                 for h in r.heads.values())))
            self.form.addRow(_key("Position"), _val(", ".join(have)))
            for n in r.metadata.get("notes") or []:
                self.form.addRow(_key("Note"), _val(n))
        for f in p.cfg.get("nav_files") or []:
            it = QListWidgetItem(os.path.basename(f))
            it.setData(Qt.ItemDataRole.UserRole, f)
            it.setToolTip(f)
            self.navs.addItem(it)
        self.auto_nav.blockSignals(True)
        self.auto_nav.setChecked(bool(p.cfg.get("auto_ephemeris", True)))
        self.auto_nav.blockSignals(False)
        span = p.capture_span()
        if span:
            t0, t1 = span
            u0, u1 = D.geo.gps_to_utc(t0), D.geo.gps_to_utc(t1)
            for w, g in ((self.t0, p.cfg["rover"].get("time_start") or t0),
                         (self.t1, p.cfg["rover"].get("time_end") or t1)):
                w.blockSignals(True)
                w.setMinimumDateTime(D._utc_qdt(int(D.geo.unix_from_utc(u0))))
                w.setMaximumDateTime(D._utc_qdt(int(D.geo.unix_from_utc(u1)) + 1))
                w.setDateTime(D._utc_qdt(int(D.geo.unix_from_utc(D.geo.gps_to_utc(g)))))
                w.blockSignals(False)

    def state(self):
        ok = [r for r in self.recs() if not isinstance(r, Exception)]
        if not ok:
            return "todo", "no sonar recordings in the job"
        ov = marine.overlaps({r.name: r.span() for r in ok})
        if ov:
            return "warn", "%d pair%s of recordings at the same time" % (len(ov), "s" if len(ov) > 1 else "")
        return "done", "%d recording%s" % (len(ok), "s" if len(ok) > 1 else "")


# ------------------------------------------------------------------------------------------------- 2 Installation
class MInstallPage(MPage):
    def __init__(self, dock):
        super().__init__(dock)
        self.lay.addWidget(_title("Installation"))
        self.lay.addWidget(_val("The boat's reference point is the GNSS antenna reference point (ARP) of the position "
                                "receiver. Each sonar head's offset goes from it to the head's acoustic centre, in "
                                "metres forward, starboard, down. They come from the recording (SonarView's Mount "
                                "Offset, as Cerulean documents it: X forward, Y starboard, Z down); type a value to "
                                "use another one. Untick USE to leave a head out of the job (PosPro, AlignPro and "
                                "CloudPro). 'Looks to' comes from the head's own tilt sensor; 'Mounted' follows "
                                "the offsets in use."))
        self.lay.addWidget(_section("Sonar heads"))
        self.t_heads = _table(["Use", "Head", "Looks to", "Mounted", "Forward (m)", "Starboard (m)", "Down (m)",
                               "Source"])
        self.lay.addWidget(self.t_heads)
        self.v_check = _val()
        self.v_check.setStyleSheet("color: #f0ad4e;")
        self.lay.addWidget(self.v_check)
        row = QHBoxLayout()
        self.b_swap = QPushButton("Swap the two heads' offsets")
        self.b_swap.setToolTip("Gives each head the other's offsets (the starboard offset sign follows)")
        self.b_swap.clicked.connect(self._swap)
        row.addWidget(self.b_swap)
        self.b_reset = QPushButton("Use the recorded offsets")
        self.b_reset.clicked.connect(self._reset)
        row.addWidget(self.b_reset)
        row.addStretch(1)
        self.lay.addLayout(row)
        self.lay.addWidget(_section("Mounting (from the data)"))
        self.t_mount = _table(["Head", "Looks to", "Tilt from vertical", "Past vertical (spec / data)",
                               "Outer edge (spec / data)", "Depth"])
        self.t_mount.setToolTip("Tilt: the beam centre's angle from vertical, from the head's own tilt sensor. Past "
                                "vertical: how far the 90° fan reaches under the boat toward the other side - spec "
                                "from the tilt, data from where 99.5 % of the soundings stop. Outer edge: how far out "
                                "from vertical the fan reaches.")
        self.lay.addWidget(self.t_mount)
        self.v_strip = _val()
        self.lay.addWidget(self.v_strip)
        self.v_mwarn = _val()
        self.v_mwarn.setStyleSheet("color: #f0ad4e;")
        self.lay.addWidget(self.v_mwarn)
        row = QHBoxLayout()
        self.b_measure = QPushButton("Measure from the data")
        self.b_measure.setToolTip("Reads each head's tilt and soundings (a few seconds per head); PROCESS does it too")
        self.b_measure.clicked.connect(self._measure)
        row.addWidget(self.b_measure)
        row.addStretch(1)
        self.lay.addLayout(row)
        self.lay.addStretch(1)
        self._spins = {}
        self._loading = False

    def _mounting(self):
        """{head id str: mounting check} from the last processing run, else from MEASURE FROM THE DATA."""
        s = self.summary() or {}
        got = {d: h.get("mounting") for d, h in (s.get("heads") or {}).items() if h.get("mounting")}
        return got or dict(self.cfg().get("mounting_measured") or {})

    def _measure(self):
        if self.project is None:
            return
        from dcs_core import marine as dmarine
        best = {}
        for r in self.recs():
            if isinstance(r, Exception):
                continue
            for did in r.heads:
                m = dmarine.mounting_check(r, did, every=10)
                if m and m["soundings"] > (best.get(str(did)) or {}).get("soundings", 0):
                    best[str(did)] = m
        self.cfg()["mounting_measured"] = best
        res = (self.project.cfg.get("results") or {}).get("marine") or {}
        for d, m in best.items():                           # newer than the last run's: show these
            if d in (res.get("heads") or {}):
                res["heads"][d]["mounting"] = m
        self.project.save()
        self.refresh()

    def _show_mounting(self, heads):
        mnt = self._mounting()
        off = set(self.cfg().get("heads_off") or [])
        rows = []
        for did, h in sorted(heads.items()):
            m = mnt.get(str(did))
            if not m:
                continue
            rows.append(["%s (id %d)" % (h["nickname"], did), m["looks"], "%.1f°" % m["tilt_deg"],
                         "%.1f° / %.1f°" % (m["nominal_reach_deg"], m["reach_deg"]),
                         "%.1f° / %.1f°" % (m["nominal_outer_deg"], m["outer_deg"]),
                         "—" if m.get("depth_m") is None else "%.1f m" % m["depth_m"]])
        _set_rows(self.t_mount, rows)
        used = {str(d): {"mounting": mnt.get(str(d)), "lever_m": self._in_use(d, h)}
                for d, h in heads.items() if str(d) not in off and mnt.get(str(d))}
        pair = marine.mounting_summary(used) if len(used) >= 2 else {}
        if not mnt:
            self.v_strip.setText("Not measured yet.")
        elif not pair:
            self.v_strip.setText("No pair of heads in use looking to opposite sides.")
        else:
            if pair["shared_m"]:
                strip = "Both heads see %.1f m of seabed under the boat at %.1f m (they meet from %.1f m of water " \
                        "down; %.1f m by the spec)." % (pair["shared_m"], pair["depth_m"], pair["meet_depth_m"],
                                                       pair["shared_nominal_m"] or 0.0)
            else:
                strip = "The heads do not meet under the boat at %.1f m: a gap along every line." % pair["depth_m"]
            self.v_strip.setText("%s Heads %.0f cm apart across the boat; tilts differ by %.1f°." % (
                strip, 100 * pair["spacing_m"], pair["tilt_diff_deg"]))
        warn = []
        if pair.get("tilt_diff_warn"):
            warn.append("the heads' tilts differ by %.1f°: set both to the same angle so the strip under the boat "
                        "is even" % pair["tilt_diff_deg"])
        if pair and not pair.get("shared_m"):
            warn.append("no seabed seen by both heads: tilt the heads steeper (smaller angle from vertical)")
        self.v_mwarn.setText("" if not warn else "Check: %s." % "; ".join(warn))
        self.v_mwarn.setVisible(bool(warn))
        return pair

    def _heads(self):
        out = {}
        for r in self.recs():
            if not isinstance(r, Exception):
                for did, h in r.heads.items():
                    out.setdefault(did, h)
        return out

    def _in_use(self, did, h):
        lev = self.cfg().get("levers") or {}
        return lev.get(str(did)) or h.get("lever_m") or None

    @staticmethod
    def _side(lv):
        if not lv:
            return "?"
        return "starboard" if lv[1] > 0 else "port" if lv[1] < 0 else "centre"

    def _mismatch(self):
        """Heads (in use) whose offsets put them on the side opposite to the one they look to."""
        off = set(self.cfg().get("heads_off") or [])
        out = []
        for did, h in sorted(self._heads().items()):
            side = self._side(self._in_use(did, h))
            if str(did) not in off and h.get("looks") in ("port", "starboard") and side in ("port", "starboard") \
                    and side != h["looks"]:
                out.append((did, h, side))
        return out

    def refresh(self):
        self._loading = True
        lev = self.cfg().get("levers") or {}
        off = set(self.cfg().get("heads_off") or [])
        rows = []
        self._spins = {}
        heads = self._heads()
        for did, h in sorted(heads.items()):
            vals = self._in_use(did, h) or [0.0, 0.0, 0.0]
            cb = QCheckBox()
            cb.setChecked(str(did) not in off)
            cb.toggled.connect(lambda on, d=did: self._use(d, on))
            spins = []
            for k in range(3):
                sp = QDoubleSpinBox()
                sp.setRange(-50, 50)
                sp.setDecimals(3)
                sp.setSingleStep(0.005)
                sp.setValue(float(vals[k]))
                sp.valueChanged.connect(lambda _v, d=did: self._edited(d))
                spins.append(sp)
            self._spins[did] = spins
            src = "entered in PosPro" if str(did) in lev else (h.get("lever_source") or "not entered").split(" (")[0]
            rows.append([cb, "%s (id %d)" % (h["nickname"], did), h.get("looks") or "?", self._side(vals)]
                        + spins + [src])
        _set_rows(self.t_heads, rows)
        mm = self._mismatch()
        self.v_check.setText("" if not mm else "Check: %s. A head normally looks to the side it is mounted on: if so "
                             "here, the offsets were entered for the other head in SonarView - SWAP THE TWO HEADS' "
                             "OFFSETS." % "; ".join("%s looks %s but its offsets put it on the %s side" % (
                                 h["nickname"], h["looks"], side) for _d, h, side in mm))
        self.b_swap.setEnabled(len([d for d in heads if str(d) not in off]) == 2)
        self._show_mounting(heads)
        self._loading = False

    def _use(self, did, on):
        if self._loading or self.project is None:
            return
        off = set(self.cfg().get("heads_off") or [])
        off.discard(str(did)) if on else off.add(str(did))
        if len(off) >= len(self._heads()):
            self.refresh()                                  # keep at least one head
            return
        self.cfg()["heads_off"] = sorted(off)
        self.project.save()
        self.refresh()
        self.dock.update_steps()

    def _edited(self, did):
        if self._loading or self.project is None:
            return
        self.cfg().setdefault("levers", {})[str(did)] = [round(sp.value(), 4) for sp in self._spins[did]]
        self.project.save()
        self.refresh()
        self.dock.update_steps()

    def _swap(self):
        if self.project is None:
            return
        off = set(self.cfg().get("heads_off") or [])
        heads = {d: h for d, h in self._heads().items() if str(d) not in off}
        if len(heads) != 2:
            return
        (a, ha), (b, hb) = sorted(heads.items())
        la, lb = self._in_use(a, ha) or [0.0, 0.0, 0.0], self._in_use(b, hb) or [0.0, 0.0, 0.0]
        self.cfg().setdefault("levers", {}).update({str(a): [float(v) for v in lb], str(b): [float(v) for v in la]})
        self.project.save()
        self.refresh()
        self.dock.update_steps()

    def _reset(self):
        if self.project is None:
            return
        self.cfg().pop("levers", None)
        self.project.save()
        self.refresh()
        self.dock.update_steps()

    def state(self):
        heads = self._heads()
        if not heads:
            return "todo", ""
        off = set(self.cfg().get("heads_off") or [])
        used = {d: h for d, h in heads.items() if str(d) not in off}
        missing = [h["nickname"] for d, h in used.items() if not self._in_use(d, h)]
        if missing:
            return "warn", "offsets not entered: %s" % ", ".join(missing)
        if self._mismatch():
            return "warn", "check the offsets: mounted and looking sides differ"
        mnt = self._mounting()
        pair = marine.mounting_summary({str(d): {"mounting": mnt.get(str(d)), "lever_m": self._in_use(d, h)}
                                        for d, h in used.items() if mnt.get(str(d))}) if len(used) >= 2 else {}
        if pair.get("tilt_diff_warn"):
            return "warn", "the two heads are tilted %.1f° differently" % pair["tilt_diff_deg"]
        if pair and not pair.get("shared_m"):
            return "warn", "the two heads do not meet under the boat"
        lev = self.cfg().get("levers") or {}
        return "done", "%d of %d heads%s" % (len(used), len(heads), ", offsets entered" if lev else "")


# ------------------------------------------------------------------------------------------------- 3 Base
class MBasePage(MPage):
    """The same Base page as for mobile mapping (dock.BasePage). The base RINEX stored in the recordings is added
    to its list once; what is set there (position source, frame, epoch, antenna type, ARP height, PPP, CSRS-PPP)
    is what marine PPK uses."""

    SHARED = True

    def __init__(self, dock):
        super().__init__(dock)
        self._synced = None

    @property
    def inner(self):
        return self.dock.page["base"]

    def _sync(self):
        """Add the recordings' base RINEX to the Base page's list (once per job and set of recordings)."""
        p = self.project
        if p is None:
            return
        if not p.cfg["rover"].get("marine_span"):
            span, xyz = marine.rover_extent(p)
            if span:
                p.cfg["rover"]["marine_span"] = list(span)
                p.cfg["rover"]["marine_xyz"] = list(xyz) if xyz else None
                self.dock.save()
        found = marine.recording_base_files(p)
        files = [f for _n, fs in found for f in fs]
        key = (p.path if hasattr(p, "path") else id(p), tuple(files))
        if self._synced == key:
            return
        self._synced = key
        if not files:
            return
        have = next((b for b in p.cfg["bases"] if b.get("from_recordings")), None)
        if have is not None and have.get("name") != marine.RECORDING_BASE:
            have["name"] = marine.RECORDING_BASE
            self.dock.save()
        if have is not None and sorted(have.get("files") or []) == sorted(files):
            return
        obs = [f for f in files if marine.pipeline_obs(f)]
        if not obs:
            return
        known = marine.recording_base_known(p)

        def added(b):
            if b is None:
                return
            b["from_recordings"] = True
            b.setdefault("note", "base RINEX stored in the sonar recordings")
            b["files"] = files                         # every recording's base file and its navigation files
            if have is not None:                       # recordings added: keep what was set on the old entry
                for k in ("xyz", "pos_source", "frame", "epoch", "antenna", "delta_hen", "ppp", "csrs", "note"):
                    if k in have:
                        b[k] = have[k]
                p.remove_base(have["id"])
                p.cfg["base_selected"] = b["id"]
            elif known:
                from ..core import geo
                lat, lon, h, frame, epoch, note = known
                b["xyz"] = list(geo.llh2ecef(lat, lon, h))
                b["pos_source"], b["frame"], b["epoch"], b["note"] = "manual", frame, epoch, note
            if not p.cfg.get("base_selected"):
                p.cfg["base_selected"] = b["id"]
            self.dock.save()
            self.inner.refresh()
            self.dock.update_steps()
        self.inner.add_base_file(obs + [f for f in files if f not in obs], marine.RECORDING_BASE, then=added)

    def refresh(self):
        try:
            self._sync()
        except Exception as e:      # noqa: BLE001
            self.dock.log("warning", "base RINEX from the recordings: %s" % e)
        self.inner.refresh()

    def state(self):
        p = self.project
        if p is None:
            return "todo", ""
        b = p.base()
        if not p.cfg["bases"]:
            return "warn", "no base (no PPK): add one with ADD RINEX… or DOWNLOAD…"
        if b is None or not b.get("xyz"):
            return "todo", "choose a base and its position"
        uncovered = []                                  # 2.8.0: each recording uses the base that covers it
        for r in self.recs():
            if isinstance(r, Exception):
                continue
            got, _why = marine.choose_base(p, r)
            if got is None or marine.base_overlap(got, *r.span()) < 0.5 * (r.span()[1] - r.span()[0]):
                uncovered.append(r.name)
        if uncovered:
            return "warn", "no base covers %s: add %s base" % (
                ", ".join(uncovered[:3]) + (" +%d" % (len(uncovered) - 3) if len(uncovered) > 3 else ""),
                "that day's" if len(uncovered) == 1 else "those days'")
        for bb in marine.usable_bases(p):
            if bb.get("pos_source") == "rinex":
                return "warn", "%s: position from the RINEX header (approximate): enter the mark or run PPP" % bb.get(
                    "name")
        if b.get("pos_source") == "rinex":
            return "warn", "base position from the RINEX header (approximate): enter the mark or run PPP"
        if not float((b.get("delta_hen") or [0])[0]) or not (b.get("antenna") or "").strip():
            return "warn", "check the base antenna type and ARP height"
        return "done", "%s (%s)" % (b.get("name"), {"manual": "entered", "ppp": "built-in PPP",
                                                     "csrs": "CSRS-PPP"}.get(b.get("pos_source"), ""))


# ------------------------------------------------------------------------------------------------- 4 Processing
class MProcessPage(MPage):
    """The mobile mapping Processing page (GNSS only, PPK, the same GNSS settings) with the Boat group; this object
    carries the marine step state."""
    SHARED = True

    def refresh(self):
        self.dock.page["process"].refresh()

    def state(self):
        try:
            st, why = self.project.dcs.state("pospro")
        except Exception:       # noqa: BLE001
            st, why = "none", []
        s = self.summary()
        if not s:
            return "todo", ""
        if s.get("settings") is not None and s["settings"] != marine.settings_key(marine.options(self.project), self.project):
            return "warn", "settings changed since: process again"
        if st == "stale":
            return "warn", "out of date: %s" % "; ".join(why)
        return ("done", "finalized") if st == "done" else ("todo", "")


# ------------------------------------------------------------------------------------------------- 5 Quality
class _Hosted(MPage):
    """A step that shows the mobile mapping page itself on a marine job (the marine processing writes the same
    results): the dock shows dock.page[KEY] for it; this object only carries its state for the step list."""
    KEY = ""
    SHARED = True

    def refresh(self):
        self.dock.page[self.KEY].refresh()

    def state(self):
        return self.dock.page[self.KEY].state()


class MQualityPage(_Hosted):
    KEY = "quality"


class MCalibPage(MPage):
    def __init__(self, dock):
        super().__init__(dock)
        self.lay.addWidget(_title("Calibration"))
        self.lay.addWidget(_val("Not used for marine jobs: the boat has one GNSS antenna and no IMU lever arm to "
                                "solve. The sonar heads' angles and offsets are checked and calibrated in AlignPro's "
                                "sonar patch test."))
        self.lay.addStretch(1)

    def state(self):
        return "done", "not used for marine"


# ------------------------------------------------------------------------------------------------- 7 Review
class MReviewPage(_Hosted):
    KEY = "review"


class MExportPage(_Hosted):
    KEY = "export"


PAGES = {"data": MDataPage, "install": MInstallPage, "base": MBasePage, "process": MProcessPage,
         "quality": MQualityPage, "calib": MCalibPage, "review": MReviewPage, "export": MExportPage}
