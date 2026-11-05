"""Data for the Data View & Fine Tuning.

Where it looks (view_region): around the roll / pitch / heading inspection boxes, else the area of
interest, else the wall patches.

What it shows (fine_view): inside the inspection boxes, every point the Lidar recorded there (full
density, re-read from the raw .clraw for just the seconds each pass was near the box, then cached);
elsewhere in the plan, the thinned line cache.
"""
import hashlib
import json
import os
import time

import numpy as np

from . import clraw, engine, hesai

DENSE_VERSION = 1
BOX_KINDS = ("roll", "pitch", "heading")
PAD_M = 1.5                 # dense points kept this far outside a box (room for live adjustments)
WIN_GAP_S = 0.5             # a pass's visits to a box closer than this are read as one window
WIN_PAD_S = 0.3
MAX_HALF = 150.0
FAR = 300.0                 # boxes further apart than this: centre on one of them


def box_frame(b, pad=0.0):
    """p0, u (along), v (across), length, half width; with pad metres added all round."""
    p0, p1 = np.array(b["p0"], float), np.array(b["p1"], float)
    L = float(np.linalg.norm(p1 - p0))
    if L < 1e-6:
        return None
    u = (p1 - p0) / L
    v = np.array([-u[1], u[0]])
    return p0 - u * pad, u, v, L + 2 * pad, float(b.get("width", 1.0)) / 2 + pad


def box_corners(b, pad=0.0):
    f = box_frame(b, pad)
    if f is None:
        return np.zeros((0, 2))
    p0, u, v, L, hw = f
    return np.array([p0 - v * hw, p0 + u * L - v * hw, p0 + u * L + v * hw, p0 + v * hw])


def in_box(xy, b, pad=0.0):
    f = box_frame(b, pad)
    if f is None:
        return np.zeros(len(xy), bool)
    p0, u, v, L, hw = f
    rel = xy - p0
    al, ac = rel @ u, rel @ v
    return (al >= 0) & (al <= L) & (np.abs(ac) <= hw)


def in_any(xy, boxes, pad=0.0):
    m = np.zeros(len(xy), bool)
    for b in boxes.values():
        m |= in_box(xy, b, pad)
    return m


def _geom(b):
    return [round(float(v), 2) for v in list(b["p0"]) + list(b["p1"])] + [round(float(b.get("width", 1)), 2)]


def signature(region):
    d = {"c": [round(float(v), 1) for v in region["center"]], "h": round(float(region["half"]), 1),
         "b": {k: _geom(b) for k, b in sorted(region["boxes"].items())}, "s": region.get("source")}
    return json.dumps(d, sort_keys=True)


def boxes_signature(project):
    bx = {k: b for k, b in (project.cfg.get("boxes") or {}).items() if k in BOX_KINDS}
    return json.dumps({k: _geom(b) for k, b in sorted(bx.items())}, sort_keys=True)


def aoi_xy(project):
    poly = project.cfg.get("aoi")
    if not poly or project.frame is None:
        return None
    pts = np.array(poly, float)
    return project.frame.from_geodetic(np.radians(pts[:, 1]), np.radians(pts[:, 0]), np.zeros(len(pts)))[:, :2]


def _square(pts, margin=10.0, minimum=15.0):
    lo, hi = pts.min(0), pts.max(0)
    c = (lo + hi) / 2
    half = float(np.clip(max(hi - lo) / 2 + margin, minimum, MAX_HALF))
    return c, half


def view_region(project, focus=None):
    """Region for the Data View. focus: 'roll' | 'pitch' | 'heading' (that box just drawn), 'aoi', or None."""
    boxes = {k: b for k, b in (project.cfg.get("boxes") or {}).items()
             if k in BOX_KINDS and box_frame(b) is not None}
    if focus == "aoi" or (not boxes and focus != "default"):
        xy = aoi_xy(project)
        if xy is not None and len(xy) >= 3:
            c, half = _square(xy)
            inside = {k: b for k, b in boxes.items() if _overlaps(b, c, half)}
            return _done({"source": "aoi", "center": c, "half": half, "boxes": inside})
    if boxes:
        sel = dict(boxes)
        pts = np.concatenate([box_corners(b) for b in sel.values()])
        if max(pts.max(0) - pts.min(0)) > FAR:
            k = focus if focus in boxes else ("heading" if "heading" in boxes else next(iter(boxes)))
            sel = {k: boxes[k]}
            pts = box_corners(boxes[k])
        c, half = _square(pts)
        sel = {k: b for k, b in boxes.items() if _overlaps(b, c, half)}
        return _done({"source": "boxes", "center": c, "half": half, "boxes": sel, "focus": focus})
    return _done({"source": "default", "center": np.asarray(project.view_center(), float)[:2], "half": 40.0,
                  "boxes": {}})


