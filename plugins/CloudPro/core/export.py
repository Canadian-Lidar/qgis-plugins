"""Exports: LAS 1.4, XYZ text and gridded surfaces (GeoTIFF through GDAL,
or ESRI ASCII grid when GDAL is not importable)."""
import os

import numpy as np

from .las import LasWriter
from .pointset import PointSet, rect_corners, to_box_frame
from .store import REJECTED, parse_key


def _iter_tiles(project, box=None, lines=None, include_rejected=False, classes=None):
    store = project.store
    if box is not None:
        cor = rect_corners(box["cx"], box["cy"], box["length"], box["width"], box["bearing"])
        keys = store.tiles_in_bbox(cor[:, 0].min(), cor[:, 1].min(), cor[:, 0].max(), cor[:, 1].max())
    else:
        keys = store.keys()
    for n, k in enumerate(keys):
        a = store.tile_array(k)
        if len(a) == 0:
            continue
        m = np.ones(len(a), bool)
        if not include_rejected:
            m &= (a["flag"] & REJECTED) == 0
        if lines:
            m &= np.isin(a["line"], list(lines))
        if classes:
            m &= np.isin(a["cls"], list(classes))
        if box is not None:
            u, v = to_box_frame(a["x"] * 1e-3, a["y"] * 1e-3, box["cx"], box["cy"], box["bearing"])
            m &= (np.abs(u) <= box["length"] / 2) & (np.abs(v) <= box["width"] / 2)
        if m.any():
            yield n, len(keys), np.asarray(a[m])


def crs_wkt(crs):
    try:
        from qgis.core import QgsCoordinateReferenceSystem
        c = QgsCoordinateReferenceSystem(crs)
        if c.isValid():
            try:
                from qgis.core import Qgis
                return c.toWkt(Qgis.CrsWktVariant.Wkt1Gdal)
            except Exception:
                return c.toWkt()
    except Exception:
        pass
    try:
        import pyproj
        return pyproj.CRS.from_user_input(crs).to_wkt("WKT1_GDAL")
    except Exception:
        return ""


def thin(a, spacing, mode, origin):
    """Indices of the records kept when thinning to one point per cell of `spacing` m: mode "3D" (cubes: walls and
    poles keep points) or "2D" (plan cells: e.g. ground models). The kept point is the real point nearest the cell
    centre, so coordinates and attributes are untouched. Cells are anchored at whole multiples of the spacing in
    the project's coordinates, so neighbouring store tiles use the same cells."""
    if not spacing or spacing <= 0 or len(a) == 0:
        return np.arange(len(a))
    s_mm = max(1, int(round(spacing * 1000)))
    o_mm = [int(round(v * 1000)) for v in origin[:3]]
    cols = ("x", "y", "z") if mode == "3D" else ("x", "y")
    keys, d2 = [], np.zeros(len(a))
    for c, om in zip(cols, o_mm):
        v = a[c].astype(np.int64) + om
        k = np.floor_divide(v, s_mm)
        d = (v - k * s_mm - s_mm / 2.0)
        d2 += d * d
        keys.append(k - k.min())
    dims = [int(k.max()) + 1 for k in keys]
    key = keys[0]
    for k, n in zip(keys[1:], dims[1:]):
        key = key * n + k
    order = np.lexsort((d2, key))
    ks = key[order]
    first = np.ones(len(ks), bool)
    first[1:] = ks[1:] != ks[:-1]
    return np.sort(order[first])


def tile_name(base, ext, tx, ty, size, line=None):
    """<base>[_L001]_<E>_<N><ext>: E, N the lower-left corner of the export tile (whole metres)."""
    part = "" if line is None else "_L%03d" % (line + 1)
    return "%s%s_%d_%d%s" % (base, part, int(round(tx * size)), int(round(ty * size)), ext)


def _split(a, origin, tile):
    """[(tx, ty, rows)] of the records by export tile (tile m, anchored at multiples of tile in project
    coordinates), or [(None, None, a)] without tiling."""
    if not tile:
        return [(None, None, a)]
    t_mm = int(round(tile * 1000))
    tx = np.floor_divide(a["x"].astype(np.int64) + int(round(origin[0] * 1000)), t_mm)
    ty = np.floor_divide(a["y"].astype(np.int64) + int(round(origin[1] * 1000)), t_mm)
    code = (tx - tx.min()) * (int(ty.max() - ty.min()) + 1) + (ty - ty.min())
    out = []
    for c in np.unique(code):
        m = code == c
        out.append((int(tx[m][0]), int(ty[m][0]), a[m]))
    return out


