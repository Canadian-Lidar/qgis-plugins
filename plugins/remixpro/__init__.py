"""RemixPro: files for BeamworX, Inertial Explorer and Qinertia from the mapper's recordings.
A side tool of the Canadian Lidar Data Collection Systems plugins; needs DCS Core."""


def classFactory(iface):  # noqa: N802 - QGIS entry point
    from .plugin import RemixProPlugin
    return RemixProPlugin(iface)
