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
d.kind.setCurrentIndex(d.kind.findData("grid"))
assert not d.thin.isEnabled() and not d.tile.isEnabled() and d.cell.isEnabled()
print("dialog: spacing (with density), thin mode, tiles and grid cell are dropdowns: OK")
print("test_export_options: OK")

# grids written block by block (any size) equal the grid made in one piece, gap filling across block edges included
from osgeo import gdal                                            # noqa: E402
for stat, fill in (("mean", 2), ("max", 0), ("count", 1)):
    G, gx, gy = EX.grid_surface(p, 0.5, stat, None, None, None, fill)
    keep = EX.GRID_BLOCK, EX.GRID_IN_MEMORY
    EX.GRID_BLOCK, EX.GRID_IN_MEMORY = 97, 0
    try:
        path, nv = EX.export_grid(p, os.path.join(out, "g_%s.tif" % stat), 0.5, stat, None, None, None, fill)
    finally:
        EX.GRID_BLOCK, EX.GRID_IN_MEMORY = keep
    ds = gdal.Open(path)
    R = ds.GetRasterBand(1).ReadAsArray().astype(float)
    R[R == -9999.0] = np.nan
    assert R.shape == G.shape and ds.GetGeoTransform()[0] == gx and abs(ds.GetGeoTransform()[3] - gy) < 1e-9
    assert np.isfinite(G).sum() > 1000
    assert np.array_equal(np.isnan(R), np.isnan(G)) and np.nanmax(np.abs(R - G.astype(np.float32))) < 1e-3, stat
    assert nv == int(np.isfinite(G).sum())
    ds = None
print("grids streamed in blocks = grids in one piece (mean + fill, max, count): OK")

# a 5 cm grid over the whole job (failed with "grid too large" before 2.5.0)
path, nv = EX.export_grid(p, os.path.join(out, "ground5.tif"), 0.05, "mean", None, None, None, 2)
ds = gdal.Open(path)
print("5 cm grid of the whole job: %d x %d cells, %s with a value, %.1f MB: OK" % (
    ds.RasterXSize, ds.RasterYSize, "{:,}".format(nv), os.path.getsize(path) / 1e6))
assert nv > 0
ds = None

# classes are a checklist with point counts; all ticked = all classes, none ticked = Export disabled
acc0, _r = p.store.class_counts()
k0 = p.store.keys()[0]
n0 = len(p.store.tile_array(k0))
p.store.write_fields(k0, np.arange(n0 // 2), cls=np.full(n0 // 2, 2, np.uint8))
p.store.sync()
acc, rej = p.store.class_counts()
assert acc[2] == acc0[2] + n0 // 2, (acc[2], acc0[2], n0)          # the per tile cache sees the edit
assert acc.sum() + rej.sum() == p.store.total
d = ExportDialog(None, p.lines, os.path.join(out, "x.las"), (acc, rej), None)
present = [k for k in range(256) if acc[k] or rej[k]]
assert d.classes.list.count() == len(present) and d.classes.selected() is None
d.classes.set_checked({present[0]})
assert d.classes.selected() == {present[0]} and d.b_ok.isEnabled()
d.classes.set_all(False)
assert d.classes.selected() == set() and not d.b_ok.isEnabled()
d = ExportDialog(None, p.lines, os.path.join(out, "x.las"), (acc, rej), [present[0]])
assert d.classes.selected() == ({present[0]} if len(present) > 1 else None)
from CloudPro.ui.dialogs import PublishDialog                     # noqa: E402
d = ExportDialog(None, p.lines, os.path.join(out, "x.las"), (acc, rej), None)
assert d.fmt() == "laz" and d.path.text().endswith(".laz") and d.kind_index() == 0 and d.per_line.isEnabled()
p.data["finalize_classes"] = [present[0]]
d = PublishDialog(None, p, [])
assert d.classes.selected() == ({present[0]} if len(present) > 1 else None)
d.classes.set_all(False)
assert not d.b_ok.isEnabled()
d.dem_on.setChecked(True)
assert d.b_ok.isEnabled()
assert d.las_fmt.currentData() == "laz" and d.las_fmt.isEnabled()
d.las.setChecked(False)
assert not d.las_fmt.isEnabled()
print("classes: checklists with point counts, remembered, Export / Finalize need something to write: OK")


# LAZ: the same points as LAS, byte for byte, read back with the bundled decompressor; tiles and lines too
from CloudPro.core import laz as LZ                               # noqa: E402
import struct                                                     # noqa: E402
assert LZ.available(), LZ.why()


def laz_points(path):
    with open(path, "rb") as f:
        h = f.read(375)
        off, nvlr = struct.unpack_from("<II", h, 96)
        fmt, = struct.unpack_from("<B", h, 104)
        n, = struct.unpack_from("<Q", h, 247)
        f.seek(375)
        rec = None
        for _ in range(nvlr):
            uid, rid, ln = struct.unpack("<2x16sHH32x", f.read(54))
            data = f.read(ln)
            if rid == 22204:
                rec = data
        assert fmt == 6 | 0x80 and rec is not None
        f.seek(off)
        buf = np.zeros(n * 30, np.uint8)
        LZ.module().LasZipDecompressor(f, rec).decompress_many(buf)
        return h, buf


def las_points(path):
    with open(path, "rb") as f:
        h = f.read(375)
        off, = struct.unpack_from("<I", h, 96)
        n, = struct.unpack_from("<Q", h, 247)
        f.seek(off)
        return h, np.frombuffer(f.read(n * 30), np.uint8)


EX.export_las(p, os.path.join(out, "z.las"), include_rejected=True)
EX.export_las(p, os.path.join(out, "z.laz"), include_rejected=True)
ha, A = las_points(os.path.join(out, "z.las"))
hb, B = laz_points(os.path.join(out, "z.laz"))
assert np.array_equal(A, B) and ha[131:375] == hb[131:375]        # points, scale/offset, extent, counts
ratio = os.path.getsize(os.path.join(out, "z.las")) / os.path.getsize(os.path.join(out, "z.laz"))
os.makedirs(os.path.join(out, "zt"))
nz = EX.export_las(p, os.path.join(out, "zt", "S.laz"), per_line=True, tile=100)
assert all(f.endswith(".laz") for f in p.last_export_files)
assert sum(len(laz_points(f)[1]) // 30 for f in p.last_export_files) == nz
print("LAZ: same points and header as LAS (%.1f x smaller), tiles per line (%d files): OK" % (
    ratio, len(p.last_export_files)))

# Finalize: <Name>.laz or <Name>.las, never both left in out/
from CloudPro.core import publish as PB                           # noqa: E402
PB.deliver(p, las=True, las_format="laz")
od = p.out_dir
assert os.path.exists(os.path.join(od, p.name + ".laz")) and not os.path.exists(os.path.join(od, p.name + ".las"))
PB.deliver(p, las=True, las_format="las")
assert os.path.exists(os.path.join(od, p.name + ".las")) and not os.path.exists(os.path.join(od, p.name + ".laz"))
print("Finalize writes the chosen format and removes the other: OK")
p.store.close()
