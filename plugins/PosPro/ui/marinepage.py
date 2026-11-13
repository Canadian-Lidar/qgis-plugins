"""PosPro's page for marine jobs (sonar recordings from the survey boat): what the job holds, how the position and
heading are taken, Process & finalize, and the result per recording and per sonar head."""
import datetime
import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QComboBox, QDoubleSpinBox, QFormLayout, QGridLayout, QHBoxLayout, QLabel,
                                 QPushButton, QVBoxLayout, QWidget)

from ..core import marine

POSITION_CHOICES = [("auto", "Best available: PPK, else the Reach log, else the autopilot"),
                    ("ppk", "PPK (Reach rover against the base)"), ("llh", "Reach real-time solution (.LLH)"),
                    ("mavlink", "Autopilot position (MAVLink)")]


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


def _utc(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class MarinePage(QWidget):
    def __init__(self, dock):
        super().__init__()
        self.dock = dock
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(_title("Marine trajectory"))
        self.job = QFormLayout()
        self.v_recs = _val()
        self.v_heads = _val()
        self.v_sources = _val()
        self.job.addRow(_key("Recordings"), self.v_recs)
        self.job.addRow(_key("Sonar heads"), self.v_heads)
        self.job.addRow(_key("Position in the recordings"), self.v_sources)
        lay.addLayout(self.job)

        lay.addWidget(_title("Settings"))
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
        bw = QWidget()
        bl = QHBoxLayout(bw)
        bl.setContentsMargins(0, 0, 0, 0)
        self.s_lat, self.s_lon, self.s_h = QDoubleSpinBox(), QDoubleSpinBox(), QDoubleSpinBox()
        for s, lo, hi, dec, suf in ((self.s_lat, -90, 90, 9, "°"), (self.s_lon, -180, 180, 9, "°"),
                                    (self.s_h, -500, 9000, 3, " m")):
            s.setRange(lo, hi)
            s.setDecimals(dec)
            s.setSuffix(suf)
            bl.addWidget(s, 1)
        self.b_tidal = QPushButton("From TidalCal")
        self.b_tidal.setToolTip("The surveyed base the TidalCal project holds")
        self.b_tidal.clicked.connect(self._from_tidalcal)
        bl.addWidget(self.b_tidal)
        f.addRow(_key("Base mark (NAD83(CSRS), ellipsoidal)"), bw)
        self.v_base = _val()
        f.addRow(_key("Base antenna"), self.v_base)
        lay.addLayout(f)
        for w in (self.c_pos, self.s_cog, self.s_lat, self.s_lon, self.s_h):
            sig = w.currentIndexChanged if isinstance(w, QComboBox) else w.valueChanged
            sig.connect(self._store)

        row = QHBoxLayout()
        self.b_run = QPushButton("Process && finalize")
        self.b_run.setObjectName("ppPrimary")
        self.b_run.clicked.connect(self.dock.run_marine)
        row.addWidget(self.b_run)
        row.addStretch(1)
        self.b_next = QPushButton("Continue in AlignPro ›")
        self.b_next.clicked.connect(self._continue)
        row.addWidget(self.b_next)
        lay.addLayout(row)

        lay.addWidget(_title("Result"))
        self.v_state = _val()
        lay.addWidget(self.v_state)
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(14)
        lay.addLayout(self.grid)
        self.heads_res = _val()
        lay.addWidget(self.heads_res)
        lay.addStretch(1)
        self._loading = False
        self._recs = []

    @property
    def project(self):
        return self.dock.project

    # ---------------------------------------------------------------- settings
    def _store(self, *_):
        p = self.project
        if p is None or self._loading:
            return
        m = p.cfg.setdefault("marine", {})
        m["position"] = self.c_pos.currentData()
        m["cog_min_speed"] = self.s_cog.value()
        if self.s_lat.value() or self.s_lon.value():
            m["base"] = {"lat": self.s_lat.value(), "lon": self.s_lon.value(), "h": self.s_h.value(),
                         "frame": "NAD83(CSRS)", "epoch": 2010.0}
        p.save()

    def _from_tidalcal(self):
        rec = self._first_recording()
        b = marine.base_of(rec, {}) if rec is not None else None
        if not b:
            self.v_base.setText("No TidalCal base in the recordings: enter the base mark.")
            return
        self._loading = True
        self.s_lat.setValue(b["lat"])
        self.s_lon.setValue(b["lon"])
        self.s_h.setValue(b["h"])
        self._loading = False
        self._store()

    def _first_recording(self):
        from dcs_core.marine import SonarRecording
        if not self._recs:
            return None
        try:
            return SonarRecording(self._recs[0])
        except Exception:       # noqa: BLE001
            return None

    # ---------------------------------------------------------------- show
    def refresh(self):
        p = self.project
        if p is None:
            return
        from dcs_core.marine import SonarRecording
        self._recs = marine.recordings(p.dcs)
        names, heads, srcs = [], {}, []
        rec0 = None
        for paths in self._recs:
            try:
                r = SonarRecording(paths)
            except Exception as e:      # noqa: BLE001
                names.append("%s (cannot be read: %s)" % (os.path.basename(paths[0]), e))
                continue
            rec0 = rec0 or r
            t0, t1 = r.span()
            names.append("%s  %s, %.0f min" % (r.name, _utc(t0), (t1 - t0) / 60.0))
            for did, h in r.heads.items():
                heads.setdefault(did, h)
            have = []
            if r.rinex("rover") and r.rinex("base"):
                have.append("rover and base RINEX (PPK)")
            elif r.rinex("rover"):
                have.append("rover RINEX (no base)")
            if r.files[0]._m.topic_id("/gnss/llh") is not None:
                have.append("Reach log")
            have.append("autopilot")
            srcs.append("%s: %s" % (r.name, ", ".join(have)))
        self.v_recs.setText("\n".join(names) or "none")
        self.v_heads.setText("\n".join("%s (id %d): offsets %s m F/S/D (%s)" % (
            h["nickname"], did, ", ".join("%+.3f" % v for v in h["lever_m"]) if h.get("lever_m") else "not entered",
            (h.get("lever_source") or "").split(" (")[0]) for did, h in sorted(heads.items())) or "none")
        self.v_sources.setText("\n".join(srcs))
        o = marine.options(p)
        self._loading = True
        self.c_pos.setCurrentIndex(max(0, self.c_pos.findData(o["position"])))
        self.s_cog.setValue(float(o["cog_min_speed"]))
        b = o.get("base") or (marine.base_of(rec0, {}) if rec0 is not None else None)
        if b:
            self.s_lat.setValue(b["lat"])
            self.s_lon.setValue(b["lon"])
            self.s_h.setValue(b["h"])
        self._loading = False
        if rec0 is not None:
            bo = [n for n in rec0.rinex("base") if marine.pipeline_obs(n)]
            if bo:
                import tempfile
                from ..core import rinex
                tmp = os.path.join(tempfile.gettempdir(), "pospro_base_head.obs")
                with open(tmp, "wb") as fh:
                    fh.write(rec0.rinex("base")[bo[0]][:20000])
                hi = rinex.header_info(tmp, scan_body=False) or {}
                d = hi.get("delta_hen") or (0, 0, 0)
                self.v_base.setText("%.3f m mark to ARP, %s (base RINEX header)" % (d[0], " ".join(x for x in (hi.get("antenna") or "").split()
                                                                                               if x != "NONE")
                                                                                      or "antenna type not given"))
            else:
                self.v_base.setText("no base RINEX in the recordings")
        self.show_result()

    def show_result(self):
        p = self.project
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        summ = (p.cfg.get("results") or {}).get("marine") if p else None
        st, why = "none", []
        try:
            st, why = p.dcs.state("pospro")
        except Exception:       # noqa: BLE001
            pass
        if not summ:
            self.v_state.setText("Not processed.")
            self.heads_res.setText("")
            self.b_next.setEnabled(False)
            return
        self.v_state.setText({"done": "Finalized for AlignPro and CloudPro.",
                              "stale": "Out of date (%s): process again." % "; ".join(why)}.get(st, "Processed."))
        self.b_next.setEnabled(st == "done")
        hdr = ["Recording", "Position", "Fixed", "Rate", "Compass heading"]
        for c, h in enumerate(hdr):
            self.grid.addWidget(_key(h), 0, c)
        for r_, rec in enumerate(summ.get("recordings", []), 1):
            q = rec.get("quality_pct") or {}
            vals = [rec["name"], rec["source_label"], "%.1f %%" % q.get("fixed", 0.0),
                    "%s Hz" % rec.get("rate_hz"), "%.1f %% of the time" % rec.get("compass_pct", 0.0)]
            for c, v in enumerate(vals):
                self.grid.addWidget(_val(v), r_, c)
        lines = []
        for did, h in sorted((summ.get("heads") or {}).items()):
            ck = h.get("check") or {}
            lines.append("%s (id %s): %s attitudes (the sonar's own, at every ping)%s" % (
                h.get("nickname"), did, "{:,}".format(h.get("attitudes", 0)),
                "; its raw motion sensor (%s Hz) agrees to %.2f° in roll and %.2f° in pitch (68 %%)" % (
                    ck.get("imu_505_rate_hz"), (ck.get("fused_505_vs_504_roll_deg") or [0, 0])[1],
                    (ck.get("fused_505_vs_504_pitch_deg") or [0, 0])[1]) if ck else ""))
        notes = summ.get("notes") or []
        self.heads_res.setText("\n".join(lines + notes))

    def state(self):
        p = self.project
        try:
            st, _ = p.dcs.state("pospro")
        except Exception:       # noqa: BLE001
            st = "none"
        return {"done": ("done", ""), "stale": ("stale", "")}.get(st, ("todo", ""))

    def _continue(self):
        from dcs_core import registry
        p = self.project
        if p is not None and p.dcs is not None:
            registry.open("alignpro", p.dcs.path)
