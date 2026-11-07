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
from dcs_core import epochs

from .geodesy import Projector, needs_datum_shift, trajectory_to_datum
from .store import REC
from dcs_core.hesai import packet_times
from .trajectory import from_nav_states, read_sbet


class ImportOptions:
    def __init__(self, **kw):
        self.returns = "both"            # both | last | strongest
        self.drop_duplicates = True
        self.rmin = 1.0
        self.rmax = 120.0
        self.window = "recording"        # recording | all | marks
        self.decimate = 1                # keep every n-th packet
        self.min_speed = 0.0             # m/s: packets fired while slower than this are skipped (0 = keep all)
        self.mount = None                # (roll, pitch, heading) deg override, else hardware.json
        self.lever = None                # (F, S, D) m override
        self.bore = (0.0, 0.0, 0.0)      # extra boresight correction deg (normally 0: AlignPro gives the mount)
        self.latency = 0.0               # Lidar time offset s (AlignPro latency_s)
        self.source_crs = None           # trajectory datum, default = target's geographic CRS
        self.geoid = None                # callable(lat_deg, lon_deg) -> N, or None
        self.threads = max(1, min(16, (os.cpu_count() or 2) - 1))   # leave one core for QGIS
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
            "latency_s": opts.latency, "decimate": opts.decimate, "min_speed": opts.min_speed, "drop_duplicates": opts.drop_duplicates,
            "returns": opts.returns, "rmin": opts.rmin, "rmax": opts.rmax, "calibration": rec.calib_source,
            "crs": project.crs, "projector": proj.backend}
    lid = project.add_line(info)
    chunks = rec.chunk_list()
    tmin, tmax = sub.t[0], sub.t[-1]
    stats = {"packets": 0, "points": 0, "outside_traj": 0, "bad_chunks": 0, "stopped_packets": 0}
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
        nstop = 0
        if opts.min_speed > 0 and len(P):
            # standing still: skip the packets before decoding (the scanner only repeats the same spot)
            moving = ctx.speed_at(packet_times(P)) >= opts.min_speed
            nstop = int((~moving).sum())
            if nstop:
                P = P[moving]
        if len(P) == 0:
            return None, nstop, 0, nstop
        d = clraw.decode_hesai(P, rec.calib, opts.rmin, opts.rmax, opts.returns, opts.drop_duplicates)
        if d is None or len(d["t"]) == 0:
            return None, len(P) + nstop, 0, nstop
        t = d["t"]
        m = (t >= tmin) & (t <= tmax)
        if t0:
            m &= t >= t0
        if t1:
            m &= t <= t1
        nout = int((~m).sum())
        if not m.any():
            return None, len(P) + nstop, nout, nstop
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
        r["az"] = np.mod(np.round(d["azimuth"] * 100), 36000).astype(np.uint16)   # negative azimuths wrap
        return store.bin(r), len(P) + nstop, nout, nstop       # binned into tiles here, in the worker

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
            r, np_, nout, nst = fut.result()
            stats["stopped_packets"] += nst
            done += 1
            stats["packets"] += np_
            stats["outside_traj"] += nout
            if r is not None:
                store.append_binned(r)
                stats["points"] += sum(len(a) for _k, a in r)
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
class Cancelled(Exception):
    """The user stopped the georeferencing: the previous points and edits are kept."""


def edit_key(t, ch, ret):
    return (np.round(np.asarray(t) * 1e6).astype(np.int64) << 7) | (np.asarray(ch).astype(np.int64) << 1) | \
        (np.asarray(ret).astype(np.int64) & 1)


EDIT_REC = np.dtype([("key", "<i8"), ("flag", "u1"), ("cls", "u1"), ("dz", "<i2")])


