"""BeamworX survey configuration XML, built from the mapper's hardware settings (taken from mapper 2.10.0).

RemixPro uses it only for a .clraw file that has no beamworx_survey_config.xml attachment (every survey-line
file has one); then the values come from the file's hardware.json (from_hardware).

The template is the NavAQ configuration recorded with this hardware in 2024
(templates/beamworx_survey_config.xml). Only the values the mapper owns are
changed, in place, so the file keeps NavAQ's exact layout:

  vehicle name, the offset locations (Forward / Starboard / Down, metres from
  the Certus reference point), the LiDAR mounting angles (NavAQ convention),
  the device addresses, and the geodetic definition.

Conventions (BeamworX / NavAQ):
  offsets   Forward +, Starboard +, Down +   (from the reference point)
  motion    roll + starboard down, pitch + bow up, heading + clockwise
  Hesai     0,0,0 = rotation axis pointing forward, connector down
            (NavAQ Hesai driver); worked out against NavAQ's own computed
            points: see lidar_matrix_from_navaq().
"""
from __future__ import annotations

import math
import os
import re

TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates",
                        "beamworx_survey_config.xml")
TEMPLATE_VEHICLE = "Hacked Lumos"


class ConfigError(ValueError):
    pass


def _sub_once(pattern, repl, text, what):
    new, n = re.subn(pattern, repl, text, count=1, flags=re.S)
    if n != 1:
        raise ConfigError("survey config template: %s not found" % what)
    return new


def _f3(v):
    return "%.3f" % float(v)


def _location(text, name, fsd):
    pat = r'(<Offset ID="\d+" Name="%s">.*?<Location )Forward="[^"]*" Starboard="[^"]*" Down="[^"]*"' % re.escape(name)
    repl = r'\g<1>Forward="%s" Starboard="%s" Down="%s"' % tuple(_f3(v) for v in fsd)
    return _sub_once(pat, repl, text, "offset " + name)


def _xml_text(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def build(hw, vehicle="Mobile Mapper", geodesy=None, template=TEMPLATE):
    """Survey configuration XML (bytes).

    hw: {"primary_antenna": [F,S,D], "secondary_antenna": [F,S,D],
         "lidar_offset": [F,S,D], "lidar_mount": {"roll","pitch","heading"},
         "certus_ip", "certus_port", "lidar_ip", "lidar_port"}
    geodesy: {"mode": "PROJ4" | "EPSG", "definition": str} or None (keep template)."""
    with open(template, encoding="utf-8") as f:
        x = f.read()
    name = _xml_text(vehicle or "Mobile Mapper")
    x = x.replace(TEMPLATE_VEHICLE, name)
    x = _location(x, "Primary GPS", hw.get("primary_antenna", [0, 0, 0]))
    x = _location(x, "Secondary GPS", hw.get("secondary_antenna", [0, 0, 0]))
    x = _location(x, "Lidar", hw.get("lidar_offset", [0, 0, 0]))
    m = hw.get("lidar_mount") or {}
    r, p, h = (float(m.get(k, 0.0)) for k in ("roll", "pitch", "heading"))
    x = _sub_once(r"<RollOffset>[^<]*</RollOffset>", "<RollOffset>%.4f</RollOffset>" % r, x, "laser roll")
    x = _sub_once(r"<PitchOffset>[^<]*</PitchOffset>", "<PitchOffset>%.4f</PitchOffset>" % p, x, "laser pitch")
    x = _sub_once(r"<HeadingOffset>[^<]*</HeadingOffset>", "<HeadingOffset>%.4f</HeadingOffset>" % h, x, "laser heading")
    for tag in ("Mounting-Tx", "Mounting-Rx"):
        x = _sub_once(r'<%s OffsetID="3" Roll="[^"]*" Pitch="[^"]*" Heading="[^"]*"/>' % tag,
                      '<%s OffsetID="3" Roll="%.6f" Pitch="%.6f" Heading="%.6f"/>' % (tag, r, p, h), x, tag)
    # device addresses: Certus (device 2, TCP) and Hesai (device 3, UDP)
    def port(text, dev_name, ip, number):
        pat = (r'(<Device Version="1" ID="\d+" Name="%s".*?<Network-PortNumber>)\d+(</Network-PortNumber>'
               r'.*?<Network-IP>)[^<]*(</Network-IP>)' % re.escape(dev_name))
        return _sub_once(pat, r"\g<1>%d\g<2>%s\g<3>" % (int(number), _xml_text(ip)), text, dev_name)
    x = port(x, "Advanced Navigation Certus Evo OEM", hw.get("certus_ip", "10.1.0.10"), hw.get("certus_port", 16718))
    x = port(x, "Hesai Laser Scanner", hw.get("lidar_ip", "10.1.0.201"), hw.get("lidar_port", 2368))
    if geodesy and geodesy.get("definition"):
        mode, defn = _xml_text(geodesy.get("mode", "PROJ4")), _xml_text(geodesy["definition"])
        x = _sub_once(r"<Mode>[^<]*</Mode>(\s*)<Definition>[^<]*</Definition>",
                      lambda m: "<Mode>%s</Mode>%s<Definition>%s</Definition>" % (mode, m.group(1), defn),
                      x, "geodetic settings")
    return x.encode("utf-8")


# ---------------------------------------------------------------------------
# LiDAR mounting angles: NavAQ convention <-> rotation matrix
# ---------------------------------------------------------------------------
def _rx(a):
    c, s = math.cos(a), math.sin(a)
    return ((1, 0, 0), (0, c, -s), (0, s, c))


def _ry(a):
    c, s = math.cos(a), math.sin(a)
    return ((c, 0, s), (0, 1, 0), (-s, 0, c))


def _rz(a):
    c, s = math.cos(a), math.sin(a)
    return ((c, -s, 0), (s, c, 0), (0, 0, 1))


def _mm(a, b):
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3))


