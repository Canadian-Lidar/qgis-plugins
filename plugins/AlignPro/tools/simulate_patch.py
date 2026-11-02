#!/usr/bin/env python3
"""Synthetic Lidar patch test in the mapper's .clraw format.

Builds a small street scene (tilted ground, three buildings, poles), drives N passes past it
(reciprocal pairs at increasing distance), ray-traces a Hesai XT-32 (dual return, 600 rpm)
mounted with a KNOWN boresight error, and writes one capture folder per pass exactly like the
mapper: clraw/NNN_YYYYMMDD_HHMMSS_0001.clraw with Certus packet 20/26 at 200 Hz, Lidar
status packets, events, quality and all four attachments. The truth trajectory is also
written as an SBET (sbet_YYYYMMDD.out) so the post-processed path can be tested.

Usage: python3 simulate_patch.py OUTDIR [--seconds-scale 1.0] [--mount 90 45 0]
                                         [--bore 0.20 -0.30 0.45] [--rt-error 0.03]
Requires: numpy, mcap (pip install mcap).
"""
import argparse
import datetime as dt
import json
import math
import os
import struct
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from alignpro.core import geo  # noqa: E402
from alignpro.core.trajectory import Trajectory, write_sbet  # noqa: E402

from mcap.writer import Writer, CompressionType  # noqa: E402

CAL = """Laser id,Elevation,Azimuth
1,14.916,0.506
2,13.899,0.503
3,12.891,0.520
4,11.886,0.519
5,10.882,0.509
6,9.882,0.506
7,8.882,0.502
8,7.879,0.495
9,6.881,0.490
10,5.881,0.484
11,4.884,0.482
12,3.890,0.471
13,2.896,0.465
14,1.900,0.459
15,0.909,0.454
16,-0.089,0.447
17,-1.084,0.441
18,-2.080,0.437
19,-3.079,0.432
20,-4.074,0.427
21,-5.071,0.417
22,-6.074,0.416
23,-7.077,0.416
24,-8.079,0.412
25,-9.080,0.402
26,-10.081,0.401
27,-11.087,0.397
28,-12.096,0.391
29,-13.100,0.372
30,-14.112,0.364
31,-15.132,0.361
32,-16.160,0.356
"""
CAL_ROWS = [tuple(float(x) for x in ln.split(",")[1:]) for ln in CAL.strip().splitlines()[1:]]
ORIGIN = (45.72680, -65.51000, 12.0)
LEVER = (0.45, 0.0, -0.85)          # F, S, D  Certus -> Lidar optical centre
REF_HEIGHT = 1.9                     # Certus reference point above ground


# ------------------------------------------------------------------ scene
def ground_z(e, n):
    return 0.01 * e + 0.02 * n


BOXES = [  # centre E, N, length, width, height, yaw deg (E axis rotated anticlockwise)
    (10.0, 19.0, 24.0, 10.0, 7.0, 20.0),
    (-25.0, -21.0, 16.0, 9.0, 5.0, -10.0),
    (40.0, -15.0, 6.0, 4.0, 3.0, 45.0),
]
POLES = [(-5.0, 9.0), (22.0, 9.5), (-35.0, -9.0), (45.0, 8.0), (-48.0, 9.0)]
POLE_R, POLE_H = 0.15, 7.0


def intersect(o, d, rmax=90.0):
    """o, d (N,3). -> range (N,) (inf = no hit), surface code (N,)."""
    n = len(o)
    best = np.full(n, np.inf)
    code = np.zeros(n, np.uint8)
    # ground plane z = a e + b n  ->  (0.01, 0.02, -1) . x = 0
    gn = np.array([0.01, 0.02, -1.0])
    den = d @ gn
    with np.errstate(divide="ignore", invalid="ignore"):
        t = -(o @ gn) / den
    ok = (t > 0.2) & (t < best)
    best[ok], code[ok] = t[ok], 1
    for i, (ce, cn, L, W, H, yaw) in enumerate(BOXES):
        z0 = ground_z(ce, cn) - 1.0
        c, s = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
        R = np.array([[c, s, 0], [-s, c, 0], [0, 0, 1]])      # world -> box
        ob = (o - [ce, cn, z0]) @ R.T
        db = d @ R.T
        lo = np.array([-L / 2, -W / 2, 0.0])
        hi = np.array([L / 2, W / 2, H + 1.0])
        with np.errstate(divide="ignore", invalid="ignore"):
            t1 = (lo - ob) / db
            t2 = (hi - ob) / db
        tmin = np.nanmax(np.minimum(t1, t2), 1)
        tmax = np.nanmin(np.maximum(t1, t2), 1)
        ok = (tmax >= tmin) & (tmin > 0.2) & (tmin < best)
        best[ok], code[ok] = tmin[ok], 2 + i
    for (pe, pn) in POLES:
        z0 = ground_z(pe, pn) - 0.5
        ox, oy = o[:, 0] - pe, o[:, 1] - pn
        a = d[:, 0] ** 2 + d[:, 1] ** 2
        b = 2 * (ox * d[:, 0] + oy * d[:, 1])
        cc = ox ** 2 + oy ** 2 - POLE_R ** 2
        disc = b * b - 4 * a * cc
        with np.errstate(invalid="ignore", divide="ignore"):
            t = (-b - np.sqrt(disc)) / (2 * a)
        z = o[:, 2] + t * d[:, 2]
        ok = (disc > 0) & (t > 0.2) & (t < best) & (z > z0) & (z < z0 + POLE_H + 0.5)
        best[ok], code[ok] = t[ok], 9
    best[best > rmax] = np.inf
    return best, code


