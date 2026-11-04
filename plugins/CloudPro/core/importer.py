"""Import .clraw recordings into a CloudPro project: decode the Lidar
packets, georeference every return at its firing time with the PosPro
trajectory (or the Certus real-time track) and the AlignPro mounting (or the
recorded one), and write the points into the tile store.

Chunks are decoded on a thread pool (numpy and zstd release the GIL), and
the results are written in order by the calling thread.
"""
import concurrent.futures as cf
import os
import time

import numpy as np

from . import clraw
from .georef import GeorefContext
from .geodesy import Projector
from .store import REC
from .trajectory import from_nav_states, read_sbet


class ImportOptions:
    def __init__(self, **kw):
        self.returns = "both"            # both | last | strongest
        self.drop_duplicates = True
        self.rmin = 1.0
        self.rmax = 120.0
        self.window = "recording"        # recording | all | marks
        self.decimate = 1                # keep every n-th packet
        self.mount = None                # (roll, pitch, heading) deg override, else hardware.json
        self.lever = None                # (F, S, D) m override
        self.bore = (0.0, 0.0, 0.0)      # extra boresight correction deg (normally 0: AlignPro gives the mount)
        self.latency = 0.0               # Lidar time offset s (AlignPro latency_s)
        self.source_crs = None           # trajectory datum, default = target's geographic CRS
        self.geoid = None                # callable(lat_deg, lon_deg) -> N, or None
        self.threads = max(1, min(8, (os.cpu_count() or 2)))
        self.time_range = None           # (t0, t1) UTC s
        self.__dict__.update(kw)


def load_trajectory(path_or_rec, week_start=None):
    if isinstance(path_or_rec, clraw.Recording):
        nav = path_or_rec.nav_states()
        if nav is None:
            raise ValueError("no ANPP packet 20 in the recording")
        return from_nav_states(*nav)
    return read_sbet(path_or_rec, week_start=week_start)


