"""Map projection of trajectory samples and the local-tangent linearisation
used to georeference Lidar points.

Only the trajectory (a few hundred thousand samples at most) is projected
exactly; each Lidar point is placed with the local Jacobian of the projection
at its vehicle position (grid convergence + scale factor, interpolated in
time) plus the Earth-curvature term d^2/2R on height.  Over 120 m (the XT-32
range) the linearisation error is far below a millimetre.

Backends, in order: pyproj (vectorised), QGIS QgsCoordinateTransform, and a
built-in GRS80 Transverse Mercator (Krueger series) / Oblique Stereographic
(EPSG 9809) for the Canadian projected systems in ``BUILTIN``.  The source
datum is the target's own geographic datum unless a source CRS is given.
"""
import math

import numpy as np

A_GRS80 = 6378137.0
F_GRS80 = 1 / 298.257222101
A_WGS84 = 6378137.0
F_WGS84 = 1 / 298.257223563


def _tm(lon0, k0=0.9996, fe=500000.0, fn=0.0, lat0=0.0, a=A_GRS80, f=F_GRS80):
    return ("tmerc", dict(lon0=lon0, k0=k0, fe=fe, fn=fn, lat0=lat0, a=a, f=f))


BUILTIN = {
    # NAD83(CSRS) / New Brunswick Stereographic
    2953: ("sterea", dict(lat0=46.5, lon0=-66.5, k0=0.999912, fe=2500000.0, fn=7500000.0, a=A_GRS80, f=F_GRS80)),
    # NAD83 / New Brunswick Stereographic (same parameters)
    2036: ("sterea", dict(lat0=46.5, lon0=-66.5, k0=0.999912, fe=2500000.0, fn=7500000.0, a=A_GRS80, f=F_GRS80)),
    # NAD83(CSRS) / Prince Edward Isl. Stereographic
    2954: ("sterea", dict(lat0=47.25, lon0=-63.0, k0=0.999912, fe=400000.0, fn=800000.0, a=A_GRS80, f=F_GRS80)),
}
# NAD83(CSRS) UTM 11N-21N (EPSG 2955-2962: 11,12,13,17,18,19,20,21) and 7-10, 14-16, 22
for code, zone in zip(range(2955, 2963), (11, 12, 13, 17, 18, 19, 20, 21)):
    BUILTIN[code] = _tm(-183.0 + 6 * zone)
for code, zone in ((3154, 7), (3155, 8), (3156, 9), (3157, 10), (2955, 11), (3158, 14), (3159, 15), (3160, 16),
                   (3761, 22)):
    BUILTIN[code] = _tm(-183.0 + 6 * zone)
for zone in range(1, 24):                     # NAD83 / UTM zone nN
    BUILTIN[26900 + zone] = _tm(-183.0 + 6 * zone)
for zone in range(1, 61):                     # WGS 84 / UTM zone nN
    BUILTIN[32600 + zone] = _tm(-183.0 + 6 * zone, a=A_WGS84, f=F_WGS84)


def epsg_code(crs):
    s = str(crs).strip().upper()
    if s.startswith("EPSG:"):
        try:
            return int(s[5:])
        except ValueError:
            return None
    try:
        return int(s)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# built-in projections
# ---------------------------------------------------------------------------
def _tm_forward(lat, lon, p):
    a, f = p["a"], p["f"]
    n = f / (2 - f)
    A = a / (1 + n) * (1 + n ** 2 / 4 + n ** 4 / 64 + n ** 6 / 256)
    al = [None,
          n / 2 - 2 * n ** 2 / 3 + 5 * n ** 3 / 16 + 41 * n ** 4 / 180 - 127 * n ** 5 / 288 + 7891 * n ** 6 / 37800,
          13 * n ** 2 / 48 - 3 * n ** 3 / 5 + 557 * n ** 4 / 1440 + 281 * n ** 5 / 630 - 1983433 * n ** 6 / 1935360,
          61 * n ** 3 / 240 - 103 * n ** 4 / 140 + 15061 * n ** 5 / 26880 + 167603 * n ** 6 / 181440,
          49561 * n ** 4 / 161280 - 179 * n ** 5 / 168 + 6601661 * n ** 6 / 7257600,
          34729 * n ** 5 / 80640 - 3418889 * n ** 6 / 1995840,
          212378941 * n ** 6 / 319334400]
    e = math.sqrt(f * (2 - f))
    lon0 = math.radians(p["lon0"])
    t = np.sinh(np.arctanh(np.sin(lat)) - e * np.arctanh(e * np.sin(lat)))
    dl = lon - lon0
    xi = np.arctan2(t, np.cos(dl))
    eta = np.arctanh(np.sin(dl) / np.sqrt(1 + t * t))
    x = eta.copy()
    y = xi.copy()
    for j in range(1, 7):
        x = x + al[j] * np.cos(2 * j * xi) * np.sinh(2 * j * eta)
        y = y + al[j] * np.sin(2 * j * xi) * np.cosh(2 * j * eta)
    E = p["fe"] + p["k0"] * A * x
    N = p["fn"] + p["k0"] * A * y
    if p.get("lat0"):
        N = N - p["k0"] * A * float(_tm_forward(np.array([math.radians(p["lat0"])]), np.array([lon0]),
                                                  dict(p, lat0=0.0, fn=0.0, fe=0.0, k0=1.0))[1][0]) / A
    return E, N


