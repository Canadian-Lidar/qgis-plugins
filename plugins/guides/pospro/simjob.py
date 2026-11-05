#!/usr/bin/env python3
"""A simulated mapper 2.10 job for the PosPro guide figures: an 8-minute drive on winding streets, written with
the mapper's own .clraw writer (RemixPro's tests/sim210.py), plus a base station RINEX file.

    python guides/pospro/simjob.py OUTDIR

    OUTDIR/Shirley_Ave/project.json, nav/ (the whole drive), lines/ (two survey lines)
    OUTDIR/base/RCH10010.26O                (1 s base observations covering the drive)

The Certus real-time solution (packet 20) follows the road at 11 m/s with a stop at a light, RTK fixed except for
a float spell under trees (with a height wobble), a DGNSS spell with a sideways jump, and a short spell with no
GNSS; the PosPro guide processes it in real-time mode. The raw observations are laid out as the Certus sends them
but are not a physically consistent constellation, so they are not for PPK.
"""
import calendar
import math
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGINS = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(PLUGINS, "RemixPro", "tests"))
sys.path.insert(0, PLUGINS)

import sim210                                   # noqa: E402
from dcs_core import anpp                       # noqa: E402

T0 = calendar.timegm((2026, 11, 3, 21, 25, 0))  # start of the drive (UTC)
LAT0, LON0, H0 = 45.9583, -66.6447, 21.0
DRIVE_S = 480.0
STOP = (200.0, 245.0)                           # stopped at a light
FLOAT = (95.0, 112.0)                           # under trees: float, height wobble
DGNSS = (300.0, 318.0)                          # DGNSS with a sideways jump
NOFIX = (380.0, 386.0)                          # no GNSS (an underpass)
M = 6367000.0
N = 6389000.0 * math.cos(math.radians(LAT0))


def _dist(ts):
    """Distance driven (m) at ts seconds, with the stop (smooth slow-down and start)."""
    v = 11.0
    a, b = STOP
    if ts < a - 5:
        return v * ts
    d0 = v * (a - 5)
    if ts < a:
        x = ts - (a - 5)
        return d0 + v * (x - x * x / 10.0)
    d1 = d0 + v * 2.5
    if ts < b:
        return d1
    if ts < b + 5:
        x = ts - b
        return d1 + v * x * x / 10.0
    return d1 + v * 2.5 + v * (ts - b - 5)


def _road(d):
    """North, east (m) and heading (rad) of the road at distance d."""
    n = d * 0.55 + 70.0 * math.sin(d / 230.0) + 25.0 * math.sin(d / 61.0)
    e = d * 0.80 + 90.0 * math.sin(d / 310.0)
    dn = 0.55 + 70.0 / 230.0 * math.cos(d / 230.0) + 25.0 / 61.0 * math.cos(d / 61.0)
    de = 0.80 + 90.0 / 310.0 * math.cos(d / 310.0)
    return n, e, math.atan2(de, dn)


def truth(ts):
    d = _dist(ts)
    n, e, hd = _road(d)
    v = (_dist(ts + 0.05) - _dist(ts - 0.05)) / 0.1
    u = 3.0 * math.sin(d / 500.0)
    return n, e, u, hd, v


def _in(ts, w):
    return w[0] <= ts <= w[1]


def state_packet(t, valid=True):
    """Packet 20 along the simulated road (replaces sim210.state_packet)."""
    ts = t - T0
    n, e, u, hd, v = truth(ts)
    fix, sd = 7, (0.012, 0.012, 0.02)
    if _in(ts, FLOAT):
        k = math.sin(math.pi * (ts - FLOAT[0]) / (FLOAT[1] - FLOAT[0]))
        u += 0.9 * k + 0.25 * math.sin(ts * 2.7)
        fix, sd = 6, (0.25, 0.25, 0.45)
    if _in(ts, DGNSS):
        k = math.sin(math.pi * (ts - DGNSS[0]) / (DGNSS[1] - DGNSS[0]))
        n += 1.6 * k * -math.sin(hd)
        e += 1.6 * k * math.cos(hd)
        fix, sd = 4, (0.6, 0.6, 1.1)
    if _in(ts, NOFIX):
        fix, sd = 0, (1.5, 1.5, 2.0)
    if valid:
        ut, us = int(t), int(round((t % 1) * 1e6))
        if us >= 1000000:
            ut, us = ut + 1, us - 1000000
        fs = 0x0008 | 0x0002 | 0x0001 | 0x0004 | (fix << 4) | 0x0200
    else:
        ut, us, fs = 1357110419, int((t % 1) * 1e6), 0x0007
    lat = math.radians(LAT0 + math.degrees(n / M))
    lon = math.radians(LON0 + math.degrees(e / N))
    vn, ve = v * math.cos(hd), v * math.sin(hd)
    body = struct.pack("<HHIIdddffffffffffffffff", 0, fs, ut, us, lat, lon, H0 + u, vn, ve, 0.0, 0, 0, 0, 9.81,
                       0.004 * math.sin(ts), -0.01, hd, 0, 0, 0, sd[0], sd[1], sd[2])
    return anpp.build(20, body)


