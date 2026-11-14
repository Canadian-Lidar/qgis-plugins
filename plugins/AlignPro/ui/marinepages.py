"""AlignPro's pages for marine jobs (sonar recordings), on the same steps as a Lidar job:
Data · Installation · Quality · Pairs · Calibration · Analyze · Report, plus the marine Data View (CHECK / FINE
TUNE): a plan of the soundings, a section you draw on it, and every head's roll, pitch, heading, latency and
offsets to nudge while the section redraws. Nothing is applied blind: each solved value is used only when you
accept it, and every value in use can be typed or nudged."""
import datetime
import os
import time

import numpy as np
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGridLayout,
                                 QHBoxLayout, QHeaderView, QLabel, QPushButton, QSplitter, QTableWidget,
                                 QTableWidgetItem, QVBoxLayout, QWidget)

from ..core import marine
from . import theme
from .common import PlotWidget
from .pointview import PointCanvas, height_colors

LABELS = {"roll": ("Roll", "°", 3, 0.01), "pitch": ("Pitch", "°", 3, 0.01), "heading": ("Heading", "°", 3, 0.02),
          "latency_s": ("Latency", " ms", 1, 1.0)}
LEVER = (("F", "Forward"), ("S", "Starboard"), ("D", "Down"))
HEAD_COLORS = [(232, 72, 85), (59, 130, 246), (245, 166, 35), (0, 229, 170)]
PASS_COLORS = [(232, 72, 85), (59, 130, 246), (245, 166, 35), (0, 229, 170), (155, 89, 182), (241, 196, 15),
               (26, 188, 156), (230, 126, 34)]


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


def _table(headers):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(True)
    return t


def _set_rows(t, rows, max_h=420):
    t.clearContents()                     # also drops the previous rows' widgets (spin boxes, tick boxes)
    t.setRowCount(0)
    t.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, v in enumerate(row):
            if isinstance(v, QWidget):
                t.setCellWidget(r, c, v)
            else:
                t.setItem(r, c, QTableWidgetItem("" if v is None else str(v)))
    t.resizeRowsToContents()
    h = t.horizontalHeader().height() + sum(t.rowHeight(r) for r in range(t.rowCount())) + 6
    t.setFixedHeight(min(max_h, max(60, h)))


def _fmt(k, v):
    if v is None:
        return ""
    return ("%+.1f ms" % (v * 1000.0)) if k == "latency_s" else ("%+.3f°" % v)


def head_name(dock, d):
    h = (dock.mdata or {}).get("heads", {}).get(d) or {}
    return "%s%s" % (h.get("nickname") or d, " (%s)" % h["side"] if h.get("side") else "")


class MPage(QWidget):
    def __init__(self, dock):
        super().__init__()
        self.dock = dock
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(4, 4, 4, 4)

    @property
    def p(self):
        return self.dock.p

    def cfg(self):
        return self.p.cfg.setdefault("marine", {}) if self.p else {}

    def refresh(self):
        pass

    def state(self):
        return "todo", ""


