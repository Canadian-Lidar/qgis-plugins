"""A simulated mapper 2.10 project, written with the mapper's own .clraw writer (tests/mapper_clraw.py).

    python tests/sim210.py OUTDIR [--name Deck_Test]

    <name>/project.json
    <name>/lines/<name>_L001_<stamp>/   clraw/<line id>_0001.clraw, _0002 ... (split), ins/ins_0001.anpp,
                                        ins/ephemeris_cache.anpp, session.json, events.jsonl, quality.jsonl,
                                        continuity.json, manifest.sha256
    <name>/lines/<name>_L002_<stamp>/   the same, one file
    <name>/lines/20261104_121000_old/   a capture from before 2.10 grouped into lines/ (003_..._0001.clraw)
    <name>/nav/<name>_NAV_<stamp>_0001.anpp / .clraw / _ephemeris.anpp / index_*.json

The Certus stream: packet 3 at the start, 200 Hz packet 20 + 28 (the first samples before the Certus knows the
time, stamped 2013), raw observations (packet 60) every 125 ms on whole seconds for both receivers, GPS
ephemerides (packet 61). The Lidar: Hesai point packets and a GPS status packet each second. Numbers are made
up but laid out exactly as the real devices send them.
"""
import argparse
import calendar
import hashlib
import json
import math
import os
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", ".."))       # plugins/ (dcs_core, RemixPro)

import mapper_clraw as clraw                               # noqa: E402
from dcs_core import anpp                                  # noqa: E402

NS = 1000000000
PRIMARY = [0.082, 0.495, 0.112]
SECONDARY = [0.082, -0.493, 0.112]
LIDAR = [0.35, 0.0, -0.45]
MOUNT = {"roll": 90.0, "pitch": 45.0, "heading": 0.0}
T_LINE1 = calendar.timegm((2026, 11, 4, 12, 45, 12))
LAT, LON, H = 45.9636, -66.6431, 40.0


def state_packet(t, valid=True):
    if valid:
        ut, us, fs = int(t), int(round((t % 1) * 1e6)), 0x0008 | 0x0002 | 0x0001 | (7 << 4) | 0x0200
        if us >= 1000000:
            ut, us = ut + 1, us - 1000000
    else:
        ut, us, fs = 1357110419, int((t % 1) * 1e6), 0x0007
    dt = t - T_LINE1
    lat = math.radians(LAT + 1e-6 * dt)
    body = struct.pack("<HHIIdddffffffffffffffff", 0, fs, ut, us, lat, math.radians(LON), H,
                       5.0, 0.2, 0.0, 0, 0, 0, 9.81, 0.01, -0.02, math.radians(12.0), 0, 0, 0, 0.02, 0.02, 0.04)
    return anpp.build(20, body)


def raw_packet(i):
    a = 0.002 * math.sin(i * 0.01)
    return anpp.build(28, struct.pack("<12f", -0.1 + a, 0.01, -9.81 + a, 0.001, -0.002, 0.0005 + a,
                                      0.2, 0.0, 0.4, 31.5, 101325.0, 30.0))


def obs_packet(t_whole, ns, rx, part, parts, sats):
    body = struct.pack("<IIiBBBB", t_whole, ns, -1000, rx, part, parts, len(sats))
    for sysid, prn, freqs in sats:
        body += struct.pack("<BBBHB", sysid, prn, 40, 120, len(freqs))
        for f in freqs:
            body += struct.pack("<BBddff", *f)
    return anpp.build(60, body)


def sats_for(t, rx):
    k = (t - T_LINE1) + rx * 0.3
    g = [(1, prn, [(1, 0x39, 113584732.815 + 1000 * prn + 1575.42 * k, 21614456.178 + 10 * prn + 0.19 * k,
                    439.8, 42.0), (5, 0x39, 88507000.1 + prn + 1227.6 * k, 21614456.9 + 10 * prn, 342.1, 38.0)])
         for prn in (3, 8, 14)]
    e = [(4, 5, [(1, 0x39, 140358487.9 + 1575.42 * k, 26709332.3, -1278.6, 41.0),
                 (9, 0x39, 104812345.6, 26709325.5, -980.3, 36.0)])]
    r = [(2, 7, [(1, 0x39, 120000000.0 + 1602.0 * k, 20100000.0, 12.0, 40.0)])]
    return g + e + r


