"""The shared Plugins > Canadian Lidar > Data Collection Systems menu.

Every DCS plugin calls add(iface, action) / remove(iface, action). A menu is
found by either of the object names the earlier helpers used (CloudPro's
dcsmenu.py: clm_menu_*, PosPro/AlignPro's cl_menu.py: CanadianLidar_*) or by
its title, so plugins built with any of them end up in one submenu. The
canonical object names (clm_menu_canadian_lidar,
clm_menu_data_collection_systems) are set on whatever menu is used. The last
plugin to leave removes the empty submenus.
"""
TOP = "Canadian Lidar"
GROUP = "Data Collection Systems"
_NAMES = {
    TOP: ("clm_menu_canadian_lidar", "CanadianLidar_CanadianLidar"),
    GROUP: ("clm_menu_data_collection_systems", "CanadianLidar_DataCollectionSystems"),
}


def _plain(text):
    return (text or "").replace("&", "").strip().lower()


def _canonical(title):
    return _NAMES.get(title, ("clm_menu_" + _plain(title).replace(" ", "_"),))[0]


def find(parent, title):
    """The submenu of parent for title, by object name first, then by title."""
    names = _NAMES.get(title, (_canonical(title),))
    menus = [a.menu() for a in parent.actions() if a.menu() is not None]
    for m in menus:
        if m.objectName() in names:
            return m
    for m in menus:
        if _plain(m.title()) == _plain(title):
            return m
    return None


def _submenu(parent, title, create=True):
    m = find(parent, title)
    if m is None and create:
        from qgis.PyQt.QtWidgets import QMenu
        m = QMenu(title, parent)
        parent.addMenu(m)
    if m is not None:
        m.setObjectName(_canonical(title))
    return m


def add(iface, action, group=GROUP):
    try:
        top = _submenu(iface.pluginMenu(), TOP)
    except Exception:       # noqa: BLE001 - unusual QGIS builds: a flat menu entry still works
        iface.addPluginToMenu("&" + TOP, action)
        return None
    sub = _submenu(top, group) if group else top
    if action not in sub.actions():
        sub.addAction(action)
    return sub


def remove(iface, action, group=GROUP):
    try:
        top = _submenu(iface.pluginMenu(), TOP, create=False)
    except Exception:       # noqa: BLE001
        iface.removePluginMenu("&" + TOP, action)
        return
    if top is None:
        return
    sub = _submenu(top, group, create=False) if group else top
    if sub is not None:
        sub.removeAction(action)
        if group and not sub.actions():
            top.removeAction(sub.menuAction())
            sub.deleteLater()
    if not top.actions():
        iface.pluginMenu().removeAction(top.menuAction())
        top.deleteLater()
