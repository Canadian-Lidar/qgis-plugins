"""Antenna check window: where GPS1 and GPS2 were measured, against the installation, seen from above
(forward up, starboard right). Used from the Installation page and the Trajectory editor."""

from qgis.PyQt.QtCore import QPointF, QRectF, Qt
from qgis.PyQt.QtGui import QColor, QPainter, QPen
from qgis.PyQt.QtWidgets import QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from dcs_core.widgets import align_flags as _af

from . import theme

GPS1_C, GPS2_C, MEAS_C = theme.GREEN, theme.BLUE, theme.AMBER
VERDICT = {"ok": (theme.GREEN, "Antennas as installed"), "swapped": (theme.RED, "Antennas swapped"),
           "unclear": (theme.AMBER, "Not conclusive")}


class AntennaView(QWidget):
    """Top-down vehicle frame: IMU at the cross, installed GPS1 / GPS2 rings, the measured GPS2 points
    (GPS1 + the measured baseline) as dots and their median as a filled marker."""

    def __init__(self, res, parent=None):
        super().__init__(parent)
        self.res = res
        self.setMinimumSize(420, 320)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.fillRect(self.rect(), QColor(theme.VIEW_BG))
        r = self.res
        l1, l2 = r["lever1"], r["lever2"]
        pts = [(l1[0] + q[1], l1[1] + q[2]) for q in r.get("points") or []]
        meas = (l1[0] + r["measured"][0], l1[1] + r["measured"][1]) if r.get("measured") else None
        xs = [0.0, l1[1], l2[1]] + [q[1] for q in pts]
        ys = [0.0, l1[0], l2[0]] + [q[0] for q in pts]
        span = max(max(xs) - min(xs), max(ys) - min(ys), 1.2) * 1.5
        cx, cy = 0.5 * (max(xs) + min(xs)), 0.5 * (max(ys) + min(ys))
        s = min(self.width(), self.height() - 40) / span
        w0, h0 = self.width() / 2, (self.height() - 40) / 2 + 30

        def scr(fwd, stb):
            return QPointF(w0 + (stb - cx) * s, h0 - (fwd - cy) * s)
        # vehicle outline (the mapper is about 1.2 m wide on the roof) and forward arrow
        p.setPen(QPen(QColor(theme.FAINT), 1, Qt.PenStyle.DashLine))
        p.setBrush(Qt.BrushStyle.NoBrush)
        a, b = scr(0.5, -0.75), scr(-0.5, 0.75)
        p.drawRect(QRectF(a, b))
        p.setPen(QPen(QColor(theme.TEXT), 2))
        top, tip = scr(0.65, 0.0), scr(1.05, 0.0)
        p.drawLine(top, tip)
        p.drawLine(tip, tip + QPointF(-6, 9))
        p.drawLine(tip, tip + QPointF(6, 9))
        p.drawText(QRectF(tip.x() - 60, tip.y() - 22, 120, 16), _af(Qt.AlignmentFlag.AlignCenter), "Forward")
        p.drawText(QRectF(self.width() - 110, h0 - 8, 100, 16), _af(Qt.AlignmentFlag.AlignRight), "Starboard →")
        p.drawText(QRectF(10, h0 - 8, 100, 16), _af(Qt.AlignmentFlag.AlignLeft), "← Port")
        # IMU
        o = scr(0, 0)
        p.drawLine(o + QPointF(-7, 0), o + QPointF(7, 0))
        p.drawLine(o + QPointF(0, -7), o + QPointF(0, 7))
        # measured GPS2 points
        mc = QColor(MEAS_C)
        mc.setAlpha(90)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(mc)
        for f_, st in pts:
            p.drawEllipse(scr(f_, st), 2.0, 2.0)
        # installed antennas
        for lev, col, name, below in ((l1, GPS1_C, "GPS1 primary", True), (l2, GPS2_C, "GPS2 secondary", False)):
            q = scr(lev[0], lev[1])
            p.setPen(QPen(QColor(col), 2.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(q, 11, 11)
            p.setPen(QColor(col))
            # one label below its ring, the other above, so two antennas side by side never collide
            y = q.y() + 16 if below else q.y() - 50
            p.drawText(QRectF(q.x() - 80, y, 160, 16), _af(Qt.AlignmentFlag.AlignCenter), name)
            p.drawText(QRectF(q.x() - 80, y + 16, 160, 16), _af(Qt.AlignmentFlag.AlignCenter), "(installed)")
        if meas:
            q = scr(*meas)
            p.setPen(QPen(QColor(theme.BG), 1))
            p.setBrush(QColor(MEAS_C))
            p.drawEllipse(q, 6.5, 6.5)
            p.setPen(QColor(MEAS_C))
            p.drawText(QRectF(q.x() - 70, q.y() + 12, 140, 16), _af(Qt.AlignmentFlag.AlignCenter), "GPS2 measured")
        # legend
        p.setPen(QColor(theme.DIM))
        p.drawText(QRectF(10, 6, self.width() - 20, 18), _af(Qt.AlignmentFlag.AlignLeft),
                   "Seen from above. Cross: IMU (Certus reference point). Dots: GPS2 measured, epoch by epoch.")


class AntennaDialog(QDialog):
    def __init__(self, res, parent=None, on_swap=None):
        super().__init__(parent)
        self.setWindowTitle("PosPro – antenna check")
        theme.apply(self)
        v = QVBoxLayout(self)
        col, head = VERDICT.get(res["verdict"], VERDICT["unclear"])
        h = QLabel(head)
        h.setStyleSheet("color: %s; font-size: 16px; font-weight: 600;" % col)
        v.addWidget(h)
        t = QLabel(res["text"])
        t.setWordWrap(True)
        v.addWidget(t)
        v.addWidget(AntennaView(res), 1)
        if res.get("measured"):
            m, e = res["measured"], res["expected"]
            info = ("GPS1 → GPS2 measured: forward %.3f, starboard %.3f, down %.3f m (± %.3f, %.3f, %.3f) "
                    "from %d fixed epochs (%d while driving straight). Installed: %.3f, %.3f, %.3f m."
                    % (tuple(m) + tuple(res.get("sd") or (0, 0, 0)) + (res["fixed"], res["moving"]) + tuple(e)))
            if res.get("certus_heading_off_deg") is not None:
                info += " Certus heading minus direction of travel: %.1f°." % res["certus_heading_off_deg"]
            i = QLabel(info)
            i.setWordWrap(True)
            i.setObjectName("ppHint")
            v.addWidget(i)
        bb = QHBoxLayout()
        if res["verdict"] == "swapped" and on_swap is not None:
            b = QPushButton("Swap the GPS1 / GPS2 offsets for this project")
            b.setObjectName("ppPrimary")
            b.setToolTip("Process this data as it was recorded: GPS1 gets GPS2's offset and the other way round, "
                         "and the INS is aligned from the direction of travel (the Certus's own heading was "
                         "turned 180\u00b0). Fix the cables on the unit for the next captures.")
            b.clicked.connect(lambda: (on_swap(), self.accept()))
            bb.addWidget(b)
        bb.addStretch(1)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)
        bb.addWidget(box)
        v.addLayout(bb)
        theme.polish(self)
        self.resize(640, 600)


def run(dock, parent):
    """Run the check in the background and show the result (Installation page and editor)."""
    from ..core import antennas
    p = dock.project
    if not p:
        return
    inst = p.cfg["installation"]
    if not any(abs(x) > 1e-6 for x in inst["lever_antenna"]):
        theme.msg_info(parent, "PosPro", "Enter (or read from the capture) the GPS1 and GPS2 offsets on the "
                                         "Installation page first; the check compares the data with them.")
        return
    work_p = dock._work_copy()

    def work(ctx):
        return antennas.check(work_p, ctx)

    def done(ok, res):
        if not ok:
            theme.msg_warn(parent, "PosPro", "The antenna check could not run:\n\n%s" % str(res).splitlines()[0])
            return
        dock.antenna_check = res
        ed = dock.plugin.editor
        if ed is not None:
            ed.set_antenna_check(res)

        def swap():
            a, b = list(inst["lever_antenna"]), list(inst["lever_antenna2"])
            inst["lever_antenna"], inst["lever_antenna2"] = b, a
            src = inst.get("source") or ""
            if "GPS1/GPS2 swapped" not in src:
                inst["source"] = (src + " + " if src else "") + "GPS1/GPS2 swapped (antenna check)"
            # the Certus's own heading was turned 180° by the swapped antennas: align the INS from the
            # direction of travel instead of from that heading
            off = res.get("certus_heading_off_deg")
            if off is not None and abs(abs(off) - 180) < 30:
                p.cfg["processing"]["align_mode"] = 2
            dock.save()
            for k in (2, 3):
                dock.pages[k].refresh()
            dock.update_steps()
            dock.toast("GPS1 and GPS2 offsets swapped%s: process again" % (
                ", alignment from motion" if p.cfg["processing"].get("align_mode") == 2 else ""), "warn")
        AntennaDialog(res, parent, on_swap=swap).exec()
    dock.start_task("Checking the antennas", work, done)
