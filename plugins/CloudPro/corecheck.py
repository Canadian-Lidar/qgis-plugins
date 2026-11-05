"""Finding DCS Core at start-up (the same file is in every DCS plugin).

load(api) -> (dcs_core or None, reason, menu)
  dcs_core  the module when it is installed and recent enough, else None
  reason    why not, in words for the message bar ("" when fine)
  menu      dcs_core.menu whenever any DCS Core can be imported, even one that is too old, so the plugin's
            entry still goes under Plugins > Canadian Lidar > Data Systems with the others; None only when
            DCS Core is missing altogether (the entry then goes under Plugins > Canadian Lidar)

QGIS keeps a plugin's code in memory until it restarts. When DCS Core is upgraded in the plugin manager
after a plugin has already loaded, the plugin still sees the old DCS Core; the files on disk are new
enough and only a restart is needed. The reason says so instead of asking for an update."""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
FALLBACK_MENU = "&Canadian Lidar"


def _api_on_disk():
    """The API number of the DCS Core installed next to this plugin, or None."""
    path = os.path.join(os.path.dirname(HERE), "dcs_core", "__init__.py")
    try:
        with open(path, encoding="utf-8") as f:
            m = re.search(r"^API\s*=\s*(\d+)", f.read(), re.M)
        return int(m.group(1)) if m else None
    except (OSError, ValueError):
        return None


def load(api):
    try:
        import dcs_core
    except ImportError:
        return None, "the DCS Core plugin is not installed", None
    try:
        from dcs_core import menu
    except ImportError:
        menu = None
    try:
        dcs_core.require(api)
        return dcs_core, "", menu
    except ImportError as e:
        why = str(e) or "DCS Core is too old"
    disk = _api_on_disk()
    if disk is not None and disk >= api:
        why = "DCS Core was updated while QGIS was running; restart QGIS to finish the update"
    return None, why, menu


def advice(why):
    """What to do, for the message bar."""
    if "restart QGIS" in why:
        return "Close QGIS and open it again."
    return "Install or update DCS Core (Plugins > Manage and Install Plugins), then restart QGIS."


def add_menu(iface, action, menu):
    if menu is not None:
        try:
            menu.add(iface, action)
            return
        except Exception:       # noqa: BLE001 - a very old DCS Core: the flat entry still works
            pass
    iface.addPluginToMenu(FALLBACK_MENU, action)


def remove_menu(iface, action, menu):
    if menu is not None:
        try:
            menu.remove(iface, action)
            return
        except Exception:       # noqa: BLE001
            pass
    iface.removePluginMenu(FALLBACK_MENU, action)
