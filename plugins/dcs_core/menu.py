"""The shared Plugins > CLDS menu (1.18.0, Sean 17 Nov 2026): RemixPro, PosPro, AlignPro, CloudPro, StripPro.

Every DCS plugin calls add(iface, action) / remove(iface, action). The entries go straight under Plugins > CLDS
in the order of ORDER (run order), whatever order QGIS loads the plugins in. The `group` argument of earlier
versions is accepted and ignored. The menus of DCS Core 1.17.0 and earlier (Canadian Lidar > Data Systems) are
removed once empty, so a plugin still loaded from before the update does not leave them behind.

BRAND is the line the header bar shows above the plugin's name (unchanged).
"""
TOP = "CLDS"
GROUP = None                    # no submenu any more (kept for plugins that read it)
BRAND = "Data Systems"
ORDER = ("RemixPro", "PosPro", "AlignPro", "CloudPro", "StripPro")
OBJECT_NAME = "clm_menu_clds"
# the menus of 1.17.0 and earlier: (title, object names)
_OLD_TOP = ("Canadian Lidar", ("clm_menu_canadian_lidar", "CanadianLidar_CanadianLidar"))
_OLD_GROUP = ("Data Systems", ("clm_menu_data_collection_systems", "CanadianLidar_DataCollectionSystems"))


def _plain(text):
    return (text or "").replace("&", "").strip().lower()


def find(parent, title, names=()):
    """The submenu of parent for title, by object name first, then by title."""
    menus = [a.menu() for a in parent.actions() if a.menu() is not None]
    for m in menus:
        if m.objectName() in names:
            return m
    for m in menus:
        if _plain(m.title()) == _plain(title):
            return m
    return None


def _top(iface, create=True):
    m = find(iface.pluginMenu(), TOP, (OBJECT_NAME,))
    if m is None and create:
        from qgis.PyQt.QtWidgets import QMenu
        m = QMenu(TOP, iface.pluginMenu())
        iface.pluginMenu().addMenu(m)
    if m is not None:
        m.setObjectName(OBJECT_NAME)
    return m


def rank(action):
    """Position of an entry in ORDER (by its text); unknown entries go last."""
    t = _plain(action.text())
    for i, name in enumerate(ORDER):
        if t.startswith(name.lower()):
            return i
    return len(ORDER)


def _drop_old(iface):
    """Remove the empty Canadian Lidar > Data Systems menus of DCS Core 1.17.0 and earlier."""
    try:
        old = find(iface.pluginMenu(), _OLD_TOP[0], _OLD_TOP[1])
        if old is None:
            return
        grp = find(old, _OLD_GROUP[0], _OLD_GROUP[1])
        if grp is not None and not grp.actions():
            old.removeAction(grp.menuAction())
            grp.deleteLater()
        if not old.actions():
            iface.pluginMenu().removeAction(old.menuAction())
            old.deleteLater()
    except Exception:       # noqa: BLE001 - never stops a plugin from loading
        pass


def add(iface, action, group=None):
    try:
        top = _top(iface)
    except Exception:       # noqa: BLE001 - unusual QGIS builds: a flat menu entry still works
        iface.addPluginToMenu("&" + TOP, action)
        return None
    if action not in top.actions():
        r = rank(action)
        before = next((a for a in top.actions() if rank(a) > r), None)
        if before is not None:
            top.insertAction(before, action)
        else:
            top.addAction(action)
    _drop_old(iface)
    return top


def remove(iface, action, group=None):
    try:
        top = _top(iface, create=False)
    except Exception:       # noqa: BLE001
        iface.removePluginMenu("&" + TOP, action)
        return
    if top is not None:
        top.removeAction(action)
        if not top.actions():
            iface.pluginMenu().removeAction(top.menuAction())
            top.deleteLater()
    _drop_old(iface)
