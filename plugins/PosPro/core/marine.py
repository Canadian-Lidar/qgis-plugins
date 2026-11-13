"""Marine jobs (sonar recordings from the survey boat, .clraw kind "sonar"): the trajectory of the GNSS antenna and
each sonar head's attitude, finalized as dcs.trajectory/1 for AlignPro and CloudPro.

Position, from the best source the recording holds (Sean, 13 Nov 2026):
  1. PPK: PosPro's engine on the Reach rover RINEX against the base RINEX (forward and backward passes, combined
     here: both fixed -> averaged, else the better pass), the base at its surveyed position with its antenna
     height (to the ARP, from the base RINEX header) and antenna type;
  2. the rover's own solution log (.LLH, RTK in real time);
  3. the autopilot's position (MAVLink GLOBAL_POSITION_INT).
Heading: course over ground (from the position track) when the boat moves faster than 0.5 m/s, else the
autopilot's compass (MAVLink ATTITUDE yaw). Vessel roll / pitch: the autopilot's ATTITUDE.
Each sonar head's attitude: its own fused attitude (packet 504) at every ping; the 100 Hz raw IMU (505) is
checked against it.
"""
import json
import math
import os
import shutil

import numpy as np

COG_MIN_SPEED = 0.5            # m/s: below it the heading is the autopilot's compass
COG_SMOOTH_S = 2.0
RS2_PCO_UP_M = 0.134           # Emlid Reach RS2: mount (ARP) to the L1 phase centre, used when no ANTEX is at hand
POS = np.dtype([("t", "<f8"), ("lat", "<f8"), ("lon", "<f8"), ("h", "<f8"), ("q", "<i2"), ("sdn", "<f4"),
                ("sde", "<f4"), ("sdu", "<f4")])
SOURCES = {"ppk": "PPK (Reach rover against the base)", "llh": "Reach real-time solution (.LLH)",
           "mavlink": "autopilot position (MAVLink)"}
Q_NAMES = {1: "fixed", 2: "float", 3: "SBAS", 4: "DGPS", 5: "single", 6: "PPP", 9: "autopilot"}


class MarineError(Exception):
    pass


# ----------------------------------------------------------------------------- the job
def recordings(dcs):
    """[[paths of one recording]] of the job's marine .clraw, in recording order."""
    from dcs_core import clraw
    files = dcs.sonar_clraw_files() if dcs is not None else []
    groups = clraw.group_recordings(files, sonar=True)
    return [sorted(v) for v in groups.values()]


def is_marine(project):
    """A job whose captures are marine sonar recordings only (no Lidar, no Certus)."""
    dcs = getattr(project, "dcs", None)
    if dcs is None:
        return False
    try:
        if not recordings(dcs):
            return False
        return not dcs.survey_clraw_files()
    except Exception:       # noqa: BLE001
        return False


def options(project):
    o = dict(project.cfg.get("marine") or {})
    o.setdefault("position", "auto")           # auto | ppk | llh | mavlink
    o.setdefault("cog_min_speed", COG_MIN_SPEED)
    o.setdefault("base", None)                 # {"lat", "lon", "h" (mark, ellipsoidal), "frame", "epoch"} or None
    return o


def base_of(rec, opts):
    """The base station for PPK: entered on PosPro's marine page, else TidalCal's surveyed base."""
    b = opts.get("base")
    if b and b.get("lat") is not None:
        return dict(b, source="entered in PosPro")
    tc = rec.tidalcal
    llh = tc.get("surveyed_base_llh")
    if llh and len(llh) == 3:
        return {"lat": float(llh[0]), "lon": float(llh[1]), "h": float(llh[2]), "source": "TidalCal project",
                "frame": tc.get("horizontal_crs") or "", "hi_tidalcal": tc.get("base_hi_m")}
    return None


# ----------------------------------------------------------------------------- positions
def llh_to_ecef(lat, lon, h):
    a, f = 6378137.0, 1 / 298.257222101            # GRS80 (NAD83)
    e2 = f * (2 - f)
    la, lo = math.radians(lat), math.radians(lon)
    n = a / math.sqrt(1 - e2 * math.sin(la) ** 2)
    return ((n + h) * math.cos(la) * math.cos(lo), (n + h) * math.cos(la) * math.sin(lo),
            (n * (1 - e2) + h) * math.sin(la))


