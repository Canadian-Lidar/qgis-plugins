"""PosPro's pages for marine jobs (sonar recordings from the survey boat), on the same steps as a Lidar job:
Data · Installation · Base · Processing · Quality · Calibration · Review · Export. The sidebar, Back / Next and
the header phases are the Lidar ones; each page shows the marine content of its step."""
import datetime
import os
import shutil

import numpy as np
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                                 QGridLayout,
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
    def __init__(self, dock):
        super().__init__(dock)
        self.lay.addWidget(_title("Data"))
        self.lay.addWidget(_val("The job's sonar recordings (.clraw made by RemixPro from SonarView and the Reach "
                                "files). Each recording holds every sonar head, the autopilot and, when they were "
                                "found, the Reach rover and base files."))
        self.lay.addWidget(_section("Recordings"))
        self.t_recs = _table(["Recording", "Start (UTC)", "Length", "Parts", "Sonar heads", "Position in the recording"])
        self.lay.addWidget(self.t_recs)
        self.v_notes = _val()
        self.lay.addWidget(self.v_notes)
        self.lay.addStretch(1)

    def refresh(self):
        rows, notes = [], []
        for r in self.recs():
            if isinstance(r, Exception):
                rows.append([str(r), "", "", "", "", ""])
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
            rows.append([r.name, _utc(t0), "%.0f min" % ((t1 - t0) / 60.0), n_parts,
                         ", ".join("%s (looks %s)" % (h["nickname"], h.get("looks") or "?") for h in r.heads.values()),
                         ", ".join(have)])
            for n in r.metadata.get("notes") or []:
                notes.append("%s: %s" % (r.name, n))
        _set_rows(self.t_recs, rows)
        self.v_notes.setText("\n".join(notes))

    def state(self):
        ok = [r for r in self.recs() if not isinstance(r, Exception)]
        if not ok:
            return "todo", "no sonar recordings in the job"
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

    def __init__(self, dock):
        super().__init__(dock)
        from .dock import BasePage
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.inner = BasePage(dock)
        self.lay.addWidget(self.inner)
        self._synced = None

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
        if b.get("pos_source") == "rinex":
            return "warn", "base position from the RINEX header (approximate): enter the mark or run PPP"
        if not float((b.get("delta_hen") or [0])[0]) or not (b.get("antenna") or "").strip():
            return "warn", "check the base antenna type and ARP height"
        return "done", "%s (%s)" % (b.get("name"), {"manual": "entered", "ppp": "built-in PPP",
                                                     "csrs": "CSRS-PPP"}.get(b.get("pos_source"), ""))


