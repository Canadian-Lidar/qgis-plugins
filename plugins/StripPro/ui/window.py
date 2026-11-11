"""The StripPro window: what the job has (left), Align strips and Finalize, and the before / after (right).

A DCS Core PluginWindow with the shared header: the chain (PosPro > AlignPro > StripPro > CloudPro) and
StripPro's two steps as breadcrumbs. The solve runs on a worker thread; the window polls its queue.
"""
import os
import queue
import threading
import traceback

import numpy as np
from qgis.PyQt.QtCore import QPointF, QRectF, Qt, QTimer
from qgis.PyQt.QtGui import QColor, QPainter, QPen
from qgis.PyQt.QtWidgets import (QFrame, QHBoxLayout, QHeaderView, QLabel, QProgressBar, QPushButton, QSplitter,
                                 QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from dcs_core import theme
from dcs_core.header import PluginHeader
from dcs_core.project import DcsProject, ProjectError
from dcs_core.widgets import KV
from dcs_core.window import PluginWindow

from ..core import __version__
from ..core import job as J
from ..core import strip as ST

KEY = "strippro"
LINE_COLS = ["Line", "Points", "Moved (median)", "Moved in holes", "95 %"]


def _cm(v):
    return "–" if v is None else ("%.1f cm" % (100 * v))


class CorrectionChart(QWidget):
    """How far the correction moves a point 20 m from the vehicle, over each line's time; holes shaded."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(170)
        self.series = []        # [(name, t, v (m), holes (n, 2))]

    def set_summary(self, summary, holes):
        self.series = []
        if summary:
            for k, (c, (a, b)) in J.corrections_of(summary).items():
                t = np.arange(a, b, 0.05)
                d, th = c.eval(t)
                v = np.linalg.norm(d, axis=1) + 20.0 * np.linalg.norm(th, axis=1)
                h = holes[(holes[:, 1] > a) & (holes[:, 0] < b)] if holes is not None and len(holes) else np.zeros((0, 2))
                self.series.append((summary["lines"][str(k)]["name"], t, v, h))
        self.update()

    def paintEvent(self, ev):                 # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.fillRect(self.rect(), QColor(theme.BG))
        if not self.series:
            p.end()
            return
        n = len(self.series)
        W, H = self.width(), self.height()
        vmax = max(0.05, max(float(np.percentile(v, 99.5)) for _n, _t, v, _h in self.series))
        rowh = (H - 8) / n
        p.setFont(theme.font("mono", 11))
        for i, (name, t, v, holes) in enumerate(self.series):
            y0 = 4 + i * rowh
            r = QRectF(70, y0 + 4, W - 80, rowh - 18)
            x = lambda tt: r.left() + (tt - t[0]) / max(t[-1] - t[0], 1e-6) * r.width()     # noqa: E731
            for a, b in holes:
                p.fillRect(QRectF(x(max(a, t[0])), r.top(), max(1.0, x(min(b, t[-1])) - x(max(a, t[0]))), r.height()),
                           QColor(245, 166, 35, 60))
            p.setPen(QPen(QColor(theme.DIM), 1))
            p.drawLine(QPointF(r.left(), r.bottom()), QPointF(r.right(), r.bottom()))
            step = max(1, len(t) // int(max(r.width(), 50)))
            pts = [QPointF(x(tt), r.bottom() - min(vv / vmax, 1.0) * r.height()) for tt, vv in zip(t[::step], v[::step])]
            p.setPen(QPen(QColor(theme.ACCENT), 1.4))
            for a, b in zip(pts[:-1], pts[1:]):
                p.drawLine(a, b)
            p.setPen(QColor(theme.TEXT))
            p.drawText(QRectF(4, y0 + 4, 62, 16), int(Qt.AlignmentFlag.AlignLeft), name)
            p.setPen(QColor(theme.DIM))
            p.drawText(QRectF(4, y0 + 20, 62, 16), int(Qt.AlignmentFlag.AlignLeft), "%.0f cm" % (100 * vmax))
        p.end()


class StripProWindow(PluginWindow):
    def __init__(self, iface, parent=None, guide=None):
        super().__init__(iface, KEY, "StripPro", parent, size=(1180, 760), dock_size=620, min_size=(760, 480))
        self._guide = guide
        self.dcs = None
        self.inputs = None
        self.summary = None
        self.worker = None
        self.q = queue.Queue()
        self.cancel = threading.Event()
        root = QWidget()
        root.setObjectName("ppRoot")
        theme.apply(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.header = PluginHeader(KEY, "StripPro", window=self, guide=self.open_guide, version=__version__)
        self.header.stepClicked.connect(self._step)
        m = self.header.new_project_menu()
        m.addAction("Open a DCS project…", self.choose_project)
        m.addSeparator()
        m.addAction("User guide (this version)", self.open_guide)
        lay.addWidget(self.header)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        split.addWidget(self._left())
        split.addWidget(self._right())
        split.setSizes([380, 780])
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(12, 10, 12, 12)
        bl.addWidget(split)
        lay.addWidget(body, 1)
        self.setWidget(root)
        theme.apply_dock(self)
        theme.polish(root)
        self.timer = QTimer(self)
        self.timer.setInterval(150)
        self.timer.timeout.connect(self._poll)
        self._fill()

    # ------------------------------------------------------------------ layout
    def _title(self, text):
        t = QLabel(text.upper())
        t.setObjectName("ppTitle")
        theme.caps(t, 108)
        return t

    def _card(self, w):
        f = QFrame()
        f.setObjectName("ppCard")
        v = QVBoxLayout(f)
        v.addWidget(w)
        return f

    def _left(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 8, 0)
        v.addWidget(self._title("This job"))
        self.kv = KV()
        v.addWidget(self._card(self.kv))
        v.addSpacing(10)
        row = QHBoxLayout()
        self.b_align = QPushButton("Align strips")
        self.b_align.setObjectName("ppPrimary")
        self.b_align.clicked.connect(self.align)
        self.b_cancel = QPushButton("Cancel")
        self.b_cancel.clicked.connect(self.cancel.set)
        row.addWidget(self.b_align, 1)
        row.addWidget(self.b_cancel)
        v.addLayout(row)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        v.addWidget(self.bar)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        v.addStretch(1)
        return w

    def _right(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(8, 0, 0, 0)
        v.addWidget(self._title("Before and after"))
        self.lbl_result = QLabel("")
        self.lbl_result.setWordWrap(True)
        self.lbl_result.setFont(theme.font("body", 15))
        v.addWidget(self.lbl_result)
        self.table = QTableWidget(0, len(LINE_COLS))
        self.table.setHorizontalHeaderLabels(LINE_COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMaximumHeight(140)
        v.addWidget(self.table)
        v.addWidget(self._title("Correction over time (a point 20 m away; holes shaded)"))
        self.chart = CorrectionChart()
        v.addWidget(self.chart, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        self.b_final = QPushButton("Finalize")
        self.b_final.setObjectName("ppPrimary")
        self.b_final.clicked.connect(self.finalize)
        row.addWidget(self.b_final)
        v.addLayout(row)
        return w

    # ------------------------------------------------------------------ project
    def choose_project(self):
        from dcs_core.dialogs import pick_project_file
        f = pick_project_file(self)
        if f:
            self.open_project(f)

    def open_project(self, path):
        self.inputs = None
        try:
            self.dcs = DcsProject.open(path)
        except (ProjectError, OSError, ValueError) as e:
            self.dcs = None
            theme.msg_warn(self, "StripPro", "That DCS project could not be opened:\n%s" % e)
        if self.dcs is not None:
            try:
                self.inputs = J.Inputs(self.dcs)
            except Exception as e:      # noqa: BLE001  (no PosPro trajectory yet, unreadable handover, ...)
                theme.msg_warn(self, "StripPro", "StripPro needs PosPro's finalized trajectory first:\n%s" % e)
        self.summary = J.load(self.dcs) if self.dcs is not None else None
        self._fill()

    def _fill(self):
        h = self.header
        h.set_project_name(self.dcs.name if self.dcs is not None else "")
        try:
            h.chain.set_project(self.dcs)
        except Exception:       # noqa: BLE001
            pass
        inp = self.inputs
        if inp is None:
            for k in ("Trajectory", "Recording holes", "Calibration", "Lines"):
                self.kv.set(k, "–")
        else:
            from dcs_core import sbet as dsbet
            rh = dsbet.holes_summary(inp.traj.t, spans=False)
            self.kv.set("Trajectory", "PosPro, finalized %s" % (inp.traj_doc.get("created") or "")[:16].replace("T", " "))
            self.kv.set("Recording holes", "%.0f %% missing · %d holes · longest %.1f s" % (
                rh["missing_pct"], rh["count"], rh["longest_s"]), "warn" if dsbet.holes_matter(rh) else "ok")
            self.kv.set("Calibration", ("AlignPro %.3f / %.3f / %.3f" % tuple(inp.mount)) if inp.mount else
                        "recorded mounting (AlignPro not finalized)", None if inp.mount else "warn")
            self.kv.set("Lines", ", ".join(n for n, _r in inp.lines()))
        self._show_result()
        self._update_buttons()

    def _show_result(self):
        s = self.summary
        holes = self.inputs.traj.holes if self.inputs is not None else None
        self.chart.set_summary(s, holes)
        if not s:
            self.lbl_result.setText("")
            self.table.setRowCount(0)
            return
        b, a = s["before"], s["after"]
        self.lbl_result.setText(
            "Between lines: %s → <b>%s</b> (median), %s → %s (95 %%).  Same line, seen again: %s → %s." % (
                _cm(b["inter"]["median_m"]), _cm(a["inter"]["median_m"]), _cm(b["inter"]["p95_m"]),
                _cm(a["inter"]["p95_m"]), _cm(b["same"]["median_m"]), _cm(a["same"]["median_m"])))
        rows = list(s["lines"].values())
        self.table.setRowCount(len(rows))
        for i, ln in enumerate(rows):
            mv = ln["moved"]
            vals = [ln["name"], "", _cm(mv["median_m"]), _cm(mv["in_holes_median_m"]), _cm(mv["p95_m"])]
            for j, val in enumerate(vals):
                it = QTableWidgetItem(val)
                if j:
                    it.setTextAlignment(int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))
                self.table.setItem(i, j, it)
        self.table.hideColumn(1)

    def _update_buttons(self):
        busy = self.worker is not None
        self.b_align.setEnabled(self.inputs is not None and not busy)
        self.b_cancel.setVisible(busy)
        self.b_final.setEnabled(self.summary is not None and not busy and self.inputs is not None)
        st = "none"
        if self.dcs is not None:
            try:
                st = self.dcs.state(KEY)[0]
            except Exception:       # noqa: BLE001
                st = "none"
        steps = [("align", "Align strips", "done" if self.summary else "todo", ""),
                 ("finalize", "Finalize", {"done": "done", "stale": "stale"}.get(st, "todo"), "")]
        try:
            self.header.set_steps(steps, "finalize" if self.summary else "align")
        except Exception:       # noqa: BLE001
            pass
        self.header.set_busy(busy)

    def _step(self, k):
        if k == "align":
            self.align()
        elif k == "finalize":
            self.finalize()

    # ------------------------------------------------------------------ work
    def align(self):
        if self.inputs is None or self.worker is not None:
            return
        self.cancel.clear()
        self.status.setText("Reading the lines…")
        self.bar.setValue(0)
        inp = self.inputs

        def work():
            try:
                s = J.run(inp, progress=lambda f, m: self.q.put(("progress", f, m)),
                          cancelled=self.cancel.is_set, log=lambda m: self.q.put(("log", m)))
                self.q.put(("done", s, None))
            except Exception as e:      # noqa: BLE001
                self.q.put(("done", None, "cancelled" if self.cancel.is_set() else
                            "%s\n%s" % (e, traceback.format_exc(limit=3))))
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()
        self.timer.start()
        self._update_buttons()

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "progress":
                    self.bar.setValue(int(1000 * msg[1]))
                    self.status.setText(msg[2])
                elif msg[0] == "done":
                    self.timer.stop()
                    self.worker = None
                    s, err = msg[1], msg[2]
                    if s is not None:
                        self.summary = s
                        self.bar.setValue(1000)
                        self.status.setText("Aligned in %.0f s. Look at the before and after, then Finalize." % s["seconds"])
                    else:
                        self.bar.setValue(0)
                        self.status.setText("Cancelled." if err == "cancelled" else "Stopped: %s" % err.splitlines()[0])
                    self._show_result()
                    self._update_buttons()
        except queue.Empty:
            pass

    def finalize(self):
        if self.summary is None or self.inputs is None:
            return
        try:
            doc = J.write_outputs(self.inputs, self.summary, __version__)
        except Exception as e:      # noqa: BLE001
            theme.msg_warn(self, "StripPro", "Finalizing failed:\n%s" % e)
            return
        self.dcs.reload()
        self.status.setText("Finalized: %d records filled into holes. CloudPro georeferences with this trajectory "
                            "from now on." % doc["strip"]["filled_records"])
        self._fill()

    def open_guide(self, *_):
        if self._guide:
            self._guide()

    def cleanup(self):
        self.cancel.set()
        if self.worker is not None:
            self.worker.join(timeout=10)
        self.timer.stop()