def ecef_to_llh(x, y, z):
    """GRS80 geodetic latitude, longitude (deg) and ellipsoidal height (m) of ECEF points (arrays)."""
    a, f = 6378137.0, 1 / 298.257222101
    e2 = f * (2 - f)
    lon = np.arctan2(y, x)
    p = np.hypot(x, y)
    lat = np.arctan2(z, p * (1 - e2))
    for _ in range(6):
        n = a / np.sqrt(1 - e2 * np.sin(lat) ** 2)
        h = p / np.cos(lat) - n
        lat = np.arctan2(z, p * (1 - e2 * n / (n + h)))
    n = a / np.sqrt(1 - e2 * np.sin(lat) ** 2)
    h = p / np.cos(lat) - n
    return np.degrees(lat), np.degrees(lon), h


def read_rtklib_pos(path):
    """RTKLIB .pos -> POS array in UTC. Reads both forms: ECEF with GPS week / seconds (PosPro's engine) and
    lat/lon/height with a date and time (GPST or UTC). Sigmas are rotated to north / east / up."""
    import calendar
    rows, gpst, ecef = [], True, False
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("%"):
                if "UTC" in line and "GPST" not in line:
                    gpst = False
                if "x-ecef" in line:
                    ecef = True
                continue
            a = line.split()
            if len(a) < 10:
                continue
            try:
                if "/" in a[0]:
                    y, mo, d = (int(x) for x in a[0].split("/"))
                    hh, mi, ss = a[1].split(":")
                    t = calendar.timegm((y, mo, d, int(hh), int(mi), 0)) + float(ss) - (18 if gpst else 0)
                else:
                    t = 315964800.0 + int(a[0]) * 604800.0 + float(a[1]) - 18.0
                rows.append([t] + [float(v) for v in a[2:13]])
            except ValueError:
                continue
    if not rows:
        return np.zeros(0, POS)
    R = np.array(rows)
    out = np.zeros(len(R), POS)
    out["t"] = R[:, 0]
    out["q"] = R[:, 4].astype(int)
    if ecef:
        lat, lon, h = ecef_to_llh(R[:, 1], R[:, 2], R[:, 3])
        out["lat"], out["lon"], out["h"] = lat, lon, h
        la, lo = np.radians(lat), np.radians(lon)
        # ENU rows of the rotation, then the covariance's diagonal in ENU
        E = np.stack([-np.sin(lo), np.cos(lo), np.zeros_like(lo)], 1)
        N = np.stack([-np.sin(la) * np.cos(lo), -np.sin(la) * np.sin(lo), np.cos(la)], 1)
        U = np.stack([np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)], 1)
        sx, sy, sz, sxy, syz, szx = (R[:, k] for k in range(6, 12))
        C = np.zeros((len(R), 3, 3))
        C[:, 0, 0], C[:, 1, 1], C[:, 2, 2] = sx ** 2, sy ** 2, sz ** 2
        for (i, j), v in (((0, 1), sxy), ((1, 2), syz), ((2, 0), szx)):
            c = np.sign(v) * v ** 2                 # RTKLIB writes sqrt(|cov|) with its sign
            C[:, i, j] = C[:, j, i] = c
        for col, vec in (("sde", E), ("sdn", N), ("sdu", U)):
            out[col] = np.sqrt(np.maximum(np.einsum("ni,nij,nj->n", vec, C, vec), 0))
    else:
        out["lat"], out["lon"], out["h"] = R[:, 1], R[:, 2], R[:, 3]
        out["sdn"], out["sde"], out["sdu"] = R[:, 6], R[:, 7], R[:, 8]
    return out


