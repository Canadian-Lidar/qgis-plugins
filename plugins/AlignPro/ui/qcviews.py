"""Quality-control views for the Quality page: tables and plots per line."""
import numpy as np
from qgis.PyQt.QtWidgets import QLabel, QVBoxLayout, QWidget

from ..core import engine, geo
from . import theme
from .common import PlotWidget, cell, fmt, make_table


def _box(*ws):
    w = QWidget()
    l = QVBoxLayout(w)
    l.setContentsMargins(0, 0, 0, 0)
    for x in ws:
        l.addWidget(x)
    return w


def _series(p, plot, key_fn):
    tr = p.traj
    for ln in p.lines:
        t0, t1 = ln.window
        m = (tr.t >= t0) & (tr.t <= t1)
        if m.sum() < 2:
            continue
        ys = key_fn(tr, m)
        if ys is None:
            continue
        xs = tr.t[m] - t0
        step = max(1, len(xs) // 600)
        plot.add(xs[::step], ys[::step], ln.color, "line", ln.name)


def _hint(text):
    l = QLabel(text)
    l.setObjectName("ppHint")
    l.setWordWrap(True)
    return l


def qc_sections(p):
    out = []
    # time sync and rates
    t = make_table(["Line", "Lidar pkt/s", "Lost pkts", "PPS lock %", "NMEA valid %", "Arrival − sensor ms",
                    "Nav Hz", "Return mode", "RPM", "Damaged chunks"])
    t.setRowCount(len(p.lines))
    for i, ln in enumerate(p.lines):
        s, h = ln.stats, ln.info.get("hesai") or {}
        pps, lag = s.get("pps_lock_pct"), s.get("sensor_lag_ms")
        vals = [(ln.name, ln.color), (fmt(s.get("lidar_pps"), "%.0f"),
                                      theme.AMBER if s.get("lidar_pps", 0) < 4900 else None),
                (s.get("lost"), theme.RED if s.get("lost") else None),
                (fmt(pps, "%.1f"), theme.RED if pps is not None and pps < 100 else theme.GREEN),
                (fmt(s.get("nmea_pct"), "%.1f"), None),
                (fmt(lag, "%.2f"), theme.AMBER if lag is not None and abs(lag) > 5 else None),
                (fmt(s.get("nav_hz"), "%.0f"), theme.AMBER if s.get("nav_hz", 0) < 50 else None),
                (h.get("mode", "-"), None), (h.get("rpm", "-"), None), (ln.info.get("bad_chunks", 0), None)]
        for j, (v, c) in enumerate(vals):
            t.setItem(i, j, cell(v, c, j > 0))
    t.setMinimumHeight(180)
    out.append(("Time sync and rates", t, True))
    # positioning
    tr = p.traj
    pl = PlotWidget("Time in pass (s)", "Position SD (m, 3D)")
    if tr.sd_pos is not None:
        _series(p, pl, lambda T, m: np.linalg.norm(T.sd_pos[m], axis=1))
    else:
        pl.title = "No position accuracies (SMRMSG missing)"
    ws = [pl]
    if tr.fix is not None:
        pf = PlotWidget("Time in pass (s)", "Fix (7 RTK fixed, 6 float)")
        _series(p, pf, lambda T, m: T.fix[m].astype(float))
        ws.append(pf)
    for w in ws:
        w.setMinimumHeight(200)
    out.append(("Positioning", _box(*ws), False))
    # attitude
    if tr.sd_att is not None:
        ws = []
        for k, nm in enumerate(("Roll", "Pitch", "Heading")):
            a = PlotWidget("Time in pass (s)", "%s SD (deg)" % nm)
            a.setMinimumHeight(170)
            _series(p, a, lambda T, m, k=k: np.degrees(T.sd_att[m][:, k]))
            ws.append(a)
        out.append(("Attitude accuracy", _box(*ws), False))
    # heading vs cog
    t2 = make_table(["Line", "COG", "Heading − COG (crab)", "SOG m/s", "Length m", "Duration s"])
    t2.setRowCount(len(p.lines))
    for i, ln in enumerate(p.lines):
        s = ln.stats
        for j, v in enumerate([ln.name, fmt(s.get("cog"), "%.1f"), fmt(s.get("crab"), "%+.2f"),
                               fmt(s.get("sog"), "%.2f"), fmt(s.get("length"), "%.0f"),
                               fmt(s.get("duration"), "%.1f")]):
            t2.setItem(i, j, cell(v, ln.color if j == 0 else None, j > 0))
    t2.setMinimumHeight(170)
    out.append(("Heading vs course", _box(t2, _hint(
        "A land vehicle should show almost no crab. A steady offset with the same sign in both directions points at "
        "the trajectory heading (dual antenna), not the Lidar.")), False))
    # residuals
    if p.P is not None and len(p.P):
        pr = PlotWidget("Point-to-plane residual (cm)", "Share of points (%)")
        pr.setMinimumHeight(240)
        sd = p.solve_data()
        used = np.array([ln.use for ln in p.lines])
        bins = np.linspace(-6, 6, 61)
        cen = (bins[:-1] + bins[1:]) / 2
        for label, mount, col in (("Recorded", p.cfg["mount_preset"], theme.RED),
                                  ("Current", p.cfg["mount"], theme.ACCENT)):
            B = geo.lidar_matrix(*mount) @ p.M.T
            e, m = engine.residuals(sd, B, p.cfg["lever"], used)
            h, _ = np.histogram(e[m] * 100, bins)
            pr.add(cen, 100.0 * h / max(1, m.sum()), col, "line", "%s  %.2f cm" % (label, np.mean(np.abs(e[m])) * 100))
        out.append(("Residuals", pr, True))
    # ranges
    pg = PlotWidget("Range (m)", "Points")
    pg.setMinimumHeight(200)
    bins = np.linspace(0, float(p.settings.get("range_max", 60)), 61)
    cen = (bins[:-1] + bins[1:]) / 2
    for ln in p.lines:
        r = np.linalg.norm(ln.data["p"][ln.idx], axis=1)
        h, _ = np.histogram(r, bins)
        pg.add(cen, h, ln.color, "line", ln.name)
    out.append(("Ranges", pg, False))
    return out
