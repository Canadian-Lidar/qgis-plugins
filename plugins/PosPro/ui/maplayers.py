"""Map layers for a PosPro project: real-time track, base stations with
distance rings, processed trajectory coloured by solution type or accuracy."""
import math

from qgis.core import (QgsCategorizedSymbolRenderer, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsFeature, QgsField, QgsGeometry, QgsGraduatedSymbolRenderer, QgsLineSymbol,
                       QgsMarkerSymbol, QgsPalLayerSettings, QgsPointXY, QgsProject, QgsRendererCategory,
                       QgsRendererRange, QgsTextFormat, QgsVectorLayer, QgsVectorLayerSimpleLabeling,
                       QgsFillSymbol)

from ..core import geo, results
from ..core.project import FRAMES

GROUP_PREFIX = "PosPro: "


def _crs(project):
    fr = FRAMES.get(project.trajectory_frame().get("name"), FRAMES["NAD83(CSRS)"])
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


def _with_arrows(sym, color, interval_mm=18, size_mm=2.4):
    """Arrowheads along a line symbol in the direction it was drawn (the direction of travel)."""
    try:
        from qgis.core import QgsMarkerLineSymbolLayer
        ml = QgsMarkerLineSymbolLayer.create({"interval": str(interval_mm), "interval_unit": "MM", "rotate": "1",
                                              "placements": "Interval", "placement": "interval",
                                              "offset_along_line": str(interval_mm / 2.0)})
        ml.setSubSymbol(QgsMarkerSymbol.createSimple({"name": "filled_arrowhead", "color": color,
                                                       "outline_style": "no", "size": str(size_mm)}))
        sym.appendSymbolLayer(ml)
    except Exception:       # noqa: BLE001 - the line still draws without arrows
        pass
    return sym


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
    lyr.renderer().setSymbol(_with_arrows(sym, "120,120,120,220"))
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


def _utc_strings(gps):
    """'YYYY-MM-DD HH:MM:SS' UTC for every GPS time at once (one leap-second count per job: a job does not
    span a leap second in practice)."""
    if not len(gps):
        return []
    try:
        import numpy as np
        leap = geo.gps_to_utc(gps[0]) - geo.GPS_EPOCH
        off = (gps[0] - leap.total_seconds())          # seconds the first UTC time is behind GPS
        unix0 = (geo.GPS_EPOCH - geo.GPS_EPOCH.replace(year=1970, month=1, day=1)).total_seconds()
        u = np.floor(np.asarray(gps, float) - off + unix0).astype("int64")
        txt = np.datetime_as_string(u.astype("datetime64[s]"), unit="s")
        return [x.replace("T", " ") for x in txt.tolist()]
    except Exception:       # noqa: BLE001
        return [geo.gps_to_utc(t).strftime("%Y-%m-%d %H:%M:%S") for t in gps]


def trajectory_layers(project, qc, colour_by="status"):
    crs = _crs(project)
    pts = QgsVectorLayer("Point?crs=%s" % crs.authid(), "Trajectory", "memory")
    pr = pts.dataProvider()
    pr.addAttributes([_field(n, k) for n, k in QC_FIELDS])
    pts.updateFields()
    feats = []
    utc = _utc_strings(qc["gps_time"])
    fields = pts.fields()
    for i in range(len(qc["gps_time"])):
        f = QgsFeature(fields)
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(qc["lon_deg"][i], qc["lat_deg"][i])))
        st = qc["status"][i]
        f.setAttributes([utc[i], qc["gps_time"][i], st,
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
    line.renderer().setSymbol(_with_arrows(QgsLineSymbol.createSimple({"color": "60,60,60,140", "width": "0.35"}),
                                           "40,40,40,230"))
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



REVIEW_COLOURS = {"open": "232,72,85", "exclude": "58,180,242", "poor": "245,166,35", "interpolate": "155,123,255",
                  "accept": "0,229,170"}


def review_layer(project, qc, items):
    """Trajectory Review windows on the map: one line per anomaly or edit,
    coloured by what was decided (red open, blue bridged with the IMU, amber poor,
    violet interpolated, mint accepted)."""
    import bisect
    from qgis.core import QgsCategorizedSymbolRenderer, QgsRendererCategory
    crs = _crs(project)
    lyr = QgsVectorLayer("LineString?crs=%s" % crs.authid(), "Review windows", "memory")
    pr = lyr.dataProvider()
    pr.addAttributes([_field("id", "s"), _field("what", "s"), _field("state", "s"), _field("start_utc", "s"),
                      _field("duration_s", "d")])
    lyr.updateFields()
    t = qc["gps_time"] if qc else []
    feats = []
    for it in items:
        i0 = bisect.bisect_left(t, it["t0"])
        i1 = bisect.bisect_right(t, it["t1"])
        pts = [QgsPointXY(qc["lon_deg"][i], qc["lat_deg"][i]) for i in range(max(0, i0 - 1), min(len(t), i1 + 1))]
        if len(pts) < 2:
            continue
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPolylineXY(pts))
        f.setAttributes([it["id"], it["what"], it["state"], geo.gps_to_utc(it["t0"]).strftime("%H:%M:%S"),
                         round(it["t1"] - it["t0"], 1)])
        feats.append(f)
    pr.addFeatures(feats)
    cats = []
    for k, rgb in REVIEW_COLOURS.items():
        s = QgsLineSymbol.createSimple({"color": rgb + ",230", "width": "1.6", "capstyle": "round"})
        cats.append(QgsRendererCategory(k, s, k))
    lyr.setRenderer(QgsCategorizedSymbolRenderer("state", cats))
    return _replace(project, lyr, "Review windows")
