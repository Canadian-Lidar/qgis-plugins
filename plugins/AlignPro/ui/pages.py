"""AlignPro workflow pages (PosPro layout): Data, Installation, Quality, Pairs, Calibration, Analyze, Report."""
import datetime as dt
import os

import numpy as np
from qgis.PyQt.QtCore import QSettings, Qt, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QDesktopServices, QGuiApplication
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
                                 QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QMenu,
                                 QPlainTextEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget)

from ..core import engine, geo, report
from . import theme
from .common import PlotWidget, cell, fmt, make_table
from .dialogs import TrajectoryBox
from .widgets import Card, KV, Section, VehicleView

KEYS = ("roll", "pitch", "heading")


def _hint(text):
    l = QLabel(text)
    l.setObjectName("ppHint")
    l.setWordWrap(True)
    return l


def _title(text):
    l = QLabel(text)
    l.setObjectName("ppTitle")
    return l


def _primary(text):
    b = QPushButton(text)
    b.setObjectName("ppPrimary")
    return b


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


def _row(*widgets, stretch_at=None):
    h = QHBoxLayout()
    h.setSpacing(8)
    for i, w in enumerate(widgets):
        if w is None:
            h.addStretch(1)
        elif isinstance(w, str):
            h.addWidget(QLabel(w))
        else:
            h.addWidget(w)
    return h


def _group(title, layout_cls=QVBoxLayout):
    g = QGroupBox(title)
    l = layout_cls(g)
    return g, l


def mapper_text(a):
    return "Roll %.4f   Pitch %.4f   Heading %.4f" % tuple(a)


class Page(QWidget):
    changed = pyqtSignal()

    def __init__(self, dock):
        super().__init__()
        self.dock = dock
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(4, 6, 4, 10)
        self.lay.setSpacing(10)

    @property
    def p(self):
        return self.dock.p

    def refresh(self):
        pass

    def state(self):
        return "todo", ""


