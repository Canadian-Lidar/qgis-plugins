"""Base-station finder: a map of the reference stations around the drive from
every network PosPro knows, with the three nearest of each highlighted.
Click a station (or a row) to see its details, choose the rate and download
its RINEX for the days of the drive, with the matching precise orbits,
clocks and antenna file.

Networks without a download service (subscription portals) still show on
the map; their button opens the portal and Import loads the file you save.
"""
import os
import webbrowser

from qgis.core import (QgsCategorizedSymbolRenderer, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsFeature, QgsFields, QgsGeometry, QgsLineSymbol, QgsMarkerSymbol, QgsPalLayerSettings,
                       QgsPointXY, QgsProject, QgsRasterLayer, QgsRectangle, QgsRendererCategory, QgsTextBufferSettings,
                       QgsTextFormat, QgsVectorLayer, QgsVectorLayerSimpleLabeling)
from qgis.gui import QgsMapCanvas, QgsMapToolEmitPoint
import time

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
                                 QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
                                 QMessageBox, QProgressBar, QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout,
                                 QWidget)

from ..core import geo, stations
from . import theme
from .maplayers import _field
from .widgets import KV

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
WEB = QgsCoordinateReferenceSystem("EPSG:3857")


def _symbol(net, big):
    st = stations.NETWORKS[net]
    size = st["size"] * (1.45 if big else 0.9)
    sym = QgsMarkerSymbol.createSimple({"name": st["shape"], "color": st["colour"], "size": "%.1f" % size,
                                        "outline_color": "#26292d" if big else st["colour"],
                                        "outline_width": "0.4" if big else "0.1"})
    if not big:
        sym.setOpacity(0.55)
    return sym


