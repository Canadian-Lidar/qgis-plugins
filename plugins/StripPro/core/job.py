"""StripPro's job: read what PosPro and AlignPro finalized and the survey lines, sample the Lidar, solve the
trajectory corrections (strip.py) and write the corrected trajectory, with its recording holes filled, as a
dcs.trajectory/1 handover that CloudPro georeferences with instead of PosPro's.

Everything here runs without QGIS (tests, command line); the plugin's window drives it.
"""
import json
import math
import os
import time

import numpy as np

from dcs_core import clraw as dclraw
from dcs_core import handover
from dcs_core import hesai
from dcs_core import sbet as dsbet

from . import strip as ST
from dcs_core import frames as _frames

from .frames import LocalFrame, rodrigues, zyx_from_rot

Z0 = _frames.HESAI_ZERO               # the hesai_navaq adapter's zero (dcs_core.frames)
KEEP_PACKET = 1               # every Lidar packet ...
KEEP_RETURN = 0.025           # ... and 2.5 % of its strongest returns (Shirley: about 11 million points)
RANGE = (1.0, 40.0)
MIN_SPEED = 1.0               # m/s: standing still adds nothing
FILL_MAX_S = 10.0             # holes longer than this inside a line are left as holes
FILL_RATE = 200.0             # Hz of the records put into a filled hole
SCHEMA_NOTE = "StripPro corrected trajectory"


def lidar_matrix(roll, pitch, heading):
    """Mapper mounting angles (deg, hesai_navaq) -> sensor-to-vehicle rotation (dcs_core.frames)."""
    return _frames.hesai_mount_matrix(roll, pitch, heading)


# ----------------------------------------------------------------------------------------------- trajectory
class Trajectory:
    """PosPro's SBET in UTC seconds, positions and attitude in one LocalFrame; interpolated the way CloudPro does
    (straight lines between records, also across holes)."""

    def __init__(self, path, week=None, leap=18.0, frame=None):
        self.path = path
        t, a, ws = dsbet.read(path, leap, week=week)
        self.t, self.a, self.week_start, self.leap = t, a, ws, leap
        self.lat, self.lon, self.h = a["lat"], a["lon"], a["h"]
        self.roll, self.pitch = a["roll"], a["pitch"]
        self.heading = np.unwrap(dsbet.true_heading(a))
        self.frame = frame or LocalFrame(float(np.median(self.lat)), float(np.median(self.lon)),
                                         float(np.median(self.h)))
        self.P = self.frame.to_local(self.lat, self.lon, self.h)
        self.holes = dsbet.holes(t)

    def at(self, tq):
        """INS reference point (n, 3) and attitude (vehicle -> frame, (n, 3, 3)) at times tq."""
        tq = np.asarray(tq, float)
        f = lambda v: np.interp(tq, self.t, v)
        s = np.stack([f(self.P[:, k]) for k in range(3)], 1)
        R = self.frame.attitude(f(self.lat), f(self.lon), f(self.roll), f(self.pitch), f(self.heading))
        return s, R

    def speed(self, tq):
        v = np.hypot(np.gradient(self.P[:, 0], self.t), np.gradient(self.P[:, 1], self.t))
        return np.interp(tq, self.t, v)


class MarineJob(Exception):
    pass


