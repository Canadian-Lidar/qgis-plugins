"""A simulated processed PosPro project for the editor tests and guide figures: a 6 min drive
around a curving road at 200 Hz (trajectory.bin, trajectory_original.bin, qc.csv), with two bad
stretches - a height spike and a sideways jump while float - like wrong GNSS fixes."""
import csv
import math
import os
import struct

from PosPro.core import review

LAT0, LON0, H0 = math.radians(45.7300), math.radians(-65.5200), 30.0


def path(t):
    """Truth: north, east, up (m) and velocity (m/s) at t seconds: a road that winds."""
    v = 11.0
    x = v * t
    n = x * 0.6 + 40.0 * math.sin(x / 180.0)
    e = x * 0.8 + 60.0 * math.sin(x / 260.0)
    dn = v * (0.6 + 40.0 / 180.0 * math.cos(x / 180.0))
    de = v * (0.8 + 60.0 / 260.0 * math.cos(x / 260.0))
    u = 2.0 * math.sin(x / 400.0)
    du = v * 2.0 / 400.0 * math.cos(x / 400.0)
    return (n, e, u), (dn, de, du)


BAD = ((100.0, 112.0, "height"), (230.0, 246.0, "sideways"))


def make(work_dir, t0=1478000000.0, seconds=360.0, rate=200):
    d = os.path.join(work_dir, "results")
    os.makedirs(d, exist_ok=True)
    M, N = review._radii(LAT0)
    n = int(seconds * rate)
    recs = []
    for i in range(n):
        ts = i / rate
        (pn, pe, pu), (vn, ve, vu) = path(ts)
        st, sd = 6, 0.012
        for a, b, kind in BAD:
            if a <= ts <= b:
                k = math.sin(math.pi * (ts - a) / (b - a))
                if kind == "height":
                    pu += 1.6 * k + 0.4 * math.sin(ts * 3.1)
                else:
                    pe += 2.2 * k * math.cos(ts * 0.7)
                st, sd = 5, 0.35
        vals = [t0 + ts, LAT0 + pn / M, LON0 + pe / (N * math.cos(LAT0)), H0 + pu, vn, ve, -vu,
                0.01, 0.0, math.atan2(ve, vn), sd, sd, sd * 1.5] + [0.0] * 21 + [st, 14, 30, 0, 0.0]
        recs.append(vals)
    blob = b"PPTRJ001" + struct.pack("<Q", n) + struct.pack("<3d", 0, 0, 0) + struct.pack("<ii", 2, 0) + b"\0" * 24
    blob += b"".join(review.TRJ.pack(*r) for r in recs)
    for name in ("trajectory.bin", "trajectory_original.bin"):
        with open(os.path.join(d, name), "wb") as f:
            f.write(blob)
    cols = ["gps_time", "lat_deg", "lon_deg", "h_m", "roll_deg", "pitch_deg", "heading_deg", "vn", "ve", "vd", "speed",
            "sd_n", "sd_e", "sd_d", "sd_roll_deg", "sd_pitch_deg", "sd_heading_deg", "sep_n", "sep_e", "sep_d",
            "sep_h", "ratio", "gap_s", "bax_mg", "bay_mg", "baz_mg", "bgx_dh", "bgy_dh", "bgz_dh",
            "status", "fwd_status", "bwd_status", "nsat", "zupt"]
    for name in ("qc.csv", "qc_original.csv"):
        with open(os.path.join(d, name), "w", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(cols)
            for r in recs[::rate]:
                sep = 0.3 if r[10] > 0.1 else 0.005
                w.writerow(["%.3f" % r[0], "%.9f" % math.degrees(r[1]), "%.9f" % math.degrees(r[2]), "%.4f" % r[3],
                            0, 0, math.degrees(r[9]), r[4], r[5], r[6], math.hypot(r[4], r[5]),
                            r[10], r[11], r[12], 0.01, 0.01, 0.05, sep, sep, sep, sep * 1.4, 30, 0, 0, 0, 0, 0, 0, 0,
                            r[34], r[34], r[34], 14, 0])
    return t0, recs


def truth_local(ts):
    return path(ts)[0]
