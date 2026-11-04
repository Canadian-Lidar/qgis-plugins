"""The AlignPro user guide shipped in docs/ and how it opens.

The guide for the installed version (version= in metadata.txt) is
docs/AlignPro_User_Guide_<version>.pdf. When that one is missing the newest guide in docs/ opens;
with none a message says so. Opened from the ? button, the project box menu and
Help > Plugins > AlignPro user guide."""
import glob
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
PREFIX = "AlignPro_User_Guide_"
MENU_TEXT = "AlignPro user guide"


def plugin_version(root=HERE):
    try:
        with open(os.path.join(root, "metadata.txt"), encoding="utf-8") as f:
            for line in f:
                if line.startswith("version="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def _vkey(path):
    v = os.path.basename(path)[len(PREFIX):-4]
    return tuple(int(n) for n in re.findall(r"\d+", v)), os.path.getmtime(path)


def guide_path(root=HERE):
    """(path, exact): the guide for this version (exact True), else the newest one (exact False),
    else (None, False)."""
    docs = os.path.join(root, "docs")
    v = plugin_version(root)
    if v:
        p = os.path.join(docs, "%s%s.pdf" % (PREFIX, v))
        if os.path.isfile(p):
            return p, True
    found = [p for p in glob.glob(os.path.join(docs, PREFIX + "*.pdf")) if os.path.isfile(p)]
    if found:
        return max(found, key=_vkey), False
    return None, False


def open_guide(iface=None, parent=None, root=HERE):
    """Open the guide in the system PDF viewer. Returns the path opened, or None."""
    from qgis.PyQt.QtCore import QUrl
    from qgis.PyQt.QtGui import QDesktopServices
    path, exact = guide_path(root)
    if path is None:
        _tell(iface, parent, "The AlignPro user guide is not installed (no PDF in %s). Reinstall AlignPro from its "
                             "zip to get it." % os.path.join(root, "docs"), warn=True)
        return None
    if not QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
        _tell(iface, parent, "No PDF viewer opened the user guide. It is here: %s" % path, warn=True)
        return None
    if not exact:
        _tell(iface, parent, "The guide for AlignPro %s is not installed; opened %s instead."
              % (plugin_version(root), os.path.basename(path)))
    return path


def _tell(iface, parent, text, warn=False):
    if iface is not None:
        try:
            from qgis.core import Qgis
            iface.messageBar().pushMessage("AlignPro", text, Qgis.MessageLevel.Warning if warn else
                                           Qgis.MessageLevel.Info, 10)
            return
        except Exception:
            pass
    from qgis.PyQt.QtWidgets import QMessageBox
    (QMessageBox.warning if warn else QMessageBox.information)(parent, "AlignPro user guide", text)