# ------------------------------------------------------------------------------------------------- 4 Processing
class MProcessPage(MPage):
    def __init__(self, dock):
        super().__init__(dock)
        self.lay.addWidget(_title("Processing"))
        f = QFormLayout()
        self.c_pos = QComboBox()
        for k, label in POSITION_CHOICES:
            self.c_pos.addItem(label, k)
        f.addRow(_key("Position"), self.c_pos)
        self.s_cog = QDoubleSpinBox()
        self.s_cog.setRange(0.0, 5.0)
        self.s_cog.setSingleStep(0.1)
        self.s_cog.setDecimals(1)
        self.s_cog.setSuffix(" m/s")
        self.s_cog.setToolTip("Heading = course over ground above this speed, the autopilot's compass below it")
        f.addRow(_key("Course over ground above"), self.s_cog)
        self.lay.addLayout(f)
        self.lay.addWidget(_val("The boat's trajectory at the GNSS antenna: PPK forward and backward, combined; the "
                                "heading from the course over ground (the compass when slow); each sonar head's own "
                                "attitude at every ping (its fused packet 504), checked against its raw motion sensor. "
                                "Processing finalizes the trajectory for AlignPro and CloudPro."))
        row = QHBoxLayout()
        self.b_run = QPushButton("Process")
        self.b_run.setObjectName("ppPrimary")
        self.b_run.clicked.connect(self.dock.run_marine)
        row.addWidget(self.b_run)
        row.addStretch(1)
        self.lay.addLayout(row)
        self.v_last = _val()
        self.lay.addWidget(self.v_last)
        self.lay.addStretch(1)
        self._loading = False
        self.c_pos.currentIndexChanged.connect(self._store)
        self.s_cog.valueChanged.connect(self._store)

    def refresh(self):
        o = marine.options(self.project)
        self._loading = True
        self.c_pos.setCurrentIndex(max(0, self.c_pos.findData(o["position"])))
        self.s_cog.setValue(float(o["cog_min_speed"]))
        self._loading = False
        s = self.summary()
        self.v_last.setText("Last run: %s" % ", ".join("%s %s, %.1f %% fixed" % (
            r["name"], r["source_label"], (r.get("quality_pct") or {}).get("fixed", 0.0)) for r in s["recordings"])
            if s else "Not processed.")

    def _store(self, *_):
        p = self.project
        if p is None or self._loading:
            return
        m = self.cfg()
        m["position"] = self.c_pos.currentData()
        m["cog_min_speed"] = self.s_cog.value()
        p.save()

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
class MQualityPage(MPage):
    def __init__(self, dock):
        super().__init__(dock)
        from .plots import TimePlot
        self.lay.addWidget(_title("Quality"))
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(14)
        self.lay.addLayout(self.grid)
        self.lay.addWidget(_section("Over time"))
        self.p_q = TimePlot("Position solution", "")
        self.p_sd = TimePlot("Estimated accuracy (horizontal / vertical)", "m")
        self.p_v = TimePlot("Speed (heading from the compass below the line)", "m/s")
        for pl in (self.p_q, self.p_sd, self.p_v):
            pl.setMinimumHeight(130)
            self.lay.addWidget(pl)
            pl.xRangeChanged.connect(self._sync)
        self.lay.addWidget(_section("Sonar heads"))
        self.v_heads = _val()
        self.lay.addWidget(self.v_heads)
        self.lay.addStretch(1)

    def _sync(self, x0, x1):
        for pl in (self.p_q, self.p_sd, self.p_v):
            if (pl.x0, pl.x1) != (x0, x1):
                pl.set_xrange(x0, x1)

    def refresh(self):
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        for pl in (self.p_q, self.p_sd, self.p_v):
            pl.clear()
        s = self.summary()
        if not s:
            self.grid.addWidget(_val("Process first."), 0, 0)
            self.v_heads.setText("")
            return
        for c, h in enumerate(["Recording", "Position", "Fixed", "Rate", "Compass heading"]):
            self.grid.addWidget(_key(h), 0, c)
        for r_, rec in enumerate(s.get("recordings", []), 1):
            q = rec.get("quality_pct") or {}
            for c, v in enumerate([rec["name"], rec["source_label"], "%.1f %%" % q.get("fixed", 0.0),
                                   "%s Hz" % rec.get("rate_hz"), "%.1f %% of the time" % rec.get("compass_pct", 0.0)]):
                self.grid.addWidget(_val(v), r_, c)
        T = self.dock.marine_trajectory()
        if T is not None and len(T):
            x = T["t"] - GPS_EPOCH_UNIX + LEAP
            cols = {1: "#2ecc71", 2: "#f0ad4e", 3: "#5bc0de", 4: "#5bc0de", 5: "#d9534f", 6: "#9b59b6", 9: "#9aa4ad"}
            self.p_q.set_strip(x, T["q"].astype(int), {k: QColor(v) for k, v in cols.items()})
            self.p_sd.add("horizontal", x, np.hypot(T["sdn"], T["sde"]), "#5bc0de")
            self.p_sd.add("vertical", x, T["sdu"], "#f0ad4e")
            self.p_v.add("speed", x, T["speed"], "#2ecc71")
            self.p_v.add("COG limit", x[[0, -1]], np.full(2, float(s.get("cog_min_speed", 0.5))), "#d9534f", 1.0)
            comp = np.flatnonzero(np.diff(np.r_[0, T["hsrc"].astype(int), 0]))
            for a, b in zip(comp[::2], comp[1::2]):
                self.p_v.add_band(x[a], x[min(b, len(x) - 1)], "#4d5a66")
        lines = []
        for did, h in sorted((s.get("heads") or {}).items()):
            ck = h.get("check") or {}
            lines.append("%s (id %s, looks %s): %s attitudes (the sonar's own, at every ping)%s" % (
                h.get("nickname"), did, h.get("looks") or "?", "{:,}".format(h.get("attitudes", 0)),
                "; its raw motion sensor (%s Hz) agrees to %.2f° in roll and %.2f° in pitch (68 %%)" % (
                    ck.get("imu_505_rate_hz"), (ck.get("fused_505_vs_504_roll_deg") or [0, 0])[1],
                    (ck.get("fused_505_vs_504_pitch_deg") or [0, 0])[1]) if ck else ""))
        self.v_heads.setText("\n".join(lines + (s.get("notes") or [])))

    def state(self):
        s = self.summary()
        if not s:
            return "todo", ""
        fx = float(s.get("fixed_pct") or 0.0)
        if "ppk" in (s.get("position_source") or []) and fx < 95:
            return "warn", "%.1f %% fixed" % fx
        return "done", ("%.1f %% fixed" % fx) if "ppk" in (s.get("position_source") or []) else \
            ", ".join(s.get("position_source") or [])


