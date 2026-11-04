"""CloudPro - stage 03 of the Canadian Lidar Data Collection Systems chain:
georeferences the mapper's .clraw captures with PosPro's trajectory and
AlignPro's calibration, cleans (manual editing and automatic filters with
preview), classifies, exports and publishes cloud.json. Needs DCS Core."""
__version__ = "1.5.2"


def classFactory(iface):  # noqa: N802 - QGIS API
    from .plugin import CloudProPlugin
    return CloudProPlugin(iface)