def combine_passes(fwd, bwd):
    """Forward and backward PPK: both fixed -> the mean, else the better pass (fixed > float > others, then the
    smaller sigma). -> POS on the union of epochs."""
    if bwd is None or not len(bwd):
        return fwd
    if fwd is None or not len(fwd):
        return bwd
    key_f = np.round(fwd["t"] * 20).astype(np.int64)
    key_b = np.round(bwd["t"] * 20).astype(np.int64)
    rank = {1: 0, 2: 1}
    out = {}
    for arr, keys in ((fwd, key_f), (bwd, key_b)):
        for k, r in zip(keys, arr):
            o = out.get(k)
            if o is None:
                out[k] = r.copy()
                continue
            if o["q"] == 1 and r["q"] == 1:
                m = o.copy()
                for c in ("lat", "lon", "h"):
                    m[c] = (o[c] + r[c]) / 2
                for c in ("sdn", "sde", "sdu"):
                    m[c] = min(o[c], r[c])
                out[k] = m
            elif (rank.get(int(r["q"]), 9), float(r["sdu"])) < (rank.get(int(o["q"]), 9), float(o["sdu"])):
                out[k] = r.copy()
    res = np.array([out[k] for k in sorted(out)], POS)
    return res


def positions_ppk(rec, wdir, base, ctx, atx=None):
    """PPK of the rover RINEX against the base RINEX with PosPro's engine. -> (POS, info) or (None, reason)."""
    from . import engine, pipeline, rinex
    rov, bas = rec.rinex("rover"), rec.rinex("base")
    robs = [n for n in rov if pipeline_obs(n)]
    bobs = [n for n in bas if pipeline_obs(n)]
    if not robs:
        return None, "no rover RINEX observations in the recording"
    if not bobs:
        return None, "no base RINEX observations in the recording"
    if base is None:
        return None, "no base position (enter it on the Marine page, or use a TidalCal project's surveyed base)"
    os.makedirs(wdir, exist_ok=True)

    def put(name, data):
        p = os.path.join(wdir, name)
        with open(p, "wb") as f:
            f.write(data)
        return p
    ro = put("rover.obs", rov[robs[0]])
    bo = put("base.obs", bas[bobs[0]])
    navs = [put("nav_%d_%s" % (i, n), d) for i, (n, d) in enumerate(list(rov.items()) + list(bas.items()))
            if not pipeline_obs(n)]
    hi = rinex.header_info(bo, scan_body=False) or {}
    delta = list(hi.get("delta_hen") or [0.0, 0.0, 0.0])
    ant = (hi.get("antenna") or "").strip()
    note = "base antenna %.3f m to the ARP (base RINEX header)%s" % (delta[0], ", %s" % ant if ant else "")
    if not atx and ant.upper().startswith("EML_REACH_RS2"):
        delta[0] += RS2_PCO_UP_M            # no antenna calibrations: Emlid's ARP -> L1 phase centre
        note += "; no ANTEX: +%.3f m (Emlid RS2 ARP to L1 phase centre)" % RS2_PCO_UP_M
    proc = {"frequencies": 2, "elmask": 12, "systems": "GREJ", "ar": True, "ar_ratio": 3.0}
    conf = os.path.join(wdir, "ppk.conf")
    with open(conf, "w", encoding="utf-8") as f:
        f.write(pipeline.rtklib_conf_ppk(proc, atx, delta, ant if atx else ""))
    xyz = llh_to_ecef(base["lat"], base["lon"], base["h"])
    t0, t1 = rec.span()
    g0 = 315964800.0 - 18.0                 # engine times: GPS seconds since 1980-01-06
    common = ["--ts", "%.3f" % (t0 - g0 - 120), "--te", "%.3f" % (t1 - g0 + 120), "--base"] + ["%.4f" % v for v in xyz]
    jobs = [("GNSS forward", ["gnss", "-k", conf, "-o", os.path.join(wdir, "fwd.pos"), "-d", "fwd"] + common +
             [ro, bo] + navs),
            ("GNSS backward", ["gnss", "-k", conf, "-o", os.path.join(wdir, "bwd.pos"), "-d", "bwd"] + common +
             [ro, bo] + navs)]
    res = engine.run_parallel(jobs, ctx.progress if ctx else None, ctx.log if ctx else None,
                              ctx.cancelled if ctx else None)
    for label, r in res.items():
        if not r.ok():
            return None, "%s failed: %s" % (label, r.message())
    if not os.path.exists(os.path.join(wdir, "fwd.pos")):
        return None, "PPK gave no solution (no common epochs of rover and base?)"
    fwd = read_rtklib_pos(os.path.join(wdir, "fwd.pos"))
    bwd = read_rtklib_pos(os.path.join(wdir, "bwd.pos")) if os.path.exists(os.path.join(wdir, "bwd.pos")) else None
    pos = combine_passes(fwd, bwd)
    if not len(pos):
        return None, "PPK gave no solution"
    info = {"base": base, "note": note, "fixed_fwd_pct": _pct(fwd, 1), "fixed_bwd_pct": _pct(bwd, 1)}
    return pos, info


