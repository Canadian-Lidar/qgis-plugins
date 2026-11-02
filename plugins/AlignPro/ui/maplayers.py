"""Map layers for the chart view: tracks, inspection boxes, patches, grids, area of interest."""
import os

import numpy as np
from qgis.core import (QgsCategorizedSymbolRenderer, QgsColorRampShader, QgsCoordinateReferenceSystem,
                       QgsFeature, QgsField, QgsFillSymbol, QgsGeometry, QgsHillshadeRenderer,
                       QgsLayerTreeGroup, QgsLineSymbol, QgsMarkerSymbol, QgsPointXY, QgsProject,
                       QgsRasterLayer, QgsRasterShader, QgsRendererCategory, QgsSingleBandPseudoColorRenderer,
                       QgsStyle, QgsVectorLayer)
from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtGui import QColor

from .common import BOX_COLORS, log

if hasattr(QVariant, "String"):          # Qt5 builds (QGIS 3.x)
    _T_STR, _T_DBL, _T_INT = QVariant.String, QVariant.Double, QVariant.Int
else:                                    # Qt6 builds (QGIS 4.x)
    from qgis.PyQt.QtCore import QMetaType
    _T_STR, _T_DBL, _T_INT = QMetaType.Type.QString, QMetaType.Type.Double, QMetaType.Type.Int

GRID_ATTRS = {"mean": ("Mean height", "Spectral", True), "sdev": ("Standard deviation", "Viridis", False),
              "separation": ("Line separation", "Magma", False), "count": ("Count", "Blues", False),
              "lines": ("Lines per cell", "Greens", False)}


def local_crs(frame):
    crs = QgsCoordinateReferenceSystem()
    if hasattr(QgsCoordinateReferenceSystem, "fromProj"):
        crs = QgsCoordinateReferenceSystem.fromProj(frame.proj4())
    if not crs.isValid():
        crs.createFromProj(frame.proj4())
    return crs


