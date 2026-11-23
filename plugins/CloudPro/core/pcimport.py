"""Point clouds imported into a CloudPro job (CloudPro 2.16.0; Sean, 23 Nov 2026: point clouds processed
without going through PosPro / AlignPro first).

Each file becomes one line (kind "pointcloud"), or one line per LAS / LAZ point source id, E57 scan or PTS
block when the file holds several ("smart" split; split "file" keeps one line per file). The points go into the
job's tile store like georeferenced points, with their colours in the tiles' colour files, the file's classes
(optional), and points the file marks withheld as rejected (reason "original"). They are in the project's CRS
(moved from the file's CRS when it differs).

Imported lines need no trajectory: georeferencing again (a job that also has Lidar or sonar lines) carries them
over unchanged (carry_over), edits included.
"""
import os
import time

import numpy as np

from . import pcread
from .geodesy import Reprojector, identify_crs, same_crs
from .store import REC, REJ_ORIG, REJECTED

KIND = "pointcloud"
MAX_OFFSET_M = 2_000_000.0          # int32 millimetres from the job origin


class ImportError_(ValueError):
    pass


class Cancelled(Exception):
    pass


class PcOptions:
    def __init__(self, source_crs=None, split="smart", keep_classes=True, withheld_rejected=True, decimate=1,
                 block=2_000_000):
        self.source_crs = source_crs        # None: the file's own CRS, else the project's
        self.split = split                  # "smart" | "file"
        self.keep_classes = keep_classes
        self.withheld_rejected = withheld_rejected
        self.decimate = max(1, int(decimate))
        self.block = block

    def record(self):
        return {"source_crs": self.source_crs, "split": self.split, "keep_classes": self.keep_classes,
                "withheld_rejected": self.withheld_rejected, "decimate": self.decimate}


def is_pointcloud_line(line):
    return (line or {}).get("kind") == KIND


def pointcloud_lines(project):
    return [ln for ln in project.lines if is_pointcloud_line(ln)]


def only_pointclouds(project):
    """True when every line of the job is an imported point cloud (no Lidar / sonar to georeference)."""
    return bool(project.lines) and all(is_pointcloud_line(ln) or ln.get("kind") == "interpolated"
                                       for ln in project.lines) and any(is_pointcloud_line(ln) for ln in project.lines)


def scan(paths):
    """-> [(path, Source or None, error text or None)] (headers only)."""
    out = []
    for p in paths:
        try:
            out.append((p, pcread.open_source(p), None))
        except (pcread.PointCloudError, OSError, ValueError) as e:
            out.append((p, None, str(e)))
    return out


def _stem(path):
    return os.path.splitext(os.path.basename(path))[0]


