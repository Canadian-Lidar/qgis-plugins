"""One coordinate frame for the whole DCS chain, and the adapters to every device's own convention.

Every value inside the chain (in .clraw hardware records, handovers, plugin code) is in the CANONICAL frame below.
A device's or program's own convention is converted only here, by a named adapter, once: when a recording is made
into a .clraw (RemixPro, the mapper), when a file for another program is written (RemixPro's exports), and when
a sensor's raw measurements are turned into geometry (DCS Core's readers). A file records the raw values beside
the converted ones and the adapter's name and version (`adapter_record`), so a corrected adapter can be applied
again on reading without converting anything again.

CANONICAL
  body (vehicle / vessel) frame   X forward, Y starboard (right), Z down (FRD), metres, origin at the platform's
                                  reference point (Certus reference point on the mapper, the GNSS antenna
                                  reference point on the boat). Lever arms are [F, S, D].
  attitude                        degrees in files and the UI; roll + starboard down, pitch + bow up, heading +
                                  clockwise from north; C_b^n = Rz(heading) Ry(pitch) Rx(roll), body -> NED.
  navigation                      NED for rotations; local east / north / up for metric work in a plane.
  time                            what each file states (UTC or GPS); SBET = GPS seconds of week.

ADAPTERS (each cites the document it follows; each has a test in dcs_core/tests/test_frames.py)
  hesai_navaq/1             Hesai XT-32 mounting angles as the mapper (and NavAQ) enter them.
  cerulean_sonarview_mount/1  SonarView's Mount Offset: FRD (Cerulean Sonar coordinate frames: +X forward,
                            +Y right / starboard, +Z down) - identity.
  cerulean_device/1         Cerulean devices' own frame (attitude packet 504, Omniscan 3D points): x forward,
                            y port, z up (FLU); os3d beam angle positive to starboard.
  inertial_explorer/1       NovAtel Inertial Explorer's vehicle axes: X right, Y forward, Z up.
  qinertia/1, beamworx/1    SBG Qinertia and BeamworX: FRD - identity.
"""
import math

import numpy as np

CANONICAL = {"body": "FRD (X forward, Y starboard, Z down), metres, origin = platform reference point",
             "attitude": "deg; roll + starboard down, pitch + bow up, heading + clockwise; C_b^n = Rz Ry Rx",
             "navigation": "NED rotations; local ENU metric frame"}


def adapter_record(name, version, raw):
    """What a converted value records about where it came from: {"adapter", "version", "raw"}."""
    return {"adapter": name, "version": int(version), "raw": raw}


# ----------------------------------------------------------------------------- rotations
def rx(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], float)


def ry(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], float)


def rz(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], float)


def body_to_ned(roll, pitch, heading):
    """C_b^n for attitude in degrees (canonical)."""
    r, p, h = (math.radians(v) for v in (roll, pitch, heading))
    return rz(h) @ ry(p) @ rx(r)


# ----------------------------------------------------------------------------- hesai_navaq/1
HESAI_NAVAQ = ("hesai_navaq", 1)
# Hesai sensor axes (x right, y = azimuth 0, z = rotation axis up) in the body frame when the mounting angles are
# all zero: rotation axis forward, connector (opposite azimuth 0) down. Established from the 2024 NavAQ recording
# (Roll 90, Pitch 45, Heading 0 reproduces NavAQ's own georeferenced points to 0.005 per matrix element). The
# Hesai roll is applied with the opposite sign to the motion roll; the heading sign follows the motion convention
# (not tested: that recording had heading 0).
HESAI_ZERO = np.array([[0.0, 0.0, 1.0], [-1.0, 0.0, 0.0], [0.0, -1.0, 0.0]])
HESAI_MATRIX_TEXT = "M = Rz(heading) Ry(pitch) Rx(-roll) Z0"


def hesai_mount_matrix(roll, pitch, heading):
    """Hesai mounting angles (deg, hesai_navaq) -> sensor-to-body rotation M = Rz(h) Ry(p) Rx(-r) Z0."""
    r, p, h = (math.radians(float(v)) for v in (roll, pitch, heading))
    return rz(h) @ ry(p) @ rx(-r) @ HESAI_ZERO


