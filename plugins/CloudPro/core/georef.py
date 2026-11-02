"""Direct georeferencing of Lidar returns with a trajectory.

    x = r(t) + C_v^n(t) (B M p_s + l)

M = Rz(h) Ry(p) Rx(-r) Z0 is the mapper's Lidar mounting (convention
navaq_hesai of the mapper's survey configuration), l the lever arm (Forward, Starboard, Down) from the INS
reference point to the Lidar optical centre, B an optional small boresight
correction (identity: AlignPro publishes the full calibrated mounting), C_v^n = Rz(psi) Ry(theta)
Rx(phi).  Each return is placed at its own firing time.
"""
import numpy as np

from .geodesy import local_jacobian, radii

Z0 = np.array([[0.0, 0.0, 1.0], [-1.0, 0.0, 0.0], [0.0, -1.0, 0.0]])


def _rx(a):
    c, s = np.cos(a), np.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])


def _ry(a):
    c, s = np.cos(a), np.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def _rz(a):
    c, s = np.cos(a), np.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


def lidar_matrix(roll, pitch, heading):
    """Mapper mounting angles (deg, navaq_hesai convention) -> sensor-to-vehicle rotation."""
    r, p, h = np.radians([roll, pitch, heading])
    return _rz(h) @ _ry(p) @ _rx(-r) @ Z0


def navaq_from_lidar_matrix(Mx):
    """Inverse of lidar_matrix: rotation -> (roll, pitch, heading) deg."""
    R = Mx @ Z0.T                       # = Rz(h) Ry(p) Rx(-r)
    p = np.arcsin(-np.clip(R[2, 0], -1, 1))
    h = np.arctan2(R[1, 0], R[0, 0])
    mr = np.arctan2(R[2, 1], R[2, 2])
    return float(np.degrees(-mr)), float(np.degrees(p)), float(np.degrees(h))


def boresight_matrix(droll, dpitch, dheading):
    """Small boresight correction B (deg) applied in the vehicle frame."""
    r, p, h = np.radians([droll, dpitch, dheading])
    return _rz(h) @ _ry(p) @ _rx(r)


def _rot_elems(roll, pitch, yaw):
    cr, sr = np.cos(roll), np.sin(roll)
    cp, sp = np.cos(pitch), np.sin(pitch)
    cy, sy = np.cos(yaw), np.sin(yaw)
    # Rz(y) Ry(p) Rx(r)
    return (cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr,
            sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr,
            -sp, cp * sr, cp * cr)


class GeorefContext:
    """Everything needed to georeference points of one line, prepared once."""

    def __init__(self, traj, projector, mount_deg, lever, bore_deg=(0.0, 0.0, 0.0), geoid=None,
                 origin=(0.0, 0.0, 0.0), latency=0.0):
        self.traj = traj
        self.latency = float(latency or 0.0)      # Lidar time offset: pose taken at t - latency
        self.M = boresight_matrix(*bore_deg) @ lidar_matrix(*mount_deg)
        self.l = np.asarray(lever, float)
        self.origin = np.asarray(origin, float)
        t = traj.t
        E, N = projector.forward(traj.lat, traj.lon)
        self.E = E - self.origin[0]
        self.N = N - self.origin[1]
        # Jacobian on a 0.2 s grid (it changes very slowly)
        step = max(1, int(round(0.2 / max(np.median(np.diff(t)), 1e-4)))) if len(t) > 1 else 1
        idx = np.unique(np.r_[np.arange(0, len(t), step), len(t) - 1])
        _e, _n, J = local_jacobian(projector, traj.lat[idx], traj.lon[idx], traj.h[idx])
        self.tj = t[idx]
        self.J = J
        M_, N_ = radii(traj.lat[idx])
        self.R = float(np.sqrt(np.mean(M_ * N_)))
        self.geoidN = None
        if geoid is not None:
            self.geoidN = np.asarray(geoid(np.degrees(traj.lat), np.degrees(traj.lon)), float)

    def _nav(self, t):
        tr = self.traj
        if self.latency:
            t = t - self.latency
        f = lambda v: np.interp(t, tr.t, v)
        E0, N0, h = f(self.E), f(self.N), f(tr.h)
        if self.geoidN is not None:
            h = h - f(self.geoidN)
        J = [np.interp(t, self.tj, self.J[:, i, j]) for i in range(2) for j in range(2)]
        return E0, N0, h, f(tr.roll), f(tr.pitch), f(tr.heading), J

    def place(self, t, v_frd):
        """Vehicle-frame vectors (n,3) at times t -> local E, N, Z (relative to origin)."""
        E0, N0, h, r, p, y, J = self._nav(t)
        c = _rot_elems(r, p, y)
        vx, vy, vz = v_frd[:, 0], v_frd[:, 1], v_frd[:, 2]
        n_ = c[0] * vx + c[1] * vy + c[2] * vz
        e_ = c[3] * vx + c[4] * vy + c[5] * vz
        d_ = c[6] * vx + c[7] * vy + c[8] * vz
        E = E0 + J[0] * e_ + J[1] * n_
        N = N0 + J[2] * e_ + J[3] * n_
        Z = h - d_ + (e_ * e_ + n_ * n_) / (2 * self.R) - self.origin[2]
        return E, N, Z

    def points(self, t, xs, ys, zs):
        """Sensor-frame points -> local E, N, Z (relative to the project origin)."""
        Ms = self.M
        vx = Ms[0, 0] * xs + Ms[0, 1] * ys + Ms[0, 2] * zs + self.l[0]
        vy = Ms[1, 0] * xs + Ms[1, 1] * ys + Ms[1, 2] * zs + self.l[1]
        vz = Ms[2, 0] * xs + Ms[2, 1] * ys + Ms[2, 2] * zs + self.l[2]
        return self.place(t, np.column_stack([vx, vy, vz]))

    def sensor_track(self, t):
        """Optical-centre position and heading at times t (E, N, Z, heading rad)."""
        v = np.tile(self.l, (len(t), 1))
        E, N, Z = self.place(t, v)
        return E, N, Z, np.interp(t - self.latency, self.traj.t, self.traj.heading)