class Inputs:
    """What one run uses: the DCS project, PosPro's trajectory, AlignPro's calibration, the survey lines."""

    def __init__(self, dcs):
        self.dcs = dcs
        self.traj_doc = handover.read(dcs, "pospro")
        if self.traj_doc.get("platform") == "marine":
            raise MarineJob("This is a marine job (sonar recordings). StripPro corrects the trajectory from Lidar "
                            "lines; a marine job goes from AlignPro's sonar patch test straight to CloudPro.")
        # AlignPro's calibration when it has finalized one; a finalized one that cannot be read is an error,
        # never a quiet fall-back to the recorded mounting
        self.align_doc = handover.read(dcs, "alignpro", required=False)
        tm = self.traj_doc.get("time") or {}
        self.week = int(tm.get("week") or 0) or None
        self.leap = float(tm.get("leap_seconds", 18))
        self.traj = Trajectory(self.traj_doc["paths"]["sbet"], self.week, self.leap)
        self.smrmsg = self.traj_doc["paths"].get("smrmsg")
        if self.align_doc:
            m = self.align_doc.get("mount_deg") or {}
            self.mount = [float(m["roll"]), float(m["pitch"]), float(m["heading"])]
            self.lever = [float(v) for v in (self.align_doc.get("lidar_lever_m") or {}).get("value") or [0, 0, 0]]
            self.latency = float(self.align_doc.get("latency_s") or 0.0)
            self.calibration = "AlignPro"
        else:
            self.mount = self.lever = None
            self.latency = 0.0
            self.calibration = "recorded"
        self.files = dcs.survey_clraw_files()
        self.groups = dclraw.group_recordings(self.files)

    def lines(self):
        """[(line label, Recording)] in recording order."""
        return [("Line %d" % int(line) if str(line).isdigit() else str(line), dclraw.Recording(paths))
                for (_folder, line), paths in self.groups.items()]


# ----------------------------------------------------------------------------------------------- sampling
def sample_line(rec, traj, mount, lever, latency=0.0, rng=None, progress=None, cancelled=None,
                keep_packet=KEEP_PACKET, keep_return=KEEP_RETURN):
    """Lidar returns of one line, thinned, placed with PosPro's trajectory: t (UTC s), p and s (frame, float32:
    the point and the INS reference point at its time). Standing still and outside the trajectory is left out."""
    rng = rng or np.random.default_rng(1)
    mount = mount if mount is not None else rec.mount()
    lever = lever if lever is not None else rec.lever_arm()
    M = lidar_matrix(*mount)
    lv = np.asarray(lever, float)
    chunks = rec.chunk_list()
    out_t, out_p, out_s = [], [], []
    cache = {}
    t_lo, t_hi = traj.t[0], traj.t[-1]
    try:
        for n, (fi, ci) in enumerate(chunks):
            if cancelled and cancelled():
                raise RuntimeError("cancelled")
            P = rec.read_lidar_chunk(cache, fi, ci)
            if len(P) == 0:
                continue
            P = P[::keep_packet]
            tp = hesai.packet_times(P)
            ok = (tp > t_lo) & (tp < t_hi)
            if ok.any():
                ok &= traj.speed(tp) >= MIN_SPEED
            if not ok.any():
                continue
            d = hesai.decode_packets(P[ok], rec.calib, tp[ok], "strongest", RANGE[0], RANGE[1], 0, False,
                                     keep_return, rng)
            if d is None or not len(d["t"]):
                continue
            t = d["t"]
            xs = np.stack([d["x"], d["y"], d["z"]], 1)
            v = xs @ M.T + lv
            s, R = traj.at(t - latency)
            p = s + np.einsum("nij,nj->ni", R, v)
            out_t.append(t)
            out_p.append(p.astype(np.float32))
            out_s.append(s.astype(np.float32))
            if progress and n % 20 == 0:
                progress((n + 1) / len(chunks))
    finally:
        for fh in cache.values():
            fh.close()
    if not out_t:
        return np.zeros(0), np.zeros((0, 3), np.float32), np.zeros((0, 3), np.float32)
    t = np.concatenate(out_t)
    o = np.argsort(t, kind="stable")
    return t[o], np.concatenate(out_p)[o], np.concatenate(out_s)[o]


# ----------------------------------------------------------------------------------------------- run
def work_dir(dcs):
    return dcs.stage_dir("strippro", "work")


def out_dir(dcs):
    return dcs.stage_dir("strippro", "out")