def _sterea_forward(lat, lon, p):
    a, f = p["a"], p["f"]
    e2 = f * (2 - f)
    e = math.sqrt(e2)
    phi0 = math.radians(p["lat0"])
    lam0 = math.radians(p["lon0"])
    s0 = math.sin(phi0)
    rho0 = a * (1 - e2) / (1 - e2 * s0 * s0) ** 1.5
    nu0 = a / math.sqrt(1 - e2 * s0 * s0)
    R = math.sqrt(rho0 * nu0)
    n = math.sqrt(1 + e2 * math.cos(phi0) ** 4 / (1 - e2))
    S1 = (1 + s0) / (1 - s0)
    S2 = (1 - e * s0) / (1 + e * s0)
    w1 = (S1 * S2 ** e) ** n
    sinchi0 = (w1 - 1) / (w1 + 1)
    c = (n + s0) * (1 - sinchi0) / ((n - s0) * (1 + sinchi0))
    w2 = c * w1
    chi0 = math.asin((w2 - 1) / (w2 + 1))
    L = n * (lon - lam0) + lam0
    sp = np.sin(lat)
    Sa = (1 + sp) / (1 - sp)
    Sb = (1 - e * sp) / (1 + e * sp)
    w = c * (Sa * Sb ** e) ** n
    chi = np.arcsin((w - 1) / (w + 1))
    B = 1 + np.sin(chi) * math.sin(chi0) + np.cos(chi) * math.cos(chi0) * np.cos(L - lam0)
    E = p["fe"] + 2 * R * p["k0"] * np.cos(chi) * np.sin(L - lam0) / B
    N = p["fn"] + 2 * R * p["k0"] * (np.sin(chi) * math.cos(chi0) - np.cos(chi) * math.sin(chi0) * np.cos(L - lam0)) / B
    return E, N


class Projector:
    """forward(lat, lon) in radians -> (E, N) metres in the target CRS."""

    def __init__(self, target, source=None, prefer=None):
        self.target = str(target)
        self.source = source
        self.backend = None
        self._fn = None
        order = prefer or ("pyproj", "qgis", "builtin")
        errs = []
        for b in order:
            try:
                getattr(self, "_init_" + b)()
                self.backend = b
                break
            except Exception as ex:          # pragma: no cover - depends on installation
                errs.append("%s: %s" % (b, ex))
        if self.backend is None:
            raise ValueError("cannot project to %s (%s)" % (self.target, "; ".join(errs)))

    def _init_pyproj(self):
        import pyproj
        tgt = pyproj.CRS.from_user_input(self.target)
        src = pyproj.CRS.from_user_input(self.source) if self.source else tgt.geodetic_crs
        tr = pyproj.Transformer.from_crs(src, tgt, always_xy=True)
        self._fn = lambda lat, lon: tr.transform(np.degrees(lon), np.degrees(lat))
        self.units_ok = tgt.axis_info[0].unit_name in ("metre", "meter")
        if not self.units_ok:
            raise ValueError("target CRS is not in metres")

    def _init_qgis(self):
        from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY, QgsProject
        tgt = QgsCoordinateReferenceSystem(self.target)
        if not tgt.isValid():
            raise ValueError("invalid CRS")
        if tgt.isGeographic():
            raise ValueError("target CRS is geographic, CloudPro needs a projected CRS in metres")
        src = QgsCoordinateReferenceSystem(self.source) if self.source else tgt.toGeographicCrs()
        tr = QgsCoordinateTransform(src, tgt, QgsProject.instance())

        def fn(lat, lon):
            la = np.degrees(np.atleast_1d(lat))
            lo = np.degrees(np.atleast_1d(lon))
            E = np.empty(len(la))
            N = np.empty(len(la))
            for i in range(len(la)):
                q = tr.transform(QgsPointXY(lo[i], la[i]))
                E[i] = q.x()
                N[i] = q.y()
            return E, N
        self._fn = fn

    def _init_builtin(self):
        code = epsg_code(self.target)
        if code not in BUILTIN:
            raise ValueError("EPSG:%s not in the built-in list" % code)
        kind, p = BUILTIN[code]
        f = _tm_forward if kind == "tmerc" else _sterea_forward
        self._fn = lambda lat, lon: f(np.asarray(lat, float), np.asarray(lon, float), p)

    def forward(self, lat, lon):
        E, N = self._fn(np.asarray(lat, float), np.asarray(lon, float))
        return np.asarray(E, float), np.asarray(N, float)


def radii(lat, a=A_GRS80, f=F_GRS80):
    e2 = f * (2 - f)
    s = np.sin(lat)
    w = np.sqrt(1 - e2 * s * s)
    M = a * (1 - e2) / w ** 3
    Nr = a / w
    return M, Nr