def pipeline_obs(name):
    import re
    return bool(re.search(r"(\.\d\d[oO]|\.obs|_obs\.rnx)$", name)) or name.lower().endswith("o.rnx")


def _pct(a, q):
    return round(100.0 * float((a["q"] == q).mean()), 1) if a is not None and len(a) else 0.0


def positions_llh(rec):
    L = rec.llh()
    if not len(L):
        return None
    out = np.zeros(len(L), POS)
    for c in ("t", "lat", "lon", "h", "q", "sdn", "sde", "sdu"):
        out[c] = L[c]
    return out


def positions_mavlink(rec):
    P = rec.vehicle()["pos"]
    if not len(P):
        return None
    out = np.zeros(len(P), POS)
    out["t"], out["lat"], out["lon"], out["h"] = P["t"], P["lat"], P["lon"], P["alt"]
    out["q"] = 9
    out["sdn"] = out["sde"] = 0.5
    out["sdu"] = 1.0
    return out


def choose_positions(rec, wdir, opts, ctx=None, atx=None):
    """(POS, source key, info, [notes]) by the order PPK > rover LLH > autopilot (or the one asked for)."""
    want = opts.get("position", "auto")
    notes = []
    order = ["ppk", "llh", "mavlink"] if want == "auto" else [want]
    for src in order:
        if src == "ppk":
            pos, info = positions_ppk(rec, os.path.join(wdir, "ppk"), base_of(rec, opts), ctx, atx)
            if pos is not None and _pct(pos, 1) >= 50:
                return pos, "ppk", info, notes
            notes.append("PPK not used: %s" % (info if pos is None else "only %.0f %% fixed" % _pct(pos, 1)))
        elif src == "llh":
            pos = positions_llh(rec)
            if pos is not None and len(pos) > 10:
                return pos, "llh", {}, notes
            notes.append("no Reach solution log in the recording")
        else:
            pos = positions_mavlink(rec)
            if pos is not None and len(pos) > 10:
                return pos, "mavlink", {}, notes
            notes.append("no autopilot position in the recording")
    raise MarineError("no position in %s: %s" % (rec.name, "; ".join(notes)))


# ----------------------------------------------------------------------------- heading and attitude
def enu(lat, lon, lat0, lon0):
    kn = 111132.954 - 559.822 * math.cos(2 * math.radians(lat0))
    ke = 111412.84 * math.cos(math.radians(lat0))
    return (lon - lon0) * ke, (lat - lat0) * kn


def heading_and_speed(pos, att, min_speed=COG_MIN_SPEED, smooth_s=COG_SMOOTH_S):
    """(heading rad [0, 2pi), speed m/s, source 0 = course over ground / 1 = compass, vn, ve) per position."""
    t = pos["t"]
    e, n = enu(pos["lat"], pos["lon"], float(pos["lat"][0]), float(pos["lon"][0]))
    ve, vn = np.gradient(e, t), np.gradient(n, t)
    dt = float(np.median(np.diff(t))) if len(t) > 2 else 0.2
    w = max(1, int(round(smooth_s / max(dt, 1e-3))))
    if w > 1 and len(t) > w:
        k = np.ones(w) / w
        ve, vn = np.convolve(ve, k, "same"), np.convolve(vn, k, "same")
    spd = np.hypot(ve, vn)
    cog = np.mod(np.arctan2(ve, vn), 2 * math.pi)
    src = (spd < min_speed).astype(np.int8)
    if att is not None and len(att):
        yaw = np.unwrap(att["yaw"].astype(float))
        comp = np.mod(np.interp(t, att["t"], yaw), 2 * math.pi)
        hd = np.where(src == 1, comp, cog)
    else:
        hd, src = cog, np.zeros(len(t), np.int8)
    return hd, spd, src, vn, ve