# ===================================================================== 1 DATA
class DataPage(Page):
    def __init__(self, dock):
        super().__init__(dock)
        self.lay.addWidget(_title("Survey lines"))
        self.lay.addWidget(_hint("The project's captures (recorded in the shared project, usually by PosPro) are used "
                                 "automatically: their .clraw files are found in each capture folder. Add more .clraw "
                                 "files or folders here if needed. One pass per Start/Stop, or several passes in one "
                                 "capture: they are split at the turns and the stops are dropped."))
        self.caps = KV()
        self.lay.addWidget(self.caps)
        self.files = QListWidget()
        self.files.setMaximumHeight(110)
        self.lay.addWidget(self.files)
        b_add = QPushButton("Add files…")
        b_add.clicked.connect(self.add_files)
        b_dir = QPushButton("Add folder…")
        b_dir.clicked.connect(self.add_folder)
        b_rem = QPushButton("Remove")
        b_rem.clicked.connect(self.remove)
        self.b_read = _primary("Read files")
        self.b_read.clicked.connect(self.read)
        self.lay.addLayout(_row(b_add, b_dir, b_rem, None, self.b_read))
        self.traj = TrajectoryBox({"type": "realtime"})
        self.lay.addWidget(self.traj)
        g, gl = _group("What the files contain")
        self.kv = KV()
        gl.addWidget(self.kv)
        self.warn = QLabel("")
        self.warn.setWordWrap(True)
        self.warn.setStyleSheet("color: %s;" % theme.AMBER)
        gl.addWidget(self.warn)
        self.lay.addWidget(g)
        g2, gl2 = _group("Lines")
        gl2.addWidget(_hint("Untick a line to leave it out; the pairs and planar surfaces are found again. Colours "
                            "match the tracks on the map and the slice views."))
        self.tbl = make_table(["Use", "Line", "COG", "SOG m/s", "Length m", "Dur s", "RTK %", "PPS %", "Lost",
                               "Points", "Range m", "Err before", "Err after", "Files"])
        self.tbl.setMinimumHeight(220)
        self.tbl.itemChanged.connect(self._use)
        gl2.addWidget(self.tbl)
        self.lay.addWidget(g2)
        self.lay.addStretch(1)

    def _inputs(self):
        return [self.files.item(i).text() for i in range(self.files.count())]

    def add_files(self):
        ps, _ = QFileDialog.getOpenFileNames(self, "Select .clraw files", QSettings().value("AlignPro/last_dir", ""),
                                             "Mapper recordings (*.clraw)")
        for x in ps:
            self.files.addItem(x)

    def add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Capture or export folder", QSettings().value("AlignPro/last_dir", ""))
        if d:
            self.files.addItem(d)

    def remove(self):
        for it in self.files.selectedItems():
            self.files.takeItem(self.files.row(it))

    def read(self):
        if self.p is None:
            return
        self.p.cfg["files"] = self._inputs()
        self.p.cfg["trajectory"] = self.traj.value()
        self.p.cfg["boxes"] = {}
        self.p.save()
        if self.p.dcs is not None and not self.p.dcs.capture_files() and self._inputs():
            from ..core import clraw
            found = clraw.split_navigation(clraw.find_clraw(self._inputs()))[0]
            if found:           # AlignPro is the first stage on this job: record the raw files it uses
                self.p.dcs.set_capture_files(found, "AlignPro")
                self.p.cfg["files"] = []
                self.p.save()
        self.dock.open_project(self.p.project_path)

    def _use(self, item):
        if item.column() == 0 and self.p is not None:
            self.dock.set_line_use(item.row(), item.checkState() == Qt.CheckState.Checked)

    def refresh(self):
        p = self.p
        if p is None:
            return
        self.files.clear()
        for f in p.cfg["files"]:
            self.files.addItem(f)
        c = p.cfg["trajectory"]
        self.traj.set_type(c.get("type"))
        self.traj.path.setText(c.get("path", ""))
        self.traj.leap.setValue(int(c.get("leap", 18)))
        self.traj.auto_date.setChecked(not c.get("date"))
        self.caps.clear()
        if p.dcs is not None:
            st, why = p.dcs.state("pospro")
            words = {"done": "published and current", "stale": "published, but out of date: " + "; ".join(why),
                     "running": "PosPro is processing now", "failed": "the last PosPro run failed",
                     "none": "not run yet: run PosPro on this project (the real-time trajectory is used meanwhile)"}
            self.traj.set_pospro_state(st in ("done", "stale"), "PosPro: " + words.get(st, st))
            caps = p.dcs.captures()
            self.caps.set("Job folder", p.dcs.job_folder)
            self.caps.set("Captures", "%d in the shared project%s" % (
                len(caps), "" if caps else " (the job folder is searched)"),
                "ok" if caps else "warn")
            miss = p.dcs.missing_captures()
            if miss:
                self.caps.set("Missing", "%d raw file(s): project menu \u203a Relocate the job folder" % len(miss),
                              "bad")
        else:
            self.traj.set_pospro_state(False, "Older stand-alone project: no PosPro trajectory to read.")
            self.traj.r_pp.setEnabled(False)
        self.kv.clear()
        if not p.lines:
            self.kv.set("Status", "not read yet")
            return
        t0 = min(l.window[0] for l in p.lines)
        t1 = max(l.window[1] for l in p.lines)
        u = lambda t: dt.datetime.utcfromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731
        self.kv.set("Recording", "%s  to  %s UTC" % (u(t0), u(t1)[11:]))
        caps = len({l.info["capture"] for l in p.lines})
        self.kv.set("Lines", "%d passes from %d capture(s), %d used" % (len(p.lines), caps,
                                                                        sum(l.use for l in p.lines)))
        h = p.lines[0].info.get("hesai") or {}
        hw = p.lines[0].info.get("hardware") or {}
        self.kv.set("Lidar", "%s · %s · %s rpm · %s mm unit" % (
            hw.get("lidar_model", "Hesai"), h.get("mode", "-"), h.get("rpm", "-"), h.get("dist_unit_mm", "-")))
        self.kv.set("Channel calibration", "from the .clraw" if p.lines[0].info.get("calibration") else
                    "missing: nominal angles used", None if p.lines[0].info.get("calibration") else "warn")
        self.kv.set("Points kept", "%s (budget %s per line)" % (
            "{:,}".format(sum(l.n for l in p.lines)), "{:,}".format(int(p.settings["points_per_line"]))))
        tr = p.traj
        rtk = [l.stats.get("fix_rtk_pct") for l in p.lines if l.stats.get("fix_rtk_pct") is not None]
        self.kv.set("Trajectory", "%s · %.0f Hz" % (tr.source, tr.rate()))
        if rtk:
            m = min(rtk)
            self.kv.set("RTK fixed", "%.0f %% (worst line)" % m, "ok" if m >= 99 else ("warn" if m > 80 else "bad"))
        self.kv.set("Mapper", "%s · %s" % (p.lines[0].info.get("mapper_version") or "-",
                                                hw.get("coordinate_system", {}).get("definition", "")))
        self.warn.setText("\n".join("⚠ " + w for w in p.warnings))
        self.fill_lines()

    def fill_lines(self):
        p = self.p
        t = self.tbl
        t.blockSignals(True)
        r = p.cfg.get("results") or {}
        eb = dict(zip(r.get("lines", []), r.get("line_err_before_cm", [])))
        ea = dict(zip(r.get("lines", []), r.get("line_err_after_cm", [])))
        t.setRowCount(len(p.lines))
        for i, ln in enumerate(p.lines):
            s = ln.stats
            u = cell("")
            u.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            u.setCheckState(Qt.CheckState.Checked if ln.use else Qt.CheckState.Unchecked)
            t.setItem(i, 0, u)
            name = cell("■ " + ln.name, ln.color, False)
            t.setItem(i, 1, name)
            rtk, pps = s.get("fix_rtk_pct"), s.get("pps_lock_pct")
            vals = [(fmt(s.get("cog"), "%.1f"), None), (fmt(s.get("sog"), "%.1f"), None),
                    (fmt(s.get("length"), "%.0f"), None), (fmt(s.get("duration"), "%.1f"), None),
                    (fmt(rtk, "%.0f"), None if rtk is None else (theme.GREEN if rtk >= 99 else theme.AMBER
                                                                 if rtk > 80 else theme.RED)),
                    (fmt(pps, "%.0f"), None if pps is None else (theme.GREEN if pps >= 100 else theme.RED)),
                    (s.get("lost", "-"), theme.RED if s.get("lost") else None), ("{:,}".format(ln.n), None),
                    (fmt(s.get("median_range"), "%.1f"), None), (fmt(eb.get(ln.name), "%.2f"), None),
                    (fmt(ea.get(ln.name), "%.2f"), theme.GREEN if ea.get(ln.name) is not None else None)]
            for j, (v, c) in enumerate(vals):
                t.setItem(i, j + 2, cell(v, c))
            t.setItem(i, 13, cell(", ".join(ln.info["files"]), None, False))
            if not s.get("traj_covers", True):
                name.setToolTip("The trajectory does not cover this whole pass")
        t.blockSignals(False)

    def state(self):
        p = self.p
        if p is None or not p.lines:
            return "todo", "add .clraw captures"
        d = sum(l.window[1] - l.window[0] for l in p.lines if l.use)
        st = "warn" if p.warnings else "done"
        return st, "%d lines · %d min %02d s" % (sum(l.use for l in p.lines), d // 60, d % 60)


# ============================================================= 2 INSTALLATION
class InstallPage(Page):
    def __init__(self, dock):
        super().__init__(dock)
        self.lay.addWidget(_title("Lidar installation"))
        self.lay.addWidget(_hint("Lever arm from the Certus reference point to the Lidar optical centre, in the "
                                 "vehicle frame (Forward, Starboard, Down). Lever arms are measured, not solved, "
                                 "as is usual for a patch test. Values start from the capture's hardware record "
                                 "(shared in the project); a corrected lever arm is kept in alignment.json only."))
        self.view = VehicleView()
        self.view.setMinimumHeight(240)
        self.lay.addWidget(self.view)
        g, gl = _group("Survey sensor", QGridLayout)
        gl.setHorizontalSpacing(12)
        self.lever, self.rec_lever, self.mount, self.rec_mount = [], [], [], []
        for c, h in enumerate(("", "Forward / Roll", "Starboard / Pitch", "Down / Heading")):
            if h:
                k = QLabel(h.upper())
                k.setObjectName("ppKey")
                gl.addWidget(k, 0, c)
        rows = (("Lever arm (m)", 1), ("Recorded", 2), ("Mounting angles (deg)", 3), ("Recorded", 4))
        for name, r in rows:
            k = QLabel(name.upper())
            k.setObjectName("ppKey")
            gl.addWidget(k, r, 0)
        for i in range(3):
            s = _dspin(-50, 50, 4, 0.01)
            self.lever.append(s)
            gl.addWidget(s, 1, i + 1)
            lb = QLabel("-")
            lb.setObjectName("ppVal")
            self.rec_lever.append(lb)
            gl.addWidget(lb, 2, i + 1)
            m = _dspin(-360, 360, 4, 0.01)
            self.mount.append(m)
            gl.addWidget(m, 3, i + 1)
            lb2 = QLabel("-")
            lb2.setObjectName("ppVal")
            self.rec_mount.append(lb2)
            gl.addWidget(lb2, 4, i + 1)
        k = QLabel("Lidar latency (ms)")
        k.setObjectName("ppKey")
        gl.addWidget(k, 5, 0)
        self.lat = _dspin(-500, 500, 3, 0.5)
        self.lat.setToolTip("Added to the Lidar time. Not needed while the PPS time lock is good.")
        gl.addWidget(self.lat, 5, 1)
        for s in self.lever:
            s.valueChanged.connect(self._draw)
        self.lay.addWidget(g)
        self.lay.addWidget(_hint("Mounting angles use the mapper's own convention: M = Rz(h) Ry(p) Rx(−r) "
                                 "Z0; roll + starboard down, pitch + bow up, heading + clockwise."))
        b_rev = QPushButton("Revert to recorded")
        b_rev.clicked.connect(self.revert)
        self.b_apply = _primary("Apply")
        self.b_apply.clicked.connect(self.apply)
        self.lay.addLayout(_row(b_rev, None, self.b_apply))
        g2, gl2 = _group("Certus as recorded")
        self.kv = KV()
        gl2.addWidget(self.kv)
        self.lay.addWidget(g2)
        self.lay.addStretch(1)

    def _hw(self):
        return (self.p.lines[0].info.get("hardware") or {}) if self.p and self.p.lines else {}

    def _draw(self):
        hw = self._hw()
        pts = {}
        for role, key in (("gps1", "ins_to_primary_antenna_m"), ("gps2", "ins_to_secondary_antenna_m")):
            if hw.get(key):
                pts[role] = tuple(hw[key]) + ("",)
        pts["output"] = tuple(s.value() for s in self.lever) + ("Lidar",)
        veh = (hw.get("vehicle") or {}).get("type", "van")
        from .widgets import VEHICLE_PRESETS
        self.view.set_points(pts, VEHICLE_PRESETS.get(veh, VEHICLE_PRESETS["van"]))

    def refresh(self):
        p = self.p
        if p is None or not p.lines:
            return
        c = p.cfg
        for s, v in zip(self.lever, c["lever"]):
            s.blockSignals(True)
            s.setValue(v)
            s.blockSignals(False)
        for s, v in zip(self.mount, c["mount"]):
            s.setValue(v)
        for lb, v in zip(self.rec_lever, c.get("lever_preset") or [0, 0, 0]):
            lb.setText("%.4f" % v)
        for lb, v in zip(self.rec_mount, c.get("mount_preset") or [0, 0, 0]):
            lb.setText("%.4f" % v)
        self.lat.setValue(c["latency_ms"])
        hw = self._hw()
        self.kv.clear()
        for k, lab in (("certus_model", "Model"), ("certus_alignment_deg", "Alignment (applied by the Certus)"),
                       ("ins_to_primary_antenna_m", "GPS1 primary (F, S, D m)"),
                       ("ins_to_secondary_antenna_m", "GPS2 secondary (F, S, D m)"), ("lidar_model", "Lidar")):
            v = hw.get(k)
            if v is not None:
                if isinstance(v, dict):
                    v = ", ".join("%s %s" % kv for kv in v.items())
                elif isinstance(v, list):
                    v = ", ".join("%.3f" % x for x in v)
                self.kv.set(lab, str(v))
        self._draw()

    def revert(self):
        c = self.p.cfg
        for s, v in zip(self.lever, c.get("lever_preset") or [0, 0, 0]):
            s.setValue(v)
        for s, v in zip(self.mount, c.get("mount_preset") or [0, 0, 0]):
            s.setValue(v)
        self.lat.setValue(0)

    def apply(self):
        self.dock.apply_installation([s.value() for s in self.lever], self.lat.value(), [s.value() for s in self.mount])

    def state(self):
        p = self.p
        if p is None or not p.lines:
            return "todo", "lever arm, mounting"
        lv = p.cfg["lever"]
        st = "warn" if lv == [0.0, 0.0, 0.0] else "done"
        return st, "lever %.2f, %.2f, %.2f m" % tuple(lv)


# ================================================================ 3 QUALITY
class QualityPage(Page):
    def __init__(self, dock):
        super().__init__(dock)
        self.lay.addWidget(_title("Data quality"))
        self.lay.addWidget(_hint("The mapper's 1PPS + GPRMC time lock means no latency is needed: PPS lock should be "
                                 "100 % and arrival minus sensor time about 1 ms. Lost packets are UDP sequence gaps."))
        cards = QHBoxLayout()
        self.c_pps, self.c_lost, self.c_rtk, self.c_err = (Card("PPS lock"), Card("Lost Lidar packets"),
                                                           Card("RTK fixed"), Card("Mean error now"))
        for c in (self.c_pps, self.c_lost, self.c_rtk, self.c_err):
            cards.addWidget(c)
        self.lay.addLayout(cards)
        self.body = QVBoxLayout()
        self.body.setSpacing(8)
        self.lay.addLayout(self.body)
        self.lay.addStretch(1)
        self._built_for = None

    def _clear(self):
        while self.body.count():
            it = self.body.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

    def refresh(self):
        p = self.p
        if p is None or not p.lines:
            return
        pps = [l.stats.get("pps_lock_pct") for l in p.lines if l.stats.get("pps_lock_pct") is not None]
        lost = sum(l.stats.get("lost", 0) or 0 for l in p.lines)
        rtk = [l.stats.get("fix_rtk_pct") for l in p.lines if l.stats.get("fix_rtk_pct") is not None]
        self.c_pps.set("%.1f %%" % min(pps) if pps else "-", "ok" if pps and min(pps) >= 100 else "bad")
        self.c_lost.set("{:,}".format(lost), "ok" if lost == 0 else "warn")
        self.c_rtk.set("%.0f %%" % min(rtk) if rtk else "SBET", "ok" if not rtk or min(rtk) >= 99 else "warn")
        e = self.dock.avg_error
        self.c_err.set(fmt(e) + " cm" if e is not None else "-",
                       None if e is None else ("ok" if e < 1 else "warn" if e < 2.5 else "bad"))
        key = (id(p), p.cfg["mount"] and tuple(p.cfg["mount"]), tuple(l.use for l in p.lines))
        if key == self._built_for:
            return
        self._built_for = key
        self._clear()
        from .qcviews import qc_sections
        for title, w, open_ in qc_sections(p):
            self.body.addWidget(Section(title, w, open_))
        theme.polish(self)
        theme.force_paint(self)

    def state(self):
        p = self.p
        if p is None or not p.lines:
            return "todo", "after reading"
        pps = [l.stats.get("pps_lock_pct") for l in p.lines if l.stats.get("pps_lock_pct") is not None]
        lost = sum(l.stats.get("lost", 0) or 0 for l in p.lines)
        bad = (pps and min(pps) < 100) or lost > 0
        return ("warn" if bad else "done"), "PPS %s · lost %d" % ("%.0f%%" % min(pps) if pps else "-", lost)


# ================================================================== 4 PAIRS
class PairsPage(Page):
    def __init__(self, dock):
        super().__init__(dock)
        self.lay.addWidget(_title("Pairs and planar surfaces"))
        self.lay.addWidget(_hint("Each pair is typed by heading difference, speed and overlap. The calibration uses "
                                 "planar patches (walls, roofs, ground) that two or more lines see; walls drive "
                                 "heading, so flat ground is capped to the number of non-flat patches."))
        cards = QHBoxLayout()
        self.c_pairs, self.c_rec, self.c_walls, self.c_flat = (Card("Pairs"), Card("Reciprocal pairs"),
                                                               Card("Walls / sloped"), Card("Flat patches"))
        for c in (self.c_pairs, self.c_rec, self.c_walls, self.c_flat):
            cards.addWidget(c)
        self.lay.addLayout(cards)
        g, gl = _group("Pairs")
        self.tbl = make_table(["Pair", "Type", "ΔHdg", "ΔSpeed", "Overlap %", "Separation m",
                               "Patches", "Err before", "Err after"])
        self.tbl.setMinimumHeight(260)
        gl.addWidget(self.tbl)
        self.lay.addWidget(g)
        g2, gl2 = _group("Calibration area")
        gl2.addWidget(_hint("Automatic by default. For a single target (pole, building corner), "
                            "draw an area of interest on the map; only surfaces inside it are used."))
        b_aoi = QPushButton("Draw area of interest")
        b_aoi.clicked.connect(self.dock.draw_aoi)
        b_clr = QPushButton("Clear area")
        b_clr.clicked.connect(self.dock.clear_aoi)
        b_box = QPushButton("Place boxes automatically")
        b_box.clicked.connect(self.dock.auto_boxes)
        b_prof = QPushButton("Free profile")
        b_prof.clicked.connect(lambda: self.dock.redraw_box("profile"))
        b_set = QPushButton("Settings…")
        b_set.clicked.connect(self.dock.settings_dialog)
        gl2.addLayout(_row(b_aoi, b_clr, b_box, b_prof, None, b_set))
        self.lay.addWidget(g2)
        self.lay.addStretch(1)

    def refresh(self):
        p = self.p
        if p is None or p.P is None:
            return
        k = p.P.kind
        rec = sum(1 for q in p.pair_list if q["type"] == "Reciprocal")
        self.c_pairs.set(str(len(p.pair_list)))
        self.c_rec.set(str(rec), "ok" if rec >= 2 else ("warn" if rec else "bad"))
        walls = int((k == 2).sum())
        self.c_walls.set("%d / %d" % (walls, int((k == 1).sum())), "ok" if walls >= 20 else "warn")
        self.c_flat.set(str(int((k == 0).sum())))
        r = p.cfg.get("results") or {}
        res = {(q["a"], q["b"]): q for q in r.get("pairs", [])}
        t = self.tbl
        t.setRowCount(len(p.pair_list))
        cols = {"Reciprocal": theme.GREEN, "Same direction": theme.BLUE, "Crossing": theme.VIOLET,
                "Latency": theme.AMBER, "Additional": theme.DIM}
        for i, q in enumerate(p.pair_list):
            rr = res.get((q["a"], q["b"]), {})
            vals = ["%s - %s" % (p.lines[q["a"]].name, p.lines[q["b"]].name), q["type"], "%.1f" % q["dhdg"],
                    "%.1f" % q["dspeed"], "%.0f" % q["overlap"], fmt(q["separation"], "%.1f"), str(q["shared"]),
                    fmt(rr.get("e_before"), "%.2f"), fmt(rr.get("e_after"), "%.2f")]
            for j, v in enumerate(vals):
                t.setItem(i, j, cell(v, cols.get(q["type"]) if j == 1 else None, j >= 2))

    def state(self):
        p = self.p
        if p is None or p.P is None:
            return "todo", "after reading"
        rec = sum(1 for q in p.pair_list if q["type"] == "Reciprocal")
        walls = int((p.P.kind == 2).sum())
        st = "done" if rec >= 2 and walls >= 20 else ("warn" if len(p.P) >= 6 else "error")
        return st, "%d pairs · %d patches" % (len(p.pair_list), len(p.P))


# ============================================================ 5 CALIBRATION
class CalibPage(Page):
    def __init__(self, dock):
        super().__init__(dock)
        self.lay.addWidget(_title("Mounting angles"))
        self.lay.addWidget(_hint("Auto calibrate pairs the lines and solves roll, pitch and heading from every planar "
                                 "patch, then again leaving out each line in turn: the spread of those combinations "
                                 "is the SDEV. Check the Data View, then Apply: the applied values are published for "
                                 "CloudPro (alignment.json). Copy them to the mapper as well (Settings › Hardware › "
                                 "Survey sensor › Mounting angles)."))
        self.b_run = _primary("Auto calibrate")
        self.b_run.clicked.connect(self.dock.autopatch)
        b_view = QPushButton("Data View \u00b7 Fine Tune")
        b_view.setToolTip("Plan, profile and section of the points, live while you adjust the values by hand")
        b_view.clicked.connect(lambda: self.dock.show_dataview("float"))
        self.lay.addLayout(_row(self.b_run, b_view, None))
        cards = QHBoxLayout()
        self.cards = [Card("Roll"), Card("Pitch"), Card("Heading")]
        self.c_err = Card("Error before → after")
        for c in self.cards + [self.c_err]:
            cards.addWidget(c)
        self.lay.addLayout(cards)
        self.bore = KV()
        self.lay.addWidget(self.bore)
        self.warn = QLabel("")
        self.warn.setWordWrap(True)
        self.warn.setStyleSheet("color: %s;" % theme.AMBER)
        self.lay.addWidget(self.warn)
        b_adv = _primary("Apply advised")
        b_adv.clicked.connect(self._apply_adv)
        self.b_copy = QPushButton("Copy for mapper")
        self.b_copy.setToolTip("Roll, pitch, heading to 4 decimals for the mapper's Settings page")
        self.b_copy.clicked.connect(self._copy)
        g0, gl0 = _group("Published for CloudPro")
        self.pub = KV()
        gl0.addWidget(self.pub)
        self.pub_warn = QLabel("")
        self.pub_warn.setWordWrap(True)
        self.pub_warn.setStyleSheet("color: %s;" % theme.AMBER)
        gl0.addWidget(self.pub_warn)
        self.b_pub = QPushButton("Publish the applied values")
        self.b_pub.setToolTip("Write alignment.json (02_AlignPro/out) for CloudPro with the values applied now")
        self.b_pub.clicked.connect(self.dock.publish_now)
        gl0.addLayout(_row(None, self.b_pub))
        self._pub_group = g0
        self.lay.addLayout(_row(self.b_copy, None, b_adv))
        self.lay.addWidget(self._pub_group)
        g, gl = _group("Current angles", QGridLayout)
        self.edit, self.pre = [], []
        for i, k in enumerate(KEYS):
            h = QLabel(k.upper())
            h.setObjectName("ppKey")
            gl.addWidget(h, 0, i + 1)
            lb = QLabel("-")
            lb.setObjectName("ppVal")
            gl.addWidget(lb, 1, i + 1)
            self.pre.append(lb)
            s = _dspin(-360, 360, 4, 0.01)
            gl.addWidget(s, 2, i + 1)
            self.edit.append(s)
        for r, t in ((1, "Recorded"), (2, "Current")):
            k = QLabel(t.upper())
            k.setObjectName("ppKey")
            gl.addWidget(k, r, 0)
        b_set = QPushButton("Apply")
        b_set.clicked.connect(lambda: self.dock.apply_angles([s.value() for s in self.edit]))
        gl.addWidget(b_set, 2, 4)
        self.lay.addWidget(g)
        g2, gl2 = _group("Combinations")
        gl2.addWidget(_hint("Select a row to try its angles in the Data View; right-click to apply them."))
        self.tbl = make_table(["Combi", "Lines", "Roll", "Pitch", "Heading", "Δ Roll", "Δ Pitch",
                               "Δ Heading", "Before cm", "After cm", "Points", "Patches"])
        self.tbl.setMinimumHeight(220)
        self.tbl.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tbl.customContextMenuRequested.connect(self._menu)
        self.tbl.itemSelectionChanged.connect(lambda: self.dock.set_combi(self.tbl.currentRow()))
        gl2.addWidget(self.tbl)
        self.lay.addWidget(g2)
        self.lay.addStretch(1)

    def refresh(self):
        p = self.p
        if p is None:
            return
        self.refresh_publish()
        for lb, v in zip(self.pre, p.cfg.get("mount_preset") or [0, 0, 0]):
            lb.setText("%.4f" % v)
        for s, v in zip(self.edit, p.cfg.get("mount") or [0, 0, 0]):
            s.blockSignals(True)
            s.setValue(v)
            s.blockSignals(False)
        r = p.cfg.get("results")
        self.bore.clear()
        if not r:
            for c in self.cards:
                c.set("-")
            self.c_err.set("-")
            self.warn.setText("")
            self.tbl.setRowCount(0)
            return
        a, sd = r["advised"], r["sdev"]
        for c, k in zip(self.cards, KEYS):
            v = sd[k]
            c.set("%.4f" % a[k], "ok" if v < 0.02 else ("warn" if v < 0.05 else "bad"))
            c.label.setText("%s · SDEV ± %.4f" % (k.upper(), v))
        self.c_err.set("%.2f → %.2f cm" % (r["error_before_cm"], r["error_after_cm"]),
                       "ok" if r["error_after_cm"] < 1 else "warn")
        if r.get("bore"):
            self.bore.set("Boresight about vehicle axes", "%+.4f  %+.4f  %+.4f deg" % tuple(r["bore"]))
            self.bore.set("Its SDEV", "%.4f  %.4f  %.4f deg" % tuple(r["bore_sdev"]))
        self.bore.set("Patches", "%d walls · %d sloped · %d flat" % (
            r["patches"]["walls"], r["patches"]["sloped"], r["patches"]["flat"]))
        self.bore.set("Trajectory", r["trajectory"])
        self.warn.setText("\n".join("⚠ " + w for w in r.get("warnings", [])))
        rows = r["combis"]
        self.tbl.blockSignals(True)
        self.tbl.setRowCount(len(rows))
        for i, row in enumerate(rows):
            self.tbl.setItem(i, 0, cell(row["label"], None, False))
            if not row.get("ok"):
                self.tbl.setItem(i, 1, cell("did not converge", theme.RED, False))
                continue
            vals = [", ".join(row["lines"]), "%.4f" % row["roll"], "%.4f" % row["pitch"], "%.4f" % row["heading"],
                    "%+.4f" % row["d_roll"], "%+.4f" % row["d_pitch"], "%+.4f" % row["d_heading"],
                    "%.2f" % row["before_cm"], "%.2f" % row["after_cm"], "{:,}".format(row["points"]),
                    str(row["patches"])]
            for j, v in enumerate(vals):
                self.tbl.setItem(i, j + 1, cell(v, None, j > 0))
        self.tbl.blockSignals(False)

    def busy(self, on):
        self.b_run.setEnabled(not on)

    def refresh_publish(self):
        p = self.p
        self.pub.clear()
        self.pub_warn.setText("")
        if p is None or p.dcs is None:
            self._pub_group.setVisible(False)
            return
        self._pub_group.setVisible(True)
        st, why, differs = p.published_state()
        words = {"done": "current", "stale": "out of date", "none": "not published yet", "running": "running now",
                 "failed": "the last run failed"}
        self.pub.set("State", words.get(st, st), {"done": "ok", "stale": "warn", "failed": "bad"}.get(st))
        pub = p.cfg.get("published")
        if pub:
            self.pub.set("Values", "%.4f  %.4f  %.4f" % tuple(pub["mount"]))
            self.pub.set("Published", "%s \u00b7 %s" % (pub["time"], pub["id"][7:19]))
        notes = list(why)
        if differs:
            notes.append("The applied values differ from the published ones: publish them for CloudPro.")
        if p.traj_info.get("source") != "pospro":
            notes.append("Solved on the %s trajectory, not PosPro's: run PosPro and calibrate again before the "
                         "final result." % ("real-time" if p.traj_info.get("source") == "realtime" else "SBET file"))
        self.pub_warn.setText("\n".join("\u26a0 " + w for w in notes))
        self.b_pub.setEnabled(bool(p.lines) and (st != "done" or differs))

    def _apply_adv(self):
        r = self.p.cfg.get("results") if self.p else None
        if r:
            self.dock.apply_angles([r["advised"][k] for k in KEYS])

    def _copy(self):
        if self.p is None:
            return
        a = list(self.p.cfg["mount"])              # the applied (published) mounting
        QGuiApplication.clipboard().setText(mapper_text(a))
        self.dock.toast("Copied for the mapper: " + mapper_text(a))

    def _menu(self, pos):
        i = self.tbl.currentRow()
        r = self.p.cfg.get("results") if self.p else None
        if not r or i < 0 or not r["combis"][i].get("ok"):
            return
        row = r["combis"][i]
        m = theme.dark_menu(QMenu(self))
        act = m.addAction("Apply and publish these angles (%.4f, %.4f, %.4f)" % (row["roll"], row["pitch"],
                                                                              row["heading"]))
        cp = m.addAction("Copy for mapper")
        ch = m.exec(self.tbl.viewport().mapToGlobal(pos))
        if ch == act:
            self.dock.apply_angles([row["roll"], row["pitch"], row["heading"]])
        elif ch == cp:
            QGuiApplication.clipboard().setText(mapper_text([row["roll"], row["pitch"], row["heading"]]))

    def state(self):
        p = self.p
        if p is None or not p.lines:
            return "todo", "auto calibrate"
        r = p.cfg.get("results")
        if not r:
            return "todo", "auto calibrate"
        a = r["advised"]
        st = "warn" if r.get("warnings") else "done"
        if p.dcs is not None:
            pst, _why, differs = p.published_state()
            if pst == "stale":
                return "warn", "out of date: re-run"
            if pst != "done" or differs:
                return "warn", "apply to publish"
        return st, "%.4f  %.4f  %.4f" % (a["roll"], a["pitch"], a["heading"])


# ================================================================ 6 ANALYZE
class AnalyzePage(Page):
    PARAMS = [("latency_ms", "Lidar latency", "ms", (20.0, 21)), ("lever_f", "Lever forward", "m", (0.3, 21)),
              ("lever_s", "Lever starboard", "m", (0.3, 21)), ("lever_d", "Lever down", "m", (0.3, 21)),
              ("roll", "Mounting roll", "deg", (0.3, 21)), ("pitch", "Mounting pitch", "deg", (0.3, 21)),
              ("heading", "Mounting heading", "deg", (0.5, 21))]

    def __init__(self, dock):
        super().__init__(dock)
        self.lay.addWidget(_title("Adjust and analyze"))
        self.lay.addWidget(_hint("Sweep one parameter with everything else fixed (planes re-fitted each step) and "
                                 "plot the mean point-to-plane error. Latency shows as an along-track shift that "
                                 "flips with direction. “If it ain't broken don't fix it”: small values "
                                 "found in quiet conditions are usually noise."))
        g, f = _group("Parameter", QFormLayout)
        self.par = QComboBox()
        for k, n, u, _r in self.PARAMS:
            self.par.addItem("%s (%s)" % (n, u), k)
        self.par.currentIndexChanged.connect(self._sel)
        f.addRow("Parameter", self.par)
        self.cur = QLabel("-")
        self.cur.setObjectName("ppVal")
        f.addRow("Current value", self.cur)
        self.center = _dspin(-1000, 1000, 4, 0.01)
        self.rng = _dspin(0.0001, 1000, 4, 0.05)
        self.steps = QSpinBox()
        self.steps.setRange(3, 201)
        f.addRow("Centre", self.center)
        f.addRow("Range ±", self.rng)
        f.addRow("Steps", self.steps)
        self.lay.addWidget(g)
        self.b_start = _primary("Analyze")
        self.b_start.clicked.connect(self._start)
        self.best = QLabel("")
        self.best.setObjectName("ppVal")
        self.b_apply = QPushButton("Apply best value")
        self.b_apply.setEnabled(False)
        self.b_apply.clicked.connect(self._apply)
        self.lay.addLayout(_row(self.b_start, self.best, None, self.b_apply))
        self.plot = PlotWidget("", "Error (cm)")
        self.plot.setMinimumHeight(280)
        self.lay.addWidget(self.plot)
        self.lay.addStretch(1)
        self.best_v = None

    def _key(self):
        return self.par.currentData()

    def _sel(self):
        if self.p is None:
            return
        k = self._key()
        v = self.p.current_value(k)
        self.cur.setText("%.4f" % v)
        self.center.setValue(v)
        r, n = dict((a, d) for a, _b, _c, d in self.PARAMS)[k]
        self.rng.setValue(r)
        self.steps.setValue(n)
        self.plot.xlabel = self.par.currentText()
        self.plot.clear()
        self.best.setText("")
        self.b_apply.setEnabled(False)

    def refresh(self):
        if self.p is not None:
            self.cur.setText("%.4f" % self.p.current_value(self._key()))
            if not self.plot.series:
                self._sel()

    def _start(self):
        self.plot.clear()
        self.dock.analyze(self._key(), self.center.value(), self.rng.value(), self.steps.value())

    def running(self, on):
        self.b_start.setEnabled(not on)

    def show_result(self, pts):
        if not pts:
            return
        xs, ys = [q[0] for q in pts], [q[1] for q in pts]
        self.plot.clear()
        self.plot.add(xs, ys, theme.ACCENT, "both")
        i = int(np.argmin(ys))
        self.plot.vlines = [(xs[i], theme.AMBER), (self.p.current_value(self._key()), theme.DIM)]
        self.plot.update()
        self.best_v = xs[i]
        self.best.setText("BEST %.4f  (%.3f cm)" % (xs[i], ys[i]))
        self.center.setValue(xs[i])
        self.b_apply.setEnabled(True)

    def _apply(self):
        if self.best_v is not None:
            self.dock.adjust_apply(self._key(), self.best_v)

    def state(self):
        if self.p is None or not self.p.lines:
            return "todo", "optional"
        return "todo", "latency, lever, angles"


# ================================================================= 7 REPORT
class ReportPage(Page):
    def __init__(self, dock):
        super().__init__(dock)
        self.lay.addWidget(_title("Report"))
        self.lay.addWidget(_hint("PDF and ASCII report: Report_YYYY-MM-DD-HHMMSS_<project>. Set up the map and slice "
                                 "views first if screenshots are added. View History reads the ASCII reports in the "
                                 "sibling folders of this project (one folder per calibration)."))
        g, f = _group("Header", QFormLayout)
        self.fields = {}
        for k in ("vehicle", "operator", "client", "location"):
            e = QLineEdit()
            self.fields[k] = e
            f.addRow(k.capitalize(), e)
        self.notes = QPlainTextEdit()
        self.notes.setMaximumHeight(80)
        f.addRow("Notes", self.notes)
        self.lay.addWidget(g)
        st = QSettings()
        self.c_head = QCheckBox("Add header")
        self.c_ask = QCheckBox("Ask for file name")
        self.c_shots = QCheckBox("Add screenshots")
        self.c_clip = QCheckBox("Copy text to clipboard")
        opts = QHBoxLayout()
        for c, k, d in ((self.c_head, "head", True), (self.c_ask, "ask", False), (self.c_shots, "shots", True),
                        (self.c_clip, "clip", False)):
            c.setChecked(st.value("AlignPro/report_" + k, d, type=bool))
            opts.addWidget(c)
        opts.addStretch(1)
        self.lay.addLayout(opts)
        b_las = QPushButton("Export corrected LAS…")
        b_las.clicked.connect(self.dock.export_las)
        self.b_rep = _primary("Create report")
        self.b_rep.clicked.connect(self.create)
        self.lay.addLayout(_row(b_las, None, self.b_rep))
        g2, gl2 = _group("Calibration history")
        row = QHBoxLayout()
        self.folder = QLineEdit()
        b = QPushButton("Browse…")
        b.clicked.connect(self._browse)
        b2 = QPushButton("Scan")
        b2.clicked.connect(self.scan)
        row.addWidget(self.folder, 1)
        row.addWidget(b)
        row.addWidget(b2)
        gl2.addLayout(row)
        self.tbl = make_table(["Data date", "Project", "Roll", "Pitch", "Heading", "SDEV r/p/h", "Report"])
        self.tbl.setMinimumHeight(160)
        self.tbl.doubleClicked.connect(self._open_row)
        gl2.addWidget(self.tbl)
        self.plot = PlotWidget("Calibration", "Change from first (deg)")
        self.plot.setMinimumHeight(200)
        gl2.addWidget(self.plot)
        b3 = QPushButton("View ASCII")
        b3.clicked.connect(lambda: self._open_sel(False))
        b4 = QPushButton("View PDF")
        b4.clicked.connect(lambda: self._open_sel(True))
        b5 = QPushButton("Explore")
        b5.clicked.connect(self._explore)
        gl2.addLayout(_row(b3, b4, b5, None))
        self.lay.addWidget(g2)
        self.lay.addStretch(1)
        self.rows = []
        self.written = None

    def refresh(self):
        p = self.p
        if p is None:
            return
        for k, e in self.fields.items():
            e.setText(p.cfg["report"].get(k, ""))
        self.notes.setPlainText(p.cfg["report"].get("notes", ""))
        if not self.folder.text():
            jf = getattr(p.dcs, "job_folder", None) if p.dcs is not None else None
            self.folder.setText(os.path.dirname(os.path.normpath(jf)) if jf else os.path.dirname(p.folder))
            self.scan()

    def create(self):
        p = self.p
        for k, e in self.fields.items():
            p.cfg["report"][k] = e.text()
        p.cfg["report"]["notes"] = self.notes.toPlainText()
        st = QSettings()
        for c, k in ((self.c_head, "head"), (self.c_ask, "ask"), (self.c_shots, "shots"), (self.c_clip, "clip")):
            st.setValue("AlignPro/report_" + k, c.isChecked())
        path = report.default_name(p, "pdf")
        if self.c_ask.isChecked():
            path, _ = QFileDialog.getSaveFileName(self, "Report", path, "PDF (*.pdf)")
            if not path:
                return
        p.save()
        if self.dock.make_report(path, self.c_head.isChecked(), self.c_shots.isChecked(), self.c_clip.isChecked()):
            self.written = path
            self.scan()

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Parent folder", self.folder.text())
        if d:
            self.folder.setText(d)
            self.scan()

    def scan(self):
        self.rows = report.history(self.folder.text())
        t = self.tbl
        t.setRowCount(len(self.rows))
        for i, r in enumerate(self.rows):
            sd = "/".join("%.4f" % v for v in r["sd"]) if r["sd"] else "-"
            vals = [r["data_date"], r["project"], "%.4f" % r["roll"], "%.4f" % r["pitch"], "%.4f" % r["heading"],
                    sd, os.path.basename(r["path"])]
            for j, v in enumerate(vals):
                t.setItem(i, j, cell(v, None, 2 <= j <= 5))
        self.plot.clear()
        if self.rows:
            f = self.rows[0]
            xs = list(range(1, len(self.rows) + 1))
            for k, c in (("roll", theme.RED), ("pitch", theme.ACCENT), ("heading", theme.BLUE)):
                self.plot.add(xs, [r[k] - f[k] for r in self.rows], c, "both", k.capitalize())

    def _sel(self):
        i = self.tbl.currentRow()
        return self.rows[i] if 0 <= i < len(self.rows) else None

    def _open_row(self, _idx):
        self._open_sel(True)

    def _open_sel(self, pdf):
        r = self._sel()
        if not r:
            return
        target = r["pdf"] if pdf and r["pdf"] else r["path"]
        QDesktopServices.openUrl(QUrl.fromLocalFile(target))

    def _explore(self):
        r = self._sel()
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(r["path"]) if r else self.folder.text()))

    def state(self):
        if self.p is None or not self.p.lines:
            return "todo", "PDF, ASCII, history"
        if self.written:
            return "done", os.path.basename(self.written)
        return "todo", "PDF, ASCII, history"