class EditSpool:
    """Edited points (status, class, height shift) keyed by firing time + laser + return, kept on disk in
    5 s buckets of firing time, so carrying the edits of a classified cloud of any size over to new points
    needs only one bucket in memory at a time."""

    BUCKET_US = 5_000_000

    def __init__(self, folder):
        import shutil
        self.dir = folder
        shutil.rmtree(folder, ignore_errors=True)
        os.makedirs(folder, exist_ok=True)
        self.count = 0
        self._sorted = False
        self._cache = {}

    def _path(self, b):
        return os.path.join(self.dir, "b_%d.bin" % b)

    @classmethod
    def bucket(cls, key):
        return (np.asarray(key) >> 7) // cls.BUCKET_US

    def add(self, key, flag, cls_, dz):
        r = np.empty(len(key), EDIT_REC)
        r["key"], r["flag"], r["cls"], r["dz"] = key, flag, cls_, dz
        b = self.bucket(key)
        o = np.argsort(b, kind="stable")
        b, r = b[o], r[o]
        cuts = np.r_[0, np.flatnonzero(np.diff(b)) + 1, len(b)]
        for s_, e_ in zip(cuts[:-1], cuts[1:]):
            with open(self._path(int(b[s_])), "ab") as f:
                r[s_:e_].tofile(f)
        self.count += len(r)

    def finish(self):
        """Sort every bucket by key (once)."""
        for fn in os.listdir(self.dir):
            p = os.path.join(self.dir, fn)
            a = np.fromfile(p, EDIT_REC)
            a = a[np.argsort(a["key"], kind="stable")]
            a.tofile(p)
        self._sorted = True

    def _load(self, b):
        a = self._cache.get(b)
        if a is None:
            p = self._path(b)
            a = np.fromfile(p, EDIT_REC) if os.path.exists(p) else np.zeros(0, EDIT_REC)
            if len(self._cache) > 16:
                self._cache.pop(next(iter(self._cache)))
            self._cache[b] = a
        return a

    def lookup(self, keys):
        """-> (positions in keys that were edited, their EDIT_REC records)."""
        b = self.bucket(keys)
        pos_out, rec_out = [], []
        for bb in np.unique(b):
            a = self._load(int(bb))
            if len(a) == 0:
                continue
            m = np.flatnonzero(b == bb)
            kk = keys[m]
            p = np.clip(np.searchsorted(a["key"], kk), 0, len(a) - 1)
            hit = a["key"][p] == kk
            if hit.any():
                pos_out.append(m[hit])
                rec_out.append(a[p[hit]])
        if not pos_out:
            return np.zeros(0, np.int64), np.zeros(0, EDIT_REC)
        return np.concatenate(pos_out), np.concatenate(rec_out)

    def close(self):
        import shutil
        self._cache = {}
        shutil.rmtree(self.dir, ignore_errors=True)


def collect_edits(project, folder=None):
    """Every edited point of the current store into an EditSpool (None when nothing is edited)."""
    sp = EditSpool(folder or os.path.join(project.work, "edits_pending"))
    for k in project.store.keys():
        a = project.store.tile_array(k)
        m = (a["flag"] != 0) | (a["cls"] != 1) | (a["dz"] != 0)
        if m.any():
            r = np.asarray(a[m])
            sp.add(edit_key(r["t"], r["ch"], r["ret"]), r["flag"], r["cls"], r["dz"])
    project.store.close()
    if sp.count == 0:
        sp.close()
        return None
    sp.finish()
    return sp


def apply_edits(project, ed, progress=None, cancel=None):
    """Put collected edits back on the re-georeferenced points (same firing, laser and return)."""
    if ed is None or ed.count == 0:
        return 0
    n = 0
    keys = project.store.keys()
    for i, k in enumerate(keys):
        if cancel and cancel():
            raise Cancelled()
        a = project.store.tile_array(k, writable=True)
        if len(a) == 0:
            continue
        j, r = ed.lookup(edit_key(a["t"], a["ch"], a["ret"]))
        if len(j):
            a["flag"][j] = r["flag"]
            a["cls"][j] = r["cls"]
            a["dz"][j] = r["dz"]
            a["z"][j] = a["z"][j] + r["dz"].astype(np.int32)
            n += len(j)
        if progress and i % 8 == 0:
            progress((i + 1) / max(1, len(keys)), "restoring edits")
    project.store.sync()
    return n


