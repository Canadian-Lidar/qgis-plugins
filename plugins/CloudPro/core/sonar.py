"""Marine sonar recordings (.clraw kind "sonar") into CloudPro's point store: each head of each recording is one
line, so line separation, the filters, editing and every export work on the soundings as on Lidar points.

A sounding (Omniscan 3D packet 3104: beam angle, time of flight, power) becomes a point:
    ray in the head's frame (dcs_core.marine.rays: range = tof * sound speed / 2, Snell for another speed, the
    head's roll correction) -> level forward / left / up with the head's own attitude at the ping (its fused up
    vector, packet 504, as PosPro finalized it) and its pitch correction -> north / east / down with the vessel's
    heading (PosPro's: course over ground, compass when slow) + the head's heading correction -> plus the lever
    arm to the acoustic centre (vessel frame, turned with the vessel's attitude) -> projected like the Lidar
    (GeorefContext: grid convergence, scale, earth curvature, geoid).
Corrections per head (roll, pitch, heading, latency) come from AlignPro's heads section when finalized.

Stored fields: i = power on a log scale (0-255 over 60 dB above the head's low threshold), ch = head (device id),
ret = 1, rng = slant range (cm), az = beam angle (0.01 deg, wrapped), cls = 1 (unclassified).
"""
import math
import os
import time

import numpy as np

from .georef import GeorefContext, _rot_elems
from .geodesy import Projector
from .store import REC

POWER_LEVELS = ("high", "medium", "low", "all")


class SonarOptions:
    def __init__(self, **kw):
        self.power = "medium"          # keep soundings at or above the head's high / medium / low threshold, or all
        self.rmin = 0.5
        self.rmax = 200.0
        self.sound_speed = None        # m/s: another sound speed than the sonar used (None = as recorded)
        self.decimate = 1              # every n-th ping
        self.geoid = None
        self.source_crs = None
        self.__dict__.update(kw)


def head_label(info, did):
    side = info.get("looks") or info.get("side")          # the side its fan looks to
    return "%s%s" % (side or "head %d" % did, " (%s)" % info["nickname"] if info.get("nickname") else "")


def power_to_intensity(pwr, low):
    """0-255 on a log scale: 0 at the low threshold, 255 at 60 dB above it."""
    low = np.maximum(np.asarray(low, float), 1.0)
    db = 10.0 * np.log10(np.maximum(pwr, 1e-9) / low)
    return np.clip(np.round(db / 60.0 * 255.0), 0, 255).astype(np.uint8)