def area_region(project, center, half, source="area"):
    """A square chosen by the user (plan view area or map extent); boxes inside it load at full density."""
    half = float(np.clip(half, 5.0, MAX_HALF))
    c = np.asarray(center, float)[:2]
    boxes = {k: b for k, b in (project.cfg.get("boxes") or {}).items()
             if k in BOX_KINDS and box_frame(b) is not None and _overlaps(b, c, half)}
    return _done({"source": source, "center": c, "half": half, "boxes": boxes})


def _overlaps(b, c, half):
    q = box_corners(b)
    return bool(len(q)) and q[:, 0].max() >= c[0] - half and q[:, 0].min() <= c[0] + half and \
        q[:, 1].max() >= c[1] - half and q[:, 1].min() <= c[1] + half


def _done(r):
    r["center"] = np.asarray(r["center"], float)
    r["sig"] = signature(r)
    return r


# ------------------------------------------------------------------ full density
def _windows(t, lo, hi):
    if not len(t):
        return []
    t = np.sort(t)
    cut = np.nonzero(np.diff(t) > WIN_GAP_S)[0]
    starts = np.concatenate([[t[0]], t[cut + 1]])
    ends = np.concatenate([t[cut], [t[-1]]])
    out = []
    for a, b in zip(starts, ends):
        a, b = max(lo, a - WIN_PAD_S), min(hi, b + WIN_PAD_S)
        if b > a:
            out.append((float(a), float(b)))
    return out


def _cache_key(project, boxes, used, budget):
    h = hashlib.sha1()
    h.update(("dense%d" % DENSE_VERSION).encode())
    for k in ("returns", "range_min", "range_max", "intensity_min"):
        h.update(("%s=%s" % (k, project.settings.get(k))).encode())
    h.update(("budget=%d" % budget).encode())
    h.update(json.dumps({k: _geom(b) for k, b in sorted(boxes.items())}).encode())
    # the applied values only choose which points fall inside a box (+ pad): small changes reuse the cache
    h.update(json.dumps([round(float(v) * 2) for v in project.cfg["mount"]] +
                        [round(float(v) * 4) for v in project.cfg["lever"]] +
                        [round(float(project.cfg["latency_ms"]) / 20)]).encode())
    h.update(json.dumps(project.traj_info.get("handover_id") if getattr(project, "traj_info", None) else "").encode())
    for i in used:
        ln = project.lines[i]
        h.update(("%s|%.3f|%.3f" % (ln.name, ln.window[0], ln.window[1])).encode())
        for pth in ln.info.get("paths", []):
            try:
                h.update(("%s|%d|%d" % (os.path.basename(pth), os.path.getsize(pth), int(os.path.getmtime(pth))))
                         .encode())
            except OSError:
                h.update(pth.encode())
    return h.hexdigest()[:16]


