"""Read results (qc.csv, rt.bin) and summarise quality. No GUI here."""
import csv
import math
import os
import struct

STATUS = {6: "Fixed", 5: "Float", 4: "PPP", 3: "Code (DGNSS)", 2: "Single", 1: "INS only", 0: "None"}
# the Mobile Mapper's GNSS solution colours: fixed mint, float blue, DGNSS
# amber, single red, INS only violet
STATUS_COLOURS = {6: "#00e5aa", 5: "#3ab4f2", 4: "#7ee0ff", 3: "#f5a623", 2: "#e84855", 1: "#9b7bff", 0: "#5d857c"}
QUALITY_CLASSES = [(0.0, 0.02, "< 2 cm", "#00e5aa"), (0.02, 0.05, "2–5 cm", "#8ef0cf"),
                   (0.05, 0.10, "5–10 cm", "#3ab4f2"), (0.10, 0.30, "10–30 cm", "#f5a623"),
                   (0.30, 0.60, "30–60 cm", "#ff7a45"), (0.60, 1e9, "> 60 cm", "#e84855")]

FLOAT_COLS = ("gps_time", "lat_deg", "lon_deg", "h_m", "roll_deg", "pitch_deg", "heading_deg", "vn", "ve", "vd",
              "speed", "sd_n", "sd_e", "sd_d", "sd_roll_deg", "sd_pitch_deg", "sd_heading_deg", "sep_n", "sep_e",
              "sep_d", "sep_h", "ratio", "gap_s", "bax_mg", "bay_mg", "baz_mg", "bgx_dh", "bgy_dh", "bgz_dh")
INT_COLS = ("status", "fwd_status", "bwd_status", "nsat", "zupt")


def load_qc(path):
    cols = {k: [] for k in FLOAT_COLS + INT_COLS}
    if not os.path.exists(path):
        return None
    with open(path, newline="") as fh:
        rd = csv.DictReader(fh)
        for row in rd:
            try:
                for k in FLOAT_COLS:
                    cols[k].append(float(row[k]))
                for k in INT_COLS:
                    cols[k].append(int(row[k]))
            except (KeyError, ValueError):
                continue
    if not cols["gps_time"]:
        return None
    cols["sd_h"] = [math.hypot(a, b) for a, b in zip(cols["sd_n"], cols["sd_e"])]
    cols["sd_3d"] = [math.sqrt(a * a + b * b + c * c) for a, b, c in zip(cols["sd_n"], cols["sd_e"], cols["sd_d"])]
    return cols


def _runs(t, flags):
    """Contiguous runs where flags is true -> list of (t0, t1)."""
    out = []
    start = None
    for i, f in enumerate(flags):
        if f and start is None:
            start = i
        elif not f and start is not None:
            out.append((t[start], t[i - 1]))
            start = None
    if start is not None:
        out.append((t[start], t[-1]))
    return out