def import_sonar_head(project, rec, did, traj, attitude, cal, opts, progress=None, cancel=None, line_name=None,
                      window=None, pass_no=None):
    """One head of one SonarRecording (or of one pass of it: window = (start, end) UTC s) -> a new line. attitude: structured (t, up[3], ...) for this head (PosPro's
    finalized file, else the recording's 504). cal: {"roll", "pitch", "heading" deg, "latency_s"} corrections.
    Returns (line id, stats)."""
    from dcs_core import marine
    store = project.store
    proj = Projector(project.crs, source=opts.source_crs)
    info_h = rec.heads[did]
    lever = info_h.get("lever_m") or [0.0, 0.0, 0.0]
    cal = dict({"roll": 0.0, "pitch": 0.0, "heading": 0.0, "latency_s": 0.0}, **(cal or {}))
    started = time.time()
    pings = rec.pings(did, every=max(1, int(opts.decimate)))
    if window is not None:
        pings = [p for p in pings if window[0] <= p["t"] < window[1]]
    if not pings:
        return None, {"pings": 0, "points": 0, "soundings": 0}
    t_ping = np.array([p["t"] for p in pings]) + float(cal["latency_s"])
    t0, t1 = float(t_ping[0]), float(t_ping[-1])
    sub = traj.subset(t0, t1, pad=2.0)
    if len(sub) < 2:
        raise ValueError("the trajectory does not cover %s" % rec.name)
    if store.origin is None:
        E, N = proj.forward(sub.lat[:1], sub.lon[:1])
        store.origin = np.array([np.floor(E[0] / 1000) * 1000, np.floor(N[0] / 1000) * 1000,
                                 np.floor(sub.h[0] / 100) * 100])
    ctx = GeorefContext(sub, proj, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), geoid=opts.geoid, origin=store.origin)
    label = "%s%s %s" % (rec.name, " pass %d" % pass_no if pass_no is not None else "", head_label(info_h, did))
    info = {"name": line_name or label, "line": len(project.lines) + 1,
            "files": [f.path for f in rec.files], "kind": "sonar", "head": int(did), "nickname": info_h.get("nickname"),
            "pass": pass_no, "window": list(window) if window is not None else None,
            "trajectory": traj.source, "lever": list(lever), "lever_source": info_h.get("lever_source"),
            "calibration_deg": {k: cal[k] for k in ("roll", "pitch", "heading")}, "latency_s": cal["latency_s"],
            "power": opts.power, "rmin": opts.rmin, "rmax": opts.rmax, "sound_speed": opts.sound_speed,
            "decimate": opts.decimate, "crs": project.crs, "projector": proj.backend}
    lid = project.add_line(info)
    A = attitude
    stats = {"pings": len(pings), "soundings": 0, "points": 0, "below_power": 0, "outside_range": 0,
             "outside_traj": 0}
    tmin, tmax = sub.t[0], sub.t[-1]
    block = 400
    for b0 in range(0, len(pings), block):
        if cancel and cancel():
            break
        bl = pings[b0:b0 + block]
        rows = []
        for p, tp in zip(bl, t_ping[b0:b0 + block]):
            pts = p["points"]
            if not len(pts):
                continue
            stats["soundings"] += len(pts)
            thr = {"high": 0, "medium": 1, "low": 2}.get(opts.power)
            m = np.ones(len(pts), bool)
            if thr is not None:
                m &= pts[:, 2] >= float(p["thresholds"][thr])
                stats["below_power"] += int((~m).sum())
            r = pts[:, 1] * p["sos"] / 2.0
            mr = (r >= opts.rmin) & (r <= opts.rmax)
            stats["outside_range"] += int((m & ~mr).sum())
            m &= mr
            if not m.any() or not (tmin <= tp <= tmax):
                stats["outside_traj"] += int(m.sum()) if not (tmin <= tp <= tmax) else 0
                continue
            k = int(np.clip(np.searchsorted(A["t"], tp - cal["latency_s"]), 0, len(A) - 1))
            if k > 0 and abs(A["t"][k - 1] - (tp - cal["latency_s"])) < abs(A["t"][k] - (tp - cal["latency_s"])):
                k -= 1
            q = pts[m]
            rows.append((tp, q, p["sos"], np.asarray(A["up"][k], float), p["thresholds"][2]))
        if not rows:
            continue
        n_each = [len(r[1]) for r in rows]
        T = np.repeat([r[0] for r in rows], n_each)
        Q = np.vstack([r[1] for r in rows])
        SOS = np.repeat([r[2] for r in rows], n_each)
        UP = np.repeat(np.vstack([r[3] for r in rows]), n_each, axis=0)
        LOW = np.repeat([r[4] for r in rows], n_each)
        v = _rays_fast(marine, Q[:, 0], Q[:, 1], SOS, cal["roll"], opts.sound_speed)
        flu = marine.level_vectors(v, UP, cal["pitch"])                 # forward, left, up (level)
        E0, N0, h, rr, pp, yy, J = ctx._nav(T)
        hd = yy + math.radians(cal["heading"])
        f, l_, u = flu[:, 0], flu[:, 1], flu[:, 2]
        e_ = f * np.sin(hd) - l_ * np.cos(hd)
        n_ = f * np.cos(hd) + l_ * np.sin(hd)
        d_ = -u
        c = _rot_elems(rr, pp, yy)                       # the lever arm turns with the vessel
        lx, ly, lz = (float(x) for x in lever)
        n_ = n_ + c[0] * lx + c[1] * ly + c[2] * lz
        e_ = e_ + c[3] * lx + c[4] * ly + c[5] * lz
        d_ = d_ + c[6] * lx + c[7] * ly + c[8] * lz
        E = E0 + J[0] * e_ + J[1] * n_
        N = N0 + J[2] * e_ + J[3] * n_
        Z = h - d_ + (e_ * e_ + n_ * n_) / (2 * ctx.R) - ctx.origin[2]
        rec_ = np.zeros(len(T), REC)
        rec_["x"] = np.round(E * 1000).astype(np.int32)
        rec_["y"] = np.round(N * 1000).astype(np.int32)
        rec_["z"] = np.round(Z * 1000).astype(np.int32)
        rec_["t"] = T
        rec_["i"] = power_to_intensity(Q[:, 2], LOW)
        rec_["ret"] = 1
        rec_["ch"] = int(did) & 0xFF
        rec_["cls"] = 1
        rec_["line"] = lid
        rec_["rng"] = np.clip(np.round(Q[:, 1] * SOS / 2.0 * 100), 0, 65535).astype(np.uint16)
        rec_["az"] = np.mod(np.round(np.degrees(Q[:, 0]) * 100), 36000).astype(np.uint16)
        store.append_binned(store.bin(rec_))
        stats["points"] += len(rec_)
        if progress:
            progress(min(1.0, (b0 + block) / len(pings)), stats)
    store.flush()
    stats["seconds"] = time.time() - started
    # the acoustic centre's track at 20 Hz (display, sections, multi-pass filters)
    tt = np.arange(max(sub.t[0], t0), min(sub.t[-1], t1), 0.05)
    if len(tt) >= 2:
        E0, N0, h, rr, pp, yy, J = ctx._nav(tt)
        c = _rot_elems(rr, pp, yy)
        lx, ly, lz = (float(x) for x in lever)
        n_ = c[0] * lx + c[1] * ly + c[2] * lz
        e_ = c[3] * lx + c[4] * ly + c[5] * lz
        d_ = c[6] * lx + c[7] * ly + c[8] * lz
        project.save_track(lid, tt, E0 + J[0] * e_ + J[1] * n_, N0 + J[2] * e_ + J[3] * n_,
                           h - d_ - ctx.origin[2], yy)
    info.update(points=stats["points"], t0=t0, t1=t1, stats=stats,
                file_spans=[list(f.span()) for f in rec.files],
                file_sizes=[os.path.getsize(f.path) if os.path.exists(f.path) else 0 for f in rec.files])
    project.lines[lid].update(info)
    project.log("Imported %s: %s soundings kept of %s in %.1f s (%s power and up)" % (
        info["name"], "{:,}".format(stats["points"]), "{:,}".format(stats["soundings"]), stats["seconds"], opts.power))
    project.save()
    return lid, stats


def _rays_fast(marine, angle, tof, sos, roll_bias_deg, sos_new=None):
    """marine.rays for arrays with a sound speed per sounding; sos_new re-scales the range and re-bends the
    angle (Snell at the face)."""
    sos = np.asarray(sos, float)
    a = np.asarray(angle, float)
    r = np.asarray(tof, float) * sos / 2.0
    if sos_new:
        a = np.arcsin(np.clip(np.sin(a) * float(sos_new) / sos, -1, 1))
        r = r * float(sos_new) / sos
    a = a + math.radians(roll_bias_deg) * marine.SIDE
    return np.stack([np.zeros_like(a), marine.SIDE * np.sin(a), -np.cos(a)], 1) * r[:, None]


def head_attitude(handover_doc, did, rec):
    """The head's attitude: PosPro's finalized file (sensors section), else the recording's packet 504."""
    try:
        s = (handover_doc or {}).get("sensors", {}).get("sonar_%d" % int(did)) or {}
        p = (handover_doc or {}).get("paths", {}).get("sonar_%d_attitude" % int(did))
        if s and p and os.path.exists(p):
            return np.load(p), "PosPro"
    except Exception:       # noqa: BLE001
        pass
    return rec.attitude(did), "recording (504)"
