"""StripPro: corrects the trajectory from the point cloud - fills recording holes from the Lidar and aligns the
strips (lines) with each other. Optional stage of the Canadian Lidar Data Collection Systems plugins, run after
CloudPro when its line separation shows a problem (CloudPro then georeferences with the corrected trajectory); needs
DCS Core."""


def classFactory(iface):  # noqa: N802 - QGIS entry point
    from .plugin import StripProPlugin
    return StripProPlugin(iface)