def dense_points(project, boxes, progress=None, cancelled=None):
    """Every recorded point inside the boxes (+ PAD_M), from the raw .clraw.

    -> dict(t, p (sensor frame), i, line, stats) or None without boxes. Points are selected with the
    applied values; the pad leaves room for the adjustments made while fine tuning."""
    if not boxes:
        return None
    budget = int(project.settings.get("fine_points_per_box", 1500000))
    used = [i for i, ln in enumerate(project.lines) if ln.use and ln.x is not None and len(ln.x)]
    key = _cache_key(project, boxes, used, budget)
    cpath = os.path.join(project.work, "fine_%s.npz" % key) if project.work else None
    names = [project.lines[i].name for i in range(len(project.lines))]
    if cpath and os.path.isfile(cpath):
        try:
            with np.load(cpath) as z:
                out = {k: z[k] for k in z.files}
            meta = json.loads(bytes(out.pop("meta")).decode())
            if meta.get("names") == names:
                out["stats"] = dict(meta["stats"], cached=True)
                return out
        except Exception:  # noqa: BLE001
            pass
    t_start = time.time()
    # 1 which seconds of which capture: the times each pass's cached points were inside a box
    per_capture = {}
    for i in used:
        ln = project.lines[i]
        m = in_any(ln.x[:, :2], boxes, PAD_M)
        if not m.any():
            continue
        t = ln.data["t"][ln.idx[m]]
        wins = _windows(t, ln.window[0], ln.window[1])
        if wins:
            per_capture.setdefault(id(ln.data), (ln.data, []))[1].extend((a, b, i) for a, b in wins)
    stats = {"windows": 0, "seconds": 0.0, "packets": 0, "decoded": 0, "kept": 0, "missing": [], "per_box": {}}
    parts = {k: [] for k in ("t", "p", "i", "line")}
    caps = list(per_capture.values())
    rs, rmin, rmax = (project.settings.get("returns", "strongest"), project.settings.get("range_min", 1.5),
                      project.settings.get("range_max", 60.0))
    imin = project.settings.get("intensity_min", 0)
    for ci, (data, wins) in enumerate(caps):
        wins.sort()
        stats["windows"] += len(wins)
        stats["seconds"] += sum(b - a for a, b, _ in wins)
        wa = np.array([w[0] for w in wins])
        wb = np.array([w[1] for w in wins])
        wl = np.array([w[2] for w in wins], np.int16)
        info = data["info"]
        off = float(info.get("log_minus_publish_ms") or 0.0) * 1e6
        lw = [(int(a * 1e9 + off - 3e9), int(b * 1e9 + off + 3e9)) for a, b, _ in wins]
        cal = info.get("calibration")
        paths = info.get("paths") or []
        for fi, pth in enumerate(paths):
            if not os.path.isfile(pth):
                stats["missing"].append(os.path.basename(pth))
                continue
            f = clraw.ClrawFile(pth)

            def prog(fr, ci=ci, fi=fi):
                if progress:
                    progress((ci + (fi + fr) / max(len(paths), 1)) / max(len(caps), 1) * 0.9,
                             "Reading full-density points: %s" % os.path.basename(pth))
            for chunk in _window_chunks(f, clraw.CH_POINTS, lw, prog):
                if cancelled and cancelled():
                    raise clraw_cancelled()
                pk, tp = [], []
                for ch, _lt, pt, payload in chunk:
                    ts = pt * 1e-9
                    k = np.searchsorted(wa, ts, side="right") - 1
                    if k >= 0 and ts <= wb[k] + 1e-3:
                        pk.append(payload)
                        tp.append(pt)
                if not pk:
                    continue
                a = hesai.packets_to_array(pk)
                stats["packets"] += len(a)
                dec = hesai.decode(a, np.array(tp, np.float64) * 1e-9, cal, rs, rmin, rmax, imin, keep=None)
                if dec is None or not len(dec["t"]):
                    continue
                k = np.searchsorted(wa, dec["t"], side="right") - 1
                ok = (k >= 0) & (dec["t"] <= wb[np.clip(k, 0, None)] + 1e-3)
                stats["decoded"] += int(ok.sum())
                parts["t"].append(dec["t"][ok])
                parts["p"].append(dec["p"][ok])
                parts["i"].append(dec["i"][ok])
                parts["line"].append(wl[k[ok]])
    if not parts["t"]:
        out = {"t": np.zeros(0), "p": np.zeros((0, 3), np.float32), "i": np.zeros(0, np.uint8),
               "line": np.zeros(0, np.int16)}
    else:
        out = {k: np.concatenate(v) for k, v in parts.items()}
        if progress:
            progress(0.92, "Placing the points")
        # 2 keep what lands inside a box (+ pad) with the applied values
        x = georef_points(project, out["t"], out["p"], project.cfg["mount"], project.cfg["lever"],
                          project.cfg["latency_ms"])
        good = np.isfinite(x[:, 0])
        xy = x[:, :2]
        owner = np.full(len(xy), -1, np.int8)
        for bi, (k, b) in enumerate(sorted(boxes.items())):
            m = good & (owner < 0) & in_box(xy, b, PAD_M)
            owner[m] = bi
        keep = []
        rng = np.random.default_rng(3)
        for bi, (k, b) in enumerate(sorted(boxes.items())):
            idx = np.nonzero(owner == bi)[0]
            stats["per_box"][k] = {"points": int((good & in_box(xy, b, PAD_M)).sum())}
            if len(idx) > budget:
                idx = np.sort(rng.choice(idx, budget, replace=False))
                stats["per_box"][k]["thinned_to"] = budget
            keep.append(idx)
        keep = np.sort(np.concatenate(keep)) if keep else np.zeros(0, int)
        out = {k: v[keep] for k, v in out.items()}
    stats["kept"] = int(len(out["t"]))
    stats["read_s"] = round(time.time() - t_start, 2)
    stats["cached"] = False
    if cpath:
        os.makedirs(project.work, exist_ok=True)
        tmp = cpath + ".tmp.npz"
        np.savez(tmp, meta=np.frombuffer(json.dumps({"names": names, "stats": stats}).encode(), np.uint8),
                 **{k: v for k, v in out.items()})
        os.replace(tmp, cpath)
        _prune(project.work, cpath)
    out["stats"] = stats
    return out


