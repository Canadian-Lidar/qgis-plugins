"""Frames, rotations and geodesy.

Conventions (the mapper's, see clraw-developer-reference.md):
  vehicle frame  X forward, Y starboard, Z down (Certus reference point)
  attitude       roll + starboard down, pitch + bow up, heading + clockwise (deg)
  C_v^n          = Rz(heading) Ry(pitch) Rx(roll)     vehicle -> NED
  Lidar mount    M = Rz(h) Ry(p) Rx(-r) Z0            Hesai sensor -> vehicle (navaq_hesai)
Processing frame: local ENU tangent plane at a project origin (metres).
"""
import math

import numpy as np

A_WGS = 6378137.0
F_WGS = 1 / 298.257223563
E2 = F_WGS * (2 - F_WGS)

from dcs_core import frames as _frames

Z0 = _frames.HESAI_ZERO               # the hesai_navaq adapter's zero (dcs_core.frames)


def rx(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], float)


def ry(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], float)


def rz(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], float)


def lidar_matrix(roll, pitch, heading):
    """Mounting angles (deg, navaq_hesai convention) -> 3x3 sensor-to-vehicle matrix."""
    return _frames.hesai_mount_matrix(roll, pitch, heading)


def navaq_from_lidar_matrix(m, near=None):
    """Inverse of lidar_matrix. Of the two Euler solutions (and the gimbal-lock family at
    pitch = +-90) returns the one closest to `near` (roll, pitch, heading deg), so a
    boresight correction never flips the user's angles into an equivalent but unfamiliar set."""
    a = np.asarray(m, float) @ Z0.T              # = Rz(h) Ry(p) Rx(phi), phi = -roll
    sp = -a[2, 0]
    sp = max(-1.0, min(1.0, sp))
    cands = []
    if abs(sp) < 1 - 1e-9:
        for p in (math.asin(sp), math.pi - math.asin(sp)):
            cp = math.cos(p)
            phi = math.atan2(a[2, 1] / cp, a[2, 2] / cp)
            h = math.atan2(a[1, 0] / cp, a[0, 0] / cp)
            cands.append((-phi, p, h))
    if abs(sp) > 1 - 1e-6:
        # gimbal lock: only h - phi (p=+90) or h + phi (p=-90) is defined; keep the user's heading
        p = math.copysign(math.pi / 2, sp)
        h0 = math.radians(near[2]) if near else 0.0
        k = rz(h0).T @ a                            # = Ry(p) Rx(phi); row 1 = [0, c, -s]
        phi = math.atan2(-k[1, 2], k[1, 1])
        cands.append((-phi, p, h0))
    out = []
    for r, p, h in cands:
        v = [math.degrees(r), math.degrees(p), math.degrees(h)]
        if near is not None:
            v = [x + 360.0 * round((n - x) / 360.0) for x, n in zip(v, near)]
        else:
            v = [((x + 180.0) % 360.0) - 180.0 for x in v]
        err = np.abs(lidar_matrix(*v) - np.asarray(m)).max()
        out.append((err, v))
    out = [o for o in out if o[0] < 1e-6] or out
    if near is not None:
        out.sort(key=lambda o: sum(abs(x - n) for x, n in zip(o[1], near)))
    return tuple(out[0][1])


def small_rotation(dx, dy, dz):
    """Rotation matrix exp([d]x) for a rotation vector (rad)."""
    th = math.sqrt(dx * dx + dy * dy + dz * dz)
    k = np.array([[0, -dz, dy], [dz, 0, -dx], [-dy, dx, 0]], float)
    if th < 1e-12:
        return np.eye(3) + k
    return np.eye(3) + math.sin(th) / th * k + (1 - math.cos(th)) / th ** 2 * (k @ k)


def boresight_from_mounts(m_old, m_new):
    """B with m_new = B m_old (vehicle-frame small rotation), returned as roll/pitch/heading
    of B in degrees (Rz Ry Rx order)."""
    b = np.asarray(m_new) @ np.asarray(m_old).T
    p = math.asin(max(-1, min(1, -b[2, 0])))
    r = math.atan2(b[2, 1], b[2, 2])
    h = math.atan2(b[1, 0], b[0, 0])
    return math.degrees(r), math.degrees(p), math.degrees(h)