def import_recording(project, rec, traj, opts, progress=None, cancel=None, line_name=None):
    """Import one Recording; returns the new line id and statistics."""
    store = project.store
    proj = Projector(project.crs, source=opts.source_crs)
    t0, t1 = rec.recording_window()
    if opts.time_range:
        t0 = max(t0 or -1e18, opts.time_range[0])
        t1 = min(t1 or 1e18, opts.time_range[1])
    sub = traj.subset(t0 if t0 else traj.t[0], t1 if t1 else traj.t[-1], pad=2.0)
    if len(sub) < 2:
        raise ValueError("the trajectory (%s) does not cover line %s" % (traj.source, rec.line))
    if store.origin is None:
        E, N = proj.forward(sub.lat[:1], sub.lon[:1])
        store.origin = np.array([np.floor(E[0] / 1000) * 1000, np.floor(N[0] / 1000) * 1000,
                                 np.floor(sub.h[0] / 100) * 100])
    mount = opts.mount if opts.mount is not None else rec.mount()
    lever = opts.lever if opts.lever is not None else rec.lever_arm()
    ctx = GeorefContext(sub, proj, mount, lever, opts.bore, opts.geoid, origin=store.origin, latency=opts.latency)
    info = {"name": line_name or "Line %s" % rec.line, "line": rec.line, "files": rec.paths,
            "trajectory": traj.source, "mount": list(mount), "lever": list(lever), "bore": list(opts.bore),
            "latency_s": opts.latency, "decimate": opts.decimate, "drop_duplicates": opts.drop_duplicates,
            "returns": opts.returns, "rmin": opts.rmin, "rmax": opts.rmax, "calibration": rec.calib_source,
            "crs": project.crs, "projector": proj.backend}
    lid = project.add_line(info)
    chunks = rec.chunk_list()
    tmin, tmax = sub.t[0], sub.t[-1]
    stats = {"packets": 0, "points": 0, "outside_traj": 0, "bad_chunks": 0}
    started = time.time()

    def work(job):
        fi, ci = job
        cache = {}
        try:
            P = rec.read_lidar_chunk(cache, fi, ci)
        finally:
            for fh in cache.values():
                fh.close()
        if opts.decimate > 1 and len(P):
            P = P[::opts.decimate]
        if len(P) == 0:
            return None, 0, 0
        d = clraw.decode_hesai(P, rec.calib, opts.rmin, opts.rmax, opts.returns, opts.drop_duplicates)
        if d is None or len(d["t"]) == 0:
            return None, len(P), 0
        t = d["t"]
        m = (t >= tmin) & (t <= tmax)
        if t0:
            m &= t >= t0
        if t1:
            m &= t <= t1
        nout = int((~m).sum())
        if not m.any():
            return None, len(P), nout
        if not m.all():
            d = {k: v[m] for k, v in d.items()}
            t = d["t"]
        E, N, Z = ctx.points(t, d["x"], d["y"], d["z"])
        r = np.zeros(len(t), REC)
        r["x"] = np.round(E * 1000).astype(np.int32)
        r["y"] = np.round(N * 1000).astype(np.int32)
        r["z"] = np.round(Z * 1000).astype(np.int32)
        r["t"] = t
        r["i"] = d["intensity"]
        r["ret"] = d["ret"]
        r["ch"] = d["channel"]
        r["cls"] = 1                       # ASPRS 1 = unclassified
        r["line"] = lid
        r["rng"] = np.clip(np.round(d["range"] * 100), 0, 65535).astype(np.uint16)
        r["az"] = np.round(d["azimuth"] * 100).astype(np.uint16) % 36000
        return r, len(P), nout

    with cf.ThreadPoolExecutor(max_workers=opts.threads) as ex:
        window = opts.threads * 2
        pending = []
        it = iter(chunks)
        done = 0
        while True:
            while len(pending) < window:
                try:
                    pending.append(ex.submit(work, next(it)))
                except StopIteration:
                    break
            if not pending:
                break
            fut = pending.pop(0)
            r, np_, nout = fut.result()
            done += 1
            stats["packets"] += np_
            stats["outside_traj"] += nout
            if r is not None:
                store.append(r)
                stats["points"] += len(r)
            if progress and done % 4 == 0:
                progress(done / max(1, len(chunks)), stats)
            if cancel and cancel():
                for f in pending:
                    f.cancel()
                break
    store.flush()
    stats["bad_chunks"] = sum(f.bad_chunks for f in rec.files)
    stats["seconds"] = time.time() - started
    # sensor track at 20 Hz for display, sections and the multi-pass filters
    ta = max(sub.t[0], t0 or sub.t[0])
    tb = min(sub.t[-1], t1 or sub.t[-1])
    tt = np.arange(ta, tb, 0.05)
    if len(tt) >= 2:
        E, N, Z, hd = ctx.sensor_track(tt)
        project.save_track(lid, tt, E, N, Z, hd)
    spans = []
    for f in rec.files:
        if f.chunk_pos:
            spans.append([min(c[5] for c in f.chunk_pos) * 1e-9, max(c[6] for c in f.chunk_pos) * 1e-9])
        else:
            spans.append([None, None])
    info.update(points=stats["points"], t0=float(ta), t1=float(tb), stats=stats, file_spans=spans,
                file_sizes=[os.path.getsize(p_) if os.path.exists(p_) else 0 for p_ in rec.paths])
    project.lines[lid].update(info)
    project.log("Imported %s: %d points (%s, %s)" % (info["name"], stats["points"], traj.source, proj.backend))
    project.save()
    return lid, stats


def rebuild_grids(project, keys=None, progress=None):
    keys = keys if keys is not None else project.store.keys()
    for n, k in enumerate(keys):
        project.store.build_grid(k)
        if progress and n % 8 == 0:
            progress((n + 1) / max(1, len(keys)), None)


# ---------------------------------------------------------------------------
# DCS chain: georeference the job's captures with the chain's inputs, and
# re-georeference after PosPro / AlignPro publish again, keeping every edit.
# ---------------------------------------------------------------------------
def edit_key(t, ch, ret):
    return (np.round(np.asarray(t) * 1e6).astype(np.int64) << 7) | (np.asarray(ch).astype(np.int64) << 1) | \
        (np.asarray(ret).astype(np.int64) & 1)


