"""DCS Core: the shared base of the Canadian Lidar Data Collection Systems
plugins (PosPro, AlignPro, CloudPro).

  project   the shared .dcsproj project (Processing/<Name>/), stages, staleness
  handover  publish / read the file each stage hands to the next
  files     atomic writes, fingerprints, portable paths
  locks     one writer per stage
  timeutil  GPS / UTC
  clraw     the mapper's .clraw recordings (files, survey lines, events)
  hesai     Lidar point packets -> points;  anpp  INS packets -> navigation states
  sbet      SBET / SMRMSG trajectories;  las  LAS 1.4 and 1.2 writers, reader
  mcap, zstd  the container and its decompression (zstandard bundled in ext/)
  menu      Plugins > Canadian Lidar > Data Systems (menu.TOP, menu.GROUP)
  header    the header bar: logo, name, workflow boxes, project box, window and guide buttons
  window    the standard plugin window: floating by default, docked only on request, remembered
  theme, widgets, stepbar, dialogs, registry   the shared look and UI

The plugins import it as `dcs_core`; it has no window of its own."""
__version__ = "1.4.0"
# API 2 (1.1.0): adds clraw, hesai, anpp, sbet, las, mcap, zstd and widgets.align_flags.
# API 3 (1.2.0): adds window.PluginWindow (the standard plugin window) and theme.FAINT, DISABLED, VIEW_BG, GRID.
# API 4 (1.3.0): adds header.PluginHeader (the same header bar in every plugin).
# API 5 (1.4.0): mapper 2.10 layout - clraw.file_info, is_navigation, split_navigation, group_*(navigation=),
# DcsProject.survey_clraw_files, dialogs.mapper_project, theme.msg_choice.
API = 5


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
