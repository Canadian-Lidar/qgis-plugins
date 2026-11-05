"""PosPro: GNSS/INS post-processing for mobile mapping (QGIS plugin).
Needs the DCS Core plugin (shared project, look and menu)."""
DCS_API = 7


def classFactory(iface):  # noqa: N802 - QGIS API
    try:
        import dcs_core
        dcs_core.require(DCS_API)
    except ImportError as e:
        return _NeedsCore(iface, str(e))
    from .plugin import PosProPlugin
    return PosProPlugin(iface)


class _NeedsCore:
    """Shown instead of PosPro when DCS Core is missing or too old."""

    def __init__(self, iface, why):
        self.iface, self.why = iface, why

    def initGui(self):  # noqa: N802
        try:
            from qgis.core import Qgis
            self.iface.messageBar().pushMessage(
                "PosPro", "PosPro needs the DCS Core plugin (install dcs_core-*.zip from Plugins > Manage and "
                "Install Plugins > Install from ZIP, then restart QGIS). %s" % self.why, Qgis.MessageLevel.Critical, 0)
        except Exception:       # noqa: BLE001
            pass

    def unload(self):
        pass
