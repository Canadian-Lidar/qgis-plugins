"""GeoTIFF writing for the sounding grid (GDAL ships with QGIS)."""
import numpy as np

BANDS = ("mean", "sdev", "separation", "count", "lines")


def write_grid(path, g, proj4):
    from osgeo import gdal, osr
    ny, nx = g["ny"], g["nx"]
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(path, nx, ny, len(BANDS), gdal.GDT_Float32, ["COMPRESS=DEFLATE", "TILED=YES"])
    cell = g["cell"]
    ds.SetGeoTransform((g["x0"], cell, 0, g["y0"] + ny * cell, 0, -cell))
    srs = osr.SpatialReference()
    srs.ImportFromProj4(proj4)
    ds.SetProjection(srs.ExportToWkt())
    for i, k in enumerate(BANDS):
        b = ds.GetRasterBand(i + 1)
        b.SetNoDataValue(-9999.0)
        b.SetDescription(k)
        a = np.flipud(np.asarray(g[k], np.float32))
        b.WriteArray(np.where(np.isfinite(a), a, -9999.0).astype(np.float32))
    ds.FlushCache()
    ds = None