def head_attitude(rec, head):
    """Each head's attitude for CloudPro and AlignPro: the sonar's own fused attitude (504) at every ping,
    as (t UTC, up[3], roll, pitch) -- plus the check against its raw IMU (505)."""
    A = rec.attitude(head)
    check = {}
    try:
        I = rec.imu(head)
        if len(I) > 100 and len(A) > 10:
            U = fuse_505(I)
            ux, uy, uz = (np.interp(A["t"], I["t"], U[:, k]) for k in range(3))
            dr = np.degrees(np.arctan2(uy, uz)) - A["roll"]
            dp = np.degrees(np.arcsin(np.clip(-ux, -1, 1))) - A["pitch"]
            check = {"imu_505_rate_hz": round((len(I) - 1) / float(I["t"][-1] - I["t"][0]), 1),
                     "fused_505_vs_504_roll_deg": [round(float(np.median(dr)), 3),
                                                   round(float(np.percentile(np.abs(dr - np.median(dr)), 68)), 3)],
                     "fused_505_vs_504_pitch_deg": [round(float(np.median(dp)), 3),
                                                    round(float(np.percentile(np.abs(dp - np.median(dp)), 68)), 3)]}
    except Exception:       # noqa: BLE001
        pass
    return A, check


def fuse_505(I, tau=0.5):
    """The world-up vector from a head's raw IMU (505): gyro-propagated, pulled towards the accelerometer with
    time constant tau (s). Only a check of the sonar's own attitude: no gyro-bias estimate, so it drifts with a
    longer tau (13 Nov 2026: tau 0.5 s agrees with 504 to ~0.08 deg, 68 %)."""
    t = I["t"]
    a = I["acc"].astype(float)
    g = np.radians(I["gyro"].astype(float))
    out = np.empty((len(t), 3))
    u = a[0] / np.linalg.norm(a[0])
    for i in range(len(t)):
        dt = t[i] - t[i - 1] if i else 0.0
        an = a[i] / max(np.linalg.norm(a[i]), 1e-9)
        if not 0 < dt <= 0.5:
            u = an
        else:
            u = u - np.cross(g[i], u) * dt
            k = dt / (tau + dt)
            u = (1 - k) * u + k * an
            u /= np.linalg.norm(u)
        out[i] = u
    return out


# ----------------------------------------------------------------------------- process and finalize
def process(project, ctx, atx=None):
    """Trajectory of every marine recording of the job -> work/marine/trajectory.npz + summary (cfg results)."""
    from dcs_core.marine import SonarRecording
    opts = options(project)
    recs = recordings(project.dcs)
    if not recs:
        raise MarineError("the job has no sonar recordings (.clraw made by RemixPro)")
    wdir = project.wpath("marine", "x")[:-2]
    parts, summary, heads = [], {"recordings": [], "notes": []}, {}
    for i, paths in enumerate(recs):
        rec = SonarRecording(paths)
        sub = ctx.sub(i / len(recs), (i + 0.8) / len(recs)) if ctx else None
        pos, src, info, notes = choose_positions(rec, os.path.join(wdir, rec.name), opts, sub, atx)
        att = rec.vehicle()["att"]
        hd, spd, hsrc, vn, ve = heading_and_speed(pos, att, float(opts["cog_min_speed"]))
        roll = np.interp(pos["t"], att["t"], att["roll"]) if len(att) else np.zeros(len(pos))
        pitch = np.interp(pos["t"], att["t"], att["pitch"]) if len(att) else np.zeros(len(pos))
        parts.append(np.rec.fromarrays([pos["t"], pos["lat"], pos["lon"], pos["h"], pos["q"], pos["sdn"], pos["sde"],
                                        pos["sdu"], vn, ve, roll, pitch, hd, hsrc, spd],
                                       names="t,lat,lon,h,q,sdn,sde,sdu,vn,ve,roll,pitch,heading,hsrc,speed"))
        for did in rec.heads:
            A, check = head_attitude(rec, did)
            heads.setdefault(did, {"parts": [], "checks": [], "info": rec.heads[did]})
            heads[did]["parts"].append(A)
            heads[did]["checks"].append(check)
        q = {Q_NAMES.get(int(k), str(int(k))): round(100.0 * float((pos["q"] == k).mean()), 1) for k in np.unique(pos["q"])}
        summary["recordings"].append({"name": rec.name, "source": src, "source_label": SOURCES[src], "epochs": len(pos),
                                      "quality_pct": q, "rate_hz": round(1 / float(np.median(np.diff(pos["t"]))), 1)
                                      if len(pos) > 2 else None, "compass_pct": round(100.0 * float(hsrc.mean()), 1),
                                      "span": [float(pos["t"][0]), float(pos["t"][-1])], "info": _jsonable(info),
                                      "notes": notes, "mavlink_boot_fit_rms_s": rec.vehicle()["boot_fit"][2]})
        summary["notes"] += ["%s: %s" % (rec.name, n) for n in notes]
    T = np.hstack(parts)
    T.sort(order="t")
    np.save(os.path.join(wdir, "trajectory.npy"), T)
    for did, h in heads.items():
        A = np.hstack(h["parts"])
        A.sort(order="t")
        np.save(os.path.join(wdir, "sonar_%d_attitude.npy" % did), A)
        h["n"] = len(A)
    summary["heads"] = {str(d): {"nickname": h["info"].get("nickname"), "lever_m": h["info"].get("lever_m"),
                                 "lever_source": h["info"].get("lever_source"), "looks": h["info"].get("looks"),
                                 "attitudes": h["n"],
                                 "check": h["checks"][0] if h["checks"] else {}} for d, h in heads.items()}
    summary["position_source"] = sorted({r["source"] for r in summary["recordings"]})
    summary["fixed_pct"] = round(100.0 * float((T["q"] == 1).mean()), 1)
    summary["compass_pct"] = round(100.0 * float(T["hsrc"].mean()), 1)
    summary["cog_min_speed"] = float(opts["cog_min_speed"])
    project.cfg.setdefault("results", {})["marine"] = summary
    if ctx:
        ctx.progress(1.0)
    return summary


