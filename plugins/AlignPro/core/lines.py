"""Reading survey lines from .clraw into a compact cache.

Stage A (slow, once per line and settings): read every message, decode the Lidar to
sensor-frame points (gated, one return per firing, randomly thinned to a budget), keep the
navigation, status, events and quality. Saved as work/<key>.npz.

Stage B (fast, repeatable) lives in engine.py: georeference the cached points with any
trajectory, mounting, lever arm and latency.
"""
import hashlib
import math
import json
import os

import numpy as np

from . import anpp, clraw, hesai

CACHE_VERSION = 5          # 5: packets fired while stopped are skipped (1.6.2)


class Cancelled(Exception):
    """The user pressed STOP."""

    def __init__(self, msg="Cancelled"):
        super().__init__(msg)


def cache_key(files, s):
    h = hashlib.sha1()
    h.update(str(CACHE_VERSION).encode())
    for f in files:
        h.update(("%s|%d|%d" % (os.path.basename(f), os.path.getsize(f), int(os.path.getmtime(f)))).encode())
    for k in ("returns", "range_min", "range_max", "intensity_min", "points_per_line", "min_speed"):
        h.update(("%s=%s" % (k, s.get(k))).encode())
    return h.hexdigest()[:16]


def _moving(t_pkt, nav, min_speed, span_s=3.0):
    """Packets fired while the vehicle moved at min_speed or more, from the packet-20 velocities read so far
    (smoothed over about 1 s). None when there is no speed to go by yet (everything is kept)."""
    if min_speed <= 0 or len(nav.s20) < 10 or len(t_pkt) == 0:
        return None
    t_lo = float(t_pkt.min()) - span_s
    tail = []
    for row in reversed(nav.s20):                  # the newest states cover this chunk
        tail.append((row[0], math.hypot(row[4], row[5])))
        if row[0] < t_lo or len(tail) > 60000:
            break
    if len(tail) < 2:
        return None
    a = np.array(tail[::-1])
    a = a[np.argsort(a[:, 0], kind="stable")]
    dt = np.median(np.diff(a[:, 0])) if len(a) > 2 else 0.02
    k = max(1, min(len(a), int(round(1.0 / max(dt, 1e-3)))))
    sp = np.convolve(a[:, 1], np.ones(k) / k, mode="same") if k > 1 else a[:, 1]
    return np.interp(t_pkt, a[:, 0], sp) >= min_speed