# ------------------------------------------------------------------------------------------------- 6 Calibration
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
class MReviewPage(MPage):
    def __init__(self, dock):
        super().__init__(dock)
        self.lay.addWidget(_title("Review"))
        self.lay.addWidget(_val("The stretches of the trajectory to look at before AlignPro and CloudPro: where the "
                                "position was not fixed and where the heading came from the compass (slow or "
                                "stopped). Soundings recorded there are placed less accurately."))
        self.t = _table(["From (UTC)", "To (UTC)", "Length", "What"])
        self.lay.addWidget(self.t)
        self.lay.addStretch(1)

    def refresh(self):
        T = self.dock.marine_trajectory()
        rows = []
        if T is not None and len(T):
            for what, m in (("position not fixed", T["q"] != 1), ("heading from the compass", T["hsrc"] == 1)):
                e = np.flatnonzero(np.diff(np.r_[0, m.astype(int), 0]))
                for a, b in zip(e[::2], e[1::2]):
                    t0, t1 = T["t"][a], T["t"][min(b, len(T) - 1)]
                    if t1 - t0 >= 2.0:
                        rows.append((t0, [_utc(t0)[11:], _utc(t1)[11:], "%.0f s" % (t1 - t0), what]))
        rows.sort(key=lambda r: r[0])
        _set_rows(self.t, [r[1] for r in rows[:500]])

    def state(self):
        return ("done", "optional") if self.summary() else ("todo", "")


# ------------------------------------------------------------------------------------------------- 8 Export
class MExportPage(MPage):
    def __init__(self, dock):
        super().__init__(dock)
        self.lay.addWidget(_title("Export"))
        self.lay.addWidget(_val("Only for other software: AlignPro and CloudPro already read the trajectory PosPro "
                                "finalized. Writes the SBET and SMRMSG (at the GNSS antenna, GPS seconds of week) and "
                                "a text file: UTC time, latitude, longitude, ellipsoidal height, heading, speed, "
                                "solution."))
        row = QHBoxLayout()
        self.b_export = QPushButton("Export…")
        self.b_export.clicked.connect(self._export)
        row.addWidget(self.b_export)
        row.addStretch(1)
        self.lay.addLayout(row)
        self.v_done = _val()
        self.lay.addWidget(self.v_done)
        self.lay.addStretch(1)

    def _export(self):
        p = self.project
        T = self.dock.marine_trajectory()
        if p is None or T is None:
            return
        d = QFileDialog.getExistingDirectory(self, "Export the boat's trajectory to", p.out_dir)
        if not d:
            return
        written = []
        for name in ("trajectory_sbet.out", "trajectory_smrmsg.out"):
            src = os.path.join(p.out_dir, name)
            if os.path.exists(src):
                shutil.copyfile(src, os.path.join(d, "%s_%s" % (p.name, name)))
                written.append("%s_%s" % (p.name, name))
        txt = os.path.join(d, "%s_trajectory.txt" % p.name)
        with open(txt, "w") as fh:
            fh.write("utc,lat_deg,lon_deg,h_m,heading_deg,speed_m_s,solution\n")
            for r in T:
                fh.write("%s,%.9f,%.9f,%.3f,%.2f,%.2f,%s\n" % (
                    datetime.datetime.fromtimestamp(r["t"], datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3],
                    r["lat"], r["lon"], r["h"], np.degrees(r["heading"]) % 360, r["speed"],
                    marine.Q_NAMES.get(int(r["q"]), str(int(r["q"])))))
        written.append(os.path.basename(txt))
        self.cfg()["exported"] = {"folder": d, "files": written}
        p.save()
        self.v_done.setText("Written in %s:\n%s" % (d, "\n".join(written)))
        self.dock.update_steps()

    def state(self):
        e = self.cfg().get("exported") if self.project else None
        return ("done", "exported") if e else ("todo", "optional")


PAGES = {"data": MDataPage, "install": MInstallPage, "base": MBasePage, "process": MProcessPage,
         "quality": MQualityPage, "calib": MCalibPage, "review": MReviewPage, "export": MExportPage}
