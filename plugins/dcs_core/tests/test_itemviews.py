"""Lists, tables and trees stay dark and readable when the platform paints them light.

On Windows, QGIS 4 / Qt 6 resets widget palettes and the Windows 11 style paints item views from its own colours:
RemixPro's recordings table came out white with light text (9 Nov 2026). theme.treat() now fills the viewport and
paints the rows itself; this test forces the white palette back after the theme and measures the rendered rows."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_window import APP, settle  # noqa: E402,F401 - sets up qgis.PyQt outside QGIS

from qgis.PyQt.QtCore import Qt  # noqa: E402
from qgis.PyQt.QtGui import QColor, QPalette  # noqa: E402
from qgis.PyQt.QtWidgets import (QListWidget, QStyledItemDelegate, QTableWidget, QTableWidgetItem,  # noqa: E402
                                 QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)


def _white(w):
    """What QGIS 4 / the Windows style does: a light palette on the view and its viewport."""
    p = QPalette()
    for role in (QPalette.ColorRole.Base, QPalette.ColorRole.Window, QPalette.ColorRole.AlternateBase):
        p.setColor(role, QColor("#ffffff"))
    p.setColor(QPalette.ColorRole.Text, QColor("#e8e8e8"))
    w.setPalette(p)
    w.viewport().setPalette(p)
    w.viewport().setAutoFillBackground(True)


def _lum(img, x, y):
    c = QColor(img.pixel(x, y))
    return 0.2126 * c.red() + 0.7152 * c.green() + 0.0722 * c.blue()


def test_views_dark_after_white_palette():
    from dcs_core import theme
    root = QWidget()
    lay = QVBoxLayout(root)
    tree = QTreeWidget()
    tree.setColumnCount(3)
    tree.setHeaderLabels(["Recording", "Kind", "State"])
    for i in range(4):
        it = QTreeWidgetItem(["2026-10-07-1%d" % i, "Sonar recording", "n/a"])
        it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        it.setCheckState(0, Qt.CheckState.Checked)
        it.setForeground(2, QColor(theme.AMBER))
        tree.addTopLevelItem(it)
    table = QTableWidget(3, 2)
    for r in range(3):
        for c in range(2):
            table.setItem(r, c, QTableWidgetItem("cell %d %d" % (r, c)))
    lst = QListWidget()
    lst.addItems(["one", "two", "three"])
    custom = QListWidget()
    custom.setItemDelegate(type("Mine", (QStyledItemDelegate,), {})(custom))
    for w in (tree, table, lst, custom):
        lay.addWidget(w)
    root.resize(700, 700)
    theme.apply(root)
    root.show()
    settle()
    for w in (tree, table, lst):
        _white(w)                                   # the platform's light palette, after the theme
    tree.setCurrentItem(tree.topLevelItem(0))
    settle()
    for w in (tree, table, lst):
        img = w.viewport().grab().toImage()
        if w is tree:
            r1 = tree.visualItemRect(tree.topLevelItem(2))       # an unselected row
            pts = [(r1.left() + 4, r1.center().y()), (img.width() - 6, img.height() - 6)]
        else:
            pts = [(img.width() - 6, img.height() - 6), (img.width() // 2, img.height() - 6)]
        for x, y in pts:
            assert _lum(img, x, y) < 80, (type(w).__name__, x, y, QColor(img.pixel(x, y)).name())
    # the selected row is a dark teal band, not the platform's bright highlight
    img = tree.viewport().grab().toImage()
    r0 = tree.visualItemRect(tree.topLevelItem(0))
    assert _lum(img, r0.right() - 4, r0.center().y()) < 120
    # rows keep their own text colours (amber state column) and check boxes, custom delegates are kept
    assert isinstance(tree.itemDelegate(), theme._view_delegate_class().__mro__[1]) and \
        type(tree.itemDelegate()).__name__ == "ViewDelegate"
    assert type(custom.itemDelegate()).__name__ == "Mine"
    assert tree.topLevelItem(0).foreground(2).color().name() == QColor(theme.AMBER).name()
    root.close()
    settle()
