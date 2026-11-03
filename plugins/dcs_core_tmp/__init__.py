"""DCS Core: the shared base of the Canadian Lidar Data Collection Systems
plugins (PosPro, AlignPro, CloudPro).

  project   the shared .dcsproj project (Processing/<Name>/), stages, staleness
  handover  publish / read the file each stage hands to the next
  files     atomic writes, fingerprints, portable paths
  locks     one writer per stage
  timeutil  GPS / UTC
  menu      Plugins > Canadian Lidar > Data Collection Systems
  theme, widgets, stepbar, dialogs, registry   the shared look and UI

The plugins import it as `dcs_core`; it has no window of its own."""
__version__ = "1.0.1"
API = 1


def classFactory(iface):  # noqa: N802 - QGIS API
    return _CorePlugin(iface)


class _CorePlugin:
    def __init__(self, iface):
        self.iface = iface

    def initGui(self):  # noqa: N802
        pass

    def unload(self):
        try:
            from . import theme
            theme.remove_guard()
        except Exception:       # noqa: BLE001
            pass


def require(api=1):
    """Raise ImportError when this DCS Core is older than a plugin needs."""
    if API < api:
        raise ImportError("DCS Core %s is too old (API %d, need %d); update it" % (__version__, API, api))
