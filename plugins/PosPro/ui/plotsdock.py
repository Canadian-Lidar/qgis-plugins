"""Bottom dock with the quality plots (Qinertia-style timeline)."""
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (QCheckBox, QDockWidget, QHBoxLayout, QLabel, QPushButton, QTabWidget,
                                 QVBoxLayout, QWidget)

from ..core import results
from .plots import TimePlot
from . import theme

N, E, D = "#3ab4f2", "#00e5aa", "#f5a623"


class PlotsDock(QDockWidget):
    def __init__(self, plugin):
        super().__init__("PosPro – quality plots")
        self.setObjectName("PosProPlots")
        self.plugin = plugin
        w = QWidget()
        theme.apply(w)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 2, 4, 2)
        top = QHBoxLayout()
        self.info = QLabel("Wheel: zoom   drag: pan   double-click: whole drive   click: show on map")
        top.addWidget(self.info, 1)
        self.link = QCheckBox("Link zoom")
        self.link.setChecked(True)
        top.addWidget(self.link)
        b = QPushButton("Whole drive")
        b.clicked.connect(self.reset)
        top.addWidget(b)
        lay.addLayout(top)
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)
        self.plots = {}
        for key, title, unit, sym in (("pos_sd", "Position accuracy (1σ)", "m", False),
                                      ("sep", "Forward − backward separation", "m", True),
                                      ("att_sd", "Attitude accuracy (1σ)", "deg", False),
                                      ("sats", "Satellites and solution", "", False),
                                      ("speed", "Speed", "m/s", False),
                                      ("height", "Ellipsoidal height", "m", False),
                                      ("att", "Roll and pitch", "deg", True),
                                      ("bias", "IMU bias estimates", "mg / deg/h", True)):
            pl = TimePlot(title, unit, symmetric=sym)
            pl.timePicked.connect(self.picked)
            pl.xRangeChanged.connect(self.zoomed)
            self.plots[key] = pl
            self.tabs.addTab(pl, title.split(" (")[0])
        self.setWidget(w)
        theme.apply_dock(self)
        theme.polish(w)
        self.qc = None

    def set_data(self, qc):
        self.qc = qc
        for pl in self.plots.values():
            pl.clear()
            pl.x0 = pl.x1 = None
        if not qc:
            return
        t = qc["gps_time"]
        P = self.plots
        P["pos_sd"].add("North", t, qc["sd_n"], N)
        P["pos_sd"].add("East", t, qc["sd_e"], E)
        P["pos_sd"].add("Down", t, qc["sd_d"], D)
        has_sep = [v if (a > 0 and b > 0) else None for v, a, b in zip(qc["sep_n"], qc["fwd_status"], qc["bwd_status"])]
        P["sep"].add("North", t, has_sep, N)
        P["sep"].add("East", t, [v if h is not None else None for v, h in zip(qc["sep_e"], has_sep)], E)
        P["sep"].add("Down", t, [v if h is not None else None for v, h in zip(qc["sep_d"], has_sep)], D)
        P["att_sd"].add("Roll", t, qc["sd_roll_deg"], N)
        P["att_sd"].add("Pitch", t, qc["sd_pitch_deg"], E)
        P["att_sd"].add("Heading", t, qc["sd_heading_deg"], D)
        P["sats"].add("Satellites", t, [float(v) for v in qc["nsat"]], "#b8d4ce")
        P["sats"].add("AR ratio (÷10)", t, [min(v, 999) / 10.0 for v in qc["ratio"]], "#9b7bff", 1.0)
        P["speed"].add("Speed", t, qc["speed"], "#3ab4f2")
        P["height"].add("Height", t, qc["h_m"], "#00e5aa")
        P["att"].add("Roll", t, qc["roll_deg"], N)
        P["att"].add("Pitch", t, qc["pitch_deg"], E)
        for k, c in (("bax_mg", N), ("bay_mg", E), ("baz_mg", D)):
            P["bias"].add("acc %s (mg)" % k[2], t, qc[k], c, 1.0)
        for k, c in (("bgx_dh", "#3ab4f2"), ("bgy_dh", "#00e5aa"), ("bgz_dh", "#f5a623")):
            P["bias"].add("gyro %s (deg/h)" % k[2], t, qc[k], c, 1.0)
        cols = {s: QColor(c) for s, c in results.STATUS_COLOURS.items()}
        gaps = results._runs(t, [s == 1 for s in qc["status"]])
        for pl in P.values():
            pl.set_strip(t, qc["status"], cols)
            for a, b in gaps:
                pl.add_band(a, b, QColor(140, 107, 177, 45))
            pl.reset_zoom()

    def reset(self):
        for pl in self.plots.values():
            pl.reset_zoom()

    def zoomed(self, x0, x1):
        if not self.link.isChecked():
            return
        for pl in self.plots.values():
            if pl is not self.sender():
                pl.set_xrange(x0, x1, False)

    def picked(self, t):
        for pl in self.plots.values():
            pl.set_marker(t)
        self.plugin.goto_time(t, None, from_plot=True)

    def zoom_to(self, t0, t1):
        pad = max(10.0, 0.25 * ((t1 or t0) - t0))
        for pl in self.plots.values():
            pl.set_xrange(t0 - pad, (t1 or t0) + pad, False)
            pl.set_marker(t0)