def run(inputs, progress=None, cancelled=None, log=None):
    """Sample every line, solve, save the corrections. Returns the summary dict (also saved as work/strip.json)."""
    log = log or (lambda *a: None)
    t_start = time.time()
    tr = inputs.traj
    lines = inputs.lines()
    T, Pp, Ss, L = [], [], [], []
    spans = {}
    names = {}
    for k, (name, rec) in enumerate(lines):
        def pr(f, k=k):
            if progress:
                progress(0.5 * (k + f) / len(lines), "Reading line %s" % name)
        t, p, s = sample_line(rec, tr, inputs.mount, inputs.lever, inputs.latency, progress=pr, cancelled=cancelled)
        log("line %s: %d points sampled" % (name, len(t)))
        if len(t) < 1000:
            continue
        T.append(t)
        Pp.append(p.astype(np.float64))
        Ss.append(s.astype(np.float64))
        L.append(np.full(len(t), k))
        spans[k] = (float(t[0]), float(t[-1]))
        names[k] = name
    if len(T) == 0:
        raise ValueError("no moving Lidar data in the survey lines")
    t, p, s, line = np.concatenate(T), np.concatenate(Pp), np.concatenate(Ss), np.concatenate(L)
    del T, Pp, Ss, L
    holes = tr.holes
    cor = {k: ST.Correction(ST.knots_for(a - 0.5, b + 0.5, holes=holes)) for k, (a, b) in spans.items()}
    before = ST.residuals(line, t, p, s)
    sv = ST.Solver(cor, holes)

    def pr2(f, h):
        if progress:
            progress(0.5 + 0.45 * f, "Solving: iteration %d, %s observations" % (h["iteration"], "{:,}".format(h["pairs"])))
        log("iteration %(iteration)d: %(pairs)d observations (%(inter)d between lines), median %(median_before_cm).2f cm, "
            "largest step %(step_max_cm).1f cm" % h)
    sv.solve(line, t, p, s, progress=pr2, cancelled=cancelled)
    after = ST.residuals(line, t, p, s, sv)
    inh = ST._in_holes_pad(t, holes, 0.0)
    shift = {}
    for k in spans:
        m = line == k
        d, th = sv.cor[k].eval(t[m])
        p2 = ST.apply(p[m], s[m], d, th)
        mv = np.linalg.norm(p2 - p[m], axis=1)
        shift[k] = {"median_m": float(np.median(mv)), "p95_m": float(np.percentile(mv, 95)),
                    "in_holes_median_m": float(np.median(mv[inh[m]])) if inh[m].any() else 0.0}

    def stats(r):
        return {k: {"n": int(len(v)), "median_m": float(np.median(v)) if len(v) else None,
                    "p95_m": float(np.percentile(v, 95)) if len(v) else None} for k, v in r.items()}
    summary = {"by": "StripPro", "utc": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
               "seconds": round(time.time() - t_start, 1), "points": int(len(t)),
               "lines": {str(k): {"name": names[k], "span": spans[k], "moved": shift[k]} for k in spans},
               "trajectory": {"handover_id": inputs.traj_doc.get("id")},
               "alignment": {"handover_id": (inputs.align_doc or {}).get("id"), "source": inputs.calibration},
               "holes": dsbet.holes_summary(tr.t, spans=False),
               "before": stats(before), "after": stats(after), "iterations": sv.history,
               "corrections": {str(k): sv.cor[k].to_json() for k in spans}}
    os.makedirs(work_dir(inputs.dcs), exist_ok=True)
    with open(os.path.join(work_dir(inputs.dcs), "strip.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh)
    return summary


def load(dcs):
    p = os.path.join(work_dir(dcs), "strip.json")
    if not os.path.exists(p):
        return None
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def corrections_of(summary):
    return {int(k): (ST.Correction.from_json(v), tuple(summary["lines"][k]["span"]))
            for k, v in summary["corrections"].items()}


# ----------------------------------------------------------------------------------------------- output
def corrected_trajectory(tr, summary):
    """PosPro's records with the corrections applied, and records put into the holes inside each line (up to
    FILL_MAX_S) at FILL_RATE. -> dict of arrays: t (UTC), lat, lon, h, roll, pitch, heading, filled (bool)."""
    cors = corrections_of(summary)
    t = tr.t
    extra = []
    for _k, (_c, (a, b)) in cors.items():
        for h0, h1 in tr.holes:
            if h1 <= a or h0 >= b or h1 - h0 > FILL_MAX_S:
                continue
            n = int((h1 - h0) * FILL_RATE)
            if n > 1:
                extra.append(h0 + (np.arange(1, n) / n) * (h1 - h0))
    tf = np.concatenate([t] + extra) if extra else t.copy()
    filled = np.r_[np.zeros(len(t), bool), np.ones(len(tf) - len(t), bool)]
    o = np.argsort(tf, kind="stable")
    tf, filled = tf[o], filled[o]
    s, R = tr.at(tf)
    for _k, (c, (a, b)) in cors.items():
        m = (tf >= a) & (tf <= b)
        if not m.any():
            continue
        d, th = c.eval(tf[m])
        s[m] = s[m] + d
        R[m] = rodrigues(th) @ R[m]
    lat, lon, h = tr.frame.to_lla(s)
    roll, pitch, heading = tr.frame.attitude_back(lat, lon, R)
    return {"t": tf, "lat": lat, "lon": lon, "h": h, "roll": roll, "pitch": pitch, "heading": heading,
            "filled": filled}


def write_outputs(inputs, summary, plugin_version="", progress=None):
    """Corrected SBET + SMRMSG + QC in out/, and trajectory.json (dcs.trajectory/1) as StripPro's handover."""
    tr = inputs.traj
    od = out_dir(inputs.dcs)
    ct = corrected_trajectory(tr, summary)
    sb = os.path.join(od, "trajectory_sbet.out")
    tmp = sb + ".tmp"
    dsbet.write(tmp, ct["t"], ct["lat"], ct["lon"], ct["h"], ct["roll"], ct["pitch"], ct["heading"],
                tr.leap, start=tr.week_start)
    os.replace(tmp, sb)
    files = {"sbet": sb}
    if inputs.smrmsg and os.path.exists(inputs.smrmsg):
        tm, m = dsbet.read_smrmsg(inputs.smrmsg, tr.week_start, tr.leap)
        out = np.zeros(len(ct["t"]), dsbet.SMRMSG)
        out["t"] = ct["t"] + tr.leap - tr.week_start
        for k in dsbet.SMRMSG.names[1:]:
            out[k] = np.interp(ct["t"], tm, m[k])
        sm = os.path.join(od, "trajectory_smrmsg.out")
        out.tofile(sm + ".tmp")
        os.replace(sm + ".tmp", sm)
        files["smrmsg"] = sm
    doc = inputs.traj_doc
    rh = dsbet.holes_summary(ct["t"])
    fields = {k: doc[k] for k in ("time", "frame", "reference_point", "body_frame", "levers_m", "rate_hz",
                                  "base_station") if k in doc}
    q = dict(doc.get("quality") or {})
    fields.update({
        "quality": q,
        "recording_holes": rh,
        "strip": {"note": SCHEMA_NOTE, "from_trajectory": doc.get("id"),
                  "alignment": (inputs.align_doc or {}).get("id"),
                  "filled_records": int(ct["filled"].sum()),
                  "lines": {v["name"]: {"moved_median_m": round(v["moved"]["median_m"], 4),
                                        "moved_in_holes_median_m": round(v["moved"]["in_holes_median_m"], 4)}
                            for v in summary["lines"].values()},
                  "before": summary["before"], "after": summary["after"]},
    })
    used = {"pospro": doc.get("id")}
    if inputs.align_doc:
        used["alignpro"] = inputs.align_doc.get("id")
    return handover.publish(inputs.dcs, "strippro", files, fields, "StripPro", plugin_version, "strip 1",
                            used=used, inputs=inputs.files)