def _prune(work, keep, n=6):
    """Keep the few most recent full-density caches."""
    fs = sorted((os.path.join(work, f) for f in os.listdir(work) if f.startswith("fine_") and f.endswith(".npz")),
                key=os.path.getmtime, reverse=True)
    for f in fs[n:]:
        if f != keep:
            try:
                os.remove(f)
            except OSError:
                pass


def _window_chunks(f, channel, windows, progress=None):
    """f.iter_chunks([channel]) limited to the chunks whose log-time span meets one of the windows (ns).

    DCS Core's reader keeps each chunk's start and end time in its index (McapFile.chunk_pos), so the
    chunks outside the windows are never read or decompressed. A reader without that index falls back
    to reading every chunk (same result, slower)."""
    m = getattr(f, "_m", None)
    pos = getattr(m, "chunk_pos", None)
    if not pos or len(pos[0]) < 7 or not hasattr(f, "_file_ids"):
        for chunk in f.iter_chunks([channel], progress=progress):
            yield chunk
        return
    from dcs_core import mcap
    want = f._file_ids([channel])
    as_ch = getattr(f, "_as_ch", {})
    wins = sorted(windows)
    with open(f.path, "rb") as fh:
        for ci, cp in enumerate(pos):
            if progress:
                progress((cp[0] + cp[1]) / max(f.size, 1))
            c0, c1 = cp[5], cp[6]
            if not any(a <= c1 and c0 <= b for a, b in wins):
                continue
            data = m.read_chunk(fh, ci)
            if data is None:
                continue
            try:
                out = [(as_ch.get(ch, ch), lt, pt, pl) for ch, lt, pt, pl in mcap.iter_messages(data, want)]
            except Exception:  # noqa: BLE001 - damaged records inside a good chunk
                continue
            if out:
                yield out


def clraw_cancelled():
    from .lines import Cancelled
    return Cancelled()


def georef_points(project, t, p, mount, lever, latency_ms, batch=1000000):
    from . import geo
    M = geo.lidar_matrix(*mount)
    x = np.full((len(t), 3), np.nan)
    tr = project.traj
    for s in range(0, len(t), batch):
        tt = t[s:s + batch]
        ins = (tt >= tr.t[0]) & (tt <= tr.t[-1])
        if not ins.any():
            continue
        ii = np.nonzero(ins)[0]
        pos, C = engine.pose_fast(tr, project.frame, tt[ii], latency_ms * 1e-3)
        x[s + ii] = engine.georef(pos, C, p[s:s + batch][ii].astype(float) @ M.T, lever)
    return x


# ------------------------------------------------------------------ view data
def fine_view(project, region, dense=None, max_pts=400000):
    """The Data View's points: thinned cache over the square, full density inside the boxes."""
    boxes = region["boxes"] if dense is not None else {}
    c, half = region["center"], region["half"]
    rng = np.random.default_rng(11)
    parts = []
    for i, ln in enumerate(project.lines):
        if not ln.use or ln.x is None or not len(ln.x):
            continue
        m = (np.abs(ln.x[:, 0] - c[0]) <= half) & (np.abs(ln.x[:, 1] - c[1]) <= half)
        if boxes:
            m &= ~in_any(ln.x[:, :2], boxes, PAD_M)
        parts.append((i, np.nonzero(m)[0]))
    tot = sum(len(ix) for _, ix in parts)
    frac = min(1.0, max_pts / max(tot, 1))
    line, t, p, inten = [], [], [], []
    for i, idx in parts:
        if frac < 1.0:
            idx = np.sort(rng.choice(idx, int(len(idx) * frac), replace=False))
        ln = project.lines[i]
        di = ln.idx[idx]
        line.append(np.full(len(di), i, np.int16))
        t.append(ln.data["t"][di])
        p.append(ln.data["p"][di].astype(np.float32))
        inten.append(ln.data["i"][di])
    n_cache = sum(len(a) for a in t)
    n_dense = 0
    if dense is not None and len(dense["t"]):
        line.append(dense["line"].astype(np.int16))
        t.append(dense["t"])
        p.append(dense["p"].astype(np.float32))
        inten.append(dense["i"])
        n_dense = len(dense["t"])
    if not line or sum(len(a) for a in t) == 0:      # no point in the square (e.g. an area of interest elsewhere)
        return None
    sub = {"line": np.concatenate(line), "t": np.concatenate(t), "p": np.concatenate(p),
           "i": np.concatenate(inten), "center": tuple(map(float, c)), "half": float(half), "poses": {},
           "region": region, "n_cache": n_cache, "n_dense": n_dense,
           "dense_stats": (dense or {}).get("stats")}
    order = rng.permutation(len(sub["t"]))           # mixed draw order so no line hides another
    for k in ("line", "t", "p", "i"):
        sub[k] = sub[k][order]
    return sub