def findings(qc, engine_summary=None):
    """Plain-language quality findings: list of (level, text, t0, t1)."""
    out = []
    if not qc:
        return out
    n = len(qc["gps_time"])
    t = qc["gps_time"]
    share = {s: 100.0 * sum(1 for v in qc["status"] if v == s) / n for s in STATUS}
    fixed = share.get(6, 0)
    if fixed >= 95:
        out.append(("ok", "%.1f%% of the drive has fixed ambiguities." % fixed, None, None))
    elif fixed >= 75:
        out.append(("warn", "Only %.1f%% of the drive is fixed; check the base distance, sky view and "
                            "elevation mask." % fixed, None, None))
    else:
        out.append(("bad", "Only %.1f%% of the drive is fixed." % fixed, None, None))
    gaps = _runs(t, [s == 1 for s in qc["status"]])
    gaps = sorted(gaps, key=lambda g: g[1] - g[0], reverse=True)
    if gaps and gaps[0][1] - gaps[0][0] > 1:
        g = gaps[0]
        lvl = "ok" if g[1] - g[0] < 20 else "warn" if g[1] - g[0] < 60 else "bad"
        out.append((lvl, "Longest GNSS gap bridged by the IMU: %.0f s (%d gaps over 2 s)." %
                    (g[1] - g[0], sum(1 for q in gaps if q[1] - q[0] > 2)), g[0], g[1]))
    sh = [v for v in qc["sep_h"] if v > 0]
    if sh:
        s_sorted = sorted(sh)
        p95 = s_sorted[int(0.95 * (len(s_sorted) - 1))]
        lvl = "ok" if p95 < 0.05 else "warn" if p95 < 0.2 else "bad"
        out.append((lvl, "Forward/backward separation: 95%% below %.3f m horizontally, maximum %.3f m." %
                    (p95, s_sorted[-1]), None, None))
        bad = _runs(t, [v > 0.10 for v in qc["sep_h"]])
        bad = sorted(bad, key=lambda g: g[1] - g[0], reverse=True)[:5]
        for a, b in bad:
            if b - a >= 1:
                out.append(("warn", "Passes disagree by more than 10 cm for %.0f s." % (b - a), a, b))
    big = _runs(t, [v > 0.10 for v in qc["sd_3d"]])
    big = sorted(big, key=lambda g: g[1] - g[0], reverse=True)[:3]
    for a, b in big:
        if b - a >= 1:
            out.append(("warn", "Estimated 3D accuracy worse than 10 cm for %.0f s." % (b - a), a, b))
    if engine_summary:
        try:
            ba = engine_summary.get("Forward pass", {}).get("final_acc_bias_mg")
            bg = engine_summary.get("Forward pass", {}).get("final_gyro_bias_deg_h")
            if ba and bg:
                out.append(("info", "Estimated IMU biases at the end: accelerometers %s mg, gyros %s deg/h." %
                            (ba.replace(",", ", "), bg.replace(",", ", ")), None, None))
        except AttributeError:
            pass
    return out


def status_shares(qc):
    if not qc:
        return []
    n = len(qc["status"])
    out = []
    for s in (6, 5, 4, 3, 2, 1):
        k = sum(1 for v in qc["status"] if v == s)
        if k:
            out.append((STATUS[s], 100.0 * k / n, STATUS_COLOURS[s]))
    return out


RT_FMT = struct.Struct("<dddd3f3f3f3fHH")


def load_rt(path, step_s=1.0):
    """Real-time (packet 20) positions, decimated: list of (t, lat, lon, h, fix)."""
    out = []
    if not os.path.exists(path):
        return out
    with open(path, "rb") as fh:
        hdr = fh.read(64)
        if hdr[:8] != b"PPRT0001":
            return out
        last = -1e9
        while True:
            b = fh.read(RT_FMT.size * 4096)
            if not b:
                break
            for rec in RT_FMT.iter_unpack(b[:len(b) - len(b) % RT_FMT.size]):
                t = rec[0]
                if t - last < step_s - 1e-6:
                    continue
                last = t
                out.append((t, math.degrees(rec[1]), math.degrees(rec[2]), rec[3], (rec[-1] >> 4) & 7))
    return out


def summary_text(proj_cfg):
    r = proj_cfg.get("results") or {}
    c = r.get("combine") or {}
    lines = []
    if c:
        lines.append("Epochs %s, fixed %s%%, float %s%%, INS only %s%%" % (
            c.get("epochs"), c.get("fixed_pct"), c.get("float_pct"), c.get("ins_only_pct")))
        lines.append("Separation RMS %s m, 95%% %s m" % (c.get("sep_h_rms_m"), c.get("sep_h_p95_m")))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# data coverage (timescale bars)
# ---------------------------------------------------------------------------
# Certus real-time fix type (packet 20) -> mapper solution colour
RT_FIX = {7: ("RTK fixed", "#00e5aa"), 6: ("RTK float", "#3ab4f2"), 5: ("PPP", "#7ee0ff"),
          4: ("DGNSS", "#f5a623"), 3: ("SBAS", "#f5a623"), 2: ("3D", "#e84855"), 1: ("2D", "#e84855"),
          0: ("No fix", "#5d857c")}


