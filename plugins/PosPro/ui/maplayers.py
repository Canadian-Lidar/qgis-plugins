"""Map layers for a PosPro project: real-time track, base stations with
distance rings, processed trajectory coloured by solution type or accuracy."""
import math

from qgis.core import (QgsCategorizedSymbolRenderer, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsFeature, QgsField, QgsFields, QgsGeometry, QgsGraduatedSymbolRenderer,
                       QgsLayerTreeGroup, QgsLineSymbol, QgsMarkerSymbol, QgsPalLayerSettings, QgsPointXY,
                       QgsProject, QgsRendererCategory, QgsRendererRange, QgsTextFormat,
                       QgsVectorLayer, QgsVectorLayerSimpleLabeling, QgsFillSymbol, QgsWkbTypes)
from qgis.PyQt.QtGui import QColor

from ..core import geo, results
from ..core.project import FRAMES

GROUP_PREFIX = "PosPro: "


def _crs(project):
    fr = FRAMES.get(project.cfg["frame"].get("name"), FRAMES["NAD83(CSRS)"])
    return QgsCoordinateReferenceSystem(fr["crs"])


def group(project, create=True):
    root = QgsProject.instance().layerTreeRoot()
    name = GROUP_PREFIX + project.name
    g = root.findGroup(name)
    if g is None and create:
        g = root.insertGroup(0, name)
    return g


def _replace(project, layer, name):
    g = group(project)
    for child in list(g.children()):
        lyr = getattr(child, "layer", lambda: None)()
        if lyr is not None and lyr.name() == name:
            QgsProject.instance().removeMapLayer(lyr.id())
    layer.setName(name)
    QgsProject.instance().addMapLayer(layer, False)
    g.insertLayer(0, layer)
    return layer


def _field(name, kind):
    # QGIS 4 / Qt6 takes QMetaType; QGIS 3 takes QVariant
    try:
        from qgis.PyQt.QtCore import QMetaType
        t = {"d": QMetaType.Type.Double, "i": QMetaType.Type.Int, "s": QMetaType.Type.QString}[kind]
        return QgsField(name, t)
    except (TypeError, AttributeError):
        from qgis.PyQt.QtCore import QVariant
        t = {"d": QVariant.Double, "i": QVariant.Int, "s": QVariant.String}[kind]
        return QgsField(name, t)


def rt_layer(project, rows):
    """Real-time track from packet 20 (what the Certus computed in the field)."""
    crs = _crs(project)
    lyr = QgsVectorLayer("LineString?crs=%s" % crs.authid(), "Real-time track", "memory")
    pts = [QgsPointXY(r[2], r[1]) for r in rows]
    if len(pts) >= 2:
        f = QgsFeature()
        f.setGeometry(QgsGeometry.fromPolylineXY(pts))
        lyr.dataProvider().addFeatures([f])
    sym = QgsLineSymbol.createSimple({"color": "120,120,120,170", "width": "0.5", "line_style": "dash"})
    lyr.renderer().setSymbol(sym)
    return _replace(project, lyr, "Real-time track")


def ring_points(lat, lon, radius_m, n=72):
    out = []
    R = 6371000.0
    for k in range(n + 1):
        b = 2 * math.pi * k / n
        dlat = radius_m * math.cos(b) / R
        dlon = radius_m * math.sin(b) / (R * math.cos(math.radians(lat)))
        out.append(QgsPointXY(lon + math.degrees(dlon), lat + math.degrees(dlat)))
    return out


def bases_layer(project):
    crs = _crs(project)
    lyr = QgsVectorLayer("Point?crs=%s" % crs.authid(), "Base stations", "memory")
    pr = lyr.dataProvider()
    pr.addAttributes([_field("name", "s"), _field("source", "s"), _field("selected", "i"),
                      _field("distance_km", "d")])
    lyr.updateFields()
    rings = QgsVectorLayer("Polygon?crs=%s" % crs.authid(), "Base distance rings", "memory")
    rp = rings.dataProvider()
    rp.addAttributes([_field("km", "i")])
    rings.updateFields()
    feats, rfeats = [], []
    rover = project.approx_xyz()
    for b in project.cfg["bases"]:
        xyz = b.get("xyz") or b.get("approx_xyz")
        if not xyz:
            continue
        lat, lon, h = geo.ecef2llh(*xyz)
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(lon, lat)))
        dist = geo.distance_m(xyz, rover) / 1000.0 if rover else None
        f.setAttributes([b.get("name", ""), b.get("pos_source", ""), 1 if b.get("id") == project.cfg.get("base_selected") else 0,
                         round(dist, 2) if dist is not None else None])
        feats.append(f)
        if b.get("id") == project.cfg.get("base_selected"):
            for km in (60, 40, 20):
                rf = QgsFeature(rings.fields())
                rf.setGeometry(QgsGeometry.fromPolygonXY([ring_points(lat, lon, km * 1000)]))
                rf.setAttributes([km])
                rfeats.append(rf)
    pr.addFeatures(feats)
    rp.addFeatures(rfeats)
    cats = []
    for val, col, size, label in ((1, "#d62728", "4.2", "Base used"), (0, "#555555", "3.0", "Other base")):
        s = QgsMarkerSymbol.createSimple({"name": "triangle", "color": col, "outline_color": "white", "size": size})
        cats.append(QgsRendererCategory(val, s, label))
    lyr.setRenderer(QgsCategorizedSymbolRenderer("selected", cats))
    lab = QgsPalLayerSettings()
    lab.fieldName = "name"
    lab.enabled = True
    fmt = QgsTextFormat()
    fmt.setSize(9)
    lab.setFormat(fmt)
    lyr.setLabeling(QgsVectorLayerSimpleLabeling(lab))
    lyr.setLabelsEnabled(True)
    rcats = []
    for km, col in ((20, "26,152,80"), (40, "230,180,0"), (60, "215,48,39")):
        s = QgsFillSymbol.createSimple({"color": "0,0,0,0", "outline_color": col, "outline_width": "0.5",
                                        "outline_style": "dash"})
        rcats.append(QgsRendererCategory(km, s, "%d km" % km))
    rings.setRenderer(QgsCategorizedSymbolRenderer("km", rcats))
    _replace(project, rings, "Base distance rings")
    return _replace(project, lyr, "Base stations")


