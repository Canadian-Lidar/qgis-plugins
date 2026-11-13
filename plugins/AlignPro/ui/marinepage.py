"""AlignPro's page for marine jobs (sonar recordings from the survey boat): the patch test of each sonar head
(roll, pitch, heading and, when asked, latency), the result with its uncertainty and whether the survey lines
determine it, and Finalize for CloudPro."""
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGridLayout, QHBoxLayout,
                                 QLabel, QPushButton, QVBoxLayout, QWidget)

from ..core import marine

LABELS = {"roll": ("Roll", "°", 3), "pitch": ("Pitch", "°", 3), "heading": ("Heading", "°", 3),
          "latency_s": ("Latency", " ms", 1)}


def _title(text):
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


def _fmt(k, v):
    _l, unit, dec = LABELS[k]
    if v is None:
        return ""
    return ("%+.*f%s" % (dec, v * 1000.0, unit)) if k == "latency_s" else ("%+.*f%s" % (dec, v, unit))


class MarinePage(QWidget):
    def __init__(self, dock):
        super().__init__()
        self.dock = dock
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(_title("Sonar patch test"))
        f = QFormLayout()
        self.v_traj = _val()
        self.v_heads = _val()
        f.addRow(_key("Boat trajectory"), self.v_traj)
        f.addRow(_key("Sonar heads"), self.v_heads)
        lay.addLayout(f)

        lay.addWidget(_title("Settings"))
        g = QFormLayout()
        self.c_every = QComboBox()
        for n, t in ((1, "every ping"), (2, "every 2nd ping"), (3, "every 3rd ping"), (5, "every 5th ping")):
            self.c_every.addItem(t, n)
        g.addRow(_key("Soundings used"), self.c_every)
        self.c_power = QComboBox()
        for k, t in (("high", "strong returns only"), ("medium", "medium and strong"), ("low", "all above the low "
                                                                                              "threshold")):
            self.c_power.addItem(t, k)
        g.addRow(_key("Returns"), self.c_power)
        self.k_lat = QCheckBox("Also solve the latency")
        self.k_lat.setToolTip("The latency moves the seabed along the track as the pitch does; it can be told "
                              "apart only with lines run at different speeds over a sloping seabed")
        g.addRow(_key(""), self.k_lat)
        lay.addLayout(g)
        for w in (self.c_every, self.c_power):
            w.currentIndexChanged.connect(self._store)
        self.k_lat.toggled.connect(self._store)

        row = QHBoxLayout()
        self.b_solve = QPushButton("Solve")
        self.b_solve.setObjectName("ppPrimary")
        self.b_solve.clicked.connect(self.dock.run_marine_solve)
        row.addWidget(self.b_solve)
        self.b_fin = QPushButton("Finalize")
        self.b_fin.clicked.connect(self.dock.run_marine_publish)
        row.addWidget(self.b_fin)
        row.addStretch(1)
        self.b_next = QPushButton("Continue in CloudPro ›")
        self.b_next.clicked.connect(self._continue)
        row.addWidget(self.b_next)
        lay.addLayout(row)

        lay.addWidget(_title("Corrections per head"))
        self.v_state = _val()
        lay.addWidget(self.v_state)
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(14)
        lay.addLayout(self.grid)
        lay.addWidget(_title("Values used (edit to override)"))
        self.edit_grid = QGridLayout()
        self.edit_grid.setHorizontalSpacing(10)
        lay.addLayout(self.edit_grid)
        self.edits = {}
        self.v_notes = _val()
        lay.addWidget(self.v_notes)
        lay.addStretch(1)
        self._loading = False

    @property
    def project(self):
        return self.dock.p

    def cfg(self):
        return self.project.cfg.setdefault("marine", {}) if self.project else {}

    def _store(self, *_):
        if self.project is None or self._loading:
            return
        m = self.cfg()
        m["every"] = self.c_every.currentData()
        m["power"] = self.c_power.currentData()
        m["latency"] = self.k_lat.isChecked()
        self.project.save()

    def _store_values(self, *_):
        if self.project is None or self._loading:
            return
        vals = {}
        for (d, k), s in self.edits.items():
            v = s.value()
            vals.setdefault(d, {})[k] = v / 1000.0 if k == "latency_s" else v
        self.cfg()["applied"] = vals
        self.project.save()
        self.dock.update_header_steps()

    # ---------------------------------------------------------------- show
    def refresh(self):
        p = self.project
        if p is None or p.dcs is None:
            return
        m = self.cfg()
        self._loading = True
        self.c_every.setCurrentIndex(max(0, self.c_every.findData(int(m.get("every", 2)))))
        self.c_power.setCurrentIndex(max(0, self.c_power.findData(m.get("power", "medium"))))
        self.k_lat.setChecked(bool(m.get("latency", False)))
        self._loading = False
        from dcs_core import handover
        try:
            doc = handover.read(p.dcs, "pospro", required=False)
        except Exception as e:      # noqa: BLE001
            doc = None
            self.v_traj.setText("cannot be read: %s" % e)
        st = "none"
        try:
            st, _w = p.dcs.state("pospro")
        except Exception:           # noqa: BLE001
            pass
        if doc and doc.get("platform") == "marine":
            mq = (doc.get("marine") or {})
            self.v_traj.setText("PosPro, %s%s" % (
                ", ".join("%s: %s" % (r.get("recording"), r.get("source")) for r in mq.get("position", [])) or
                "finalized", "" if st == "done" else " (out of date: process it again in PosPro)"))
            heads = (doc.get("sensors") or {})
            self.v_heads.setText("\n".join("%s (id %s), offsets %s m F/S/D" % (
                s.get("nickname"), k.split("_")[-1], ", ".join("%+.3f" % v for v in s.get("lever_m") or []) or "not entered")
                for k, s in sorted(heads.items()) if k.startswith("sonar_")) or "none")
        else:
            self.v_traj.setText("Not finalized: process the boat's trajectory in PosPro (Marine) first.")
            self.v_heads.setText("")
        self.b_solve.setEnabled(bool(doc) and doc.get("platform") == "marine")
        self.show_result()

    def show_result(self):
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        while self.edit_grid.count():
            w = self.edit_grid.takeAt(0).widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        self.edits = {}
        m = self.cfg()
        res = m.get("result")
        st = self.state()[0]
        self.b_fin.setEnabled(bool(res))
        self.b_next.setEnabled(st == "done")
        if not res:
            self.v_state.setText("Not solved.")
            self.v_notes.setText("")
            return
        self.v_state.setText("The seabed agrees to %.1f cm as recorded and %.1f cm with the corrections (median "
                             "between passes and between heads; %s compared soundings, %d passes).%s" % (
                                 res["before_cm"], res["after_cm"], "{:,}".format(res["observations"]),
                                 res["passes"], {"done": " Finalized for CloudPro.",
                                                 "stale": " Finalized, out of date."}.get(st, "")))
        hdr = ["Head"] + [LABELS[k][0] for k in marine.PARAMS]
        for c, h in enumerate(hdr):
            self.grid.addWidget(_key(h), 0, c)
            self.edit_grid.addWidget(_key(h), 0, c)
        applied = m.get("applied") or {}
        for r_, (d, e) in enumerate(sorted(res["heads"].items()), 1):
            self.grid.addWidget(_val(str(m.get("names", {}).get(d) or "id %s" % d)), r_, 0)
            self.edit_grid.addWidget(_val(str(m.get("names", {}).get(d) or "id %s" % d)), r_, 0)
            for c, k in enumerate(marine.PARAMS, 1):
                if k in (res.get("estimated") or {}).get(d, []):
                    sd = e.get(k + "_sd")
                    t = "%s ± %s" % (_fmt(k, e[k]), _fmt(k, sd).lstrip("+")) if sd is not None else _fmt(k, e[k])
                else:
                    t = "not determined (0)"
                self.grid.addWidget(_val(t), r_, c)
                s = QDoubleSpinBox()
                _l, unit, dec = LABELS[k]
                s.setDecimals(dec)
                s.setSuffix(unit)
                s.setRange(-500.0, 500.0) if k == "latency_s" else s.setRange(-30.0, 30.0)
                s.setSingleStep(1.0 if k == "latency_s" else 0.01)
                v = (applied.get(d) or {}).get(k, e[k])
                s.setValue(v * 1000.0 if k == "latency_s" else v)
                s.valueChanged.connect(self._store_values)
                self.edit_grid.addWidget(s, r_, c)
                self.edits[(d, k)] = s
        notes = []
        for st_ in res.get("steps", []):
            notes.append("%s: %.1f cm" % (st_["step"], st_["cm"]))
        if res.get("correlated"):
            notes.append("Hard to tell apart: " + "; ".join("%s and %s (%.2f)" % tuple(c) for c in res["correlated"]))
        notes.append("A correction is kept only when it makes the seabed agree at least %.1f cm better; the "
                     "others need lines that see them (a slope or a feature crossed in opposite directions for "
                     "pitch, a feature off to the side for heading, two speeds for latency)." % marine.MIN_GAIN_CM)
        self.v_notes.setText("\n".join(notes))

    def state(self):
        p = self.project
        try:
            st, _ = p.dcs.state("alignpro")
        except Exception:       # noqa: BLE001
            st = "none"
        if st in ("done", "stale"):
            return st, ""
        return ("todo", "") if not (self.cfg() or {}).get("result") else ("todo", "solved, not finalized")

    def values(self):
        """The corrections to finalize: the edited values, else the solution."""
        m = self.cfg()
        res = m.get("result") or {}
        out = {}
        for d, e in (res.get("heads") or {}).items():
            out[d] = {k: float(((m.get("applied") or {}).get(d) or {}).get(k, e[k])) for k in marine.PARAMS}
        return out

    def _continue(self):
        from dcs_core import registry
        p = self.project
        if p is not None and p.dcs is not None:
            registry.open("cloudpro", p.dcs.path)