def read_line(files, settings, cache_dir=None, progress=None, cancelled=None):
    """files: list of ClrawFile (one line, name order). Returns a dict of arrays + info."""
    paths = [f.path for f in files]
    key = cache_key(paths, settings)
    cpath = os.path.join(cache_dir, "line_%s.npz" % key) if cache_dir else None
    if cpath and os.path.isfile(cpath):
        try:
            out = _load(cpath)
            try:
                os.utime(cpath)            # most recently used (prune_caches keeps the recent ones)
            except OSError:
                pass
            return out
        except Exception:  # noqa: BLE001 - a damaged cache is read again from the .clraw
            pass
    first = files[0]
    cal = None
    for f in files:
        cal = f.calibration()
        if cal:
            break
    hw = first.hardware
    total = sum(f.size for f in files) or 1
    est_pkts = total / 1.64e6 * 5000.0           # .clraw ~5.9 GB/h -> 5000 packets/s
    budget = float(settings.get("points_per_line", 1500000))
    keep = min(1.0, budget / max(est_pkts * 128 * 0.5, 1.0))
    rng = np.random.default_rng(12345)
    min_speed = float(settings.get("min_speed", 1.0) or 0.0)
    seen_pk = n_stopped = 0
    nav = anpp.NavCollector()
    pts = {k: [] for k in ("t", "p", "i", "ch")}
    n_pkts = 0
    seq_gaps = 0
    last_seq = None
    status = [0, 0, 0]          # count, nmea valid, pps locked
    events, quality = [], []
    lp_off = []
    hinfo = {}
    done = 0
    for f in files:
        base_done = done

        def prog(fr, base_done=base_done, f=f):
            if progress:
                progress((base_done + fr * f.size) / total, "Reading %s" % f.name)
        for chunk in f.iter_chunks(progress=prog):
            if cancelled and cancelled():
                raise Cancelled()
            pk, tp = [], []
            for ch, lt, pt, data in chunk:
                if ch == clraw.CH_POINTS:
                    pk.append(data)
                    tp.append(pt)
                    if len(lp_off) < 20000 and n_pkts % 50 == 0:
                        lp_off.append((lt - pt) * 1e-9)
                elif ch == clraw.CH_ANPP:
                    nav.add(data, pt)
                elif ch == clraw.CH_STATUS:
                    st = hesai.parse_status(data)
                    if st:
                        status[0] += 1
                        status[1] += st[0]
                        status[2] += st[1]
                elif ch == clraw.CH_EVENTS:
                    try:
                        events.append(json.loads(bytes(data).decode("utf-8")))
                    except ValueError:
                        pass
                elif ch == clraw.CH_QUALITY:
                    try:
                        q = json.loads(bytes(data).decode("utf-8"))
                        quality.append({"t": q.get("t"), "fix": q.get("fix_name"),
                                        "hdop": (q.get("satellites") or {}).get("hdop")})
                    except ValueError:
                        pass
            if not pk:
                continue
            a = hesai.packets_to_array(pk)
            if not hinfo:
                hinfo = hesai.header_info(a)
            n_pkts += len(a)
            sq = hesai.sequence(a)
            if last_seq is not None:
                sq_all = np.concatenate([[last_seq], sq])
            else:
                sq_all = sq
            d = np.diff(sq_all.astype(np.int64))
            seq_gaps += int(np.sum(np.where(d > 1, d - 1, 0)))
            last_seq = sq[-1]
            t_pkt = np.array(tp, np.float64) * 1e-9
            # skip the packets fired while the vehicle was slower than the pass threshold: the solve drops
            # them anyway (passes start at min_speed), and while parked they would take a large share of the
            # line's point budget away from the moving passes
            mv = _moving(t_pkt, nav, min_speed)
            seen_pk += len(a)
            if mv is not None and not mv.all():
                a, t_pkt = a[mv], t_pkt[mv]
                n_stopped += int((~mv).sum())
                if len(a) == 0:
                    continue
            # the thinning was set from the file size: as stopped data is left out, keep more of the rest
            kept = 1.0 - n_stopped / max(seen_pk, 1)
            keep_now = min(1.0, keep / max(kept, 0.25))
            dec = hesai.decode(a, t_pkt, cal, settings.get("returns", "strongest"),
                               settings.get("range_min", 1.5), settings.get("range_max", 60.0),
                               settings.get("intensity_min", 0), keep=keep_now, rng=rng)
            if dec is None:
                continue
            for k in pts:
                pts[k].append(dec[k])
        done += f.size
    out = {k: (np.concatenate(v) if v else np.zeros((0, 3) if k == "p" else 0)) for k, v in pts.items()}
    if len(out["t"]) > budget * 1.2:
        sel = np.sort(rng.choice(len(out["t"]), int(budget), replace=False))
        out = {k: v[sel] for k, v in out.items()}
    nv = nav.arrays()
    for k, v in nv.items():
        out["nav_" + k] = v
    ev_t = {e.get("event"): e.get("t") for e in events}
    t_start = ev_t.get("clraw_start") or (float(out["t"].min()) if len(out["t"]) else None)
    t_stop = ev_t.get("clraw_stop") or (float(out["t"].max()) if len(out["t"]) else None)
    info = {
        "files": [os.path.basename(p) for p in paths], "paths": paths,
        "line": first.line, "capture": first.metadata.get("capture", ""),
        "capture_name": first.metadata.get("capture_name", ""),
        "mapper_version": first.metadata.get("mapper_version", ""),
        "hardware": hw, "calibration": cal, "hesai": hinfo,
        "packets": n_pkts, "lost_packets": seq_gaps, "stopped_packets": n_stopped,
        "status": {"count": status[0], "nmea_valid": status[1], "pps_locked": status[2]},
        "events": events, "quality": quality[:100000],
        "t_start": t_start, "t_stop": t_stop, "keep_fraction": keep,
        "log_minus_publish_ms": float(np.median(lp_off) * 1e3) if lp_off else None,
        "bad_chunks": sum(f.bad_chunks for f in files),
        "points": int(len(out["t"])),
    }
    out["info_json"] = np.frombuffer(json.dumps(info).encode(), np.uint8)
    if cpath:
        os.makedirs(cache_dir, exist_ok=True)
        tmp = cpath + ".tmp.npz"
        np.savez(tmp, **out)
        os.replace(tmp, cpath)
    return _unpack(out)


def prune_caches(cache_dir, keep, budget_bytes=2e9):
    """Delete line caches not in `keep` (paths), oldest first, until the others take at most budget_bytes.
    Caches from earlier settings (another density, range gate...) are kept while they fit, so switching
    back is quick, but they no longer grow without limit."""
    try:
        fs = [os.path.join(cache_dir, f) for f in os.listdir(cache_dir) if f.startswith("line_") and f.endswith(".npz")]
    except OSError:
        return 0
    keep = {os.path.abspath(k) for k in keep}
    other = sorted((f for f in fs if os.path.abspath(f) not in keep), key=os.path.getmtime, reverse=True)
    total, removed = 0, 0
    for f in other:
        try:
            sz = os.path.getsize(f)
        except OSError:
            continue
        total += sz
        if total > budget_bytes:
            try:
                os.remove(f)
                removed += 1
            except OSError:
                pass
    return removed


def cache_path(files, settings, cache_dir):
    return os.path.join(cache_dir, "line_%s.npz" % cache_key([f.path for f in files], settings))


def _load(path):
    with np.load(path) as z:
        out = {k: z[k] for k in z.files}
    return _unpack(out)


def _unpack(out):
    info = json.loads(bytes(out.pop("info_json")).decode())
    out["info"] = info
    out["nav"] = {k[4:]: out.pop(k) for k in list(out) if k.startswith("nav_")}
    return out
