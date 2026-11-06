"""NAD83(CSRS) reference epochs: move coordinates between epochs with NRCan's velocity grid.

NAD83(CSRS) is fixed to the North American plate, but points still move slowly (post-glacial rebound, a few
millimetres a year), so a coordinate is only complete with its epoch. Provinces adopt one epoch for their
deliverables (NRCan, "Adopted NAD83(CSRS) epochs"): New Brunswick 1997.0; Nova Scotia, Prince Edward Island,
Newfoundland and Labrador and the federal standard 2010.0. Between 2026 and 1997.0 a point near Fredericton
moves about 8 cm horizontally and 2 cm vertically.

    propagate(lon_deg, lat_deg, h, from_epoch, to_epoch) -> (lon_deg, lat_deg, h)   (numpy arrays)

uses PROJ's deformation operation with the NAD83v70VG velocity grid bundled in dcs_core/grids (Open Government
Licence - Canada). Needs pyproj (shipped with QGIS).
"""
import datetime as _dt
import os
import re

GRID = os.path.join(os.path.dirname(os.path.abspath(__file__)), "grids", "ca_nrc_NAD83v70VG.tif")

# NRCan's table of adopted epochs (provinces and territories); the federal standard is 2010.0
ADOPTED = {"New Brunswick": 1997.0, "Nova Scotia": 2010.0, "Prince Edward Island": 2010.0,
           "Newfoundland and Labrador": 2010.0, "federal": 2010.0}
# projected CRSs that are provincial systems, with their province's adopted epoch
CRS_EPOCH = {2953: 1997.0, 2036: 1997.0,            # NAD83(CSRS) / NAD83 New Brunswick Stereographic
             2954: 2010.0, 2291: 2010.0,            # Prince Edward Island Stereographic
             }


class EpochError(ValueError):
    """Coordinates cannot be moved to the requested epoch here."""


def parse(text):
    """A decimal year from '1997.0', '2026.843', '1997-01-01', 'YY:DDD:SSSSS' (CSRS-PPP); None for '', 'CURR',
    'observation date' or anything unreadable."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).strip()
    if not s or s.upper() in ("CURR", "OBSERVATION DATE", "AS OBSERVED"):
        return None
    m = re.match(r"^(\d{4})(\.\d+)?$", s)
    if m:
        return float(s)
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        d = _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        y0 = _dt.date(d.year, 1, 1)
        return d.year + (d - y0).days / float((_dt.date(d.year + 1, 1, 1) - y0).days)
    m = re.match(r"^(\d{2}):(\d{3}):(\d{5})$", s)
    if m:
        yy = int(m.group(1))
        y = 2000 + yy if yy < 80 else 1900 + yy
        days = 366.0 if (y % 4 == 0 and (y % 100 or y % 400 == 0)) else 365.0
        return y + (int(m.group(2)) - 1 + int(m.group(3)) / 86400.0) / days
    try:
        return float(s)
    except ValueError:
        return None


def default_epoch(crs):
    """The adopted epoch for a deliverable CRS: the province's for its provincial projection, else 2010.0."""
    try:
        code = int(str(crs).upper().replace("EPSG:", "").strip())
    except (TypeError, ValueError):
        return ADOPTED["federal"]
    return CRS_EPOCH.get(code, ADOPTED["federal"])


def is_nad83csrs(frame_name):
    n = (frame_name or "").upper().replace(" ", "")
    return "NAD83(CSRS)" in n or n.startswith("NAD83CSRS")


def available():
    """'' when epochs can be moved here, else the reason."""
    if not os.path.exists(GRID):
        return "the NAD83(CSRS) velocity grid is missing from DCS Core (reinstall it)"
    try:
        import pyproj  # noqa: F401
    except ImportError:
        return "pyproj is not available"
    return ""


_ADDED = False


def _use_grid_folder():
    """Add dcs_core/grids to PROJ's search path once (paths with spaces or quotes stay out of the pipeline)."""
    global _ADDED
    if _ADDED:
        return
    import pyproj
    folder = os.path.dirname(GRID)
    try:
        pyproj.datadir.append_data_dir(folder)
    except Exception:       # noqa: BLE001  (older pyproj)
        os.environ["PROJ_DATA"] = os.pathsep.join(p for p in (os.environ.get("PROJ_DATA"), folder) if p)
    _ADDED = True


def propagate(lon_deg, lat_deg, h, from_epoch, to_epoch):
    """Move NAD83(CSRS) coordinates from one epoch to another (decimal years) with NRCan's velocity grid.
    Returns new (lon_deg, lat_deg, h) arrays. Raises EpochError when it cannot be done here or a point is
    outside the grid (Canada)."""
    import numpy as np
    why = available()
    if why:
        raise EpochError(why)
    dt = float(to_epoch) - float(from_epoch)
    lon = np.atleast_1d(np.asarray(lon_deg, float))
    lat = np.atleast_1d(np.asarray(lat_deg, float))
    hh = np.atleast_1d(np.asarray(h, float))
    if abs(dt) < 1e-6:
        return lon.copy(), lat.copy(), hh.copy()
    from pyproj import Transformer
    _use_grid_folder()
    grid = os.path.basename(GRID)              # found through PROJ's search path (no quoting of user paths)
    pipe = ("+proj=pipeline +step +proj=unitconvert +xy_in=deg +xy_out=rad +step +proj=cart +ellps=GRS80 "
            "+step +proj=deformation +dt=%.6f +grids=%s +ellps=GRS80 +step +inv +proj=cart +ellps=GRS80 "
            "+step +proj=unitconvert +xy_in=rad +xy_out=deg" % (dt, grid))
    try:
        tr = Transformer.from_pipeline(pipe)
        x, y, z = tr.transform(lon, lat, hh)
    except Exception as e:      # noqa: BLE001
        raise EpochError("moving the coordinates from epoch %.3f to %.3f failed: %s" % (from_epoch, to_epoch, e))
    x, y, z = np.asarray(x, float), np.asarray(y, float), np.asarray(z, float)
    if not (np.all(np.isfinite(x)) and np.all(np.isfinite(y)) and np.all(np.isfinite(z))):
        raise EpochError("some points are outside the NAD83(CSRS) velocity grid (Canada)")
    return x, y, z