# ------------------------------------------------------------------------------------------------- 1 Data
class MDataPage(MPage):
    def __init__(self, dock):
        super().__init__(dock)
        self.lay.addWidget(_title("Data"))
        self.lay.addWidget(_val("The job's sonar recordings, placed with the boat trajectory PosPro finalized and each "
                                "head's own attitude. The patch test reads a sample of the soundings: choose how many "
                                "and which, then READ SOUNDINGS."))
        f = QFormLayout()
        self.v_traj = _val()
        f.addRow(_key("Boat trajectory"), self.v_traj)
        self.c_every = QComboBox()
        for n, t in ((1, "every ping"), (2, "every 2nd ping"), (3, "every 3rd ping"), (5, "every 5th ping")):
            self.c_every.addItem(t, n)
        f.addRow(_key("Soundings used"), self.c_every)
        self.c_power = QComboBox()
        for k, t in (("high", "strong returns only"), ("medium", "medium and strong"),
                     ("low", "all above the low threshold")):
            self.c_power.addItem(t, k)
        f.addRow(_key("Returns"), self.c_power)
        self.s_rmax = QDoubleSpinBox()
        self.s_rmax.setRange(5, 300)
        self.s_rmax.setSuffix(" m")
        self.s_rmax.setToolTip("Far soundings meet the seabed at grazing angles, where a small sound-speed error "
                               "moves them by decimetres")
        f.addRow(_key("Slant range up to"), self.s_rmax)
        self.lay.addLayout(f)
        row = QHBoxLayout()
        self.b_read = QPushButton("Read soundings")
        self.b_read.setObjectName("ppPrimary")
        self.b_read.clicked.connect(self.dock.marine_read)
        row.addWidget(self.b_read)
        row.addStretch(1)
        self.lay.addLayout(row)
        self.lay.addWidget(_section("Read"))
        self.t = _table(["Head", "Looks to", "Soundings read", "Recordings"])
        self.lay.addWidget(self.t)
        self.lay.addStretch(1)
        self._loading = False
        for w in (self.c_every, self.c_power):
            w.currentIndexChanged.connect(self._store)
        self.s_rmax.valueChanged.connect(self._store)

    def refresh(self):
        m = self.cfg()
        self._loading = True
        self.c_every.setCurrentIndex(max(0, self.c_every.findData(int(m.get("every", 2)))))
        self.c_power.setCurrentIndex(max(0, self.c_power.findData(m.get("power", "medium"))))
        self.s_rmax.setValue(float(m.get("rmax", marine.RMAX)))
        self._loading = False
        doc = self.dock.marine_traj_doc()
        if doc:
            q = doc.get("quality") or {}
            self.v_traj.setText("PosPro: %s, %s %% fixed%s" % (q.get("positioning"), q.get("fixed_pct"),
                                "" if self.dock.pospro_state() == "done" else " (out of date: process again in PosPro)"))
        else:
            self.v_traj.setText("Not finalized: process the boat's trajectory in PosPro first.")
        self.b_read.setEnabled(bool(doc))
        d = self.dock.mdata
        rows = []
        if d:
            for did, h in sorted(d["heads"].items()):
                rows.append([h.get("nickname"), h.get("side") or "?", "{:,}".format(len(h["S"]["t"])),
                             ", ".join(h.get("recordings") or [])])
        _set_rows(self.t, rows)

    def _store(self, *_):
        if self.p is None or self._loading:
            return
        m = self.cfg()
        m["every"], m["power"], m["rmax"] = self.c_every.currentData(), self.c_power.currentData(), self.s_rmax.value()
        self.p.save()

    def state(self):
        if not self.dock.marine_traj_doc():
            return "todo", "PosPro first"
        if self.dock.mdata is None:
            return "todo", "read the soundings"
        return "done", "%s soundings" % "{:,}".format(sum(len(h["S"]["t"]) for h in self.dock.mdata["heads"].values()))


# ------------------------------------------------------------------------------------------------- 2 Installation
class MInstallPage(MPage):
    def __init__(self, dock):
        super().__init__(dock)
        self.lay.addWidget(_title("Installation"))
        self.lay.addWidget(_val("Each sonar head's offset from the GNSS antenna (forward, starboard, down) as PosPro "
                                "finalized it, and the values in use here. Change them in the Data View (CHECK / FINE "
                                "TUNE) or on the Calibration page, where you see what they do to the seabed."))
        self.t = _table(["Head", "Looks to", "Mounted", "PosPro offsets (m)", "Source", "In use (m)", "Attitude"])
        self.lay.addWidget(self.t)
        self.lay.addStretch(1)

    def refresh(self):
        d = self.dock.mdata
        rows = []
        if d:
            vals = self.dock.marine_values()
            for did, h in sorted(d["heads"].items()):
                lv = h["lever"]
                use = (vals.get(str(did)) or {}).get("lever_m") or lv
                rows.append([h.get("nickname"), h.get("side") or "?",
                             "starboard" if lv[1] > 0 else "port" if lv[1] < 0 else "centre",
                             ", ".join("%+.3f" % v for v in lv), (h.get("lever_source") or "").split(" (")[0],
                             ", ".join("%+.3f" % v for v in use), "the sonar's own (504), from PosPro"])
        _set_rows(self.t, rows)

    def state(self):
        return ("done", "%d heads" % len(self.dock.mdata["heads"])) if self.dock.mdata else ("todo", "")


# ------------------------------------------------------------------------------------------------- 3 Quality
class MQualityPage(MPage):
    def __init__(self, dock):
        super().__init__(dock)
        self.lay.addWidget(_title("Quality"))
        self.lay.addWidget(_val("The boat's passes (straight runs) and how they see each other. A patch test needs "
                                "reciprocal passes (driven both ways) over the same seabed for roll and pitch, a "
                                "feature seen off to the side for heading, and two speeds for latency."))
        self.v_traj = _val()
        self.lay.addWidget(self.v_traj)
        self.lay.addWidget(_section("Passes"))
        self.t = _table(["Pass", "Start (UTC)", "Length", "Duration", "Heading", "Speed"])
        self.lay.addWidget(self.t)
        self.lay.addStretch(1)

    def refresh(self):
        doc = self.dock.marine_traj_doc() or {}
        mq = doc.get("marine") or {}
        q = doc.get("quality") or {}
        self.v_traj.setText("Position %s, %s %% fixed; heading from the compass %s %% of the time (slow or stopped)." % (
            q.get("positioning", "?"), q.get("fixed_pct", "?"), (mq.get("heading") or {}).get("compass_pct", "?"))
            if doc else "")
        rows = []
        if self.dock.mdata:
            for i, (a, b, hd, L, v) in enumerate(marine.pass_spans(self.dock.mdata)):
                rows.append([i + 1, datetime.datetime.fromtimestamp(a, datetime.timezone.utc).strftime("%H:%M:%S"),
                             "%.0f m" % L, "%.0f s" % (b - a), "%.0f°" % hd if np.isfinite(hd) else "", "%.2f m/s" % v])
        _set_rows(self.t, rows)

    def state(self):
        if not self.dock.mdata:
            return "todo", ""
        sp = marine.pass_spans(self.dock.mdata)
        hd = [s[2] for s in sp if s[3] > 50]
        recip = any(abs((a - b + 180) % 360 - 180) > 120 for i, a in enumerate(hd) for b in hd[i + 1:])
        return ("done", "%d passes" % len(sp)) if recip else ("warn", "no reciprocal passes")