def plan(project, box=None, tile=None):
    """Number of export tiles the area would make (for a warning before many files are written)."""
    if not tile:
        return 1
    store = project.store
    o = store.origin
    if box is not None:
        cor = rect_corners(box["cx"], box["cy"], box["length"], box["width"], box["bearing"])
        x0, y0, x1, y1 = cor[:, 0].min(), cor[:, 1].min(), cor[:, 0].max(), cor[:, 1].max()
    else:
        ext = store.extent()
        if not ext:
            return 0
        x0, y0, x1, y1 = ext
    nx = int(np.floor((x1 + o[0]) / tile) - np.floor((x0 + o[0]) / tile)) + 1
    ny = int(np.floor((y1 + o[1]) / tile) - np.floor((y0 + o[1]) / tile)) + 1
    return nx * ny


class Cancelled(Exception):
    """The export was stopped: no file was left behind."""


def _tmp(path):
    d, b = os.path.split(os.path.abspath(path))
    return os.path.join(d, ".tmp_" + b)


def _finish(pairs, cancelled):
    """Rename the temporary files to their names (all or nothing), or delete them when cancelled."""
    if cancelled:
        for a, _b in pairs:
            try:
                os.unlink(a)
            except OSError:
                pass
        raise Cancelled("cancelled: nothing was written")
    for a, b in pairs:
        os.replace(a, b)


def export_las(project, path, box=None, lines=None, include_rejected=False, classes=None, per_line=False,
               progress=None, cancel=None, wkt=None, spacing=None, thin_mode="3D", tile=None):
    """LAS 1.4 written to temporary names and renamed at the end; Cancel deletes them (raises Cancelled).
    spacing: thin to one point per cell (thin_mode "3D" or "2D"); tile: split into files of tile x tile m named
    by their lower-left corner. project.last_export_warnings lists what the reader should know (e.g. no CRS);
    project.last_export_files the files written."""
    o = project.store.origin
    wkt = crs_wkt(project.crs) if wkt is None else wkt
    project.last_export_warnings = [] if wkt else [
        "the LAS file has no coordinate system (neither QGIS nor pyproj could describe %s)" % project.crs]
    writers = {}
    pairs = []
    base, ext = os.path.splitext(path)
    ext = ext or ".las"
    total = 0
    cancelled = False
    try:
        for n, N, a in _iter_tiles(project, box, lines, include_rejected, classes):
            if spacing:
                a = a[thin(a, spacing, thin_mode, o)]
            groups = [(None, a)] if not per_line else [(int(l), a[a["line"] == l]) for l in np.unique(a["line"])]
            for lid, r0 in groups:
                for tx, ty, r in _split(r0, o, tile):
                    key = (lid, tx, ty)
                    if key not in writers:
                        if tile:
                            p = tile_name(base, ext, tx, ty, tile, lid)
                        else:
                            p = path if not per_line else "%s_L%03d%s" % (base, lid + 1, ext)
                        writers[key] = LasWriter(_tmp(p), (np.floor(o[0]), np.floor(o[1]), np.floor(o[2])), wkt=wkt)
                        pairs.append((_tmp(p), p))
                    writers[key].write(r["x"] * 1e-3 + o[0], r["y"] * 1e-3 + o[1], r["z"] * 1e-3 + o[2], r["t"],
                                       r["i"], r["ret"], r["cls"], r["ch"], r["line"], r["flag"])
                    total += len(r)
            if progress:
                progress((n + 1) / N, "%s points%s" % ("{:,}".format(total),
                                                       ", %d files" % len(writers) if len(writers) > 1 else ""))
            if cancel and cancel():
                cancelled = True
                break
        if not writers and not per_line and not tile:   # nothing selected: still an (empty) valid file
            writers[0] = LasWriter(_tmp(path), (np.floor(o[0]), np.floor(o[1]), np.floor(o[2])), wkt=wkt)
            pairs.append((_tmp(path), path))
    finally:
        for w in writers.values():
            w.close()
    _finish(pairs, cancelled)
    project.last_export_files = sorted(b for _a, b in pairs)
    return total