# ----------------------------------------------------------------- vectorised
def euler_to_matrix(roll, pitch, heading):
    """Arrays (rad) -> (N,3,3) C_v^n = Rz(h) Ry(p) Rx(r)."""
    cr, sr = np.cos(roll), np.sin(roll)
    cp, sp = np.cos(pitch), np.sin(pitch)
    ch, sh = np.cos(heading), np.sin(heading)
    m = np.empty(np.shape(roll) + (3, 3))
    m[..., 0, 0] = ch * cp
    m[..., 0, 1] = ch * sp * sr - sh * cr
    m[..., 0, 2] = ch * sp * cr + sh * sr
    m[..., 1, 0] = sh * cp
    m[..., 1, 1] = sh * sp * sr + ch * cr
    m[..., 1, 2] = sh * sp * cr - ch * sr
    m[..., 2, 0] = -sp
    m[..., 2, 1] = cp * sr
    m[..., 2, 2] = cp * cr
    return m


def geodetic_to_ecef(lat, lon, h):
    """lat/lon in rad, h ellipsoidal m."""
    sl, cl = np.sin(lat), np.cos(lat)
    n = A_WGS / np.sqrt(1 - E2 * sl * sl)
    return np.stack([(n + h) * cl * np.cos(lon), (n + h) * cl * np.sin(lon), (n * (1 - E2) + h) * sl], -1)


def ecef_to_geodetic(xyz):
    x, y, z = xyz[..., 0], xyz[..., 1], xyz[..., 2]
    lon = np.arctan2(y, x)
    p = np.hypot(x, y)
    lat = np.arctan2(z, p * (1 - E2))
    for _ in range(5):
        sl = np.sin(lat)
        n = A_WGS / np.sqrt(1 - E2 * sl * sl)
        h = p / np.cos(lat) - n
        lat = np.arctan2(z, p * (1 - E2 * n / (n + h)))
    sl = np.sin(lat)
    n = A_WGS / np.sqrt(1 - E2 * sl * sl)
    h = p / np.cos(lat) - n
    return lat, lon, h


def ned_to_ecef_matrix(lat, lon):
    """(N,3,3) C_n^e."""
    sl, cl = np.sin(lat), np.cos(lat)
    so, co = np.sin(lon), np.cos(lon)
    m = np.empty(np.shape(lat) + (3, 3))
    m[..., 0, 0] = -sl * co
    m[..., 0, 1] = -so
    m[..., 0, 2] = -cl * co
    m[..., 1, 0] = -sl * so
    m[..., 1, 1] = co
    m[..., 1, 2] = -cl * so
    m[..., 2, 0] = cl
    m[..., 2, 1] = 0
    m[..., 2, 2] = -sl
    return m


class LocalFrame:
    """ENU tangent plane at an origin (lat, lon deg, h m). Horizontal ENU here is the
    ellipsoidal orthographic projection, so QGIS can display it with +proj=ortho."""

    def __init__(self, lat_deg, lon_deg, h=0.0):
        self.lat, self.lon, self.h = float(lat_deg), float(lon_deg), float(h)
        la, lo = math.radians(self.lat), math.radians(self.lon)
        self.o_ecef = geodetic_to_ecef(np.array(la), np.array(lo), np.array(self.h))
        cne = ned_to_ecef_matrix(np.array(la), np.array(lo))
        ned_to_enu = np.array([[0, 1, 0], [1, 0, 0], [0, 0, -1]], float)
        self.r_e2l = ned_to_enu @ cne.T            # ECEF -> ENU at origin

    def from_geodetic(self, lat, lon, h):
        e = geodetic_to_ecef(lat, lon, h) - self.o_ecef
        return e @ self.r_e2l.T

    def to_geodetic(self, enu):
        e = np.asarray(enu) @ self.r_e2l + self.o_ecef
        return ecef_to_geodetic(e)

    def vehicle_to_local(self, lat, lon, roll, pitch, heading):
        """(N,3,3) C_v^L: vehicle -> local ENU at origin."""
        cvn = euler_to_matrix(roll, pitch, heading)
        cne = ned_to_ecef_matrix(lat, lon)
        return np.einsum("ij,njk,nkl->nil", self.r_e2l, cne, cvn)

    def proj4(self):
        return ("+proj=ortho +lat_0=%.9f +lon_0=%.9f +x_0=0 +y_0=0 +ellps=WGS84 +units=m +no_defs"
                % (self.lat, self.lon))

    def to_dict(self):
        return {"lat": self.lat, "lon": self.lon, "h": self.h}
