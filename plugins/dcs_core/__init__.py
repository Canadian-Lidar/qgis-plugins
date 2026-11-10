"""DCS Core: the shared base of the Canadian Lidar Data Collection Systems
plugins (PosPro, AlignPro, CloudPro, RemixPro).

  project   the shared .dcsproj project (Processing/<Name>/), stages, staleness
  handover  publish / read the file each stage hands to the next
  files     atomic writes, fingerprints, portable paths
  locks     one writer per stage
  timeutil  GPS / UTC
  clraw     the mapper's .clraw recordings (files, survey lines, events)
  hesai     Lidar point packets -> points;  anpp  INS packets -> navigation states, the raw Certus stream
  sbet      SBET / SMRMSG trajectories;  las  LAS 1.4 and 1.2 writers, reader
  mcap, zstd  the container and its decompression (zstandard bundled in ext/)
  ping      Cerulean / Blue Robotics Ping packets: SonarView .svlog / .svlz recordings
  menu      Plugins > Canadian Lidar > Data Systems (menu.TOP, menu.GROUP)
  header    the header bar: logo, name, workflow boxes, project box, window and guide buttons
  window    the standard plugin window: floating by default, docked only on request, remembered
  epochs    NAD83(CSRS) reference epochs (New Brunswick 1997.0, federal 2010.0) with NRCan's velocity grid
  profiles  system profiles: a mapper system's checked offsets and mounting, saved on this computer
  profiles_ui  the System profiles window and the "profile found for this system" question
  theme, widgets, stepbar, dialogs, registry   the shared look and UI

The plugins import it as `dcs_core`; it has no window of its own."""
__version__ = "1.10.0"
# API 2 (1.1.0): adds clraw, hesai, anpp, sbet, las, mcap, zstd and widgets.align_flags.
# API 3 (1.2.0): adds window.PluginWindow (the standard plugin window) and theme.FAINT, DISABLED, VIEW_BG, GRID.
# API 4 (1.3.0): adds header.PluginHeader (the same header bar in every plugin).
# API 5 (1.4.0): mapper 2.10 layout - clraw.file_info, is_navigation, split_navigation, group_*(navigation=),
# DcsProject.survey_clraw_files, dialogs.mapper_project, theme.msg_choice.
# API 6 (1.5.0): anpp reads the raw Certus stream for RemixPro - iter_packets, build, system_state, state_time,
# raw_sensors, raw_sat_data, gps_ephemeris, device_info and the packet ids. Marine sonar recordings: ping (SonarView
# .svlog/.svlz, Cerulean Ping packets), clraw.SONAR / is_sonar / sonar_topics / TOPIC_* and group_*(sonar=),
# DcsProject.sonar_clraw_files; split_navigation and survey_clraw_files leave sonar recordings out.
# API 7 (1.6.0): stepnav.StepNav (Back / Next under a plugin's step pages, "Continue in <next plugin>"),
# stepnav.advice, stepnav.NEXT_PLUGIN; header.plugin_version and PluginHeader(version=) - the header shows the
# plugin's version after its name, read from the caller's metadata.txt.
# API 8 (1.7.0): profiles (system profiles: folder, list_profiles, load, save, new, update_sections, delete, export,
# import_file, identity, identity_of_files, describe_system, match, differences, fmt, SECTIONS, SECTION_TITLES,
# FIELDS) and profiles_ui (ProfilesDialog, open_dialog, suggest).
# API 9 (1.8.0): handover.publish(..., used=, inputs=) and DcsProject.built_from_now(key, used=, inputs=) - built_from
# names the upstream handovers really used and fingerprints the survey lines AlignPro / CloudPro read ("inputs",
# "survey"), so a changed or added survey line makes them stale; profiles.save_sections; locks.thread_lock,
# Lock(wait=); files.replace, read_json_retry, fingerprint_matches; anpp.nav_states(stats=), NavCollector.rejected;
# timeutil.LEAP. Locks are exclusive (O_EXCL) and DcsProject.modify waits for another writer.
# API 10 (1.9.0): epochs (NAD83(CSRS) reference epochs with NRCan's NAD83v70VG velocity grid, bundled in grids/):
# parse, default_epoch, is_nad83csrs, available, propagate, EpochError, ADOPTED, CRS_EPOCH.
# API 11 (1.10.0): registry.on_publish / off_publish / notify_publish - plugins are told when a stage publishes
#   (CloudPro georeferences in the background once its upstream is current).
API = 11


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