def import_files(project, paths, opts=None, progress=None, cancel=None):
    """Import point-cloud files into the job (appended to the current points; on an error or Cancel the lines
    added here are taken out again). -> ([(line id, stats)], warnings)."""
    opts = opts or PcOptions()
    store = project.store
    target = project.crs
    first_lid = len(project.lines)
    added = []
    warnings = []
    touched = set()
    started = time.time()
    try:
        total = 0
        sources = []
        for p in paths:
            src = pcread.open_source(p)
            sources.append(src)
            total += src.count or os.path.getsize(p) // 40
        done = 0
        for src in sources:
            crs = opts.source_crs or src.crs or target
            rp = None
            if not same_crs(crs, target):
                rp = Reprojector(crs, target)
            lines = {}                                    # part -> line id
            stats = {}
            t_min, t_max = np.inf, -np.inf

            def line_for(part, src=src, lines=lines, crs=crs, rp=rp):
                key = part if opts.split == "smart" else 0
                lid = lines.get(key)
                if lid is None:
                    label = src.parts.get(key) if src.parts else None
                    name = _stem(src.path)
                    if opts.split == "smart" and key is not None and (src.parts and len(src.parts) > 1 or
                                                                        src.part_kind == "point source"):
                        name += " · %s" % (label or "source %d" % key)
                    info = {"name": name, "kind": KIND, "files": [src.path], "part": None if key is None else int(key),
                            "part_label": label, "part_kind": src.part_kind, "format": src.fmt,
                            "source_crs": identify_crs(crs), "crs": target,
                            "moved": None if rp is None else "%s -> %s (%s)" % (identify_crs(crs)[:40], target, rp.backend),
                            "rgb": bool(src.has_rgb), "time_basis": src.time_basis, "options": opts.record(),
                            "imported": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "file_sizes": [os.path.getsize(src.path)], "points": 0}
                    lid = project.add_line(info)
                    lines[key] = lid
                    added.append(lid)
                    stats[lid] = {"points": 0, "rejected": 0}
                return lid

            for b in src.blocks(opts.block):
                if cancel and cancel():
                    raise Cancelled()
                n = len(b["x"])
                if n == 0:
                    continue
                if opts.decimate > 1:
                    b = {k: v[::opts.decimate] for k, v in b.items()}
                    n = len(b["x"])
                x, y, z = b["x"], b["y"], b["z"]
                ok = np.isfinite(x) & np.isfinite(y) & np.isfinite(z)
                if not ok.all():
                    b = {k: v[ok] for k, v in b.items()}
                    x, y, z = b["x"], b["y"], b["z"]
                    n = len(x)
                if rp is not None:
                    x, y, z = rp.transform(x, y, z)
                if store.origin is None:
                    store.origin = np.array([np.floor(np.median(x) / 1000) * 1000,
                                             np.floor(np.median(y) / 1000) * 1000,
                                             np.floor(np.median(z) / 100) * 100])
                dx, dy, dz = x - store.origin[0], y - store.origin[1], z - store.origin[2]
                if max(np.abs(dx).max(), np.abs(dy).max(), np.abs(dz).max()) > MAX_OFFSET_M:
                    raise ImportError_(
                        "%s: points lie more than 2,000 km from the job's other points: check the coordinate "
                        "system (the file says %s, the job is in %s)" % (src.name, identify_crs(crs)[:60] or "nothing",
                                                                         target))
                parts = b.get("part")
                if parts is None or opts.split == "file":
                    groups = [(None if opts.split == "smart" else 0, slice(None))]
                else:
                    u = np.unique(parts)
                    groups = [(int(v), parts == v) for v in u] if len(u) > 1 else [(int(u[0]), slice(None))]
                for part, sel in groups:
                    lid = line_for(part)
                    m = len(x) if isinstance(sel, slice) else int(sel.sum())
                    r = np.zeros(m, REC)
                    r["x"] = np.round(dx[sel] * 1000).astype(np.int32)
                    r["y"] = np.round(dy[sel] * 1000).astype(np.int32)
                    r["z"] = np.round(dz[sel] * 1000).astype(np.int32)
                    if "t" in b:
                        r["t"] = b["t"][sel]
                        t_min = min(t_min, float(np.min(b["t"][sel])))
                        t_max = max(t_max, float(np.max(b["t"][sel])))
                    if "i" in b:
                        r["i"] = b["i"][sel]
                    r["ret"] = b["ret"][sel] if "ret" in b else 2
                    r["cls"] = b["cls"][sel] if (opts.keep_classes and "cls" in b) else 1
                    if opts.withheld_rejected and "withheld" in b:
                        w = b["withheld"][sel]
                        r["flag"] = np.where(w, REJECTED | REJ_ORIG, 0)
                        stats[lid]["rejected"] += int(w.sum())
                    r["line"] = lid
                    parts_ = store.bin(r, b["rgb"][sel] if "rgb" in b else None)
                    touched.update(p_[0] for p_ in parts_)
                    store.append_binned(parts_)
                    stats[lid]["points"] += m
                done += n
                if progress:
                    progress(min(0.95, done / max(1, total)), "%s: %s points" % (src.name, "{:,}".format(done)))
            store.flush()
            if opts.split == "smart" and len(lines) == 1:            # one source id only: just the file name
                project.lines[next(iter(lines.values()))]["name"] = _stem(src.path)
            for lid in lines.values():
                ln = project.lines[lid]
                ln["points"] = stats[lid]["points"]
                ln["stats"] = dict(stats[lid], seconds=round(time.time() - started, 1))
                if np.isfinite(t_min):
                    ln["t0"], ln["t1"] = t_min, t_max
                ln["file_spans"] = [[ln.get("t0"), ln.get("t1")]]
            if src.notes:
                warnings += ["%s: %s" % (src.name, n_) for n_ in src.notes]
            if not lines:
                warnings.append("%s: no points" % src.name)
        store.save_meta()
        from .importer import rebuild_grids
        if progress:
            progress(0.96, "building the project grids")
        rebuild_grids(project, sorted(touched))
        project.data.setdefault("pointclouds", {})["imported"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        n_pts = sum(project.lines[l_]["points"] for l_ in added)
        project.log("Imported %d point-cloud file(s) as %d line(s): %s points in %.1f s" % (
            len(sources), len(added), "{:,}".format(n_pts), time.time() - started))
        project.save()
        return [(l_, project.lines[l_].get("stats", {})) for l_ in added], warnings
    except BaseException:
        store.flush()
        for lid in sorted(added, reverse=True):
            try:
                store.remove_line(lid)
            except Exception:       # noqa: BLE001
                pass
        del project.lines[first_lid:]
        store.save_meta()
        raise


def remove(project, lids):
    """Take imported lines out of the job (their points removed; the line entries stay, empty and disabled, so
    the other lines keep their numbers)."""
    keys = set()
    for lid in lids:
        ln = project.lines[lid]
        if not is_pointcloud_line(ln):
            continue
        keys.update(project.store.remove_line(lid))
        ln["points"] = 0
        ln["enabled"] = False
        ln["removed"] = True
    from .importer import rebuild_grids
    rebuild_grids(project, sorted(keys))
    project.save()
    return sorted(keys)


def carry_over(project, old_store, old_lines, old_crs=None):
    """While georeferencing again (Project.begin_rebuild): copy the imported lines' points from the store being
    replaced into the new one, with their edits and colours. Called before the Lidar / sonar lines are imported;
    sets the new store's origin to the old one. -> {old line id: new line id}."""
    keep = [ln for ln in old_lines if is_pointcloud_line(ln) and not ln.get("removed")]
    if not keep or old_store is None:
        return {}
    st = project.store
    if st.origin is None and old_store.origin is not None:
        st.origin = np.array(old_store.origin, float)
    shift = None
    if old_store.origin is not None and st.origin is not None:
        shift = np.round((np.asarray(old_store.origin) - st.origin) * 1000).astype(np.int64)
    rp = None
    if old_crs and not same_crs(old_crs, project.crs):         # the job's CRS changed with this georeference
        rp = Reprojector(old_crs, project.crs)
    remap = {}
    for ln in keep:
        info = {k: v for k, v in ln.items() if k != "id"}
        if rp is not None:
            info["crs"] = project.crs
            info["moved"] = ((info.get("moved") or "") + "; then %s -> %s" % (old_crs, project.crs)).lstrip("; ")
        remap[ln["id"]] = project.add_line(info)
    lut = np.full(max(remap) + 1, -1, np.int64)
    for a, b in remap.items():
        lut[a] = b
    for k in old_store.keys():
        a = old_store.tile_array(k)
        ln_ = a["line"].astype(np.int64)
        m = (ln_ < len(lut))
        m[m] &= lut[ln_[m]] >= 0
        if not m.any():
            continue
        r = np.array(a[m])
        r["line"] = lut[r["line"].astype(np.int64)]
        if rp is not None:
            o = np.asarray(old_store.origin, float)
            X, Y, Z = rp.transform(r["x"] * 1e-3 + o[0], r["y"] * 1e-3 + o[1], r["z"] * 1e-3 + o[2])
            r["x"] = np.round((X - st.origin[0]) * 1000).astype(np.int32)
            r["y"] = np.round((Y - st.origin[1]) * 1000).astype(np.int32)
            r["z"] = np.round((Z - st.origin[2]) * 1000).astype(np.int32)
        elif shift is not None and shift.any():
            r["x"] = (r["x"].astype(np.int64) + shift[0]).astype(np.int32)
            r["y"] = (r["y"].astype(np.int64) + shift[1]).astype(np.int32)
            r["z"] = (r["z"].astype(np.int64) + shift[2]).astype(np.int32)
        c = old_store.tile_rgb(k)
        cols = None
        if c is not None:
            mm = m[:len(c)]
            cols = np.zeros((len(r), 3), np.uint8)
            cols[:int(mm.sum())] = np.asarray(c)[mm]
        st.append(r, cols)
    st.flush()
    return remap


def pointcloud_line_ids(project):
    return {ln["id"] for ln in project.lines if is_pointcloud_line(ln)}
