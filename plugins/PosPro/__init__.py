"""PosPro: GNSS/INS post-processing for mobile mapping (QGIS plugin).
Needs the DCS Core plugin (shared project, look and menu)."""
DCS_API = 16


def classFactory(iface):  # noqa: N802 - QGIS API
    from . import corecheck
    core, why, _menu = corecheck.load(DCS_API)
    if core is None:
        return _NeedsCore(iface, why)
    from .plugin import PosProPlugin
    return PosProPlugin(iface)


class _NeedsCore:
    """Shown instead of PosPro when DCS Core is missing or too old."""

    def __init__(self, iface, why):
        self.iface, self.why = iface, why

    def initGui(self):  # noqa: N802
        try:
            from qgis.core import Qgis
            from . import corecheck
            self.iface.messageBar().pushMessage(
                "PosPro", "PosPro can't start: %s. %s" % (self.why, corecheck.advice(self.why)),
                Qgis.MessageLevel.Critical, 0)
        except Exception:       # noqa: BLE001
            pass

    def unload(self):
        pass