# -------------------------------------------------------------- vehicle
class Pass:
    def __init__(self, idx, north, eastbound, speed, t0, x0=-62.0, x1=62.0):
        self.idx, self.north, self.east = idx, north, eastbound
        self.speed, self.t0 = speed, t0
        self.length = x1 - x0
        self.x0, self.x1 = x0, x1
        self.dur = self.length / speed
        self.hdg = math.radians(90.0 if eastbound else 270.0)

    def pose(self, t):
        """Truth at times t: ENU of the Certus reference point, roll, pitch, heading (rad)."""
        tau = np.asarray(t) - self.t0
        sgn = 1.0 if self.east else -1.0
        e = (self.x0 if self.east else self.x1) + sgn * self.speed * tau
        n = self.north + 0.15 * np.sin(2 * np.pi * tau / 9.0 + self.idx)
        u = ground_z(e, n) + REF_HEIGHT
        ph = self.idx * 0.7
        roll = np.radians(1.2 * np.sin(2 * np.pi * tau / 3.1 + ph))
        slope = math.atan(0.01) * sgn   # vehicle pitch follows the ground along track
        pitch = slope + np.radians(0.8 * np.sin(2 * np.pi * tau / 4.7 + 1 + ph))
        heading = self.hdg + np.radians(0.8 * np.sin(2 * np.pi * tau / 6.0 + ph))
        return np.stack([e, n, u], -1), roll, pitch, heading


# ------------------------------------------------------------- packets
def anpp_packet(pid, payload):
    from alignpro.core.anpp import crc16
    crc = crc16(payload)
    hdr = [pid, len(payload), crc & 0xFF, crc >> 8]
    lrc = (-sum(hdr)) & 0xFF
    return bytes([lrc] + hdr) + payload


_p20 = struct.Struct("<HHII3d3f3ff3f3f3f")


def hesai_packets(t_pkt, az, dist, inten, seq0):
    """t_pkt (N,), az (N,4) centi-deg, dist/inten (N,4,32) -> (N,1080) uint8."""
    n = len(t_pkt)
    a = np.zeros((n, 1080), np.uint8)
    a[:, 0], a[:, 1], a[:, 2], a[:, 3] = 0xEE, 0xFF, 6, 1
    a[:, 6], a[:, 7], a[:, 8], a[:, 9], a[:, 10] = 32, 8, 0x38, 4, 2
    for b in range(8):
        f = b // 2
        base = 12 + 130 * b
        a[:, base] = az[:, f] & 0xFF
        a[:, base + 1] = az[:, f] >> 8
        k = np.arange(32)
        off = base + 2 + 4 * k
        a[:, off] = (dist[:, f] & 0xFF).astype(np.uint8)
        a[:, off + 1] = (dist[:, f] >> 8).astype(np.uint8)
        a[:, off + 2] = inten[:, f]
    a[:, 1062] = 0x39
    a[:, 1063], a[:, 1064] = 600 & 0xFF, 600 >> 8
    for i in range(n):
        tt = dt.datetime.fromtimestamp(math.floor(t_pkt[i]), dt.timezone.utc)
        a[i, 1065:1071] = [tt.year - 1900, tt.month, tt.day, tt.hour, tt.minute, tt.second]
    us = np.round((t_pkt - np.floor(t_pkt)) * 1e6).astype("<u4")
    a[:, 1071:1075] = us.view(np.uint8).reshape(-1, 4)
    sq = (seq0 + np.arange(n)).astype("<u4")
    a[:, 1076:1080] = sq.view(np.uint8).reshape(-1, 4)
    return a