# ------------------------------------------------------------------------------------------------- 4 Pairs
class MPairsPage(MPage):
    def __init__(self, dock):
        super().__init__(dock)
        self.lay.addWidget(_title("Pairs"))
        self.lay.addWidget(_val("Where two passes of a head, or the two heads, see the same seabed: how many soundings "
                                "were compared and how far apart the seabed is (median), with the values in use. "
                                "Signed: how far the second lies above the first. Double-click a pair to look at it "
                                "in the Data View."))
        row = QHBoxLayout()
        self.b_measure = QPushButton("Measure pairs")
        self.b_measure.setObjectName("ppPrimary")
        self.b_measure.clicked.connect(self.dock.marine_measure_pairs)
        row.addWidget(self.b_measure)
        row.addStretch(1)
        self.lay.addLayout(row)
        self.t = _table(["First", "Second", "Kind", "Compared", "Apart (median)", "Signed"])
        self.t.cellDoubleClicked.connect(self._open)
        self.lay.addWidget(self.t)
        self.v_note = _val()
        self.lay.addWidget(self.v_note)
        self.lay.addStretch(1)

    def refresh(self):
        rows = []
        pt = self.dock.mpairs or []
        for e in pt:
            rows.append(["%s pass %d" % (head_name(self.dock, e["a"][0]), e["a"][1] + 1),
                         "%s pass %d" % (head_name(self.dock, e["b"][0]), e["b"][1] + 1), e["kind"],
                         "{:,}".format(e["n"]), "%.1f cm" % e["median_cm"], "%+.1f cm" % e["signed_cm"]])
        _set_rows(self.t, rows, 520)
        self.b_measure.setEnabled(self.dock.mdata is not None)
        if pt:
            kinds = {k: sum(e["n"] for e in pt if e["kind"] == k) for k in ("reciprocal", "same direction",
                                                                             "between heads", "crossing")}
            self.v_note.setText("Compared soundings: " + ", ".join("%s %s" % (k, "{:,}".format(v))
                                                                  for k, v in kinds.items() if v))
        else:
            self.v_note.setText("")

    def _open(self, r, _c):
        pt = self.dock.mpairs or []
        if 0 <= r < len(pt):
            self.dock.marine_dataview(pair=pt[r])

    def state(self):
        if not self.dock.mdata:
            return "todo", ""
        if self.dock.mpairs is None:
            return "todo", "measure the pairs"
        if not any(e["kind"] == "reciprocal" for e in self.dock.mpairs):
            return "warn", "no reciprocal pairs"
        return "done", "%d pairs" % len(self.dock.mpairs)


