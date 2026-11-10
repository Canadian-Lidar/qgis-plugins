"""Export options: thinning to a grid spacing (3D or 2D cells, the real point nearest each cell centre) and
splitting into tiles named by their lower-left corner; LAS and XYZ; the dialog's dropdowns.

    QT_QPA_PLATFORM=offscreen python3 CloudPro/tests/test_export_options.py      (from the plugins folder)
"""
import glob
import os
import sys
import tempfile

import numpy as np

PLUGINS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(PLUGINS, "guides"))
sys.path.insert(0, PLUGINS)

from CloudPro.tests import _job                                    # noqa: E402
from CloudPro.core import chain, importer as IM, export as EX     # noqa: E402
from CloudPro.core.las import read_las                            # noqa: E402

dcs, p = _job.fresh_job("expo")
IM.import_chain(p, chain.resolve(dcs), IM.ImportOptions())
out = tempfile.mkdtemp()
o = p.store.origin


def rd(path):
    L = read_las(path)
    return np.column_stack([L["x"], L["y"], L["z"]]) if isinstance(L, dict) else np.column_stack([L.x, L.y, L.z])


full = EX.export_las(p, os.path.join(out, "all.las"))
A = rd(os.path.join(out, "all.las"))
assert len(A) == full

# thinning: one point per cell, each one of the original points, the one nearest its cell centre
for mode, cols in (("3D", 3), ("2D", 2)):
    s = 0.5
    n = EX.export_las(p, os.path.join(out, "thin%s.las" % mode), spacing=s, thin_mode=mode)
    T = rd(os.path.join(out, "thin%s.las" % mode))
    assert 0 < n < full and len(T) == n
    ka = np.floor(np.round(A[:, :cols], 3) / s + 1e-9).astype(np.int64)
    kt = np.floor(np.round(T[:, :cols], 3) / s + 1e-9).astype(np.int64)
    ua = np.unique(ka, axis=0)
    ut, ct = np.unique(kt, axis=0, return_counts=True)
    # store tiles are 50 m, a multiple of 0.5 m, so every cell is in one tile: exactly one point per cell
    assert len(ut) == len(ua) == n and (ct == 1).all(), (mode, len(ut), len(ua), n)
    sa = {tuple(np.round(r, 3)) for r in A}
    assert all(tuple(np.round(r, 3)) in sa for r in T[:2000])
    # nearest the centre: no original point in the same cell is closer
    da = ((np.round(A[:, :cols], 3) - (ka + 0.5) * s) ** 2).sum(1)
    order = np.lexsort((da, *[ka[:, c] for c in range(cols - 1, -1, -1)]))
    best = A[order][np.r_[True, (np.diff(ka[order], axis=0) != 0).any(1)]]
    assert len(best) == n
    bd = ((np.round(best[:, :cols], 3) - (np.floor(np.round(best[:, :cols], 3) / s + 1e-9) + 0.5) * s) ** 2).sum(1)
    td = ((np.round(T[:, :cols], 3) - (kt + 0.5) * s) ** 2).sum(1)
    assert abs(np.sort(bd).sum() - np.sort(td).sum()) < 1e-6
    print("thin %s to %.1f m: %s of %s points, one per cell, nearest each centre: OK" % (mode, s, "{:,}".format(n),
                                                                                       "{:,}".format(full)))

# tiles: files named by their lower-left corner, together exactly the full export
EX.export_las(p, os.path.join(out, "tiles", "Shirley.las"), tile=100) if os.makedirs(os.path.join(out, "tiles")) is None else 0
files = sorted(glob.glob(os.path.join(out, "tiles", "*.las")))
assert len(files) > 1 and files == sorted(p.last_export_files)
tot = 0
for f in files:
    e, nn = [int(v) for v in os.path.basename(f)[:-4].split("_")[1:3]]
    P = rd(f)
    assert (P[:, 0] >= e - 1e-6).all() and (P[:, 0] < e + 100).all() and (P[:, 1] >= nn - 1e-6).all() and \
        (P[:, 1] < nn + 100).all(), f
    assert e % 100 == 0 and nn % 100 == 0
    tot += len(P)
assert tot == full
print("100 m tiles: %d files named by their corner, %s points in total = the full export: OK" % (
    len(files), "{:,}".format(tot)))

# tiles + per line + thinning, and XYZ tiles
os.makedirs(os.path.join(out, "tl"))
n = EX.export_las(p, os.path.join(out, "tl", "S.las"), per_line=True, tile=250, spacing=0.25)
names = [os.path.basename(f) for f in p.last_export_files]
assert all("_L0" in nm for nm in names) and sum(len(rd(f)) for f in p.last_export_files) == n
os.makedirs(os.path.join(out, "xyz"))
n = EX.export_xyz(p, os.path.join(out, "xyz", "S.csv"), tile=100)
assert n == full and sum(sum(1 for _ in open(f)) - 1 for f in p.last_export_files) == full
print("per line + tiles + thinning (%d files), XYZ tiles: OK" % len(names))

# the dialog: dropdowns
import harness                                                    # noqa: E402
app, iface = harness.start(PLUGINS, tempfile.mkdtemp())
from CloudPro.ui.dialogs import ExportDialog, THIN_SPACINGS, TILE_SIZES, GRID_CELLS   # noqa: E402
d = ExportDialog(None, p.lines, os.path.join(out, "x.las"))
assert [d.thin.itemData(i) for i in range(d.thin.count())] == [0.0] + list(THIN_SPACINGS)
assert d.thin.itemText(3) == "0.1 m  (100 pts/m²)", d.thin.itemText(3)
assert [d.tile.itemData(i) for i in range(d.tile.count())] == [0] + list(TILE_SIZES)
assert [d.cell.itemData(i) for i in range(d.cell.count())] == list(GRID_CELLS) and d.cell.currentData() == 0.25
assert not d.thin_mode.isEnabled()
d.thin.setCurrentIndex(3)
assert d.thin_mode.isEnabled()
d.kind.setCurrentIndex(2)
assert not d.thin.isEnabled() and not d.tile.isEnabled() and d.cell.isEnabled()
print("dialog: spacing (with density), thin mode, tiles and grid cell are dropdowns: OK")
p.store.close()
print("test_export_options: OK")