def ephemeris_packet(prn, t):
    gps = t - 315964800 + 18
    week = int(gps // 604800)
    toe = int((gps % 604800) // 7200 * 7200)
    b = bytearray(132)
    struct.pack_into("<IBBIHH", b, 0, int(t), 1, prn, toe, 50 + prn, 50 + prn)
    struct.pack_into("<fff", b, 14, 1.2e-5, 3.4e-12, 0.0)
    struct.pack_into("<ffdfdf", b, 26, 12.5, 4.5e-9, 1.1, 2.0e-6, 0.005 + prn * 1e-4, 8.0e-6)
    struct.pack_into("<d f d f d f", b, 58, 5153.6 + prn * 0.01, 1.0e-7, -2.1, 3.0e-8, 0.96, 220.0)
    struct.pack_into("<ddd f", b, 94, 0.7, -8.0e-9, 1.0e-10, -1.0e-8)
    struct.pack_into("<HIHH", b, 122, week, int(gps % 604800) - 30, 2, 0)
    return anpp.build(61, bytes(b))


def device_packet():
    return anpp.build(3, struct.pack("<IIIIII", 2020, 26, 1100, 0x0A0B0C0D, 2, 3))


def hesai_packet(seq, t):
    p = bytearray(hashlib.shake_256(b"%d" % seq).digest(1080))
    p[0], p[1] = 0xEE, 0xFF
    p[9] = 4
    g = time.gmtime(int(t))
    struct.pack_into("<6BI", p, 1052 + 13, g.tm_year - 1900, g.tm_mon, g.tm_mday, g.tm_hour, g.tm_min, g.tm_sec,
                     int(round((t % 1) * 1e6)) % 1000000)
    struct.pack_into("<I", p, 1076, seq)
    return bytes(p)


def hesai_gps(t):
    p = bytearray(512)
    p[0], p[1] = 0xFF, 0xEE
    return bytes(p)


def certus_stream(t0, t1, invalid_first=0, rate=200, obs=True):
    """[(t, packet bytes)] of the Certus stream between t0 and t1."""
    out = [(t0, device_packet())]
    out += [(t0, ephemeris_packet(prn, t0)) for prn in (3, 8, 14)]
    n = int(round((t1 - t0) * rate))
    for i in range(n):
        t = t0 + i / rate
        out.append((t, state_packet(t, valid=i >= invalid_first)))
        out.append((t, raw_packet(i)))
        if obs and i >= invalid_first and i % (rate // 8) == 0:
            tw = int(round(t * 8)) / 8.0
            whole, ns = int(tw), int(round((tw % 1) * 1e9))
            for rx in (0, 1):
                ss = sats_for(tw, rx)
                out.append((t, obs_packet(whole, ns, rx, 1, 2, ss[:3])))
                out.append((t, obs_packet(whole, ns, rx, 2, 2, ss[3:])))
    return out


def hardware(line=True):
    hw = {"ins_to_primary_antenna_m": PRIMARY, "ins_to_secondary_antenna_m": SECONDARY, "ins_to_lidar_m": LIDAR,
          "lidar_mount_navaq_deg": MOUNT, "certus_alignment_deg": {"roll": 180, "pitch": 45, "heading": 0},
          "devices": {"certus_ip": "10.1.0.10", "lidar_ip": "10.1.0.201"},
          "certus_model": "Advanced Navigation Certus Evo OEM", "certus_reported": {}}
    if line:
        hw["coordinate_system"] = {"type": "EPSG", "definition": "EPSG:2953"}
        hw["vehicle"] = {"type": "van"}
        hw["ins_to_rear_axle_m"] = [-2.1, 0.0, 0.6]
    return hw


def survey_xml(vehicle):
    from RemixPro.core import surveyconfig
    return surveyconfig.build({"primary_antenna": PRIMARY, "secondary_antenna": SECONDARY, "lidar_offset": LIDAR,
                               "lidar_mount": MOUNT}, vehicle=vehicle,
                              geodesy={"mode": "EPSG", "definition": "EPSG:2953"})


def write_line(lines_dir, project, line, t0, seconds, split_bytes=None, old_name=None, points_hz=600):
    """One survey line, as the mapper records it."""
    digits = 3
    if old_name:
        sid = old_name
        prefix = None
    else:
        sid = "%s_L%03d_%s" % (project, line, time.strftime("%Y%m%d_%H%M%S", time.gmtime(t0)))
        prefix = sid
    sdir = os.path.join(lines_dir, sid)
    for sub in ("clraw", "ins", "lidar", "processed"):
        os.makedirs(os.path.join(sdir, sub), exist_ok=True)
    vehicle = "CLM Van 01"
    settings = {"ins_to_primary_antenna_m": PRIMARY, "ins_to_secondary_antenna_m": SECONDARY,
                "ins_to_lidar_m": LIDAR, "lidar_mount_navaq_deg": MOUNT}
    sess = {"id": sid, "name": project, "kind": "survey", "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(t0)), "started_unix": t0, "clock_source": "INS (GNSS time)", "settings": settings,
            "rtk": {"enabled": True, "caster": "ntrip.example.ca", "mountpoint": "FRDN"}}
    if not old_name:
        sess.update({"project": {"id": project, "name": project}, "line": line, "line_digits": digits,
                     "file_prefix": sid, "layout": "2.10"})
    sess_bytes = json.dumps(sess, indent=1).encode()
    with open(os.path.join(sdir, "session.json"), "wb") as f:
        f.write(sess_bytes)
    md = {"format": "clraw", "format_version": "1", "mapper_version": "2.10.0", "capture": sid,
          "capture_name": project, "line": line, "line_digits": digits, "vehicle": vehicle,
          "coordinate_system": "EPSG:2953", "lidar_model": "PandarXT-32"}
    if not old_name:
        md.update({"project": project, "project_id": project})
    atts = [("beamworx_survey_config.xml", "application/xml", survey_xml(vehicle)),
            ("hardware.json", "application/json", json.dumps(hardware(), indent=1).encode()),
            ("session.json", "application/json", sess_bytes)]
    rec = clraw.Recorder(os.path.join(sdir, "clraw"), line, md, atts, split_bytes=split_bytes, digits=digits,
                         name_ns=int(t0 * NS), prefix=prefix)
    t_start, t_stop = t0, t0 + seconds
    stream = certus_stream(t_start - 3.0, t_stop + 3.0, invalid_first=0)
    with open(os.path.join(sdir, "ins", "ins_0001.anpp"), "wb") as f:
        f.write(b"".join(p for _t, p in stream))
    with open(os.path.join(sdir, "ins", "ephemeris_cache.anpp"), "wb") as f:
        f.write(b"".join(ephemeris_packet(prn, t0 - 3600) for prn in (1, 3, 8, 14, 22)))
    msgs = [(t, clraw.CH_ANPP, p) for t, p in stream]
    seq = 0
    n = int(seconds * points_hz)
    for i in range(n):
        t = t_start + i / points_hz
        msgs.append((t + 1e-5, clraw.CH_POINTS, hesai_packet(seq, t)))
        seq += 1
    for s in range(int(seconds) + 1):
        msgs.append((t_start + s + 2e-5, clraw.CH_STATUS, hesai_gps(t_start + s)))
    msgs.sort(key=lambda m: m[0])
    started = False
    stopped = False
    for t, ch, data in msgs:
        if not started and t >= t_start:
            rec.put(clraw.CH_EVENTS, int(t_start * NS), int(t_start * NS),
                    json.dumps({"event": "clraw_start", "t": round(t_start, 6)}).encode())
            started = True
        if not stopped and t > t_stop:
            end = int(t_stop * NS)
            rec.hold_split = True
            rec.put(clraw.CH_EVENTS, end, end, json.dumps({"event": "clraw_stop", "t": round(t_stop, 6)}).encode())
            stopped = True
        if stopped and ch == clraw.CH_POINTS:
            continue
        lt = int(round(t * NS))
        rec.put(ch, lt, clraw.sensor_time_ns(ch, data, lt), data)
    files = rec.close()
    for name in ("events.jsonl", "quality.jsonl"):
        with open(os.path.join(sdir, name), "w") as f:
            f.write(json.dumps({"event": "start", "t": t_start}) + "\n")
    with open(os.path.join(sdir, "continuity.json"), "w") as f:
        json.dump({"ok": True}, f)
    with open(os.path.join(sdir, "manifest.sha256"), "w") as f:
        for root, _d, fs in os.walk(sdir):
            for fn in sorted(fs):
                if fn != "manifest.sha256":
                    p = os.path.join(root, fn)
                    with open(p, "rb") as g:
                        digest = hashlib.sha256(g.read()).hexdigest()
                    f.write("%s  %s\n" % (digest, os.path.relpath(p, sdir).replace(os.sep, "/")))
    return sdir, [os.path.join(sdir, "clraw", n) for n in files]


def write_nav(pdir, project, t0, t1):
    prefix = "%s_NAV_%s" % (project, time.strftime("%Y%m%d_%H%M%S", time.gmtime(t0)))
    nd = os.path.join(pdir, "nav")
    os.makedirs(nd, exist_ok=True)
    stream = certus_stream(t0, t1, invalid_first=40, rate=100)
    with open(os.path.join(nd, prefix + "_0001.anpp"), "wb") as f:
        f.write(b"".join(p for _t, p in stream))
    with open(os.path.join(nd, prefix + "_ephemeris.anpp"), "wb") as f:
        f.write(b"".join(ephemeris_packet(prn, t0 - 3600) for prn in (3, 8, 14)))
    with open(os.path.join(nd, "index_%s.json" % prefix), "w") as f:
        json.dump({"files": [prefix + "_0001.anpp"]}, f)
    md = {"format": "clraw", "format_version": "1", "mapper_version": "2.10.0", "kind": "navigation",
          "capture": prefix, "capture_name": project, "project": project, "project_id": project, "line": 0,
          "line_digits": 3, "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0))}
    rec = clraw.Recorder(nd, 0, md, [("hardware.json", "application/json",
                                      json.dumps(hardware(line=False), indent=1).encode())], prefix=prefix)
    rec.put(clraw.CH_EVENTS, int(t0 * NS), int(t0 * NS), json.dumps({"event": "clraw_start", "t": t0}).encode())
    for t, p in stream:
        lt = int(round(t * NS))
        rec.put(clraw.CH_ANPP, lt, clraw.sensor_time_ns(clraw.CH_ANPP, p, lt), p)
    rec.close()
    return prefix


def make_project(root, name="Deck_Test", seconds=(12.0, 6.0, 4.0), split_bytes=3 << 20):
    pdir = os.path.join(root, name)
    lines_dir = os.path.join(pdir, "lines")
    os.makedirs(lines_dir, exist_ok=True)
    with open(os.path.join(pdir, "project.json"), "w") as f:
        json.dump({"name": name, "id": name, "created": "2026-11-04T12:30:00Z", "next_line": 3}, f)
    out = {"project": pdir, "lines": []}
    out["old"] = write_line(lines_dir, name, 3, T_LINE1 - 900, seconds[2], old_name="20261104_123012_old")
    out["lines"].append(write_line(lines_dir, name, 1, T_LINE1, seconds[0], split_bytes=split_bytes))
    out["lines"].append(write_line(lines_dir, name, 2, T_LINE1 + 60, seconds[1]))
    out["nav"] = write_nav(pdir, name, T_LINE1 - 20, T_LINE1 + 75)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("outdir")
    ap.add_argument("--name", default="Deck_Test")
    a = ap.parse_args()
    r = make_project(a.outdir, a.name)
    print(json.dumps(r, indent=1))
