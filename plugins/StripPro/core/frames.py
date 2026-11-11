"""Frames for StripPro: GRS80 geodetic <-> ECEF <-> one local East-North-Up frame for the whole job, and the
vehicle attitude in that frame.

The trajectory's attitude C (vehicle -> local NED at the vehicle) is Rz(heading) Ry(pitch) Rx(roll), as in the
SBET. StripPro works in one fixed ENU frame (tangent at the job's first record) so that a correction is one
vector and one small rotation for the whole job; the rotation between the vehicle's own NED and that frame
(meridian convergence and the Earth's curvature, about 0.01 deg per km) is carried exactly.
"""
import numpy as np

A = 6378137.0
F = 1.0 / 298.257222101           # GRS80 (NAD83(CSRS)); WGS84 differs by 0.1 mm
E2 = F * (2 - F)


def lla_to_ecef(lat, lon, h):
    """Radians, metres -> ECEF (n, 3)."""
    lat, lon, h = (np.asarray(v, float) for v in (lat, lon, h))
    s, c = np.sin(lat), np.cos(lat)
    N = A / np.sqrt(1 - E2 * s * s)
    return np.stack([(N + h) * c * np.cos(lon), (N + h) * c * np.sin(lon), (N * (1 - E2) + h) * s], -1)


def ecef_to_lla(X):
    """ECEF (n, 3) -> lat, lon (rad), h (m) (Bowring, then two refinements: well under 1 mm)."""
    X = np.asarray(X, float).reshape(-1, 3)
    x, y, z = X[:, 0], X[:, 1], X[:, 2]
    lon = np.arctan2(y, x)
    p = np.hypot(x, y)
    lat = np.arctan2(z, p * (1 - E2))
    for _ in range(4):
        s = np.sin(lat)
        N = A / np.sqrt(1 - E2 * s * s)
        h = p / np.cos(lat) - N
        lat = np.arctan2(z, p * (1 - E2 * N / (N + h)))
    s = np.sin(lat)
    N = A / np.sqrt(1 - E2 * s * s)
    h = p / np.cos(lat) - N
    return lat, lon, h


def r_enu_from_ecef(lat, lon):
    """Rotation(s) ECEF -> local ENU at lat, lon (radians); (3, 3) or (n, 3, 3)."""
    lat, lon = np.asarray(lat, float), np.asarray(lon, float)
    sl, cl, so, co = np.sin(lat), np.cos(lat), np.sin(lon), np.cos(lon)
    z = np.zeros_like(sl)
    R = np.stack([np.stack([-so, co, z], -1),
                  np.stack([-sl * co, -sl * so, cl], -1),
                  np.stack([cl * co, cl * so, sl], -1)], -2)
    return R


NED_FROM_ENU = np.array([[0.0, 1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, -1.0]])      # its own inverse


def rot_zyx(roll, pitch, heading):
    """Rz(heading) Ry(pitch) Rx(roll), vectorised: (n, 3, 3) (vehicle -> NED)."""
    r, p, y = (np.asarray(v, float) for v in (roll, pitch, heading))
    cr, sr, cp, sp, cy, sy = np.cos(r), np.sin(r), np.cos(p), np.sin(p), np.cos(y), np.sin(y)
    return np.stack([np.stack([cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr], -1),
                     np.stack([sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr], -1),
                     np.stack([-sp, cp * sr, cp * cr], -1)], -2)


def zyx_from_rot(R):
    """(n, 3, 3) vehicle -> NED -> roll, pitch, heading (radians)."""
    R = np.asarray(R, float).reshape(-1, 3, 3)
    pitch = -np.arcsin(np.clip(R[:, 2, 0], -1, 1))
    roll = np.arctan2(R[:, 2, 1], R[:, 2, 2])
    heading = np.arctan2(R[:, 1, 0], R[:, 0, 0])
    return roll, pitch, heading


def rodrigues(theta):
    """Rotation vectors (n, 3) -> rotation matrices (n, 3, 3)."""
    th = np.asarray(theta, float).reshape(-1, 3)
    a = np.linalg.norm(th, axis=1)
    k = np.where(a[:, None] > 1e-12, th / np.maximum(a, 1e-12)[:, None], 0.0)
    K = np.zeros((len(th), 3, 3))
    K[:, 0, 1], K[:, 0, 2] = -k[:, 2], k[:, 1]
    K[:, 1, 0], K[:, 1, 2] = k[:, 2], -k[:, 0]
    K[:, 2, 0], K[:, 2, 1] = -k[:, 1], k[:, 0]
    s, c = np.sin(a)[:, None, None], np.cos(a)[:, None, None]
    return np.eye(3)[None] + s * K + (1 - c) * (K @ K)


class LocalFrame:
    """One ENU frame for the job, tangent at (lat0, lon0, h0)."""

    def __init__(self, lat0, lon0, h0=0.0):
        self.lat0, self.lon0, self.h0 = float(lat0), float(lon0), float(h0)
        self.X0 = lla_to_ecef(self.lat0, self.lon0, self.h0)
        self.R0 = r_enu_from_ecef(self.lat0, self.lon0)                  # ECEF -> frame ENU

    def to_local(self, lat, lon, h):
        return (lla_to_ecef(lat, lon, h) - self.X0) @ self.R0.T

    def to_lla(self, P):
        return ecef_to_lla(np.asarray(P, float).reshape(-1, 3) @ self.R0 + self.X0)

    def ned_to_frame(self, lat, lon):
        """Rotation(s) from the vehicle's own NED at lat, lon to this frame's ENU: (n, 3, 3)."""
        Re = r_enu_from_ecef(lat, lon)                                    # ECEF -> ENU at the vehicle
        return self.R0[None] @ np.swapaxes(Re, -1, -2) @ NED_FROM_ENU[None]

    def attitude(self, lat, lon, roll, pitch, heading):
        """Vehicle -> frame ENU rotations (n, 3, 3)."""
        return self.ned_to_frame(lat, lon) @ rot_zyx(roll, pitch, heading)

    def attitude_back(self, lat, lon, Rf):
        """Vehicle -> frame ENU rotations -> roll, pitch, heading in the vehicle's own NED (radians)."""
        Rn = np.swapaxes(self.ned_to_frame(lat, lon), -1, -2) @ Rf
        return zyx_from_rot(Rn)