def write_base(path, t0, t1):
    """A small RINEX 3.04 observation file: a Reach base 1 s, GPS + Galileo, approximate position in the header."""
    from datetime import datetime, timezone
    lat, lon, h = math.radians(LAT0 + 0.004), math.radians(LON0 - 0.006), 30.0
    a, f = 6378137.0, 1 / 298.257222101
    e2 = f * (2 - f)
    nn = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    x, y, z = ((nn + h) * math.cos(lat) * math.cos(lon), (nn + h) * math.cos(lat) * math.sin(lon),
               (nn * (1 - e2) + h) * math.sin(lat))
    d0 = datetime.fromtimestamp(t0, timezone.utc)
    hl = lambda txt, lab: "%-60s%-20s\n" % (txt, lab)       # noqa: E731
    out = [hl("     3.04           OBSERVATION DATA    M (MIXED)", "RINEX VERSION / TYPE"),
           hl("PosPro guide        Canadian Lidar      20261103 220000 UTC", "PGM / RUN BY / DATE"),
           hl("RCH1", "MARKER NAME"), hl("Canadian Lidar      Canadian Lidar", "OBSERVER / AGENCY"),
           hl("82734               EMLID REACH RS2     2.24", "REC # / TYPE / VERS"),
           hl("                    EMLRS2          NONE", "ANT # / TYPE"),
           hl("%14.4f%14.4f%14.4f" % (x, y, z), "APPROX POSITION XYZ"),
           hl("%14.4f%14.4f%14.4f" % (0.0, 0.0, 0.0), "ANTENNA: DELTA H/E/N"),
           hl("G    4 C1C L1C C2W L2W", "SYS / # / OBS TYPES"), hl("E    4 C1C L1C C7Q L7Q", "SYS / # / OBS TYPES"),
           hl("     1.000", "INTERVAL"),
           hl("  %4d    %2d    %2d    %2d    %2d   %10.7f     GPS" % (d0.year, d0.month, d0.day, d0.hour, d0.minute,
                                                                     d0.second), "TIME OF FIRST OBS"),
           hl("", "END OF HEADER")]
    sats = [("G03", 21614456.2), ("G08", 22114456.9), ("G14", 20914456.1), ("G22", 23914456.4), ("G27", 22714456.8),
            ("E05", 26709332.3), ("E09", 25809332.1)]
    t = t0
    while t <= t1:
        d = datetime.fromtimestamp(t + 18, timezone.utc)
        out.append("> %4d %02d %02d %02d %02d %10.7f  0 %2d\n" % (d.year, d.month, d.day, d.hour, d.minute, d.second,
                                                                    len(sats)))
        k = t - t0
        for prn, rho in sats:
            r = rho + 0.31 * k
            out.append("%s%14.3f  %14.3f  %14.3f  %14.3f  \n" % (prn, r, r / 0.190293672798, r + 2.1,
                                                                 (r + 2.1) / 0.244210213425))
        t += 1.0
    with open(path, "w", newline="\n") as fh:
        fh.write("".join(out))


def make(outdir, name="Shirley_Ave"):
    sim210.state_packet = state_packet
    sim210.T_LINE1 = T0
    pdir = os.path.join(outdir, name)
    lines_dir = os.path.join(pdir, "lines")
    os.makedirs(lines_dir, exist_ok=True)
    import json
    with open(os.path.join(pdir, "project.json"), "w") as f:
        json.dump({"name": name, "id": name, "created": "2026-11-03T21:20:00Z", "next_line": 3}, f)
    lines = [sim210.write_line(lines_dir, name, 1, T0 + 20, 60.0, points_hz=20),
             sim210.write_line(lines_dir, name, 2, T0 + 260, 60.0, points_hz=20)]
    nav = sim210.write_nav(pdir, name, T0 - 30, T0 + DRIVE_S + 20)
    bdir = os.path.join(outdir, "base")
    os.makedirs(bdir, exist_ok=True)
    write_base(os.path.join(bdir, "RCH10010.26O"), T0 - 600, T0 + DRIVE_S + 300)
    return {"project": pdir, "lines": lines, "nav": nav, "base": bdir}


if __name__ == "__main__":
    print(make(sys.argv[1]))