def export_xyz(project, path, box=None, lines=None, include_rejected=False, classes=None, progress=None,
               cancel=None, fields=("E", "N", "Z", "I", "C", "L", "T"), spacing=None, thin_mode="3D", tile=None):
    """XYZ text (CSV); thinning and tiles as export_las."""
    o = project.store.origin
    total = 0
    cancelled = False
    base, ext = os.path.splitext(path)
    ext = ext or ".csv"
    files = {}
    fmt = {"E": "%.3f", "N": "%.3f", "Z": "%.3f", "I": "%d", "C": "%d", "L": "%d", "T": "%.6f", "R": "%.2f", "CH": "%d"}

    def out(tx, ty):
        key = (tx, ty)
        if key not in files:
            p = tile_name(base, ext, tx, ty, tile) if tile else path
            f = open(_tmp(p), "w")
            f.write(",".join(fields) + "\n")
            files[key] = (f, p)
        return files[key][0]
    try:
        if not tile:
            out(None, None)
        for n, N, a in _iter_tiles(project, box, lines, include_rejected, classes):
            if spacing:
                a = a[thin(a, spacing, thin_mode, o)]
            for tx, ty, r in _split(a, o, tile):
                cols = {"E": r["x"] * 1e-3 + o[0], "N": r["y"] * 1e-3 + o[1], "Z": r["z"] * 1e-3 + o[2],
                        "I": r["i"], "C": r["cls"], "L": r["line"] + 1, "T": r["t"], "R": r["rng"] * 0.01,
                        "CH": r["ch"] + 1}
                np.savetxt(out(tx, ty), np.column_stack([cols[k] for k in fields]), fmt=[fmt[k] for k in fields],
                           delimiter=",")
                total += len(r)
            if progress:
                progress((n + 1) / N, "%s points" % "{:,}".format(total))
            if cancel and cancel():
                cancelled = True
                break
    finally:
        for f, _p in files.values():
            f.close()
    _finish([(_tmp(p), p) for _f, p in files.values()], cancelled)
    project.last_export_files = sorted(p for _f, p in files.values())
    return total


def grid_surface(project, cell=0.5, stat="mean", box=None, lines=None, classes=None, fill=0, progress=None):
    """Grid accepted points -> (array (ny, nx) with NaN, x0, y1 world of the top-left corner)."""
    store = project.store
    o = store.origin
    ext = store.extent() if box is None else None
    if box is not None:
        cor = rect_corners(box["cx"], box["cy"], box["length"], box["width"], box["bearing"])
        ext = (cor[:, 0].min(), cor[:, 1].min(), cor[:, 0].max(), cor[:, 1].max())
    x0, y0, x1, y1 = ext
    nx = int(np.ceil((x1 - x0) / cell))
    ny = int(np.ceil((y1 - y0) / cell))
    if nx * ny > 400_000_000:
        raise ValueError("grid too large (%d x %d cells): use a larger cell or an area" % (nx, ny))
    S = np.zeros(nx * ny)
    C = np.zeros(nx * ny)
    MX = np.full(nx * ny, -np.inf)
    MN = np.full(nx * ny, np.inf)
    for n, N, a in _iter_tiles(project, box, lines, False, classes):
        x = a["x"] * 1e-3
        y = a["y"] * 1e-3
        z = a["z"] * 1e-3
        ix = np.floor((x - x0) / cell).astype(np.int64)
        iy = np.floor((y - y0) / cell).astype(np.int64)
        ok = (ix >= 0) & (iy >= 0) & (ix < nx) & (iy < ny)
        c = iy[ok] * nx + ix[ok]
        if stat == "count":
            np.add.at(C, c, 1)
        elif stat == "intensity":
            np.add.at(S, c, a["i"][ok].astype(float))
            np.add.at(C, c, 1)
        else:
            np.add.at(S, c, z[ok])
            np.add.at(C, c, 1)
            np.maximum.at(MX, c, z[ok])
            np.minimum.at(MN, c, z[ok])
        if progress:
            progress((n + 1) / N, None)
    with np.errstate(invalid="ignore", divide="ignore"):
        if stat == "count":
            G = np.where(C > 0, C, np.nan)
        elif stat == "max":
            G = np.where(C > 0, MX, np.nan) + o[2]
        elif stat == "min":
            G = np.where(C > 0, MN, np.nan) + o[2]
        elif stat == "intensity":
            G = S / C
        else:
            G = S / C + o[2]
    G = G.reshape(ny, nx)[::-1]                 # north up
    if fill > 0:
        from scipy import ndimage
        m = np.isnan(G)
        idx = ndimage.distance_transform_edt(m, return_distances=True, return_indices=True)
        dist, ind = idx
        near = m & (dist <= fill)
        G[near] = G[tuple(i[near] for i in ind)]
    return G, x0 + o[0], y0 + ny * cell + o[1]