def _jsonable(o):
    return json.loads(json.dumps(o, default=lambda x: x.item() if hasattr(x, "item") else str(x)))


def write_sbet(T, path, leap=18):
    """SBET (17 doubles) + SMRMSG at the GNSS antenna; time = GPS seconds of week. -> (sbet, smrmsg, week)."""
    from dcs_core import sbet as dsbet
    gps = T["t"] + leap
    week = int((gps[0] - dsbet.GPS_EPOCH_UNIX) // dsbet.WEEK)
    sow = gps - dsbet.week_start(week)
    S = np.zeros(len(T), dsbet.SBET)
    S["t"] = sow
    S["lat"], S["lon"], S["h"] = np.radians(T["lat"]), np.radians(T["lon"]), T["h"]
    S["vx"], S["vy"] = T["vn"], T["ve"]
    S["roll"], S["pitch"], S["heading"] = T["roll"], T["pitch"], T["heading"]
    S.tofile(path)
    M = np.zeros(len(T), dsbet.SMRMSG)
    M["t"] = sow
    M["sd_n"], M["sd_e"], M["sd_d"] = T["sdn"], T["sde"], T["sdu"]
    hsd = np.where(T["hsrc"] == 1, 2.0, np.degrees(np.arctan2(np.hypot(T["sdn"], T["sde"]) * 2,
                                                              np.maximum(T["speed"] * 2.0, 0.1))))
    M["sd_roll"] = M["sd_pitch"] = 30.0
    M["sd_heading"] = np.minimum(hsd, 30.0) * 60.0
    import re
    smr = os.path.join(os.path.dirname(path), re.sub(r"(?i)sbet", "smrmsg", os.path.basename(path)))
    M.tofile(smr)
    return path, smr, week


def publish(project, ctx):
    """Finalize the marine trajectory: SBET + SMRMSG at the GNSS antenna, each head's attitude, trajectory.json
    (dcs.trajectory/1 with the marine and sensors sections)."""
    from dcs_core import handover
    from . import pipeline
    summ = (project.cfg.get("results") or {}).get("marine")
    wdir = os.path.join(project.work_dir, "marine")
    tf = os.path.join(wdir, "trajectory.npy")
    if not summ or not os.path.exists(tf):
        raise MarineError("process the marine trajectory first")
    T = np.load(tf)
    od = project.out_dir
    os.makedirs(od, exist_ok=True)
    try:
        os.remove(os.path.join(od, "trajectory.json"))
    except OSError:
        pass
    tmp = os.path.join(wdir, "handover")
    os.makedirs(tmp, exist_ok=True)
    sb, smr, week = write_sbet(T, os.path.join(tmp, "trajectory_sbet.out"))
    files = {"sbet": os.path.join(od, "trajectory_sbet.out"), "smrmsg": os.path.join(od, "trajectory_smrmsg.out")}
    os.replace(sb, files["sbet"])
    os.replace(smr, files["smrmsg"])
    sensors = {}
    for did in summ.get("heads", {}):
        src = os.path.join(wdir, "sonar_%s_attitude.npy" % did)
        dst = os.path.join(od, "sonar_%s_attitude.npy" % did)
        shutil.copyfile(src, dst + ".tmp")
        os.replace(dst + ".tmp", dst)
        files["sonar_%s_attitude" % did] = dst
        h = summ["heads"][did]
        sensors["sonar_%s" % did] = {"attitude": os.path.basename(dst), "time": "UTC seconds",
                                     "format": "numpy .npy: t (UTC s), up[3] (Cerulean's world-up vector in the sonar's "
                                               "frame), roll, pitch (deg)",
                                     "source": "the sonar's own fused attitude (packet 504) at every ping",
                                     "nickname": h.get("nickname"), "lever_m": h.get("lever_m"),
                                     "lever_source": h.get("lever_source"), "looks": h.get("looks"),
                                     "check": h.get("check")}
    recs = summ["recordings"]
    base = next((r["info"].get("base") for r in recs if r["source"] == "ppk"), None)
    frame = {"name": (base or {}).get("frame") or ("NAD83(CSRS)" if base else "as the GNSS receiver gave it"),
             "epoch": (base or {}).get("epoch"), "heights": "ellipsoidal"}
    if base and not frame["epoch"] and "NAD83" in (frame["name"] or "").upper():
        frame["epoch"] = 2010.0
    span = (float(T["t"][0]), float(T["t"][-1]))
    rate = recs[0].get("rate_hz") if recs else None
    fields = {
        "time": {"system": "GPS", "week": week, "leap_seconds": 18, "start_gps": span[0] + 18, "end_gps": span[1] + 18,
                 "sbet_time": "GPS seconds of week"},
        "frame": frame,
        "platform": "marine",
        "reference_point": {"name": "GNSS antenna reference point (ARP) of the position receiver",
                            "offset_from_imu_m": [0.0, 0.0, 0.0]},
        "body_frame": "vessel: X forward, Y starboard, Z down; roll + starboard down, pitch + bow up, heading + "
                      "clockwise (roll / pitch from the autopilot; heading per marine.heading)",
        "levers_m": {"gnss1": {"value": [0.0, 0.0, 0.0], "source": "reference point"}},
        "rate_hz": rate,
        "quality": {"mode": "gnss", "positioning": "+".join(summ["position_source"]), "fixed_pct": summ["fixed_pct"],
                    "max_sd_h_m": round(float(np.max(np.hypot(T["sdn"], T["sde"]))), 3),
                    "max_sd_v_m": round(float(np.max(T["sdu"])), 3)},
        "base_station": "%s (%s)" % ("surveyed base", base.get("source")) if base else None,
        "marine": {"position": [{"recording": r["name"], "source": r["source_label"], "quality_pct": r["quality_pct"]}
                                for r in recs],
                   "heading": {"course_over_ground_above_m_s": summ["cog_min_speed"],
                               "below": "autopilot compass (MAVLink ATTITUDE yaw)", "compass_pct": summ["compass_pct"]},
                   "vessel_attitude": "autopilot (MAVLink ATTITUDE)", "notes": summ.get("notes", [])},
        "sensors": sensors,
        "attitude": "vessel roll / pitch from the autopilot; each sonar's own attitude in sensors",
    }
    doc = handover.publish(project.dcs, "pospro", files, fields, "PosPro", pipeline.plugin_version(), "")
    if ctx:
        ctx.log("info", "marine trajectory finalized for AlignPro and CloudPro: %s" % os.path.join(od, "trajectory.json"))
    return {"id": doc["id"], "path": os.path.join(od, "trajectory.json")}