def hesai_mount_angles(m):
    """Inverse of hesai_mount_matrix -> (roll, pitch, heading) deg, pitch in [-90, 90], heading in (-180, 180]."""
    a = np.asarray(m, float) @ HESAI_ZERO.T              # = Rz(h) Ry(p) Rx(-r)
    p = math.asin(max(-1.0, min(1.0, -a[2, 0])))
    if abs(a[2, 0]) > 1.0 - 1e-9:                       # pitch +-90: roll and heading not separable, roll 0
        return 0.0, math.degrees(p), math.degrees(math.atan2(-a[0, 1], a[1, 1]))
    return (-math.degrees(math.atan2(a[2, 1], a[2, 2])) + 0.0, math.degrees(p),
            math.degrees(math.atan2(a[1, 0], a[0, 0])))


# ----------------------------------------------------------------------------- cerulean_sonarview_mount/1
SONARVIEW_MOUNT = ("cerulean_sonarview_mount", 1)
SONARVIEW_MOUNT_TEXT = "SonarView Mount Offset (FRD: X forward, Y starboard, Z down)"


def sonarview_mount_to_frd(xyz):
    """SonarView's Mount Offset [X, Y, Z] m (from the position source to the transducer) -> canonical [F, S, D].
    Cerulean: +X forward, +Y right (starboard), +Z down - the canonical frame, so the values are kept as they are.
    None when not entered (missing or all zero)."""
    if xyz is None:
        return None
    try:
        v = [float(x) for x in xyz]
    except (TypeError, ValueError):
        return None
    if len(v) != 3 or not any(v):
        return None
    return v


# ----------------------------------------------------------------------------- cerulean_device/1
CERULEAN_DEVICE = ("cerulean_device", 1)
# The device frame is x forward, y PORT, z up (FLU); the os3d beam angle is positive to starboard, i.e. toward -y.
# Verified 13 Nov 2026 on reciprocal passes at Lumston (0.23 m median agreement; 5.6 m with the mirror).
CERULEAN_STARBOARD_Y = -1.0
CERULEAN_TO_FRD = np.diag([1.0, -1.0, -1.0])          # device FLU vector -> body-aligned FRD vector


def cerulean_beam(angle, range_m):
    """os3d beam angle (rad, + starboard) and slant range (m) -> vector in the device frame (FLU), (n, 3)."""
    a = np.asarray(angle, float)
    r = np.asarray(range_m, float)
    return np.stack([np.zeros_like(a), CERULEAN_STARBOARD_Y * np.sin(a), -np.cos(a)], -1) * r[..., None]


def cerulean_attitude(up):
    """Packet 504's world-up vector in the device frame -> (roll, pitch) deg, canonical signs (roll + starboard
    down, pitch + bow up): roll = atan2(y, z), pitch = asin(-x)."""
    u = np.asarray(up, float)
    return np.degrees(np.arctan2(u[..., 1], u[..., 2])), np.degrees(np.arcsin(np.clip(-u[..., 0], -1, 1)))


def cerulean_looks(angles):
    """Which side a head looks to, from its beam angles (rad): "starboard" / "port" / "down"."""
    a = np.asarray(angles, float)
    if not a.size:
        return None
    m = float(np.median(a))
    return "starboard" if m > math.radians(5) else "port" if m < -math.radians(5) else "down"


# ----------------------------------------------------------------------------- export frames
INERTIAL_EXPLORER = ("inertial_explorer", 1)


def frd_to_inertial_explorer(frd):
    """Canonical [F, S, D] -> Inertial Explorer's vehicle axes [X right, Y forward, Z up] = [S, F, -D]."""
    f, s, d = (float(v) for v in frd)
    return [s, f, -d]


def frd_to_qinertia(frd):
    """Qinertia's lever arms are FRD: unchanged."""
    return [float(v) for v in frd]


frd_to_beamworx = frd_to_qinertia
