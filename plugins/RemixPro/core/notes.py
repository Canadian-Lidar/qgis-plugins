"""The setup notes written beside the Inertial Explorer and Qinertia files.

From the mapper (mapper/export/package.py _write_readmes, 2.10.0). The text is the mapper's, with the
lever arm marked "not recorded" when the input carries none, and the source files named at the end.

Frames: the lever arms are recorded in the vehicle frame (X forward, Y starboard, Z down, metres from the
Certus reference point). Inertial Explorer uses X right, Y forward, Z up: lever_ie() converts.
"""
import json
import os

from . import software

COORDINATES = {
    "positions": "WGS 84 geographic 3D, EPSG:4979: latitude and longitude in decimal degrees, "
                 "ellipsoidal height in metres",
    "heights": "Ellipsoidal (WGS 84 ellipsoid). No geoid applied. For orthometric heights in "
               "Canada (CGVD2013) apply geoid model CGG2013a after processing.",
    "rtk_note": "With RTK corrections on, real-time positions are in the reference frame of "
                "the correction service (often NAD83(CSRS) in Canada) although the Certus "
                "labels them WGS 84. Post-processed positions are in the frame of the base "
                "station coordinates used.",
}
IE_NOTE = "README_INERTIAL_EXPLORER.txt"
QINERTIA_NOTE = "README_QINERTIA.txt"


def lever_ie(lever_vehicle):
    """Vehicle frame (X fwd, Y right, Z down) -> Inertial Explorer frame (X right, Y forward, Z up)."""
    from dcs_core import frames
    return frames.frd_to_inertial_explorer(lever_vehicle)


def _fmt3(v):
    if not v:
        return "not recorded with this data: measure it or take it from the mapper's Settings page"
    return ", ".join("%+.3f" % x for x in v)


def _common(meta, s):
    imu = s.get("imu") or {}
    obs = (s.get("rinex_obs") or [{}])[0]
    nav = s.get("rinex_nav") or {}
    rtk = meta.get("rtk") or {}
    if rtk.get("enabled"):
        rtk_text = "ON during capture (%s/%s). " % (rtk.get("caster"), rtk.get("mountpoint")) + COORDINATES["rtk_note"]
    elif "rtk" in meta:
        rtk_text = "off during capture: real-time positions are standalone/SBAS WGS 84"
    else:
        rtk_text = "not recorded with this data. " + COORDINATES["rtk_note"]
    return """Session        %(id)s
Started (UTC)  %(start)s      clock source: %(clock)s
Valid data     %(v0)s  to  %(v1)s  (%(dur).0f s)
IMU            %(n)d samples at %(rate)s Hz, %(gaps)d gaps, %(drop)d samples dropped before GNSS time was valid
GNSS           %(ep)d epochs every %(int)s s, about %(sats)s satellites, signals %(sig)s
Ephemeris      GPS only (%(neph)d records). Galileo and others: let the software download broadcast ephemeris.
Leap seconds   GPS - UTC = %(leap)d s
Coordinates    %(crs)s
Heights        %(hts)s
RTK            %(rtk)s
""" % {
        "id": meta.get("id"), "start": meta.get("started_utc"), "clock": meta.get("clock_source"),
        "v0": s.get("first_valid_utc"), "v1": s.get("last_valid_utc"), "dur": s.get("valid_duration_s", 0),
        "n": imu.get("samples", 0), "rate": imu.get("rate_hz"), "gaps": imu.get("gaps_over_2_samples", 0),
        "drop": imu.get("dropped_before_time_valid", 0),
        "ep": obs.get("epochs", 0), "int": obs.get("interval_s"), "sats": obs.get("mean_satellites"),
        "sig": json.dumps(obs.get("signals", {})), "neph": nav.get("gps_ephemerides", 0),
        "leap": s.get("leap_seconds", 18), "crs": COORDINATES["positions"], "hts": COORDINATES["heights"],
        "rtk": rtk_text,
    }