# ------------------------------------------------------------------------------------------------- 5 Calibration
class MCalibPage(MPage):
    def __init__(self, dock):
        super().__init__(dock)
        self.lay.addWidget(_title("Calibration"))
        self.lay.addWidget(_val("1 SOLVE suggests a correction per head where the survey lines show one. 2 Look at it: "
                                "CHECK / FINE TUNE draws sections through the seabed while you nudge each value; tick "
                                "ACCEPT to use a solved value, or type your own under IN USE. 3 FINALIZE writes the "
                                "values in use for CloudPro. Nothing solved is used until you accept it."))
        f = QHBoxLayout()
        self.k_lat = QCheckBox("Also solve the latency")
        self.k_lat.setToolTip("The latency moves the seabed along the track as the pitch does; it can be told apart "
                              "only with lines run at different speeds over a sloping seabed")
        self.k_lat.toggled.connect(self._store)
        f.addWidget(self.k_lat)
        f.addStretch(1)
        self.lay.addLayout(f)
        row = QHBoxLayout()
        self.b_solve = QPushButton("1 Solve")
        self.b_solve.setObjectName("ppPrimary")
        self.b_solve.clicked.connect(self.dock.run_marine_solve)
        self.b_view = QPushButton("2 Check / fine tune…")
        self.b_view.clicked.connect(lambda: self.dock.marine_dataview())
        self.b_fin = QPushButton("3 Finalize the values in use")
        self.b_fin.clicked.connect(self.dock.run_marine_publish)
        for b in (self.b_solve, self.b_view, self.b_fin):
            row.addWidget(b)
        row.addStretch(1)
        self.lay.addLayout(row)
        self.v_state = _val()
        self.lay.addWidget(self.v_state)
        self.t = _table(["Head", "Value", "Solved", "Seen by the lines", "Accept", "In use"])
        self.lay.addWidget(self.t)
        self.v_steps = _val()
        self.lay.addWidget(self.v_steps)
        self.lay.addStretch(1)
        self._loading = False

    def _store(self, *_):
        if self.p is None or self._loading:
            return
        self.cfg()["latency"] = self.k_lat.isChecked()
        self.p.save()

    def refresh(self):
        m = self.cfg()
        self._loading = True
        self.k_lat.setChecked(bool(m.get("latency", False)))
        self._loading = False
        d = self.dock.mdata
        self.b_solve.setEnabled(d is not None)
        self.b_view.setEnabled(d is not None)
        res = m.get("result")
        self.b_fin.setEnabled(d is not None)
        st = self.dock.marine_final_state()
        self.v_state.setText(("Solved: the seabed agrees to %.1f cm as recorded, %.1f cm with the solved values (median "
                              "between passes and between heads). " % (res["before_cm"], res["after_cm"]) if res else
                              "Not solved yet. ") + {"done": "The values in use are finalized for CloudPro.",
                                                     "stale": "The finalized values are out of date.",
                                                     "changed": "The values in use differ from the finalized ones: "
                                                                "FINALIZE again."}.get(st, ""))
        rows = []
        vals = self.dock.marine_values()
        acc = m.get("accepted") or {}
        heads = sorted(d["heads"]) if d else []
        for did in heads:
            ds = str(did)
            e = (res or {}).get("heads", {}).get(ds) or {}
            for k in marine.PARAMS:
                lab, unit, dec, step = LABELS[k]
                solved = e.get(k) if k in (res or {}).get("estimated", {}).get(ds, []) else None
                sd = e.get(k + "_sd")
                cb = QCheckBox()
                cb.setEnabled(solved is not None)
                cb.setChecked(bool(acc.get(ds, {}).get(k)) and solved is not None)
                cb.toggled.connect(lambda on, d_=ds, k_=k, v_=solved: self._accept(d_, k_, v_, on))
                sp = QDoubleSpinBox()
                sp.setDecimals(dec)
                sp.setSuffix(unit)
                sp.setSingleStep(step)
                sp.setRange(-500, 500) if k == "latency_s" else sp.setRange(-30, 30)
                v = (vals.get(ds) or {}).get(k, 0.0)
                sp.setValue(v * 1000.0 if k == "latency_s" else v)
                sp.valueChanged.connect(lambda val, d_=ds, k_=k: self._typed(d_, k_, val))
                rows.append([head_name(self.dock, did), lab,
                             ("%s ± %s" % (_fmt(k, solved), _fmt(k, sd).lstrip("+")) if sd is not None else
                              _fmt(k, solved)) if solved is not None else "not determined",
                             "yes" if e.get(k + "_determined") else "no", cb, sp])
            lv = (vals.get(ds) or {}).get("lever_m") or d["heads"][did]["lever"]
            for j, (code, name) in enumerate(LEVER):
                sp = QDoubleSpinBox()
                sp.setDecimals(3)
                sp.setSuffix(" m")
                sp.setSingleStep(0.005)
                sp.setRange(-50, 50)
                sp.setValue(float(lv[j]))
                sp.valueChanged.connect(lambda val, d_=ds, j_=j: self._typed_lever(d_, j_, val))
                rows.append([head_name(self.dock, did), "Offset %s" % name, "PosPro %+.3f m" % d["heads"][did]["lever"][j],
                             "", "", sp])
        _set_rows(self.t, rows, 900)
        self.v_steps.setText("\n".join("%s: %.1f cm" % (s["step"], s["cm"]) for s in (res or {}).get("steps", [])))

    def _accept(self, d, k, solved, on):
        m = self.cfg()
        m.setdefault("accepted", {}).setdefault(d, {})[k] = bool(on)
        vals = self.dock.marine_values()
        vals.setdefault(d, {})[k] = float(solved) if on else 0.0
        self.dock.set_marine_values(vals)
        self.refresh()

    def _typed(self, d, k, val):
        vals = self.dock.marine_values()
        vals.setdefault(d, {})[k] = val / 1000.0 if k == "latency_s" else val
        self.dock.set_marine_values(vals, refresh_pages=False)

    def _typed_lever(self, d, j, val):
        vals = self.dock.marine_values()
        e = vals.setdefault(d, {})
        lv = list(e.get("lever_m") or self.dock.mdata["heads"][int(d)]["lever"])
        lv[j] = val
        e["lever_m"] = lv
        self.dock.set_marine_values(vals, refresh_pages=False)

    def state(self):
        if not self.dock.mdata:
            return "todo", ""
        st = self.dock.marine_final_state()
        if st == "done":
            return "done", "finalized"
        if st == "stale":
            return "warn", "finalized values out of date"
        if st == "changed":
            return "warn", "values changed: finalize again"
        return ("warn", "values not finalized") if self.cfg().get("result") else ("todo", "solve or set the values")