def move_epoch(inp, fr, datum, want, target_crs):
    """Bring a NAD83(CSRS) trajectory (PPK in its base's epoch, or PPP after the move from ITRF2020, at the
    observation date) to the deliverable epoch `want` with NRCan's velocity grid. Returns the datum record
    to store, or None when nothing is moved (already there, real-time track, other datum, 'as observed')."""
    if inp.traj is None or getattr(inp.traj, "epoch_moved", None) is not None:
        return None
    to = epochs.parse(want)
    target_name = ""
    try:
        import pyproj
        target_name = pyproj.CRS.from_user_input(target_crs).geodetic_crs.name
    except Exception:       # noqa: BLE001
        target_name = "NAD83(CSRS)" if "2953" in str(target_crs) else ""
    frame = (datum or {}).get("to") or fr.get("name") or ""
    if to is None or not epochs.is_nad83csrs(frame) or not epochs.is_nad83csrs(target_name):
        return None
    src = epochs.parse((datum or {}).get("epoch") or fr.get("epoch"))
    if src is None:
        inp.warnings.append("The trajectory's NAD83(CSRS) epoch is not known (base position without an epoch), "
                            "so it is used as epoch %.1f as it is." % to)
        return None
    if abs(src - to) < 0.005:
        return None
    import numpy as np
    tr = inp.traj
    try:
        lon, lat, h = epochs.propagate(np.degrees(tr.lon), np.degrees(tr.lat), tr.h, src, to)
    except epochs.EpochError as e:
        inp.warnings.append("The trajectory was left at epoch %.3f: %s" % (src, e))
        return None
    dn = float(np.median(np.radians(lat) - tr.lat)) * 6371000.0
    de = float(np.median((np.radians(lon) - tr.lon) * np.cos(tr.lat))) * 6371000.0
    du = float(np.median(h - tr.h))
    tr.lat, tr.lon, tr.h = np.radians(lat), np.radians(lon), h
    note = ("epoch %.3f to %.1f with NRCan's NAD83(CSRS) velocity grid: %.3f m north, %.3f m east, %.3f m up" %
            (src, to, dn, de, du))
    tr.epoch_moved = note
    d = dict(datum or {"from": fr, "to": frame})
    d["note"] = ("%s; %s" % (d["note"], note)) if d.get("note") else "trajectory moved from " + note
    d["epoch"] = "%.1f" % to
    if hasattr(inp, "datum_note"):
        inp.datum_note = d["note"]
    return d