# ================================================================= no project
class WelcomePage(Page):
    """Shown when no project is open. Projects are created and opened from the project box in the
    header (New project, Open project, Recent projects), as in PosPro and CloudPro; this page only
    says so and gives the field procedure for a patch test."""

    def __init__(self, dock):
        super().__init__(dock)
        self.lay.addWidget(_title("No project open"))
        self.lay.addWidget(_hint("Use the project box at the top right: New project (from the job folder that holds "
                                 "the mapper captures), Open project (the project PosPro made), or Recent projects. "
                                 "AlignPro works on the job's shared processing project (Processing/<Name>/ in the job "
                                 "folder), after PosPro."))
        g2, gl2 = _group("Patch test field procedure")
        for t in ("Vertical surfaces (walls, building corners, thick poles) beside open flat ground, good GNSS, "
                  "a base station within 20 km for PPK.",
                  "At least 3 passes, ideally 6: reciprocal pairs (opposite directions) at increasing distance.",
                  "One pass per Start/Stop is easiest; passes driven in one capture are split at the turns.",
                  "File split large enough that a pass is never split (3.9 GB is about 40 minutes of .clraw).",
                  "5 minutes static at the start and the end for the PPK; press Mark when passing the target."):
            gl2.addWidget(_hint("\u2022  " + t))
        self.lay.addWidget(g2)
        self.lay.addStretch(1)

    def refresh(self):
        pass