# ------------------------------------------------------------------------------------------------- 6 Analyze
class MAnalyzePage(MPage):
    def __init__(self, dock):
        super().__init__(dock)
        self.lay.addWidget(_title("Analyze"))
        self.lay.addWidget(_val("Before (as recorded) and with the values in use. Against the beam's angle from the "
                                "vertical (+ outboard): a slope that changes sign between heads means roll, a smile "
                                "means sound speed. Against time: steps between passes mean pitch, heading or "
                                "latency. The map shows how far the seabed seen by different passes and heads "
                                "disagrees."))
        row = QHBoxLayout()
        self.b_run = QPushButton("Analyze")
        self.b_run.setObjectName("ppPrimary")
        self.b_run.clicked.connect(self.dock.marine_analyze)
        row.addWidget(self.b_run)
        row.addStretch(1)
        self.lay.addLayout(row)
        self.p_ang = PlotWidget("angle from the vertical (deg, + outboard)", "seabed apart (cm)")
        self.p_time = PlotWidget("time since the start (s)", "seabed apart (cm)")
        for pl, t in ((self.p_ang, "By beam angle"), (self.p_time, "By time")):
            self.lay.addWidget(_section(t))
            pl.setMinimumHeight(220)
            self.lay.addWidget(pl)
        self.lay.addWidget(_section("Disagreement map (before | in use)"))
        mp = QHBoxLayout()
        self.m_before = PointCanvas("As recorded", "Easting (m, local)", "Northing (m)", equal=True)
        self.m_after = PointCanvas("Values in use", "Easting (m, local)", "Northing (m)", equal=True)
        for c in (self.m_before, self.m_after):
            c.setMinimumHeight(320)
            mp.addWidget(c)
        self.lay.addLayout(mp)
        self.v_scale = _val()
        self.lay.addWidget(self.v_scale)
        self.lay.addStretch(1)

    def refresh(self):
        a = self.dock.manalysis
        self.b_run.setEnabled(self.dock.mdata is not None)
        for pl in (self.p_ang, self.p_time):
            pl.clear()
        if not a:
            self.m_before.set_data(None, None, None)
            self.m_after.set_data(None, None, None)
            self.v_scale.setText("")
            return
        for i, d in enumerate(sorted(a["after"]["prof"])):
            col = "#%02x%02x%02x" % HEAD_COLORS[i % len(HEAD_COLORS)]
            for which, style in (("before", "dots"), ("after", "both")):
                pr = a[which]["prof"][d]
                self.p_ang.add(pr["angle"][0], pr["angle"][1], col, style, "%s %s" % (head_name(self.dock, d), which))
                self.p_time.add(pr["time"][0], pr["time"][1], col, style, "%s %s" % (head_name(self.dock, d), which))
        lo, hi = 0.0, float(np.nanpercentile(np.r_[a["before"]["grid"][3].ravel(), a["after"]["grid"][3].ravel()], 98))
        for c, which in ((self.m_before, "before"), (self.m_after, "after")):
            x0, y0, cell, G = a[which]["grid"]
            iy, ix = np.nonzero(np.isfinite(G))
            v = G[iy, ix]
            c.set_data(x0 + (ix + 0.5) * cell, y0 + (iy + 0.5) * cell, height_colors(v, lo, hi), keep_view=False)
            c.note = "median %.1f cm" % (100 * float(np.nanmedian(G))) if np.isfinite(G).any() else ""
        self.v_scale.setText("Colour: 0 cm (blue) to %.0f cm (red) apart; median %.1f cm as recorded, %.1f cm with the "
                             "values in use." % (100 * hi, 100 * float(np.nanmedian(a["before"]["grid"][3])),
                                                 100 * float(np.nanmedian(a["after"]["grid"][3]))))

    def state(self):
        return ("done", "analyzed") if self.dock.manalysis else ("todo", "optional")