def status_packet(t):
    b = bytearray(512)
    b[0], b[1] = 0xFF, 0xEE
    tt = dt.datetime.fromtimestamp(math.floor(t), dt.timezone.utc)
    nmea = "$GPRMC,%02d%02d%02d.00,A,4543.6080,N,06530.6000,W,0.0,0.0,%02d%02d%02d,,,A*00" % (
        tt.hour, tt.minute, tt.second, tt.day, tt.month, tt.year % 100)
    b[18:18 + len(nmea)] = nmea.encode()
    b[506], b[507] = 0x41, 1
    return bytes(b)


# --------------------------------------------------------------- writer
def write_pass(outdir, ps, frame, m_true, m_entered, rt_err, rng, scale):
    t0 = ps.t0
    t1 = t0 + ps.dur
    stamp = dt.datetime.fromtimestamp(t0, dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
    cap = "%s_PatchTest" % stamp
    line = ps.idx
    cdir = os.path.join(outdir, cap, "clraw")
    os.makedirs(cdir, exist_ok=True)
    fname = "%03d_%s_0001.clraw" % (line, stamp)
    path = os.path.join(cdir, fname)
    hw = {
        "ins_to_primary_antenna_m": [-0.082, -0.495, 0.112],
        "ins_to_secondary_antenna_m": [-0.082, 0.493, 0.112],
        "ins_to_lidar_m": list(LEVER),
        "lidar_mount_navaq_deg": dict(zip(("roll", "pitch", "heading"), m_entered)),
        "certus_alignment_deg": {"roll": 180, "pitch": 45, "heading": 0},
        "coordinate_system": {"type": "EPSG", "definition": "EPSG:2953", "note": ""},
        "lidar_model": "Hesai PandarXT-32", "certus_model": "Certus Evo OEM",
        "vehicle": {"type": "van"},
    }
    session = {"id": cap, "name": "PatchTest", "line": line, "software": "mapper 2.9.0 (simulated)",
               "start_utc": dt.datetime.fromtimestamp(t0, dt.timezone.utc).isoformat()}
    with open(path, "wb") as fh:
        w = Writer(fh, chunk_size=4 * 1024 * 1024, compression=CompressionType.ZSTD)
        w.start(profile="clraw", library="mapper")
        w.add_metadata("clraw", {"format": "clraw", "format_version": "1", "mapper_version": "2.9.0",
                                 "capture": cap, "capture_name": "PatchTest", "line": str(line),
                                 "line_digits": "3", "file_number": "1", "file_name": fname,
                                 "vehicle": "CLM-Mapper-01 (sim)", "coordinate_system": "EPSG:2953",
                                 "lidar_model": "Hesai PandarXT-32", "lidar_ip": "10.1.0.201",
                                 "certus_ip": "10.1.0.10", "time": "publish_time = sensor time"})
        tns = int(t0 * 1e9)
        w.add_attachment(tns, tns, "beamworx_survey_config.xml", "application/xml", b"<SurveyConfig/>")
        w.add_attachment(tns, tns, "hardware.json", "application/json", json.dumps(hw).encode())
        w.add_attachment(tns, tns, "session.json", "application/json", json.dumps(session).encode())
        s1 = w.register_schema("clraw.HesaiPandarXT32.Point", "x-clraw-doc", b"Hesai packet")
        s2 = w.register_schema("clraw.HesaiPandarXT32.GpsStatus", "x-clraw-doc", b"Hesai status")
        s3 = w.register_schema("clraw.AdvancedNavigation.ANPP", "x-clraw-doc", b"ANPP")
        s4 = w.register_schema("clraw.Json", "jsonschema", b"{}")
        ch = [w.register_channel("/lidar/points", "x-hesai-pandar-udp", s1),
              w.register_channel("/lidar/status", "x-hesai-pandar-udp", s2),
              w.register_channel("/ins/anpp", "x-anpp", s3),
              w.register_channel("/capture/events", "json", s4),
              w.register_channel("/capture/quality", "json", s4)]
        assert ch == [1, 2, 3, 4, 5], ch
        seqs = [0] * 6

        def msg(c, t_log, t_pub, data):
            seqs[c] += 1
            w.add_message(c, int(t_log * 1e9), data, int(t_pub * 1e9), sequence=seqs[c])

        def ev(t, obj):
            obj = dict(obj)
            obj.setdefault("t", t)
            msg(4, t, t, json.dumps(obj).encode())

        ev(t0, {"event": "clraw_start"})
        ev(t0, {"event": "start", "name": "PatchTest", "kind": "survey"})
        nav_t0, nav_t1 = t0 - 3.0, t1 + 3.0
        cal_written = False
        mark_done = False
        hseq = 1000 * line
        sec = math.floor(nav_t0)
        cal = np.array(CAL_ROWS)
        while sec < nav_t1:
            a, b = max(sec, nav_t0), min(sec + 1.0, nav_t1)
            out = []   # (log_t, channel, pub_t, bytes)
            # ---- navigation 200 Hz
            tn = np.arange(math.ceil(a * 200) / 200.0, b, 0.005)
            if len(tn):
                pos, r, p, h = ps.pose(tn)
                pos = pos + rt_err[0] + rt_err[1] * (tn - t0)[:, None]
                lat, lon, hh = frame.to_geodetic(pos)
                pos2, *_ = ps.pose(tn + 0.01)
                v = (pos2 - ps.pose(tn)[0]) / 0.01          # ENU velocity
                for i in range(len(tn)):
                    sec_i = int(math.floor(tn[i]))
                    us = int(round((tn[i] - sec_i) * 1e6))
                    pl = _p20.pack(0, (1 | 2 | 4 | 8 | (7 << 4) | (1 << 9) | (1 << 10)), sec_i, us,
                                   lat[i], lon[i], hh[i], v[i, 1], v[i, 0], -v[i, 2], 0, 0, 0, 1.0,
                                   r[i] + rng.normal(0, 2e-5), p[i] + rng.normal(0, 2e-5),
                                   ((h[i] + rng.normal(0, 5e-5) + math.pi) % (2 * math.pi)) - math.pi,
                                   0, 0, 0, 0.012, 0.012, 0.02)
                    out.append((tn[i] + 0.002, 3, tn[i], anpp_packet(20, pl)))
                    out.append((tn[i] + 0.0021, 3, tn[i] + 0.0021,
                                anpp_packet(26, struct.pack("<3f", 3.5e-4, 3.5e-4, 1.4e-3))))
            if a <= sec + 0.5 < b:
                out.append((sec + 0.5, 2, sec + 0.5, status_packet(sec + 0.5)))
                q = {"t": sec + 0.5, "connected": True, "fix_name": "RTK fixed", "time_valid": True,
                     "satellites": {"hdop": 0.7, "vdop": 1.1, "total": 24}}
                out.append((sec + 0.5, 5, sec + 0.5, json.dumps(q).encode()))
            # ---- Lidar between t0 and t1
            la, lb = max(a, t0), min(b, t1)
            if lb > la:
                nf = int((lb - la) / 50e-6)
                nf -= nf % 4
                tf = la + np.arange(nf) * 50e-6                        # firing times
                azf = (tf * 3600.0) % 360.0                            # 600 rpm
                tray = (tf[:, None] + 1.512e-6 * np.arange(32)[None, :] + 3.56e-6).ravel()
                pos, r, p, h = ps.pose(tray)
                lat, lon, hh = frame.to_geodetic(pos)
                C = frame.vehicle_to_local(lat, lon, r, p, h)
                el = np.radians(cal[:, 0])[None, :]
                al = np.radians(np.round(azf * 100)[:, None] / 100.0 + cal[None, :, 1])
                ps_dir = np.stack([np.cos(el) * np.sin(al), np.cos(el) * np.cos(al),
                                   np.broadcast_to(np.sin(el), al.shape)], -1).reshape(-1, 3)
                dv = ps_dir @ m_true.T
                d = np.einsum("nij,nj->ni", C, dv)
                o = pos + np.einsum("nij,j->ni", C, np.array(LEVER))
                rr, code = intersect(o, d)
                rr = rr + rng.normal(0, 0.008, len(rr))
                dist = np.where(np.isfinite(rr), np.round(rr / 0.004), 0).astype(np.uint16)
                inten = np.where(code == 1, 30, np.where(code == 9, 120, 80)).astype(np.uint8)
                inten[dist == 0] = 0
                npk = nf // 4
                tlast = tf.reshape(npk, 4)[:, 3]
                # Hesai: point time = tp + 3.28 - 50(S-s) + 1.512k + 0.28 us, and the rays above
                # were cast at firing + 1.512k + 3.56 us, so the packet time is the last firing.
                tp = tlast
                azc = np.round(azf * 100).astype(np.uint16).reshape(npk, 4)
                pk = hesai_packets(tp, azc, dist.reshape(npk, 4, 32), inten.reshape(npk, 4, 32), hseq)
                hseq += npk
                for i in range(npk):
                    out.append((tp[i] + 0.001, 1, tp[i], pk[i].tobytes()))
                if not mark_done and np.any(np.abs(pos[:, 0] - 10.0) < 0.2):
                    tm = float(tray[np.argmin(np.abs(pos[:, 0] - 10.0))])
                    out.append((tm, 4, tm, json.dumps({"t": tm, "event": "mark", "n": 1,
                                                       "label": "Building 1"}).encode()))
                    mark_done = True
            out.sort(key=lambda x: x[0])
            for lt, c, pt, data in out:
                msg(c, lt, pt, data)
            if not cal_written and sec >= t0:
                tc = int((sec + 1.0) * 1e9)
                w.add_attachment(tc, tc, "lidar_calibration.csv", "text/csv", CAL.encode())
                cal_written = True
            sec += 1.0
        ev(t1, {"event": "stop", "reason": "operator"})
        ev(t1, {"event": "clraw_stop"})
        w.finish()
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("outdir")
    ap.add_argument("--mount", nargs=3, type=float, default=[90.0, 45.0, 0.0])
    ap.add_argument("--bore", nargs=3, type=float, default=[0.20, -0.30, 0.45],
                    help="true boresight error roll pitch heading (deg) in the vehicle frame")
    ap.add_argument("--rt-error", type=float, default=0.04,
                    help="real-time position error per pass (m, random) in packet 20")
    ap.add_argument("--passes", type=int, default=6)
    ap.add_argument("--start", default="2026-10-20T15:00:00")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    rng = np.random.default_rng(args.seed)
    os.makedirs(args.outdir, exist_ok=True)
    frame = geo.LocalFrame(*ORIGIN)
    m_entered = geo.lidar_matrix(*args.mount)
    br, bp, bh = (math.radians(v) for v in args.bore)
    B = geo.rz(bh) @ geo.ry(bp) @ geo.rx(br)
    m_true = B @ m_entered
    true_angles = geo.navaq_from_lidar_matrix(m_true, near=args.mount)
    t = dt.datetime.fromisoformat(args.start).replace(tzinfo=dt.timezone.utc).timestamp()
    plan = [(0.0, True, 8.0), (0.0, False, 8.0), (-4.0, True, 10.0), (-4.0, False, 10.0),
            (4.5, True, 6.0), (4.5, False, 6.0), (-8.0, True, 9.0), (-8.0, False, 9.0)][:args.passes]
    passes, paths = [], []
    for i, (n, east, v) in enumerate(plan):
        ps = Pass(i + 1, n, east, v, t)
        err = (rng.normal(0, args.rt_error, 3), rng.normal(0, args.rt_error / 30.0, 3))
        print("pass %d  N=%+.1f %s %.0f m/s  %.1f s" % (ps.idx, n, "E" if east else "W", v, ps.dur), flush=True)
        paths.append(write_pass(args.outdir, ps, frame, m_true, args.mount, err, rng, 1.0))
        passes.append(ps)
        t += ps.dur + 40.0
    # truth SBET at 200 Hz over everything (turns between passes are not needed)
    ts, segs = [], []
    for ps in passes:
        tt = np.arange(ps.t0 - 5.0, ps.t0 + ps.dur + 5.0, 0.005)
        pos, r, p, h = ps.pose(tt)
        lat, lon, hh = frame.to_geodetic(pos)
        segs.append((tt, lat, lon, hh, r, p, h))
    cat = [np.concatenate([s[k] for s in segs]) for k in range(7)]
    tr = Trajectory(*cat, source="truth")
    day = dt.datetime.fromtimestamp(passes[0].t0, dt.timezone.utc).strftime("%Y%m%d")
    write_sbet(os.path.join(args.outdir, "sbet_%s.out" % day), tr)
    truth = {"mount_entered": args.mount, "boresight_deg": args.bore, "mount_true": true_angles,
             "lever": LEVER, "origin": ORIGIN, "files": [os.path.relpath(p, args.outdir) for p in paths]}
    with open(os.path.join(args.outdir, "truth.json"), "w") as f:
        json.dump(truth, f, indent=1)
    print("true mounting (navaq_hesai): %.4f %.4f %.4f" % tuple(true_angles))


if __name__ == "__main__":
    main()