# Hesai sensor axes (x right, y = azimuth 0, z = rotation axis up) in the
# vehicle frame (Forward, Starboard, Down) when NavAQ's angles are all zero:
# rotation axis forward, connector (opposite azimuth 0) down.
# The zero comes from dcs_core.frames (adapter hesai_navaq/1); the arithmetic here stays the mapper's own, so
# the files are byte for byte the mapper's (tests/test_same_as_mapper.py; tests/test_frames.py checks the match).
from dcs_core import frames as _frames                                         # noqa: E402

NAVAQ_HESAI_ZERO = tuple(tuple(int(v) for v in row) for row in _frames.HESAI_ZERO)


def lidar_matrix_from_navaq(roll, pitch, heading):
    """Sensor -> vehicle (F, S, D) rotation for NavAQ Hesai mounting angles.

    Established from the 2024 NavAQ recording: with Roll 90, Pitch 45,
    Heading 0 this reproduces NavAQ's own georeferenced points to within
    0.005 in every matrix element (about 0.3 degrees). NavAQ's Hesai roll is
    applied with the opposite sign to its motion roll. The heading sign could
    not be tested (the recording had heading 0) and follows the motion
    convention, clockwise positive."""
    r, p, h = (math.radians(v) for v in (roll, pitch, heading))
    return _mm(_mm(_mm(_rz(h), _ry(p)), _rx(-r)), NAVAQ_HESAI_ZERO)


def navaq_from_lidar_matrix(m):
    """Inverse of lidar_matrix_from_navaq: (roll, pitch, heading) degrees."""
    zt = tuple(zip(*NAVAQ_HESAI_ZERO))                       # transpose = inverse
    a = _mm(m, zt)                                           # = Rz(h) Ry(p) Rx(-r)
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, -a[2][0]))))
    if abs(a[2][0]) > 1.0 - 1e-9:
        # pitch +-90: only heading and roll together are defined; take roll 0
        rneg = 0.0
        heading = math.degrees(math.atan2(-a[0][1], a[1][1]))
    else:
        rneg = math.degrees(math.atan2(a[2][1], a[2][2]))
        heading = math.degrees(math.atan2(a[1][0], a[0][0]))
    return -rneg + 0.0, pitch, heading % 360.0 if heading < 0 else heading


def from_hardware(hw_json, vehicle=None):
    """Survey configuration XML (bytes) from a capture's hardware.json, for a .clraw file that does not carry
    beamworx_survey_config.xml. Device addresses come from hardware.json devices when recorded."""
    hw_json = hw_json or {}
    dev = hw_json.get("devices") or {}
    hw = {
        "primary_antenna": hw_json.get("ins_to_primary_antenna_m") or [0, 0, 0],
        "secondary_antenna": hw_json.get("ins_to_secondary_antenna_m") or [0, 0, 0],
        "lidar_offset": hw_json.get("ins_to_lidar_m") or [0, 0, 0],
        "lidar_mount": hw_json.get("lidar_mount_navaq_deg") or {},
        "certus_ip": dev.get("certus_ip") or "10.1.0.10",
        "certus_port": dev.get("certus_port") or 16718,
        "lidar_ip": dev.get("lidar_ip") or "10.1.0.201",
        "lidar_port": dev.get("lidar_data_port") or 2368,
    }
    cs = hw_json.get("coordinate_system") or {}
    geo = None
    if isinstance(cs, dict) and cs.get("definition"):
        geo = {"mode": cs.get("type") or "EPSG", "definition": cs["definition"]}
    return build(hw, vehicle=vehicle or "Mobile Mapper", geodesy=geo)