def _cov_runs(t0, vals, classify):
    """Per-second values -> [(t_start, t_end, colour, label)] runs."""
    out = []
    for i, v in enumerate(vals):
        c = classify(v)
        if out and out[-1][2] == c[0] and out[-1][1] == t0 + i:
            out[-1][1] = t0 + i + 1
        else:
            out.append([t0 + i, t0 + i + 1, c[0], c[1]])
    return out


def load_coverage(path, imu_rate=None):
    """coverage.csv from the extractor -> {"t0", "t1", "rows": [(name, runs)]}.
    Colours: mint good, amber degraded, red missing."""
    if not os.path.exists(path):
        return None
    t, imu, s1, s2, fx = [], [], [], [], []
    with open(path, newline="") as fh:
        rd = csv.reader(fh)
        next(rd, None)
        for row in rd:
            try:
                t.append(int(float(row[0])))
                imu.append(int(row[1]))
                s1.append(int(row[2]))
                s2.append(int(row[3]))
                fx.append(int(row[4]))
            except (ValueError, IndexError):
                continue
    if not t:
        return None
    # rows are contiguous per second from the extractor; fill any holes
    t0 = t[0]
    n = t[-1] - t0 + 1
    A = {"imu": [0] * n, "s1": [0] * n, "s2": [0] * n, "fx": [-1] * n}
    for k, ti in enumerate(t):
        i = ti - t0
        A["imu"][i], A["s1"][i], A["s2"][i], A["fx"][i] = imu[k], s1[k], s2[k], fx[k]
    rate = imu_rate or (sorted(A["imu"])[n // 2] or 1)
    good, warn, bad = "#00e5aa", "#f5a623", "#e84855"

    def c_imu(v):
        if v == 0:
            return bad, "IMU gap"
        return (good, "full rate (%d/s)" % rate) if v >= 0.9 * rate else (warn, "samples missing (below %d/s)" % rate)

    def c_sat(v):
        if v == 0:
            return bad, "no observations"
        return (good, "5 or more satellites") if v >= 5 else (warn, "fewer than 5 satellites")

    def c_fix(v):
        if v < 0:
            return bad, "no real-time solution"
        lab, col = RT_FIX.get(v, ("?", "#5d857c"))
        return col, lab
    rows = [("IMU", _cov_runs(t0, A["imu"], c_imu)), ("GNSS 1", _cov_runs(t0, A["s1"], c_sat))]
    if any(A["s2"]):
        rows.append(("GNSS 2", _cov_runs(t0, A["s2"], c_sat)))
    rows.append(("Real-time", _cov_runs(t0, A["fx"], c_fix)))
    return {"t0": t0, "t1": t0 + n, "rows": rows}


def base_runs(base, t0, t1):
    """Coverage runs of a base station over [t0, t1]: mint where it has
    data, amber where it tracks fewer than 5 satellites, red where missing."""
    seg = sorted(base.get("coverage") or [])
    if not seg and base.get("first") and base.get("last"):
        import datetime
        g0 = datetime.datetime(1980, 1, 6)
        try:
            seg = [[(datetime.datetime.fromisoformat(base["first"]) - g0).total_seconds(),
                    (datetime.datetime.fromisoformat(base["last"]) - g0).total_seconds()]]
        except ValueError:
            seg = []
    dt = float(base.get("interval") or 1.0)
    out, cur = [], t0
    for a, b in seg:
        b = b + dt
        if b <= t0 or a >= t1:
            continue
        a, b = max(a, t0), min(b, t1)
        if a > cur:
            out.append([cur, a, "#e84855", "no base data"])
        out.append([a, b, "#00e5aa", "base data (%g s)" % dt])
        cur = max(cur, b)
    if cur < t1:
        out.append([cur, t1, "#e84855", "no base data"])
    for a, b in base.get("low_sats") or []:
        if b + dt > t0 and a < t1:
            out.append([max(a, t0), min(b + dt, t1), "#f5a623", "base tracks fewer than 5 satellites"])
    return out