def import_chain(project, inp, opts, files=None, progress=None, cancel=None, keep_edits=True, settings=None):
    """(Re)georeference the job's .clraw (or the given files) with the chain inputs. The new points are built
    beside the current ones (Project.begin_rebuild) and replace them only when everything worked: on an error
    or Cancel (raises Cancelled) the previous points, edits and settings are kept. With keep_edits the manual
    and filter edits are carried over to the new points. A line the trajectory does not cover is skipped with
    a warning. settings: project values that describe the points (crs, vertical, geoid, geoid_path), applied
    with the new points and put back with the old ones on failure. Returns (lines [(lid, stats)], edits
    restored)."""
    from . import clraw as _clraw
    files = files or inp.files
    groups = _clraw.group_recordings(files)
    if not groups:
        raise ValueError("No .clraw files found for this job")
    old_lines = {tuple(sorted(l.get("files", []))): l for l in project.lines}
    opts.latency = inp.latency
    if inp.source_crs:
        opts.source_crs = inp.source_crs
    settings = dict(settings or {})
    target_crs = settings.get("crs") or project.crs
    fr = (inp.traj_info or {}).get("frame") or {}
    datum = None
    if inp.traj is not None and needs_datum_shift(fr.get("name")) and not getattr(inp.traj, "datum_moved", None):
        # PosPro PPP trajectories are in ITRF2020 at the observation date: move them to the target's datum
        # (raises DatumError rather than leaving the cloud metres off)
        inp.datum_note, to = trajectory_to_datum(inp.traj, fr.get("name"), fr.get("epoch"), target_crs)
        inp.traj.datum_moved = inp.datum_note
        inp.datum_to = to
    if inp.traj is not None and getattr(inp.traj, "datum_moved", None):
        opts.source_crs = None
        datum = {"from": fr, "note": inp.traj.datum_moved, "to": getattr(inp, "datum_to", ""),
                 "epoch": fr.get("epoch")}
    # the coordinate epoch of the deliverable (New Brunswick 1997.0, federal 2010.0, or as observed)
    want = settings.get("epoch", project.data.get("epoch", "default"))
    if want == "default":
        want = "%.1f" % epochs.default_epoch(target_crs)
    datum = move_epoch(inp, fr, datum, want, target_crs) or datum
    ed = collect_edits(project) if keep_edits and project.lines else None
    project.begin_rebuild()
    try:
        project.data.update(settings)
        project.store.crs = project.crs
        if datum:
            project.data["trajectory_datum"] = datum
        else:
            project.data.pop("trajectory_datum", None)
        out = []
        n = len(groups)
        for k, (key, paths) in enumerate(groups.items()):         # in recording order (DCS Core)
            if cancel and cancel():
                raise Cancelled()
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
            if not _covers(traj, rec):
                if prev:
                    # dropping a line that has points (and perhaps edits) now would lose them: refuse instead
                    raise ValueError("Line %s is not covered by the trajectory (%s), but it was georeferenced "
                                     "before: nothing was changed. Process the whole time span in PosPro." % (
                                         rec.line, inp.traj_info.get("description") or traj.source))
                inp.warnings.append("Line %s is not covered by the trajectory (%s): not georeferenced" % (
                    rec.line, traj.source))
                continue
            lid, st = import_recording(project, rec, traj, o,
                                       progress=(lambda f, s, k=k, rec=rec: progress((k + f) / n * 0.9,
                                                 "line %s: %s points" % (rec.line, "{:,}".format(s["points"]))))
                                       if progress else None,
                                       cancel=cancel, line_name=prev.get("name") if prev else None)
            if cancel and cancel():
                raise Cancelled()
            project.lines[lid]["calibration"] = inp.align_info.get("calibration")
            out.append((lid, st))
        if not out:
            raise ValueError("No line could be georeferenced: the trajectory (%s) covers none of the %d line(s)"
                             % (inp.traj_info.get("description") or "?", n))
        restored = apply_edits(project, ed, progress, cancel) if ed is not None else 0
        if progress:
            progress(0.95, "building the project grids")
        rebuild_grids(project)
        project.data["inputs"] = inp.record()
        project.data["inputs"]["georeferenced"] = _now()
        project.data["inputs"]["warnings"] = list(inp.warnings)
        project.data["inputs"]["crs"] = project.crs
        project.log("Georeferenced %d line(s) with %s; %s%s" % (
            len(out), inp.traj_info.get("description", "?"), inp.align_info.get("calibration"),
            ("; %d edited points carried over" % restored) if restored else ""))
        project.commit_rebuild()
    except BaseException:
        project.abort_rebuild()
        raise
    finally:
        if ed is not None:
            ed.close()
    return out, restored


def _covers(traj, rec):
    """Does the trajectory overlap the recording's time window at all?"""
    try:
        t0, t1 = rec.recording_window()
    except Exception:           # noqa: BLE001
        return True
    if not t0 or not t1:
        return True
    return len(traj) >= 2 and traj.t[0] < t1 and traj.t[-1] > t0


def _now():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