class MapLayers:
    def __init__(self, iface):
        self.iface = iface
        self.ids = {}
        self.group_name = None
        self.grid_seq = 0
        self.grid_attr = "separation"

    # ------------------------------------------------------------ helpers
    def _group(self, project):
        root = QgsProject.instance().layerTreeRoot()
        name = "AlignPro – %s" % project.name
        g = root.findGroup(name)
        if g is None:
            g = root.insertGroup(0, name)
        self.group_name = name
        return g

    def _replace(self, key, layer, project, top=False):
        old = self.ids.get(key)
        if old and QgsProject.instance().mapLayer(old):
            QgsProject.instance().removeMapLayer(old)
        QgsProject.instance().addMapLayer(layer, False)
        g = self._group(project)
        if top:
            g.insertLayer(0, layer)
        else:
            g.addLayer(layer)
        self.ids[key] = layer.id()
        return layer

    def layer(self, key):
        lid = self.ids.get(key)
        return QgsProject.instance().mapLayer(lid) if lid else None

    def remove_all(self):
        for lid in list(self.ids.values()):
            if QgsProject.instance().mapLayer(lid):
                QgsProject.instance().removeMapLayer(lid)
        self.ids = {}
        if self.group_name:
            root = QgsProject.instance().layerTreeRoot()
            g = root.findGroup(self.group_name)
            if g is not None and not g.children():
                root.removeChildNode(g)

    # ------------------------------------------------------------ layers
    def update_all(self, project, zoom=False):
        self.update_tracks(project)
        self.update_boxes(project)
        self.update_patches(project)
        self.update_grid(project)
        self.update_aoi(project)
        if zoom:
            self.zoom(project)

    def zoom(self, project):
        lyr = self.layer("tracks")
        if lyr is None:
            return
        canvas = self.iface.mapCanvas()
        from qgis.core import QgsCoordinateTransform
        tr = QgsCoordinateTransform(lyr.crs(), canvas.mapSettings().destinationCrs(), QgsProject.instance())
        ext = tr.transformBoundingBox(lyr.extent())
        ext.scale(1.3)
        canvas.setExtent(ext)
        canvas.refresh()

    def update_tracks(self, project):
        crs = local_crs(project.frame)
        vl = QgsVectorLayer("LineString?crs=%s" % crs.toWkt(), "Tracks", "memory")
        if not vl.crs().isValid():
            vl.setCrs(crs)
        pr = vl.dataProvider()
        pr.addAttributes([QgsField("line", _T_STR), QgsField("use", _T_INT), QgsField("cog", _T_DBL),
                          QgsField("sog", _T_DBL), QgsField("points", _T_INT)])
        vl.updateFields()
        pts = QgsVectorLayer("Point?crs=%s" % crs.toWkt(), "Line starts", "memory")
        if not pts.crs().isValid():
            pts.setCrs(crs)
        pp = pts.dataProvider()
        pp.addAttributes([QgsField("line", _T_STR)])
        pts.updateFields()
        cats, cats2 = [], []
        feats, feats2 = [], []
        for ln in project.lines:
            tr = ln.stats.get("track")
            if tr is None or len(tr) < 2:
                continue
            step = max(1, len(tr) // 400)
            f = QgsFeature(vl.fields())
            f.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(float(e), float(n)) for e, n in tr[::step, :2]]))
            f.setAttributes([ln.name, int(ln.use), float(ln.stats.get("cog", 0)), float(ln.stats.get("sog", 0)), ln.n])
            feats.append(f)
            g = QgsFeature(pts.fields())
            g.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(float(tr[0, 0]), float(tr[0, 1]))))
            g.setAttributes([ln.name])
            feats2.append(g)
            sym = QgsLineSymbol.createSimple({"color": ln.color, "width": "0.7" if ln.use else "0.3",
                                              "line_style": "solid" if ln.use else "dash"})
            cats.append(QgsRendererCategory(ln.name, sym, ln.name + ("" if ln.use else " (not used)")))
            ms = QgsMarkerSymbol.createSimple({"name": "circle", "color": "0,0,0,0", "outline_color": ln.color,
                                               "outline_width": "0.6", "size": "3"})
            cats2.append(QgsRendererCategory(ln.name, ms, ln.name))
        pr.addFeatures(feats)
        pp.addFeatures(feats2)
        vl.setRenderer(QgsCategorizedSymbolRenderer("line", cats))
        pts.setRenderer(QgsCategorizedSymbolRenderer("line", cats2))
        self._replace("tracks", vl, project, top=True)
        self._replace("starts", pts, project, top=True)

    def update_boxes(self, project):
        crs = local_crs(project.frame)
        vl = QgsVectorLayer("Polygon?crs=%s" % crs.toWkt(), "Calibration boxes", "memory")
        if not vl.crs().isValid():
            vl.setCrs(crs)
        pr = vl.dataProvider()
        pr.addAttributes([QgsField("kind", _T_STR)])
        vl.updateFields()
        feats, cats = [], []
        for kind, b in (project.cfg.get("boxes") or {}).items():
            p0, p1 = np.array(b["p0"]), np.array(b["p1"])
            L = np.linalg.norm(p1 - p0)
            if L < 1e-3:
                continue
            u = (p1 - p0) / L
            v = np.array([-u[1], u[0]]) * b["width"] / 2
            ring = [p0 - v, p1 - v, p1 + v, p0 + v, p0 - v]
            f = QgsFeature(vl.fields())
            f.setGeometry(QgsGeometry.fromPolygonXY([[QgsPointXY(float(x), float(y)) for x, y in ring]]))
            f.setAttributes([kind])
            feats.append(f)
            s = min(1.5, L * 0.1)
            tri = [p1, p1 - u * s + v * 0.9, p1 - u * s - v * 0.9, p1]
            g = QgsFeature(vl.fields())
            g.setGeometry(QgsGeometry.fromPolygonXY([[QgsPointXY(float(x), float(y)) for x, y in tri]]))
            g.setAttributes([kind + "_end"])
            feats.append(g)
            col = BOX_COLORS.get(kind, "#888888")
            cats.append(QgsRendererCategory(kind, QgsFillSymbol.createSimple(
                {"color": "0,0,0,0", "outline_color": col, "outline_width": "0.6"}), kind.capitalize()))
            cats.append(QgsRendererCategory(kind + "_end", QgsFillSymbol.createSimple(
                {"color": col, "outline_style": "no"}), ""))
        pr.addFeatures(feats)
        vl.setRenderer(QgsCategorizedSymbolRenderer("kind", cats))
        self._replace("boxes", vl, project, top=True)

    def update_patches(self, project):
        P = project.P
        crs = local_crs(project.frame)
        vl = QgsVectorLayer("Point?crs=%s" % crs.toWkt(), "Planar patches", "memory")
        if not vl.crs().isValid():
            vl.setCrs(crs)
        pr = vl.dataProvider()
        pr.addAttributes([QgsField("kind", _T_STR), QgsField("lines", _T_INT), QgsField("z", _T_DBL)])
        vl.updateFields()
        names = {0: "flat", 1: "sloped", 2: "wall"}
        feats = []
        if P is not None:
            for c, k, ls in zip(P.center, P.kind, P.lines):
                f = QgsFeature(vl.fields())
                f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(float(c[0]), float(c[1]))))
                f.setAttributes([names[int(k)], len(ls), float(c[2])])
                feats.append(f)
        pr.addFeatures(feats)
        cats = []
        from . import theme
        for nm, col, shape in (("wall", theme.BLUE, "square"), ("sloped", theme.AMBER, "diamond"),
                               ("flat", theme.DIM, "circle")):
            cats.append(QgsRendererCategory(nm, QgsMarkerSymbol.createSimple(
                {"name": shape, "color": col, "outline_style": "no", "size": "1.6"}), nm))
        vl.setRenderer(QgsCategorizedSymbolRenderer("kind", cats))
        self._replace("patches", vl, project)
        node = QgsProject.instance().layerTreeRoot().findLayer(vl.id())
        if node:
            node.setItemVisibilityChecked(False)

    def update_grid(self, project, attr=None):
        from . import gridio
        if attr:
            self.grid_attr = attr
        from ..core import engine
        g = engine.grid_stats(project.lines, float(project.settings.get("grid_cell_m", 0.5)))
        project.grid_summary = engine.grid_summary(g)
        if g is None:
            return
        self.grid_seq += 1
        path = os.path.join(project.work, "grid_%d.tif" % (self.grid_seq % 2))
        try:
            gridio.write_grid(path, g, project.frame.proj4())
        except Exception as e:  # noqa: BLE001
            log("Grid not written: %s" % e)
            return
        band = list(gridio.BANDS).index(self.grid_attr) + 1
        title, ramp, hill = GRID_ATTRS[self.grid_attr]
        rl = QgsRasterLayer(path, "Grid – %s" % title)
        if not rl.isValid():
            return
        arr = g[self.grid_attr]
        fin = arr[np.isfinite(arr)]
        if len(fin):
            lo, hi = float(np.percentile(fin, 2)), float(np.percentile(fin, 98))
            if self.grid_attr in ("separation", "sdev"):
                lo, hi = 0.0, max(hi, 0.01)
            if hi <= lo:
                hi = lo + 1e-3
            _pseudocolor(rl, band, lo, hi, ramp)
        self._replace("grid", rl, project)
        if hill or self.grid_attr == "mean":
            hl = QgsRasterLayer(path, "Grid – hillshade")
            if hl.isValid():
                hl.setRenderer(QgsHillshadeRenderer(hl.dataProvider(), 1, 315, 45))
                hl.renderer().setOpacity(0.45) if hasattr(hl.renderer(), "setOpacity") else None
                self._replace("hill", hl, project)
        elif self.layer("hill"):
            QgsProject.instance().removeMapLayer(self.ids.pop("hill"))

    def update_aoi(self, project):
        poly = project.cfg.get("aoi")
        if not poly:
            if self.layer("aoi"):
                QgsProject.instance().removeMapLayer(self.ids.pop("aoi"))
            return
        vl = QgsVectorLayer("Polygon?crs=EPSG:4326", "Area of interest", "memory")
        f = QgsFeature()
        f.setGeometry(QgsGeometry.fromPolygonXY([[QgsPointXY(x, y) for x, y in poly + [poly[0]]]]))
        vl.dataProvider().addFeatures([f])
        vl.setRenderer(vl.renderer().clone())
        vl.renderer().setSymbol(QgsFillSymbol.createSimple(
            {"color": "242,163,58,30", "outline_color": "#f2a33a", "outline_width": "0.5",
             "outline_style": "solid" if project.settings.get("use_aoi") else "dash"}))
        self._replace("aoi", vl, project, top=True)


def _pseudocolor(rl, band, lo, hi, ramp_name):
    ramp = QgsStyle.defaultStyle().colorRamp(ramp_name)
    fcn = QgsColorRampShader(lo, hi)
    fcn.setColorRampType(QgsColorRampShader.Type.Interpolated)
    items = []
    n = 8
    for i in range(n + 1):
        v = lo + (hi - lo) * i / n
        c = ramp.color(i / n) if ramp else QColor.fromHsvF(0.66 * (1 - i / n), 0.8, 0.9)
        items.append(QgsColorRampShader.ColorRampItem(v, c, "%.3f" % v))
    fcn.setColorRampItemList(items)
    sh = QgsRasterShader()
    sh.setRasterShaderFunction(fcn)
    r = QgsSingleBandPseudoColorRenderer(rl.dataProvider(), band, sh)
    rl.setRenderer(r)