# ------------------------------------------------------------------------------------------------- 7 Report
class MReportPage(MPage):
    def __init__(self, dock):
        super().__init__(dock)
        self.lay.addWidget(_title("Report"))
        self.lay.addWidget(_val("Writes the patch test report (text) with the plots of the Analyze page into "
                                "02_AlignPro/out/sonar_patch_test_<time>/."))
        row = QHBoxLayout()
        self.b = QPushButton("Create report")
        self.b.setObjectName("ppPrimary")
        self.b.clicked.connect(self._write)
        row.addWidget(self.b)
        row.addStretch(1)
        self.lay.addLayout(row)
        self.v = _val()
        self.lay.addWidget(self.v)
        self.lay.addStretch(1)

    def refresh(self):
        self.b.setEnabled(self.dock.mdata is not None)
        r = self.cfg().get("report")
        self.v.setText("Last report: %s" % r if r else "")

    def _write(self):
        p, d = self.p, self.dock.mdata
        if p is None or d is None:
            return
        m = self.cfg()
        out = os.path.join(p.out_dir if hasattr(p, "out_dir") else p.folder, "sonar_patch_test_%s" %
                           time.strftime("%Y%m%d_%H%M%S"))
        os.makedirs(out, exist_ok=True)
        res = m.get("result") or {}
        vals = self.dock.marine_values()
        L = ["Sonar patch test - %s" % p.name, "Written %s UTC by AlignPro" % time.strftime("%Y-%m-%d %H:%M:%S",
                                                                                            time.gmtime()), ""]
        doc = self.dock.marine_traj_doc() or {}
        q = doc.get("quality") or {}
        L += ["Boat trajectory: PosPro %s, %s %% fixed (handover %s)" % (q.get("positioning"), q.get("fixed_pct"),
                                                                       (doc.get("id") or "")[7:19]),
              "Soundings: every %s ping(s), %s returns, slant range up to %.0f m" % (m.get("every", 2), m.get(
                  "power", "medium"), float(m.get("rmax", marine.RMAX))), ""]
        L.append("Passes:")
        for i, (a, b, hd, Ln, v) in enumerate(marine.pass_spans(d)):
            L.append("  %d  %s  %5.0f m  %4.0f s  heading %3.0f  %.2f m/s" % (
                i + 1, datetime.datetime.fromtimestamp(a, datetime.timezone.utc).strftime("%H:%M:%S"), Ln, b - a,
                hd if np.isfinite(hd) else 0, v))
        if res:
            L += ["", "Solve: as recorded %.1f cm, solved %.1f cm (median between passes and heads)" % (
                res["before_cm"], res["after_cm"])] + ["  %s: %.1f cm" % (s["step"], s["cm"]) for s in res.get("steps", [])]
        L += ["", "%-22s %-10s %-22s %-10s %-12s" % ("Head", "Value", "Solved", "Accepted", "In use")]
        acc = m.get("accepted") or {}
        for did in sorted(d["heads"]):
            ds = str(did)
            e = (res.get("heads") or {}).get(ds) or {}
            for k in marine.PARAMS:
                solved = e.get(k) if k in (res.get("estimated") or {}).get(ds, []) else None
                L.append("%-22s %-10s %-22s %-10s %-12s" % (head_name(self.dock, did), LABELS[k][0],
                                                           ("%s ± %s" % (_fmt(k, solved), _fmt(k, e.get(k + "_sd"))))
                                                           if solved is not None else "not determined",
                                                           "yes" if (acc.get(ds) or {}).get(k) else "no",
                                                           _fmt(k, (vals.get(ds) or {}).get(k, 0.0))))
            lv = (vals.get(ds) or {}).get("lever_m") or d["heads"][did]["lever"]
            L.append("%-22s %-10s PosPro %s  in use %s m" % (head_name(self.dock, did), "Offsets", ", ".join(
                "%+.3f" % x for x in d["heads"][did]["lever"]), ", ".join("%+.3f" % x for x in lv)))
        if self.dock.mpairs:
            L += ["", "Pairs (values in use when measured):"]
            for e in self.dock.mpairs:
                L.append("  %-28s %-28s %-15s %8s  %6.1f cm  %+6.1f cm" % (
                    "%s pass %d" % (head_name(self.dock, e["a"][0]), e["a"][1] + 1),
                    "%s pass %d" % (head_name(self.dock, e["b"][0]), e["b"][1] + 1), e["kind"], "{:,}".format(e["n"]),
                    e["median_cm"], e["signed_cm"]))
        L += ["", "Finalized for CloudPro: %s" % {"done": "yes", "stale": "out of date"}.get(self.dock.alignpro_state(),
                                                                                              "no")]
        with open(os.path.join(out, "sonar_patch_test.txt"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(L) + "\n")
        an = self.dock.mpage["analyze"]
        if self.dock.manalysis:
            for w, name in ((an.p_ang, "by_angle.png"), (an.p_time, "by_time.png"), (an.m_before, "map_recorded.png"),
                            (an.m_after, "map_in_use.png")):
                try:
                    w.grab().save(os.path.join(out, name))
                except Exception:       # noqa: BLE001
                    pass
        m["report"] = out
        p.save()
        self.refresh()
        self.dock.toast("Report written: %s" % out)
        self.dock.update_steps()

    def state(self):
        return ("done", "written") if self.cfg().get("report") else ("todo", "optional")


PAGES = [MDataPage, MInstallPage, MQualityPage, MPairsPage, MCalibPage, MAnalyzePage, MReportPage]
KEYS = ["data", "install", "quality", "pairs", "calib", "analyze", "report"]


# ================================================================================================ Data View
class MarineView(QWidget):
    """CHECK / FINE TUNE for sonar: plan of the soundings (right-drag draws a section), the section, and each head's
    values to nudge; the section is placed again at every change (milliseconds)."""

    def __init__(self, dock, parent=None):
        super().__init__(parent)
        self.dock = dock
        self.setWindowTitle("AlignPro - sonar Data View")
        self.resize(1300, 820)
        theme.apply(self) if hasattr(theme, "apply") else None
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(_key("Colour by"))
        self.c_col = QComboBox()
        for k in ("head", "pass", "height"):
            self.c_col.addItem(k)
        self.c_col.currentIndexChanged.connect(self._redraw_all)
        top.addWidget(self.c_col)
        top.addWidget(_key("Section width"))
        self.s_w = QDoubleSpinBox()
        self.s_w.setRange(0.2, 20)
        self.s_w.setValue(2.0)
        self.s_w.setSuffix(" m")
        self.s_w.valueChanged.connect(self._rebuild_section)
        top.addWidget(self.s_w)
        self.v_info = _val("Right-drag on the plan to draw a section.")
        self.v_info.setTextFormat(Qt.TextFormat.RichText)
        top.addWidget(self.v_info, 1)
        self.b_use = QPushButton("Use these values")
        self.b_use.setObjectName("ppPrimary")
        self.b_use.setToolTip("They become the values in use on the Calibration page (FINALIZE writes them for "
                              "CloudPro)")
        self.b_use.clicked.connect(self._use)
        top.addWidget(self.b_use)
        lay.addLayout(top)
        sp = QSplitter(Qt.Orientation.Horizontal)
        self.plan = PointCanvas("Plan", "Easting (m, local)", "Northing (m)", equal=True)
        self.sec = PointCanvas("Section", "Along the section (m)", "Height (m)")
        self.plan.lineDrawn.connect(self._line)
        sp.addWidget(self.plan)
        sp.addWidget(self.sec)
        sp.setSizes([520, 780])
        lay.addWidget(sp, 1)
        self.grid = QGridLayout()
        lay.addLayout(self.grid)
        self.values = {}
        self.spins = {}
        self._section = None
        self._line_pts = None
        self._pair = None
        self._sample = None

    def open(self, values, pair=None):
        d = self.dock.mdata
        self.values = {k: dict(v) for k, v in (values or {}).items()}
        self._pair = pair
        self._build_controls(d)
        self._sample = marine.view_sample(d, 200000)
        self._redraw_plan(fit=True)
        if pair is not None:
            self._section_for_pair(pair)
        self.show()
        self.raise_()

    def _build_controls(self, d):
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        self.spins = {}
        cols = ["Head"] + [LABELS[k][0] for k in marine.PARAMS] + ["Forward", "Starboard", "Down"]
        for c, h in enumerate(cols):
            self.grid.addWidget(_key(h), 0, c)
        for r, did in enumerate(sorted(d["heads"]), 1):
            ds = str(did)
            lab = QLabel(head_name(self.dock, did))
            rgb = HEAD_COLORS[(r - 1) % len(HEAD_COLORS)]
            lab.setStyleSheet("color: rgb(%d,%d,%d); font-weight: 600;" % rgb)
            self.grid.addWidget(lab, r, 0)
            v = self.values.setdefault(ds, {})
            for c, k in enumerate(marine.PARAMS, 1):
                _l, unit, dec, step = LABELS[k]
                s = QDoubleSpinBox()
                s.setDecimals(dec)
                s.setSuffix(unit)
                s.setSingleStep(step)
                s.setRange(-500, 500) if k == "latency_s" else s.setRange(-30, 30)
                s.setValue(v.get(k, 0.0) * (1000.0 if k == "latency_s" else 1.0))
                s.valueChanged.connect(self._changed)
                self.spins[(ds, k)] = s
                self.grid.addWidget(s, r, c)
            lv = v.get("lever_m") or d["heads"][did]["lever"]
            for j in range(3):
                s = QDoubleSpinBox()
                s.setDecimals(3)
                s.setSuffix(" m")
                s.setSingleStep(0.005)
                s.setRange(-50, 50)
                s.setValue(float(lv[j]))
                s.valueChanged.connect(self._changed)
                self.spins[(ds, "lever", j)] = s
                self.grid.addWidget(s, r, 5 + j)

    def _read(self):
        d = self.dock.mdata
        vals = {}
        for did in d["heads"]:
            ds = str(did)
            e = {k: self.spins[(ds, k)].value() / (1000.0 if k == "latency_s" else 1.0) for k in marine.PARAMS}
            lv = [self.spins[(ds, "lever", j)].value() for j in range(3)]
            if any(abs(a - b) > 1e-9 for a, b in zip(lv, d["heads"][did]["lever"])):
                e["lever_m"] = lv
            vals[ds] = e
        return vals

    def _changed(self, *_):
        self.values = self._read()
        self._redraw_section()

    def _colors(self, h, ps, z):
        mode = self.c_col.currentText()
        if mode == "height":
            lo, hi = np.percentile(z, [2, 98]) if len(z) else (0, 1)
            return height_colors(z, lo, hi)
        heads = sorted(self.dock.mdata["heads"])
        if mode == "head":
            pal = np.array(HEAD_COLORS, np.uint8)
            return pal[np.searchsorted(heads, h) % len(pal)]
        pal = np.array(PASS_COLORS, np.uint8)
        return pal[ps.astype(int) % len(pal)]

    def _redraw_plan(self, fit=False):
        x, y, z, h, ps = marine.plan(self.dock.mdata, self.values, self._sample)
        self.plan.set_data(x, y, self._colors(h, ps, z), keep_view=not fit)
        self.plan.overlays = [self._draw_line]
        self.plan.update()

    def _draw_line(self, painter, to_screen):
        if self._line_pts is None:
            return
        from qgis.PyQt.QtGui import QColor, QPen
        painter.setPen(QPen(QColor(theme.ACCENT), 2))
        a, b = self._line_pts
        painter.drawLine(to_screen(*a), to_screen(*b))

    def _redraw_all(self, *_):
        self._redraw_plan()
        self._redraw_section()

    def _line(self, x0, y0, x1, y1):
        self._line_pts = ((x0, y0), (x1, y1))
        self._rebuild_section()

    def _rebuild_section(self, *_):
        if self._line_pts is None:
            return
        self._section = marine.Section(self.dock.mdata, self.values, self._line_pts[0], self._line_pts[1],
                                       self.s_w.value())
        self._redraw_section(fit=True)
        self.plan.update()

    def _section_for_pair(self, pair):
        """A section across the middle of where the pair overlaps (perpendicular to the first pass)."""
        d = self.dock.mdata
        (ha, pa), (hb, pb) = pair["a"], pair["b"]
        x, y, z, h, ps = marine.plan(d, self.values, self._sample)
        ma = (h == ha) & (ps == pa)
        mb = (h == hb) & (ps == pb)
        if not ma.any() or not mb.any():
            return
        cell = 2.0
        ka = set(zip((x[ma] // cell).astype(int), (y[ma] // cell).astype(int)))
        kb = set(zip((x[mb] // cell).astype(int), (y[mb] // cell).astype(int)))
        both = np.array(sorted(ka & kb), float)
        if not len(both):
            return
        c = (np.median(both, 0) + 0.5) * cell
        spans = marine.pass_spans(d)
        hd = np.radians(spans[pa][2]) if pa < len(spans) and np.isfinite(spans[pa][2]) else 0.0
        u = np.array([np.cos(hd), -np.sin(hd)])          # across the first pass (east, north)
        self._line_pts = (tuple(c - 40 * u), tuple(c + 40 * u))
        self.c_col.setCurrentText("pass" if ha == hb else "head")
        self._rebuild_section()
        self.plan.set_view(c[0] - 80, c[0] + 80, c[1] - 80, c[1] + 80)

    def _redraw_section(self, fit=False):
        if self._section is None:
            return
        a, z, h, ps, _c = self._section.place(self.values)
        if self._pair is not None:
            keep = np.zeros(len(a), bool)
            for hh, pp in (self._pair["a"], self._pair["b"]):
                keep |= (h == hh) & (ps == pp)
            if keep.any():
                a, z, h, ps = a[keep], z[keep], h[keep], ps[keep]
        self.sec.set_data(a, z, self._colors(h, ps, z), keep_view=not fit)
        legend = ""
        if self.c_col.currentText() == "pass":
            legend = "  ·  " + "  ".join("<span style='color: rgb(%d,%d,%d)'>pass %d</span>" % (
                PASS_COLORS[k % len(PASS_COLORS)] + (k + 1,)) for k in sorted({int(x) for x in ps}))
        self.v_info.setText("%s soundings within %.1f m of the section%s%s" % (
            "{:,}".format(len(a)), self.s_w.value(), " (this pair only)" if self._pair is not None else "", legend))

    def _use(self):
        self.dock.set_marine_values(self._read())
        self.dock.toast("Values from the Data View are in use (Calibration page); FINALIZE writes them for CloudPro")