QC_FIELDS = [("time_utc", "s"), ("gps_time", "d"), ("status", "i"), ("solution", "s"), ("nsat", "i"),
             ("h_m", "d"), ("roll", "d"), ("pitch", "d"), ("heading", "d"), ("speed", "d"),
             ("sd_h", "d"), ("sd_v", "d"), ("sd_3d", "d"), ("sep_h", "d"), ("sep_v", "d"), ("gap_s", "d")]


def trajectory_layers(project, qc, colour_by="status"):
    crs = _crs(project)
    pts = QgsVectorLayer("Point?crs=%s" % crs.authid(), "Trajectory", "memory")
    pr = pts.dataProvider()
    pr.addAttributes([_field(n, k) for n, k in QC_FIELDS])
    pts.updateFields()
    feats = []
    for i in range(len(qc["gps_time"])):
        f = QgsFeature(pts.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(qc["lon_deg"][i], qc["lat_deg"][i])))
        st = qc["status"][i]
        f.setAttributes([geo.gps_to_utc(qc["gps_time"][i]).strftime("%Y-%m-%d %H:%M:%S"), qc["gps_time"][i], st,
                         results.STATUS.get(st, ""), qc["nsat"][i], qc["h_m"][i], qc["roll_deg"][i],
                         qc["pitch_deg"][i], qc["heading_deg"][i], qc["speed"][i], qc["sd_h"][i], qc["sd_d"][i],
                         qc["sd_3d"][i], qc["sep_h"][i], abs(qc["sep_d"][i]), qc["gap_s"][i]])
        feats.append(f)
    pr.addFeatures(feats)
    pts.updateExtents()
    style_trajectory(pts, colour_by)
    line = QgsVectorLayer("LineString?crs=%s" % crs.authid(), "Trajectory line", "memory")
    seg = [QgsPointXY(x, y) for x, y in zip(qc["lon_deg"], qc["lat_deg"])]
    if len(seg) >= 2:
        f = QgsFeature()
        f.setGeometry(QgsGeometry.fromPolylineXY(seg))
        line.dataProvider().addFeatures([f])
    line.renderer().setSymbol(QgsLineSymbol.createSimple({"color": "60,60,60,140", "width": "0.35"}))
    _replace(project, line, "Trajectory line")
    return _replace(project, pts, "Trajectory")


def style_trajectory(lyr, colour_by):
    def dot(col):
        return QgsMarkerSymbol.createSimple({"name": "circle", "color": col, "outline_style": "no", "size": "1.6"})
    if colour_by == "status":
        cats = [QgsRendererCategory(s, dot(results.STATUS_COLOURS[s]), results.STATUS[s])
                for s in (6, 5, 4, 3, 2, 1)]
        lyr.setRenderer(QgsCategorizedSymbolRenderer("status", cats))
    else:
        field = "sd_3d" if colour_by == "accuracy" else "sep_h"
        ranges = [QgsRendererRange(lo, hi, dot(col), label) for lo, hi, label, col in results.QUALITY_CLASSES]
        lyr.setRenderer(QgsGraduatedSymbolRenderer(field, ranges))
    lyr.triggerRepaint()


def zoom_to(iface, layer):
    if layer is None or not layer.isValid():
        return
    canvas = iface.mapCanvas()
    ext = layer.extent()
    tr = QgsCoordinateTransform(layer.crs(), canvas.mapSettings().destinationCrs(), QgsProject.instance())
    try:
        ext = tr.transformBoundingBox(ext)
    except Exception:          # noqa: BLE001
        return
    ext.scale(1.15)
    canvas.setExtent(ext)
    canvas.refresh()