def local_jacobian(proj, lat, lon, h):
    """Jacobian of (E, N) w.r.t. local (east, north) metres at each sample.
    Returns J with shape (n, 2, 2): [[dE/de, dE/dn], [dN/de, dN/dn]]."""
    M, Nr = radii(lat)
    d = 10.0
    dlat = d / (M + h)
    dlon = d / ((Nr + h) * np.cos(lat))
    E0, N0 = proj.forward(lat, lon)
    Ee, Ne = proj.forward(lat, lon + dlon)
    En, Nn = proj.forward(lat + dlat, lon)
    J = np.empty((len(lat), 2, 2))
    J[:, 0, 0] = (Ee - E0) / d
    J[:, 1, 0] = (Ne - N0) / d
    J[:, 0, 1] = (En - E0) / d
    J[:, 1, 1] = (Nn - N0) / d
    return E0, N0, J


# ----------------------------------------------------------------------------- trajectory datum
class DatumError(ValueError):
    """The trajectory's frame cannot be moved to the target's datum here."""


def _dynamic_frame(name):
    n = (name or "").upper().replace(" ", "")
    if "ITRF2020" in n or "IGS20" in n:
        return "EPSG:9989"          # ITRF2020 geographic 3D
    if "ITRF2014" in n or "IGS14" in n:
        return "EPSG:7912"
    return None


def needs_datum_shift(frame_name):
    """True for trajectories in a global, time-dependent frame (PosPro PPP: ITRF2020 at the observation date)."""
    return _dynamic_frame(frame_name) is not None


def trajectory_to_datum(traj, frame_name, epoch, target):
    """Move a trajectory from ITRF2020 / ITRF2014 at its observation epoch (decimal year) to the geodetic datum
    of the target CRS, in place (lat, lon, h); returns (description, datum name). NAD83(CSRS) targets use the NRCan time-dependent transformation
    to NAD83(CSRS)v8 (about 1.5 m horizontally and 1 m vertically in Atlantic Canada in 2026). Raises DatumError when only a "ballpark" (zero) transformation is available - never moves
    the cloud silently by the wrong amount."""
    src = _dynamic_frame(frame_name)
    if src is None:
        return "", ""
    try:
        ep = float(epoch)
    except (TypeError, ValueError):
        raise DatumError("the trajectory is in %s but its epoch is unknown" % frame_name)
    try:
        import pyproj
        from pyproj.transformer import TransformerGroup
    except ImportError:
        raise DatumError("pyproj is not available to move the %s trajectory to the target datum" % frame_name)
    tgt = pyproj.CRS.from_user_input(target)
    geod = tgt.geodetic_crs
    name = (geod.name or "").upper()
    if "WGS84" in name.replace(" ", "") or "ITRF" in name:
        # WGS 84 (G2139 and later) is aligned with ITRF2014/2020 to a few centimetres: nothing to move
        return ("trajectory in %s (epoch %.3f) used as %s (the same to a few centimetres)" % (frame_name, ep, geod.name),
                geod.name)
    if "NAD83(CSRS)" not in name.replace(" ", ""):
        raise DatumError("moving a %s trajectory to %s is not supported; choose a NAD83(CSRS) or WGS 84 CRS, or "
                         "process the trajectory with a base station" % (frame_name, geod.name))
    dst = pyproj.CRS.from_epsg(10413)       # NAD83(CSRS)v8 geographic 3D: the current realisation
    grp = TransformerGroup(pyproj.CRS.from_user_input(src), dst, always_xy=True)
    good = [t for t in grp.transformers if "ballpark" not in t.description.lower()]
    if not good:
        raise DatumError("no transformation from %s to %s is known to PROJ %s (only a ballpark one, which "
                         "would leave the cloud metres off)" % (frame_name, geod.name, pyproj.proj_version_str))
    tr = good[0]
    lon, lat, h = np.degrees(traj.lon), np.degrees(traj.lat), np.asarray(traj.h, float)
    x, y, z, _ = tr.transform(lon, lat, h, np.full(len(lon), ep))
    x, y, z = np.asarray(x, float), np.asarray(y, float), np.asarray(z, float)
    if not (np.all(np.isfinite(x)) and np.all(np.isfinite(y)) and np.all(np.isfinite(z))):
        raise DatumError("the transformation from %s to %s failed for some samples" % (frame_name, geod.name))
    dn = float(np.median(np.radians(y) - traj.lat)) * 6371000.0
    de = float(np.median(np.radians(x) - traj.lon) * np.cos(np.median(traj.lat))) * 6371000.0
    du = float(np.median(z - h))
    traj.lat, traj.lon, traj.h = np.radians(y), np.radians(x), z
    steps = [x.strip() for x in tr.description.split("+")]
    how = next((x for x in steps if " to " in x and "Conversion" not in x), steps[0])
    return ("trajectory moved from %s (epoch %.3f) to %s: %.2f m north, %.2f m east, %.2f m up (%s)" %
            (frame_name, ep, dst.name, dn, de, du, how[:80]), dst.name)