class BaseFinder(QDialog):
    def __init__(self, page, project, days, centre_llh, track=None):
        super().__init__(page)
        self.page, self.project, self.days = page, project, days
        self.centre = centre_llh              # (lat, lon, h) degrees
        self.track = track or []
        self.stations = []
        self.sel = None
        self.result = None                    # (files, station) after a download
        self.saved_to = None
        self.setWindowTitle("PosPro – base stations near the drive")
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMinMaxButtonsHint |
                            Qt.WindowType.WindowCloseButtonHint)
        self.setSizeGripEnabled(True)
        self.resize(1250, 780)
        theme.apply(self)
        self.setObjectName("ppRoot")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        head = QHBoxLayout()
        t = QLabel("Base stations near the drive")
        t.setObjectName("ppTitle")
        head.addWidget(t)
        head.addStretch(1)
        self.status = QLabel("")
        self.status.setObjectName("ppStatus")
        head.addWidget(self.status)
        outer.addLayout(head)

        split = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(split, 1)

        # ---- map ----
        mapw = QWidget()
        ml = QVBoxLayout(mapw)
        ml.setContentsMargins(0, 0, 0, 0)
        self.canvas = QgsMapCanvas()
        self.canvas.setCanvasColor(QColor(theme.VIEW_BG))
        self.canvas.setDestinationCrs(WEB)
        self.canvas.enableAntiAliasing(True)
        ml.addWidget(self.canvas, 1)
        leg = QGridLayout()
        leg.setHorizontalSpacing(14)
        self.filters = {}
        for i, (net, d) in enumerate(stations.NETWORKS.items()):
            cb = QCheckBox(d["label"])
            cb.setChecked(True)
            cb.setStyleSheet("QCheckBox { color: %s; }" % d["colour"])
            cb.toggled.connect(self.redraw)
            self.filters[net] = cb
            leg.addWidget(cb, i // 6, i % 6)
        ml.addLayout(leg)
        split.addWidget(mapw)

        # ---- side panel ----
        side = QWidget()
        sl = QVBoxLayout(side)
        sl.setContentsMargins(8, 0, 0, 0)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["NETWORK", "STATION", "KM", "RATES", "ACCESS"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self.row_selected)
        sl.addWidget(self.table, 2)

        card = QFrame()
        card.setObjectName("ppCard")
        cl = QVBoxLayout(card)
        self.c_title = QLabel("Click a station on the map")
        self.c_title.setObjectName("ppTitle")
        self.c_title.setWordWrap(True)
        cl.addWidget(self.c_title)
        self.kv = KV()
        cl.addWidget(self.kv)
        f = QFormLayout()
        self.rate = QComboBox()
        self.products = QCheckBox("Precise orbits, clocks and antenna file")
        self.products.setChecked(True)
        self.products.setToolTip("IGS final (or rapid) orbits and clocks for the drive's days, and igs20.atx. "
                                 "Used for the base PPP and cached for later projects.")
        f.addRow("Rate", self.rate)
        cl.addLayout(f)
        cl.addWidget(self.products)
        self.note = QLabel("")
        self.note.setObjectName("ppHint")
        self.note.setWordWrap(True)
        cl.addWidget(self.note)
        br = QHBoxLayout()
        self.b_dl = QPushButton("Download")
        self.b_dl.setObjectName("ppPrimary")
        self.b_dl.clicked.connect(self.download)
        self.b_portal = QPushButton("Open portal")
        self.b_portal.clicked.connect(self.open_portal)
        self.b_import = QPushButton("Import file…")
        self.b_import.clicked.connect(self.import_file)
        br.addWidget(self.b_dl)
        br.addWidget(self.b_portal)
        br.addWidget(self.b_import)
        cl.addLayout(br)
        sl.addWidget(card, 3)

        # ---- download progress (shown while a download runs) ----
        self.pframe = QFrame()
        self.pframe.setObjectName("ppCard")
        pl = QVBoxLayout(self.pframe)
        pl.setContentsMargins(10, 8, 10, 8)
        prow = QHBoxLayout()
        self.ptitle = QLabel("")
        self.ptitle.setObjectName("ppSection")
        prow.addWidget(self.ptitle, 1)
        self.b_pcancel = QPushButton("Cancel")
        self.b_pcancel.clicked.connect(self._cancel_download)
        prow.addWidget(self.b_pcancel)
        pl.addLayout(prow)
        self.pbar = QProgressBar()
        self.pbar.setTextVisible(False)
        self.pbar.setFixedHeight(8)
        pl.addWidget(self.pbar)
        self.pfile = QLabel("")
        self.pfile.setObjectName("ppHint")
        self.pfile.setWordWrap(True)
        pl.addWidget(self.pfile)
        self.pframe.setVisible(False)
        sl.addWidget(self.pframe)
        self._task = None
        self._list_task = None
        self._spin = QTimer(self)
        self._spin.setInterval(500)
        self._spin.timeout.connect(self._tick)
        self._t0 = 0.0

        bb = QHBoxLayout()
        self.b_refresh = QPushButton("Refresh station lists")
        self.b_refresh.clicked.connect(lambda: self.load(force=True))
        b_close = QPushButton("Close")
        b_close.clicked.connect(self.reject)
        bb.addWidget(self.b_refresh)
        bb.addStretch(1)
        bb.addWidget(b_close)
        sl.addLayout(bb)
        split.addWidget(side)
        split.setSizes([760, 470])

        theme.mapperize(self)
        theme.polish(self)
        self._layers()
        self.tool_click = QgsMapToolEmitPoint(self.canvas)
        self.tool_click.canvasClicked.connect(self.map_clicked)
        self.canvas.setMapTool(self.tool_click)
        self.show_station(None)
        self.load()

    # ------------------------------------------------------------ layers
    def _layers(self):
        self.base = QgsRasterLayer("type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png&zmax=19&zmin=0",
                                   "OpenStreetMap", "wms")
        if self.base.isValid():
            # darker basemap to match the panel
            try:
                r = self.base.renderer()
                r.setOpacity(0.85)
                self.base.brightnessFilter().setBrightness(-60)
                self.base.brightnessFilter().setContrast(-10)
                hs = self.base.hueSaturationFilter()
                try:
                    from qgis.core import QgsHueSaturationFilter
                    hs.setGrayscaleMode(QgsHueSaturationFilter.GrayscaleMode.GrayscaleLightness)
                except (ImportError, AttributeError, TypeError):
                    hs.setGrayscaleMode(1)
            except Exception:           # noqa: BLE001 - cosmetic only
                pass
        self.lyr_track = QgsVectorLayer("LineString?crs=EPSG:4326", "Drive", "memory")
        if len(self.track) > 1:
            f = QgsFeature()
            f.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(lo, la) for la, lo in self.track]))
            self.lyr_track.dataProvider().addFeatures([f])
        self.lyr_track.renderer().setSymbol(QgsLineSymbol.createSimple({"color": theme.MAPLE, "width": "0.9"}))
        self.lyr_rings = QgsVectorLayer("LineString?crs=EPSG:4326", "Rings", "memory")
        feats = []
        la0, lo0 = self.centre[0], self.centre[1]
        for km in (20, 40, 60):
            pts = []
            for i in range(0, 361, 4):
                la, lo = geo.destination(la0, lo0, i, km * 1000.0)
                pts.append(QgsPointXY(lo, la))
            f = QgsFeature()
            f.setGeometry(QgsGeometry.fromPolylineXY(pts))
            feats.append(f)
        self.lyr_rings.dataProvider().addFeatures(feats)
        self.lyr_rings.renderer().setSymbol(QgsLineSymbol.createSimple({"color": "93,133,124,160", "width": "0.3",
                                                                        "line_style": "dash"}))
        fields = QgsFields()
        for name, kind in (("key", "s"), ("net", "s"), ("sid", "s"), ("near", "i"), ("dist", "d")):
            fields.append(_field(name, kind))
        self.lyr_st = QgsVectorLayer("Point?crs=EPSG:4326", "Stations", "memory")
        self.lyr_st.dataProvider().addAttributes(fields.toList())
        self.lyr_st.updateFields()
        cats = []
        for net in stations.NETWORKS:
            for near in (1, 0):
                cats.append(QgsRendererCategory("%s|%d" % (net, near), _symbol(net, bool(near)),
                                                "%s%s" % (stations.NETWORKS[net]["label"], "" if near else " (farther)")))
        self.lyr_st.setRenderer(QgsCategorizedSymbolRenderer("concat(\"net\", '|', \"near\")", cats))
        lab = QgsPalLayerSettings()
        lab.fieldName = "sid"
        lab.isExpression = False
        fmt = QgsTextFormat()
        fmt.setFont(theme.font("ui", 13, 700))
        fmt.setSize(9)
        fmt.setColor(QColor(theme.BRIGHT))
        buf = QgsTextBufferSettings()
        buf.setEnabled(True)
        buf.setColor(QColor(theme.BG))
        buf.setSize(1.0)
        fmt.setBuffer(buf)
        lab.setFormat(fmt)
        try:
            lab.dist = 1.6
        except AttributeError:
            pass
        self.lyr_st.setLabeling(QgsVectorLayerSimpleLabeling(lab))
        self.lyr_st.setLabelsEnabled(True)
        self.lyr_sel = QgsVectorLayer("Point?crs=EPSG:4326", "Selected", "memory")
        self.lyr_sel.renderer().setSymbol(QgsMarkerSymbol.createSimple(
            {"name": "circle", "color": "0,0,0,0", "outline_color": theme.BRIGHT, "outline_width": "0.6", "size": "9"}))
        layers = [self.lyr_sel, self.lyr_st, self.lyr_track, self.lyr_rings]
        if self.base.isValid():
            layers.append(self.base)
        self.canvas.setLayers(layers)
        self._zoom_km(70)

    def _zoom_km(self, km):
        la0, lo0 = self.centre[0], self.centre[1]
        n = geo.destination(la0, lo0, 0, km * 1000)[0]
        s = geo.destination(la0, lo0, 180, km * 1000)[0]
        e = geo.destination(la0, lo0, 90, km * 1000)[1]
        w = geo.destination(la0, lo0, 270, km * 1000)[1]
        tr = QgsCoordinateTransform(WGS84, WEB, QgsProject.instance())
        self.canvas.setExtent(tr.transformBoundingBox(QgsRectangle(w, s, e, n)))
        self.canvas.refresh()

    # ------------------------------------------------------------ data
    def load(self, force=False):
        self.status.setText("Loading station lists…")
        self.b_refresh.setEnabled(False)

        def work(ctx):
            return stations.nearby(self.centre, self.days, force=force, log=lambda m: ctx.log("info", m),
                                   cancelled=ctx.cancelled)

        def done(ok, res):
            self._list_task = None
            if getattr(self, "_closed", False):
                return
            self.b_refresh.setEnabled(True)
            if not ok:
                self.status.setText("Station lists not loaded: %s" % str(res).splitlines()[0][:120])
                return
            self.stations, problems = res
            self.status.setText("%d stations within %d km%s" % (
                len(self.stations), stations.RADIUS_KM,
                ("  ·  not reached: " + ", ".join(problems)) if problems else ""))
            self.redraw()
        self._list_task = self.page.dock.start_task("Finding base stations", work, done, quiet=True)
        if self._list_task is None:         # another job is running: let the user try again later
            self.b_refresh.setEnabled(True)
            self.status.setText("Another PosPro job is running; press Refresh station lists when it ends.")

    def visible(self):
        return [s for s in self.stations if self.filters[s.network].isChecked()]

    def redraw(self, *_):
        vis = self.visible()
        pr = self.lyr_st.dataProvider()
        pr.truncate()
        feats = []
        for s in vis:
            f = QgsFeature(self.lyr_st.fields())
            f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(s.lon, s.lat)))
            f.setAttributes([s.key, s.network, s.sid, 1 if s.nearest else 0, s.dist_km])
            feats.append(f)
        pr.addFeatures(feats)
        self.lyr_st.updateExtents()
        self.lyr_st.triggerRepaint()
        rows = [s for s in vis if s.nearest]
        rows.sort(key=lambda s: s.dist_km)
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for i, s in enumerate(rows):
            vals = [stations.NETWORKS[s.network]["label"], s.sid, "%.0f" % s.dist_km,
                    " / ".join("%g s" % r for r in s.rates) or "–", stations.ACCESS_LABEL[s.access]]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if c == 0:
                    it.setForeground(QColor(stations.NETWORKS[s.network]["colour"]))
                    it.setData(Qt.ItemDataRole.UserRole, s.key)
                self.table.setItem(i, c, it)
        self.table.blockSignals(False)
        self.canvas.refresh()

    # ------------------------------------------------------------ selection
    def map_clicked(self, pt, button):
        tr = QgsCoordinateTransform(WEB, WGS84, QgsProject.instance())
        best, bd = None, 1e18
        mupp = self.canvas.mapUnitsPerPixel()
        for s in self.visible():
            p = QgsCoordinateTransform(WGS84, WEB, QgsProject.instance()).transform(QgsPointXY(s.lon, s.lat))
            d = ((p.x() - pt.x()) ** 2 + (p.y() - pt.y()) ** 2) ** 0.5 / max(mupp, 1e-9)
            if d < bd:
                best, bd = s, d
        if best is not None and bd < 14:
            self.show_station(best)
            for r in range(self.table.rowCount()):
                if self.table.item(r, 0).data(Qt.ItemDataRole.UserRole) == best.key:
                    self.table.blockSignals(True)
                    self.table.selectRow(r)
                    self.table.blockSignals(False)
        _ = tr

    def row_selected(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        key = self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        for s in self.stations:
            if s.key == key:
                self.show_station(s)
                tr = QgsCoordinateTransform(WGS84, WEB, QgsProject.instance())
                self.canvas.setCenter(tr.transform(QgsPointXY(s.lon, s.lat)))
                self.canvas.refresh()
                break

    def show_station(self, s):
        self.sel = s
        pr = self.lyr_sel.dataProvider()
        pr.truncate()
        if s is not None:
            f = QgsFeature()
            f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(s.lon, s.lat)))
            pr.addFeatures([f])
        self.lyr_sel.triggerRepaint()
        self.kv.clear()
        has = s is not None
        for w in (self.b_dl, self.b_portal, self.b_import, self.rate, self.products):
            w.setEnabled(has)
        if not has:
            self.c_title.setText("CLICK A STATION ON THE MAP")
            self.note.setText("Large symbols are the three nearest stations of each network; rings at 20, 40 "
                              "and 60 km from the drive.")
            return
        d = stations.NETWORKS[s.network]
        self.c_title.setText(("%s  ·  %s" % (s.sid, d["label"])).upper())
        self.kv.set("Name", s.name or "–")
        self.kv.set("Operator", s.operator or d.get("operator", "–"))
        self.kv.set("Distance", "%.1f km" % s.dist_km, "ok" if s.dist_km <= 20 else "warn" if s.dist_km <= 40 else "bad")
        self.kv.set("Position", "%.6f, %.6f, %.1f m" % (s.lat, s.lon, s.h or 0))
        if s.receiver:
            self.kv.set("Receiver", s.receiver)
        if s.antenna:
            self.kv.set("Antenna", s.antenna)
        if s.extra.get("status"):
            self.kv.set("Status", s.extra["status"])
        if s.extra.get("also"):
            self.kv.set("Also", s.extra["also"])
        self.kv.set("Rates", " / ".join("%g s" % r for r in s.rates) or "unknown")
        self.kv.set("Access", stations.ACCESS_LABEL[s.access], "ok" if s.access == "public" else "warn")
        self.rate.clear()
        for r in s.rates or [30]:
            self.rate.addItem("%g s%s" % (r, "  (15-minute files)" if r < 5 else "  (daily file)"), r)
        self.b_dl.setEnabled(s.access in ("public", "login"))
        self.b_portal.setEnabled(bool(s.portal or d.get("portal")))
        notes = []
        if s.access == "portal":
            notes.append("%s has no download service PosPro can call. Open the portal, download the RINEX for "
                         "the drive's days, then Import file." % d["label"])
        if s.access == "login":
            notes.append("Needs your %s account; PosPro asks once and keeps it in the QGIS password store." % d["label"])
        if s.extra.get("note"):
            notes.append(s.extra["note"])
        days = ", ".join(x.isoformat() for x in self.days)
        notes.append("Days: %s (UTC)." % days)
        self.note.setText("  ".join(notes))

    # ------------------------------------------------------------ actions
    def _busy(self, on, title=""):
        self.pframe.setVisible(on)
        for b in (self.b_dl, self.b_import, self.b_refresh):
            b.setEnabled(not on)
        if on:
            self.ptitle.setText(title)
            self.pfile.setText("connecting\u2026")
            self.pbar.setRange(0, 100)
            self.pbar.setValue(0)
            self._t0 = time.time()
            self._spin.start()
        else:
            self._spin.stop()
            self._task = None

    def _tick(self):
        el = int(time.time() - self._t0)
        base = self.ptitle.text().split("  \u00b7  ")[0]
        self.ptitle.setText("%s  \u00b7  %d:%02d" % (base, el // 60, el % 60))

    def _cancel_download(self):
        if self._task is not None:
            self._task.cancel()
            self.pfile.setText("cancelling\u2026")

    def reject(self):
        if self._task is not None:
            if theme.msg_question(self, "PosPro", "A download is running. Cancel it and close?") \
                    != QMessageBox.StandardButton.Yes:
                return
            self._task.cancel()
        if getattr(self, "_list_task", None) is not None:
            try:
                self._list_task.cancel()
            except RuntimeError:
                pass
        self._closed = True
        super().reject()

    def accept(self):
        self._closed = True
        super().accept()

    def open_portal(self):
        s = self.sel
        url = s.portal or stations.NETWORKS[s.network].get("portal")
        if url:
            webbrowser.open(url)

    def import_file(self):
        fs, _ = QFileDialog.getOpenFileNames(self, "RINEX observation files", self.project.folder,
                                             "RINEX (*.??o *.??d *.rnx *.crx *.obs *.gz *.Z);;All files (*)")
        if fs:
            self.result = (fs, self.sel)
            self.accept()

    def download(self):
        s = self.sel
        if not s:
            return
        rate = self.rate.currentData() or 30
        creds = None
        if s.access == "login":
            creds = stations.credentials(s.network, self)
            if not creds:
                return
        want_products = self.products.isChecked()
        span = self.project.rover_span()
        out_dir = self.project.wpath("base", "x")[:-2]
        ndays = max(1, len(self.days))

        def work(ctx):
            state = {"n": 0, "url": None, "step": 0.0, "span": 1.0}

            def hook(url, rec, tot):
                if url != state["url"]:
                    state["url"] = url
                    state["n"] += 1
                name = url.rsplit("/", 1)[-1] or url
                size = ("%.1f of %.1f MB" % (rec / 1e6, tot / 1e6)) if tot > 0 else ("%.1f MB" % (rec / 1e6))
                frac = state["step"] + state["span"] * (rec / tot if tot > 0 else 0.0)
                ctx.progress(min(0.999, frac), "file %d · %s · %s" % (state["n"], name, size))
                return ctx.cancelled()

            def log(m):
                ctx.log("info", m)
            with stations.products.progress_hook(hook):
                files = []
                share = 0.7 if want_products else 1.0
                for i, day in enumerate(self.days):
                    if ctx.cancelled():
                        break
                    state["step"], state["span"] = share * i / ndays, share / ndays
                    ctx.progress(state["step"], "RINEX for %s" % day)
                    files += stations.download(s, [day], rate, span, out_dir, creds, log=log, cancelled=ctx.cancelled)
                if want_products and not ctx.cancelled():
                    state["step"], state["span"] = 0.7, 0.3
                    ctx.progress(0.7, "precise orbits and clocks")
                    stations.download_products(self.days, log=log)
            if ctx.cancelled():             # cancelled: what came in so far is not kept
                for f in files:
                    try:
                        os.remove(f)
                    except OSError:
                        pass
                return []
            return files

        def done(ok, res):
            self._task = None
            if getattr(self, "_closed", False):
                return
            self._busy(False)
            if not ok:
                if str(res).startswith("Cancelled"):
                    self.status.setText("Download cancelled")
                    return
                theme.msg_warn(self, "PosPro", "Download failed:\n\n%s" % res)
                return
            if not res:
                theme.msg_warn(self, "PosPro", "No files were found for %s on these days." % s.sid)
                return
            self.result = (res, s)
            self.saved_to = out_dir
            self.accept()
        t = self.page.dock.start_task("Downloading %s" % s.sid, work, done)
        if t is None:
            return
        self._task = t
        self._busy(True, "Downloading %s (%s s)" % (s.sid, rate))
        t.stage.connect(self.pfile.setText)
        t.progressChanged.connect(lambda v: self.pbar.setValue(int(v)))
        t.message.connect(lambda lvl, m: self.status.setText(m[:110]))
