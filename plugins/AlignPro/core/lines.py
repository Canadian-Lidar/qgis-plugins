"""Reading survey lines from .clraw into a compact cache.

Stage A (slow, once per line and settings): read every message, decode the Lidar to
sensor-frame points (gated, one return per firing, randomly thinned to a budget), keep the
navigation, status, events and quality. Saved as work/<key>.npz.

Stage B (fast, repeatable) lives in engine.py: georeference the cached points with any
trajectory, mounting, lever arm and latency.
"""
import hashlib
import json
import os

import numpy as np

from . import anpp, clraw, hesai

CACHE_VERSION = 4


class Cancelled(Exception):
    pass


def cache_key(files, s):
    h = hashlib.sha1()
    h.update(str(CACHE_VERSION).encode())
    for f in files:
        h.update(("%s|%d|%d" % (os.path.basename(f), os.path.getsize(f), int(os.path.getmtime(f)))).encode())
    for k in ("returns", "range_min", "range_max", "intensity_min", "points_per_line"):
        h.update(("%s=%s" % (k, s.get(k))).encode())
    return h.hexdigest()[:16]


def read_line(files, settings, cache_dir=None, progress=None, cancelled=None):
    """files: list of ClrawFile (one line, name order). Returns a dict of arrays + info."""
    paths = [f.path for f in files]
    key = cache_key(paths, settings)
    cpath = os.path.join(cache_dir, "line_%s.npz" % key) if cache_dir else None
    if cpath and os.path.isfile(cpath):
        try:
            return _load(cpath)
        except Exception:
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
            dec = hesai.decode(a, t_pkt, cal, settings.get("returns", "strongest"),
                               settings.get("range_min", 1.5), settings.get("range_max", 60.0),
                               settings.get("intensity_min", 0), keep=keep, rng=rng)
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
        "packets": n_pkts, "lost_packets": seq_gaps,
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


def _load(path):
    with np.load(path) as z:
        out = {k: z[k] for k in z.files}
    return _unpack(out)


def _unpack(out):
    info = json.loads(bytes(out.pop("info_json")).decode())
    out["info"] = info
    out["nav"] = {k[4:]: out.pop(k) for k in list(out) if k.startswith("nav_")}
    return out
