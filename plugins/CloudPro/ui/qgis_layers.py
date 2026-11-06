"""Layers in the QGIS project: drive lines, Inspection Area, exports."""
import os

from ..core.pointset import rect_corners


def _group(name="CloudPro"):
    from qgis.core import QgsProject
    root = QgsProject.instance().layerTreeRoot()
    g = root.findGroup(name)
    return g or root.insertGroup(0, name)


def _field(name, kind):
    """QgsField for QGIS 4 / Qt6 (QMetaType) and QGIS 3 (QVariant), as PosPro and AlignPro do."""
    from qgis.core import QgsField
    try:
        from qgis.PyQt.QtCore import QMetaType
        t = {"i": QMetaType.Type.Int, "s": QMetaType.Type.QString, "l": QMetaType.Type.LongLong}[kind]
        return QgsField(name, t)
    except (TypeError, AttributeError, ImportError):
        from qgis.PyQt.QtCore import QVariant
        t = {"i": QVariant.Int, "s": QVariant.String, "l": QVariant.LongLong}[kind]
        return QgsField(name, t)


def add_project_layers(iface, project, tracks, box):
    from qgis.core import (QgsFeature, QgsGeometry, QgsPointXY, QgsProject, QgsVectorLayer,
                           QgsLineSymbol, QgsFillSymbol, QgsSingleSymbolRenderer)
    o = project.store.origin
    crs = project.crs
    grp = _group()
    lay = QgsVectorLayer("LineString?crs=%s" % crs, "%s - drive lines" % project.name, "memory")
    pr = lay.dataProvider()
    pr.addAttributes([_field("line", "i"), _field("name", "s"), _field("points", "l")])
    lay.updateFields()
    feats = []
    for ln in project.lines:
        tr = tracks.get(ln["id"])
        if tr is None:
            continue
        f = QgsFeature(lay.fields())
        pts = [QgsPointXY(e + o[0], n + o[1]) for e, n in zip(tr["E"][::10], tr["N"][::10])]
        f.setGeometry(QgsGeometry.fromPolylineXY(pts))
        f.setAttributes([ln["id"] + 1, ln.get("name", ""), int(ln.get("points", 0))])
        feats.append(f)
    pr.addFeatures(feats)
    lay.setRenderer(QgsSingleSymbolRenderer(QgsLineSymbol.createSimple({"color": "0,229,170", "width": "0.6"})))
    QgsProject.instance().addMapLayer(lay, False)
    grp.addLayer(lay)
    if box:
        bl = QgsVectorLayer("Polygon?crs=%s" % crs, "%s - inspection area" % project.name, "memory")
        f = QgsFeature()
        cor = rect_corners(box["cx"], box["cy"], box["length"], box["width"], box["bearing"])
        f.setGeometry(QgsGeometry.fromPolygonXY([[QgsPointXY(x + o[0], y + o[1]) for x, y in cor]]))
        bl.dataProvider().addFeatures([f])
        bl.setRenderer(QgsSingleSymbolRenderer(QgsFillSymbol.createSimple(
            {"color": "0,229,170,40", "outline_color": "0,229,170", "outline_width": "0.5"})))
        QgsProject.instance().addMapLayer(bl, False)
        grp.addLayer(bl)
    try:
        iface.mapCanvas().setExtent(lay.extent())
        iface.mapCanvas().refresh()
    except Exception:
        pass


def add_file(iface, path, kind):
    from qgis.core import QgsProject, QgsRasterLayer
    name = os.path.basename(path)
    lay = None
    if kind == "grid":
        lay = QgsRasterLayer(path, name)
    elif kind == "las":
        try:
            from qgis.core import QgsPointCloudLayer
            lay = QgsPointCloudLayer(path, name, "pdal")
        except Exception:
            lay = None
    if lay is not None and lay.isValid():
        QgsProject.instance().addMapLayer(lay, False)
        _group().addLayer(lay)
