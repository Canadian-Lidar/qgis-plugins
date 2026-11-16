"""The SIDE SCAN tab (marine jobs, CloudPro 2.10.0): process the Omniscan side scan onto the 3D seabed, look
through it pass by pass in a waterfall (port left, starboard right), mark contacts, make mosaics and write XTF."""
import os

import numpy as np

from ..qt import QtCore, QtGui, QtWidgets, Qt, Signal
from ..core import sidescan as SS


def grey(v, lo, hi):
    """dB -> 8-bit grey (NaN = black)."""
    g = np.clip((v - lo) / max(hi - lo, 1e-6) * 255.0, 0, 255)
    return np.where(np.isfinite(v), g, 0).astype(np.uint8)


def fit_columns(D, w):
    """Rows of samples to w columns: the mean of the samples in each column when there are more samples than
    columns (no aliasing speckle), the nearest sample otherwise."""
    ns = D.shape[1]
    if ns <= w:
        return D[:, np.clip((np.arange(w) / w * ns).astype(int), 0, ns - 1)]
    edges = np.linspace(0, ns, w + 1).astype(int)
    ok = np.isfinite(D)
    s = np.add.reduceat(np.where(ok, D, 0.0), edges[:-1], axis=1)
    c = np.add.reduceat(ok.astype(np.float32), edges[:-1], axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(c > 0, s / c, np.nan)


class Waterfall(QtWidgets.QWidget):
    """Port (left, far range at the edge) and starboard (right) of one pass, a ping per row, the latest at the
    top. Wheel or the scroll bar moves along the pass, Ctrl + wheel zooms the pings; drag a box round a target
    to add a contact. hovered(x, y local) for the Plan's cross; boxed(line id, i0, i1, s0, s1)."""
    hovered = Signal(object)
    boxed = Signal(int, int, int, int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumHeight(220)
        self.A = self.B = None            # port, starboard LineSS (either may be None)
        self.lv = {}
        self.top = 0                      # index (in the port / main line) of the top row's ping
        self.rows_per_px = 1.0
        self.contrast = 1.0
        self.levelled = True
        self.contacts = []
        self._drag = None
        self._img = None
        self.scroll = QtWidgets.QScrollBar(Qt.Orientation.Vertical, self)
        self.scroll.valueChanged.connect(self._scrolled)

    # -- data
    def set_lines(self, A, B):
        """A: port LineSS (or None), B: starboard LineSS (or None)."""
        self.A, self.B = A, B
        self.lv = {}
        for L in (A, B):
            if L is not None:
                d = L.levelled()
                ok = np.isfinite(d)
                lo, hi = (np.percentile(d[ok][::17], [1, 99]) if ok.any() else (0.0, 1.0))
                self.lv[L.lid] = (d, L.db, float(lo), float(hi), np.percentile(L.db[np.isfinite(L.db)][::17],
                                                                               [1, 99]))
        self.top = self.main.n - 1 if self.main is not None else 0
        self._sync_scroll()
        self._img = None
        self.update()

    @property
    def main(self):
        return self.A if self.A is not None else self.B

    def _other_index(self, i):
        """The other head's ping nearest in time to ping i of the main line (or None)."""
        if self.A is None or self.B is None:
            return None
        t = self.A.t[i]
        j = int(np.clip(np.searchsorted(self.B.t, t), 1, self.B.n - 1))
        j = j - 1 if abs(self.B.t[j - 1] - t) < abs(self.B.t[j] - t) else j
        return j if abs(self.B.t[j] - t) < 0.5 else None

    def _sync_scroll(self):
        m = self.main
        self.scroll.blockSignals(True)
        if m is None:
            self.scroll.setRange(0, 0)
        else:
            self.scroll.setRange(0, m.n - 1)
            self.scroll.setPageStep(max(1, int(self.height() * self.rows_per_px)))
            self.scroll.setValue(m.n - 1 - self.top)
        self.scroll.blockSignals(False)

    def _scrolled(self, v):
        if self.main is not None:
            self.top = self.main.n - 1 - int(v)
            self._img = None
            self.update()

    def go_to(self, lid, i):
        """Bring ping i of line lid to the middle."""
        m = self.main
        if m is None:
            return
        if lid != m.lid and self.B is not None and lid == self.B.lid:
            i = int(np.clip(np.searchsorted(m.t, self.B.t[i]), 0, m.n - 1))
        self.top = int(min(m.n - 1, i + self.height() * self.rows_per_px / 2))
        self._sync_scroll()
        self._img = None
        self.update()

    # -- geometry
    def _layout(self):
        w = self.width() - self.scroll.width()
        half = (w - 6) // 2
        return w, half

    def _at(self, x, y):
        """(LineSS, ping index, sample index) under the widget point x, y, or None."""
        m = self.main
        if m is None:
            return None
        w, half = self._layout()
        i = int(round(self.top - y * self.rows_per_px))
        if i < 0 or i >= m.n:
            return None
        ns = m.db.shape[1]
        if x < half:
            if self.A is None:
                return None
            s = int((half - 1 - x) / half * ns)
            return self.A, i, int(np.clip(s, 0, ns - 1))
        if x >= half + 6 and self.B is not None:
            j = i if self.A is None else self._other_index(i)
            if j is None:
                return None
            s = int((x - half - 6) / half * ns)
            return self.B, j, int(np.clip(s, 0, ns - 1))
        return None

    def _xy(self, L, i, s):
        """Widget point of sample s of ping i of line L (i in L's own pings)."""
        m = self.main
        w, half = self._layout()
        ns = L.db.shape[1]
        if L is self.B and self.A is not None:
            i = int(np.clip(np.searchsorted(m.t, L.t[i]), 0, m.n - 1))
        y = (self.top - i) / self.rows_per_px
        x = half - 1 - s / ns * half if L is self.A else half + 6 + s / ns * half
        return x, y

    # -- drawing
    def _render(self):
        m = self.main
        w, half = self._layout()
        h = self.height()
        if m is None or w < 20 or h < 10:
            return None
        rows = np.round(self.top - np.arange(h) * self.rows_per_px).astype(int)
        valid = (rows >= 0) & (rows < m.n)
        rows_c = np.clip(rows, 0, m.n - 1)
        out = np.zeros((h, w), np.uint8)
        for L, side in ((self.A, "port"), (self.B, "stbd")):
            if L is None:
                continue
            d, raw, lo, hi, rp = self.lv[L.lid]
            data = d if self.levelled else raw
            if not self.levelled:
                lo, hi = float(rp[0]), float(rp[1])
            mid = (lo + hi) / 2
            span = (hi - lo) / 2 / self.contrast
            if L is m:
                idx = rows_c
                ok = valid
            else:
                idx = np.array([self._other_index(i) if v else -1 for i, v in zip(rows_c, valid)])
                ok = idx >= 0
                idx = np.where(ok, idx, 0)
            img = grey(fit_columns(data[idx], half), mid - span, mid + span)
            img[~ok] = 0
            if side == "port":
                out[:, :half] = img[:, ::-1]
            else:
                out[:, half + 6:half + 6 + half] = img
        out[:, half:half + 6] = 40
        return np.ascontiguousarray(out)

    def paintEvent(self, _ev):
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtGui.QColor(14, 18, 24))
        if self._img is None:
            a = self._render()
            if a is not None:
                q = QtGui.QImage(a.data, a.shape[1], a.shape[0], a.shape[1], QtGui.QImage.Format.Format_Grayscale8)
                self._img = (q.copy(), a)
        if self._img is not None:
            p.drawImage(0, 0, self._img[0])
        else:
            p.setPen(QtGui.QColor(150, 160, 170))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No side scan for this pass yet")
        m = self.main
        if m is not None:
            pen = QtGui.QPen(QtGui.QColor(255, 196, 0), 1.5)
            p.setPen(pen)
            for c in self.contacts:
                L = self.A if self.A is not None and c["line_id"] == self.A.lid else \
                    self.B if self.B is not None and c["line_id"] == self.B.lid else None
                if L is None:
                    continue
                i0, i1, s0, s1 = c["box"]
                x0, y0 = self._xy(L, i0, s0)
                x1, y1 = self._xy(L, i1, s1)
                r = QtCore.QRectF(QtCore.QPointF(x0, y0), QtCore.QPointF(x1, y1)).normalized()
                if r.bottom() < 0 or r.top() > self.height():
                    continue
                p.drawRect(r)
                p.drawText(r.topRight() + QtCore.QPointF(3, 10), c["name"])
            w, half = self._layout()
            fm = p.fontMetrics()
            for txt, x in (("PORT", 6), ("STARBOARD", w - 12 - fm.horizontalAdvance("STARBOARD"))):
                r = QtCore.QRectF(x - 4, 2, fm.horizontalAdvance(txt) + 8, fm.height() + 2)
                p.fillRect(r, QtGui.QColor(14, 18, 24, 200))
                p.setPen(QtGui.QColor(0, 229, 170))
                p.drawText(r, Qt.AlignmentFlag.AlignCenter, txt)
            p.setPen(QtGui.QPen(QtGui.QColor(255, 196, 0), 1.5))
        if self._drag is not None:
            p.setPen(QtGui.QPen(QtGui.QColor(0, 229, 170), 1.5, Qt.PenStyle.DashLine))
            p.drawRect(QtCore.QRectF(self._drag[0], self._drag[1]).normalized())
        p.end()

    def resizeEvent(self, ev):
        sw = self.style().pixelMetric(QtWidgets.QStyle.PixelMetric.PM_ScrollBarExtent)
        self.scroll.setGeometry(self.width() - sw, 0, sw, self.height())
        self._sync_scroll()
        self._img = None
        super().resizeEvent(ev)

    # -- mouse
    def wheelEvent(self, ev):
        if self.main is None:
            return
        d = ev.angleDelta().y() / 120.0
        if ev.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.rows_per_px = float(np.clip(self.rows_per_px * (0.8 ** d), 0.25, 16.0))
        else:
            self.top = int(np.clip(self.top + d * 40 * self.rows_per_px, 0, self.main.n - 1))
        self._sync_scroll()
        self._img = None
        self.update()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and self._at(ev.position().x(), ev.position().y()):
            self._drag = [ev.position(), ev.position()]

    def mouseMoveEvent(self, ev):
        x, y = ev.position().x(), ev.position().y()
        if self._drag is not None:
            self._drag[1] = ev.position()
            self.update()
        hit = self._at(x, y)
        if hit is None:
            self.hovered.emit(None)
            return
        L, i, s = hit
        e, n, z = L.at(i, s)
        self.hovered.emit((e, n, z, L, i, s) if np.isfinite(e) else None)

    def mouseReleaseEvent(self, ev):
        if self._drag is None:
            return
        a, b = self._drag
        self._drag = None
        self.update()
        if abs(a.x() - b.x()) < 3 and abs(a.y() - b.y()) < 3:
            return
        h0 = self._at(a.x(), a.y())
        h1 = self._at(b.x(), b.y())
        if h0 is None or h1 is None or h0[0] is not h1[0]:
            return
        L = h0[0]
        self.boxed.emit(int(L.lid), h0[1], h1[1], h0[2], h1[2])

    def leaveEvent(self, _ev):
        self.hovered.emit(None)


class SideScanPanel(QtWidgets.QWidget):
    """The SIDE SCAN tab."""
    process_requested = Signal()
    mosaic_requested = Signal()
    xtf_requested = Signal()
    contact_added = Signal(object)
    contact_selected = Signal(object)
    hovered = Signal(object)
    export_contacts_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.project = None
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)
        row = QtWidgets.QHBoxLayout()
        self.b_process = QtWidgets.QPushButton("Process side scan")
        self.b_process.setObjectName("ppPrimary")
        self.b_process.setToolTip("Place every side-scan sample on the 3D seabed of its ping (all passes)")
        row.addWidget(self.b_process)
        row.addWidget(QtWidgets.QLabel("Pass"))
        self.pass_cb = QtWidgets.QComboBox()
        self.pass_cb.setMinimumWidth(150)
        row.addWidget(self.pass_cb, 1)
        self.b_mosaic = QtWidgets.QPushButton("Mosaic...")
        self.b_xtf = QtWidgets.QPushButton("Export XTF...")
        row.addWidget(self.b_mosaic)
        row.addWidget(self.b_xtf)
        v.addLayout(row)
        row2 = QtWidgets.QHBoxLayout()
        self.levelled = QtWidgets.QCheckBox("Gain levelled")
        self.levelled.setChecked(True)
        self.levelled.setToolTip("Even out the image from nadir to the far range (the sonar's raw dB when off)")
        row2.addWidget(self.levelled)
        row2.addWidget(QtWidgets.QLabel("Contrast"))
        self.contrast = QtWidgets.QSlider(Qt.Orientation.Horizontal)
        self.contrast.setRange(50, 400)
        self.contrast.setValue(100)
        row2.addWidget(self.contrast, 1)
        self.info = QtWidgets.QLabel("")
        row2.addWidget(self.info)
        v.addLayout(row2)
        split = QtWidgets.QSplitter(Qt.Orientation.Vertical)
        self.wf = Waterfall()
        split.addWidget(self.wf)
        box = QtWidgets.QWidget()
        bl = QtWidgets.QVBoxLayout(box)
        bl.setContentsMargins(0, 0, 0, 0)
        hr = QtWidgets.QHBoxLayout()
        hr.addWidget(QtWidgets.QLabel("Contacts"))
        hr.addStretch(1)
        self.b_del = QtWidgets.QPushButton("Delete")
        self.b_export = QtWidgets.QPushButton("Export contacts...")
        hr.addWidget(self.b_del)
        hr.addWidget(self.b_export)
        bl.addLayout(hr)
        self.table = QtWidgets.QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(["Name", "Easting", "Northing", "Z", "Length", "Width", "Height",
                                              "Remarks"])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setStretchLastSection(True)
        bl.addWidget(self.table)
        split.addWidget(box)
        split.setSizes([400, 160])
        v.addWidget(split, 1)
        self.b_process.clicked.connect(self.process_requested.emit)
        self.b_mosaic.clicked.connect(self.mosaic_requested.emit)
        self.b_xtf.clicked.connect(self.xtf_requested.emit)
        self.b_export.clicked.connect(self.export_contacts_requested.emit)
        self.b_del.clicked.connect(self._delete)
        self.pass_cb.currentIndexChanged.connect(self._load_pass)
        self.levelled.toggled.connect(self._display)
        self.contrast.valueChanged.connect(self._display)
        self.wf.hovered.connect(self._hover)
        self.wf.boxed.connect(self._boxed)
        self.table.itemDoubleClicked.connect(self._go)
        self.table.itemChanged.connect(self._edited)
        self._passes = []
        self._filling = False

    # -- state
    def set_project(self, project, busy=False):
        self.project = project
        cur = self.pass_cb.currentData()
        self._passes = SS.passes(project) if project is not None else []
        self.pass_cb.blockSignals(True)
        self.pass_cb.clear()
        for k, (key, label, ids) in enumerate(self._passes):
            done = all(SS.processed(project, i) for i in ids)
            self.pass_cb.addItem(label + ("" if done else "  (not processed)"), k)
        self.pass_cb.blockSignals(False)
        if cur is not None and cur < self.pass_cb.count():
            self.pass_cb.setCurrentIndex(cur)
        n_done = sum(all(SS.processed(project, i) for i in ids) for _k, _l, ids in self._passes)
        self.b_process.setEnabled(bool(self._passes) and not busy)
        self.b_mosaic.setEnabled(n_done > 0 and not busy)
        self.b_xtf.setEnabled(n_done > 0 and not busy)
        self.info.setText("%d of %d passes processed" % (n_done, len(self._passes)) if self._passes else
                          "No sonar lines in this job")
        self._load_pass()
        self.fill_contacts()

    def current_pass(self):
        k = self.pass_cb.currentData()
        return self._passes[k] if k is not None and k < len(self._passes) else None

    def _load_pass(self):
        P = self.current_pass()
        A = B = None
        if P is not None and self.project is not None:
            for lid in P[2]:
                if SS.processed(self.project, lid):
                    L = SS.LineSS(self.project, lid)
                    if L.side > 0:
                        A = L
                    else:
                        B = L
        self.wf.set_lines(A, B)
        self._display()

    def _display(self):
        self.wf.levelled = self.levelled.isChecked()
        self.wf.contrast = self.contrast.value() / 100.0
        self.wf.contacts = SS.contacts(self.project) if self.project is not None else []
        self.wf._img = None
        self.wf.update()

    def fill_contacts(self):
        self._filling = True
        cs = SS.contacts(self.project) if self.project is not None else []
        self.table.setRowCount(len(cs))
        for r, c in enumerate(cs):
            vals = [c["name"], "%.2f" % c["e"], "%.2f" % c["n"], "%.2f" % c["z"], "%.2f" % c["length_m"],
                    "%.2f" % c["width_m"], "%.2f" % c["height_m"], c.get("remarks") or ""]
            for k, t in enumerate(vals):
                it = QtWidgets.QTableWidgetItem(t)
                if k not in (0, 7):
                    it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(r, k, it)
        self.table.resizeColumnsToContents()
        self._filling = False
        self._display()

    # -- events
    def _hover(self, h):
        if h is None:
            self.hovered.emit(None)
            return
        e, n, z, L, i, s = h
        self.info.setText("ping %d · %s %.1f m slant · Z %.2f" % (int(L.ping[i]), "port" if L.side > 0 else
                                                                   "starboard", float(L.r[s]), z))
        self.hovered.emit((e, n, z))

    def _boxed(self, lid, i0, i1, s0, s1):
        if self.project is None:
            return
        c = SS.add_contact(self.project, lid, i0, i1, s0, s1)
        self._save_clip(c)
        self.fill_contacts()
        self.contact_added.emit(c)

    def _save_clip(self, c):
        """A picture of the contact (its box and some seabed round it) for the report."""
        try:
            L = SS.LineSS(self.project, c["line_id"])
            i0, i1, s0, s1 = c["box"]
            pi, ps = max(20, (i1 - i0)), max(20, (s1 - s0))
            a = L.levelled()[max(0, i0 - pi):i1 + pi + 1, max(0, s0 - ps):s1 + ps * 2 + 1]
            ok = np.isfinite(a)
            lo, hi = np.percentile(a[ok], [1, 99]) if ok.any() else (0, 1)
            g = np.ascontiguousarray(grey(a, lo, hi)[::-1])
            if L.side > 0:
                g = np.ascontiguousarray(g[:, ::-1])
            q = QtGui.QImage(g.data, g.shape[1], g.shape[0], g.shape[1], QtGui.QImage.Format.Format_Grayscale8)
            d = os.path.join(SS.folder(self.project), "contacts")
            os.makedirs(d, exist_ok=True)
            f = os.path.join(d, "%s.png" % c["name"])
            q.copy().scaled(max(200, g.shape[1] * 2), max(200, g.shape[0] * 2)).save(f)
            c["image"] = f
        except Exception:       # noqa: BLE001 - the picture is a convenience
            pass

    def _row_contact(self, r):
        cs = SS.contacts(self.project)
        return cs[r] if 0 <= r < len(cs) else None

    def _go(self, item):
        c = self._row_contact(item.row())
        if c is None:
            return
        for k, (_key, _label, ids) in enumerate(self._passes):
            if c["line_id"] in ids and self.pass_cb.currentData() != k:
                self.pass_cb.setCurrentIndex(self.pass_cb.findData(k))
        self.wf.go_to(c["line_id"], (c["box"][0] + c["box"][1]) // 2)
        self.contact_selected.emit(c)

    def _edited(self, item):
        if self._filling:
            return
        c = self._row_contact(item.row())
        if c is None:
            return
        if item.column() == 0 and item.text().strip():
            c["name"] = item.text().strip()
        elif item.column() == 7:
            c["remarks"] = item.text()
        self._display()

    def _delete(self):
        rows = sorted({i.row() for i in self.table.selectedItems()}, reverse=True)
        cs = SS.contacts(self.project) if self.project is not None else []
        for r in rows:
            if 0 <= r < len(cs):
                cs.pop(r)
        self.fill_contacts()


class MosaicDialog(QtWidgets.QDialog):
    """Mosaic settings: passes, cell size, overlap priority, gain levelling, gap fill, the output file."""

    def __init__(self, parent, default_path, has_pass):
        super().__init__(parent)
        self.setWindowTitle("Side-scan mosaic")
        f = QtWidgets.QFormLayout(self)
        self.which = QtWidgets.QComboBox()
        self.which.addItem("Every processed pass", "all")
        if has_pass:
            self.which.addItem("The pass in the waterfall", "pass")
        f.addRow("Passes", self.which)
        self.cell = QtWidgets.QDoubleSpinBox()
        self.cell.setRange(0.05, 5.0)
        self.cell.setSingleStep(0.05)
        self.cell.setValue(0.25)
        self.cell.setSuffix(" m")
        f.addRow("Cell size", self.cell)
        self.prio = QtWidgets.QComboBox()
        self.prio.addItem("Nearest to the track wins", "nearest")
        self.prio.addItem("Average", "average")
        f.addRow("Where passes overlap", self.prio)
        self.lev = QtWidgets.QCheckBox("Gain levelled")
        self.lev.setChecked(True)
        f.addRow("", self.lev)
        self.fill = QtWidgets.QSpinBox()
        self.fill.setRange(0, 10)
        self.fill.setValue(2)
        self.fill.setSuffix(" cells")
        f.addRow("Fill gaps up to", self.fill)
        row = QtWidgets.QHBoxLayout()
        self.path = QtWidgets.QLineEdit(default_path)
        b = QtWidgets.QPushButton("...")
        b.clicked.connect(self._browse)
        row.addWidget(self.path, 1)
        row.addWidget(b)
        f.addRow("GeoTIFF", row)
        self.byte = QtWidgets.QCheckBox("Also an 8-bit grey image (_8bit.tif)")
        self.byte.setChecked(True)
        f.addRow("", self.byte)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok |
                                        QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def _browse(self):
        p, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Mosaic GeoTIFF", self.path.text(), "GeoTIFF (*.tif)")
        if p:
            self.path.setText(p if p.lower().endswith(".tif") else p + ".tif")

    def values(self):
        return dict(which=self.which.currentData(), cell=float(self.cell.value()), priority=self.prio.currentData(),
                    levelled=self.lev.isChecked(), fill=int(self.fill.value()), path=self.path.text().strip(),
                    byte=self.byte.isChecked())