def collect_edits(project):
    """Every edited point (status, class, height shift) keyed by firing time + laser + return."""
    keys, flag, cls, dz = [], [], [], []
    for k in project.store.keys():
        a = project.store.tile_array(k)
        m = (a["flag"] != 0) | (a["cls"] != 1) | (a["dz"] != 0)
        if m.any():
            r = np.asarray(a[m])
            keys.append(edit_key(r["t"], r["ch"], r["ret"]))
            flag.append(r["flag"])
            cls.append(r["cls"])
            dz.append(r["dz"])
    if not keys:
        return None
    key = np.concatenate(keys)
    o = np.argsort(key, kind="stable")
    return {"key": key[o], "flag": np.concatenate(flag)[o], "cls": np.concatenate(cls)[o], "dz": np.concatenate(dz)[o]}


def apply_edits(project, ed, progress=None):
    """Put collected edits back on the re-georeferenced points (same firing, laser and return)."""
    if ed is None or len(ed["key"]) == 0:
        return 0
    n = 0
    keys = project.store.keys()
    for i, k in enumerate(keys):
        a = project.store.tile_array(k, writable=True)
        if len(a) == 0:
            continue
        kk = edit_key(a["t"], a["ch"], a["ret"])
        pos = np.clip(np.searchsorted(ed["key"], kk), 0, len(ed["key"]) - 1)
        hit = ed["key"][pos] == kk
        if hit.any():
            j = np.flatnonzero(hit)
            p = pos[hit]
            a["flag"][j] = ed["flag"][p]
            a["cls"][j] = ed["cls"][p]
            a["dz"][j] = ed["dz"][p]
            a["z"][j] = a["z"][j] + ed["dz"][p].astype(np.int32)
            n += len(j)
        if progress and i % 8 == 0:
            progress((i + 1) / max(1, len(keys)), "restoring edits")
    project.store.sync()
    return n


def import_chain(project, inp, opts, files=None, progress=None, cancel=None, keep_edits=True):
    """(Re)georeference the job's .clraw (or the given files) with the chain inputs. Existing points are
    replaced; with keep_edits the manual and filter edits are carried over to the new points.
    Returns (lines [(lid, stats)], edits restored)."""
    from . import clraw as _clraw
    files = files or inp.files
    groups = _clraw.group_recordings(files)
    if not groups:
        raise ValueError("No .clraw files found for this job")
    old_lines = {tuple(sorted(l.get("files", []))): l for l in project.lines}
    ed = collect_edits(project) if keep_edits and project.lines else None
    project.reset_points()
    opts.latency = inp.latency
    if inp.source_crs:
        opts.source_crs = inp.source_crs
    out = []
    n = len(groups)
    for k, (key, paths) in enumerate(groups.items()):         # in recording order (DCS Core)
        rec = _clraw.Recording(paths)
        traj = inp.traj if inp.traj is not None else load_trajectory(rec)
        o = ImportOptions(**dict(opts.__dict__))
        prev = old_lines.get(tuple(sorted(rec.paths)))
        if prev:                      # keep the per-line point options used before
            for f in ("returns", "rmin", "rmax", "decimate", "drop_duplicates"):
                if f in prev:
                    setattr(o, f, prev[f])
        if inp.mount is not None:
            o.mount = inp.mount
        if inp.lever is not None:
            o.lever = inp.lever
        lid, st = import_recording(project, rec, traj, o,
                                   progress=(lambda f, s, k=k: progress((k + f) / n * 0.9, "line %s: %s points" % (
                                       rec.line, "{:,}".format(s["points"])))) if progress else None,
                                   cancel=cancel, line_name=prev.get("name") if prev else None)
        project.lines[lid]["calibration"] = inp.align_info.get("calibration")
        out.append((lid, st))
        if cancel and cancel():
            break
    restored = apply_edits(project, ed, progress) if ed is not None else 0
    if progress:
        progress(0.95, "building the project grids")
    rebuild_grids(project)
    project.data["inputs"] = inp.record()
    project.data["inputs"]["georeferenced"] = _now()
    project.data["inputs"]["warnings"] = list(inp.warnings)
    project.log("Georeferenced %d line(s) with %s; %s%s" % (
        len(out), inp.traj_info.get("description", "?"), inp.align_info.get("calibration"),
        ("; %d edited points carried over" % restored) if restored else ""))
    project.save()
    return out, restored


def _now():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
