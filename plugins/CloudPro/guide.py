"""The user guide that ships inside the plugin (docs/CloudPro_User_Guide_<version>.pdf).

Opened from the window's "?" button, the project menu ("User guide (this
version)") and QGIS Help > Plugins > "CloudPro user guide"."""
import glob
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(HERE, "docs")
PATTERN = "CloudPro_User_Guide_*.pdf"


def installed_version():
    """version= from metadata.txt (what QGIS installed), else the package's __version__."""
    try:
        with open(os.path.join(HERE, "metadata.txt"), encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("version="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    from . import __version__
    return __version__


def _key(path):
    m = re.search(r"_(\d+(?:\.\d+)*)\.pdf$", os.path.basename(path))
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def guide_path(docs=DOCS):
    """The guide for the installed version; else the newest one in docs/; else None."""
    exact = os.path.join(docs, "CloudPro_User_Guide_%s.pdf" % installed_version())
    if os.path.isfile(exact):
        return exact
    found = sorted(glob.glob(os.path.join(docs, PATTERN)), key=_key)
    return found[-1] if found else None


def open_guide(parent=None):
    """Open the guide in the system PDF viewer; a message when it is missing. Returns the path or None."""
    from .qt import QtCore, QtGui
    p = guide_path()
    if p is None:
        from .ui import theme
        theme.msg_warn(parent, "CloudPro", "The user guide is not in this installation (%s).\nReinstall CloudPro "
                       "%s from its zip." % (DOCS, installed_version()))
        return None
    QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(p))
    return p