def write_grid(path, G, x0, ytop, cell, crs):
    """GeoTIFF (or ESRI ASCII grid without GDAL), written under a temporary name and renamed."""
    try:
        from osgeo import gdal
    except ImportError:
        gdal = None
    if gdal is None:
        p = os.path.splitext(path)[0] + ".asc"
        with open(_tmp(p), "w") as f:
            f.write("ncols %d\nnrows %d\nxllcorner %.3f\nyllcorner %.3f\ncellsize %.4f\nNODATA_value -9999\n" %
                    (G.shape[1], G.shape[0], x0, ytop - G.shape[0] * cell, cell))
            np.savetxt(f, np.where(np.isnan(G), -9999.0, G), fmt="%.3f")
        os.replace(_tmp(p), p)
        return p
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(_tmp(path), G.shape[1], G.shape[0], 1, gdal.GDT_Float32, ["COMPRESS=DEFLATE", "TILED=YES"])
    ds.SetGeoTransform((x0, cell, 0, ytop, 0, -cell))
    wkt = crs_wkt(crs)
    if wkt:
        ds.SetProjection(wkt)
    b = ds.GetRasterBand(1)
    b.SetNoDataValue(-9999.0)
    b.WriteArray(np.where(np.isnan(G), -9999.0, G).astype(np.float32))
    ds.FlushCache()
    ds = None
    os.replace(_tmp(path), path)
    return path


def raster_sampler(path, origin):
    """Bilinear sampler of a raster (GDAL) in project local coordinates -> callable(x, y) -> z."""
    from osgeo import gdal
    ds = gdal.Open(path)
    if ds is None:
        raise ValueError("cannot open raster %s" % path)
    gt = ds.GetGeoTransform()
    band = ds.GetRasterBand(1)
    A = band.ReadAsArray().astype(float)
    nd = band.GetNoDataValue()
    if nd is not None:
        A[A == nd] = np.nan

    def sample(x, y):
        E = x + origin[0]
        N = y + origin[1]
        px = (E - gt[0]) / gt[1] - 0.5
        py = (N - gt[3]) / gt[5] - 0.5
        i0 = np.floor(px).astype(int)
        j0 = np.floor(py).astype(int)
        ok = (i0 >= 0) & (j0 >= 0) & (i0 < A.shape[1] - 1) & (j0 < A.shape[0] - 1)
        out = np.full(len(x), np.nan)
        fx = px[ok] - i0[ok]
        fy = py[ok] - j0[ok]
        a, b = i0[ok], j0[ok]
        out[ok] = (A[b, a] * (1 - fx) * (1 - fy) + A[b, a + 1] * fx * (1 - fy) + A[b + 1, a] * (1 - fx) * fy +
                   A[b + 1, a + 1] * fx * fy)
        return out
    return sample


def geoid_sampler(path):
    """Geoid undulation raster in geographic coordinates (e.g. CGG2013a GeoTIFF/GTX) -> callable(lat, lon)."""
    from osgeo import gdal
    ds = gdal.Open(path)
    if ds is None:
        raise ValueError("cannot open geoid grid %s" % path)
    gt = ds.GetGeoTransform()
    A = ds.GetRasterBand(1).ReadAsArray().astype(float)

    def sample(lat, lon):
        lon = np.where(lon < gt[0], lon + 360, lon) if gt[0] > 0 else lon
        px = (lon - gt[0]) / gt[1] - 0.5
        py = (lat - gt[3]) / gt[5] - 0.5
        i0 = np.clip(np.floor(px).astype(int), 0, A.shape[1] - 2)
        j0 = np.clip(np.floor(py).astype(int), 0, A.shape[0] - 2)
        fx = np.clip(px - i0, 0, 1)
        fy = np.clip(py - j0, 0, 1)
        return (A[j0, i0] * (1 - fx) * (1 - fy) + A[j0, i0 + 1] * fx * (1 - fy) + A[j0 + 1, i0] * (1 - fx) * fy +
                A[j0 + 1, i0 + 1] * fx * fy)
    return sample