def _made_from(s):
    src = s.get("source_files") or []
    kind = "the /ins/anpp channel of" if s.get("source_kind") == "clraw" else ""
    return "Made by %s from %s %s" % (software(), kind, ", ".join(src)) if src else "Made by %s" % software()


def write_ie(out_dir, meta, s):
    """README_INERTIAL_EXPLORER.txt in out_dir."""
    lever = meta.get("lever_primary")
    f = s.get("files", {})
    ie = lever_ie(lever) if lever else None
    with open(os.path.join(out_dir, IE_NOTE), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("""Inertial Explorer 8.90 package
==============================

%s
Files
  %s      raw IMU, Waypoint IMR format (512-byte header, 32-byte records)
  %s      rover GNSS observations, RINEX 3.04
  %s      GPS broadcast ephemeris, RINEX 3.04

Steps
  1. File > Convert > Raw GNSS to GPB. Receiver type RINEX. Convert the .obs file.
  2. New project. Add the rover GPB, your base station, and the .imr as the IMU file.
  3. IMU settings:
       Sensor frame    The IMR is already in Inertial Explorer's vehicle frame
                       (X right, Y forward, Z up). Leave body-to-IMU rotations at 0, 0, 0.
       Lever arm       Enter in Inertial Explorer (its own axes: X right, Y forward, Z up):
                         %s
                       This is the same lever arm as on the Certus and the mapper's
                       Settings page (X+ forward, Y+ starboard, Z+ down):
                         %s
                       only written in Inertial Explorer's axes, which is why the
                       order and signs differ. Also stored in the IMR header.
       Error model     There is no built-in profile for the Certus Evo. Start from a
                       tactical-grade MEMS profile and refine it, or ask NovAtel.
  4. Process GNSS first, check it, then process tightly coupled, forward and reverse.
  5. Export the trajectory as SBET for BeamworX.

Notes
  Time tags are GPS seconds of week, week %s.
  Gyro values are angular rates (deg/s after scaling), accelerations are m/s^2.
  The raw observation interval comes from the receiver setup. Captures from mapper
  2.8.2 on are on whole seconds (0.125 s); older ones are 0.072 or 0.143 s.

%s
""" % (_common(meta, s), f.get("imr", "-"), f.get("obs", "-"), f.get("nav", "-"), _fmt3(ie), _fmt3(lever),
       (s.get("imu") or {}).get("gps_week"), _made_from(s)))
    return IE_NOTE


def write_qinertia(out_dir, meta, s):
    """README_QINERTIA.txt in out_dir."""
    f = s.get("files", {})
    with open(os.path.join(out_dir, QINERTIA_NOTE), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("""Qinertia package
================

%s
Files
  %s      IMU, Qinertia "Generic IMU" ASCII file ($qimu version 2)
  %s      rover GNSS observations, RINEX 3.04
  %s      GPS broadcast ephemeris, RINEX 3.04

Import
  IMU file        Generic IMU. Time source is GPS seconds since 6 Jan 1980.
                  Columns: time; accel X Y Z (m/s^2); rotation rate X Y Z (rad/s);
                  temperature (C); status.
  IMU frame       X forward, Y right, Z down (same as the SBG convention).
  IMU model       Advanced Navigation is not in Qinertia's model list. Ask SBG which
                  model to use for a Certus Evo.
  Lever arms      metres, same axes as the Certus and the mapper's Settings page
                  (X+ forward, Y+ starboard, Z+ down), from the IMU:
                    primary antenna    %s
                    secondary antenna  %s
  GNSS            import the RINEX observation and navigation files.

%s
""" % (_common(meta, s), f.get("qimu", "-"), f.get("obs", "-"), f.get("nav", "-"),
       _fmt3(meta.get("lever_primary")), _fmt3(meta.get("lever_secondary")), _made_from(s)))
    return QINERTIA_NOTE
