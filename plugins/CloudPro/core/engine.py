"""The CloudPro engine: a small C library (bin/linux/libcpengine.so, bin/windows/cpengine.dll; source in the
repository's engine/cloudpro-engine) that does the per-point loops of georeferencing and the tile grids.

Called through ctypes, which releases Python's lock for the call, so CloudPro's worker threads run on every core.
Each function gives the same numbers as the Python code it replaces (tests/test_engine.py); when the library
is missing or fails to load, or CLOUDPRO_NO_ENGINE=1 is set, CloudPro uses the Python code.

    available()                 True when the library is loaded
    read_points(rec, cache, fi, ci)   Recording.read_lidar_chunk with the packets split out in C
    Georef(ctx, ...)            prepared once per line; .chunk(P) -> [(tile key, records)] as Store.bin
    grid_stats(a, ...)          Store.grid_stats' sums for one tile
    ray_count(...)              Moving objects: voxels passed through by each line's rays, counted
    cluster(xyz, r)             features.cluster (single-linkage clusters closer than r), same labels
    line_counts(P2, ...)        Vertical plane: RANSAC candidate scores
"""
import ctypes as C
import os
import sys

import numpy as np

VERSION = 3                         # cp_version() this code expects
_lib = None
_why = ""


class _Georef(C.Structure):
    _fields_ = [(f, C.c_void_p) for f in ("t", "E", "N", "h", "roll", "pitch", "heading", "speed")] + \
               [("n", C.c_int64)] + [(f, C.c_void_p) for f in ("tj", "J00", "J01", "J10", "J11")] + \
               [("nj", C.c_int64), ("M", C.c_double * 9), ("lever", C.c_double * 3), ("R", C.c_double),
                ("origin_z", C.c_double), ("latency", C.c_double)]


class _Opts(C.Structure):
    _fields_ = [("calib", C.c_double * 64), ("rmin", C.c_double), ("rmax", C.c_double), ("returns", C.c_int),
                ("drop_duplicates", C.c_int), ("min_speed", C.c_double), ("tmin", C.c_double),
                ("tmax", C.c_double), ("tile", C.c_double), ("line", C.c_int)]


class _Stats(C.Structure):
    _fields_ = [("packets_stopped", C.c_int64), ("points_outside", C.c_int64), ("points", C.c_int64),
                ("ntiles", C.c_int64)]


def _path():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if sys.platform.startswith("win"):
        return os.path.join(here, "bin", "windows", "cpengine.dll")
    if sys.platform.startswith("linux"):
        return os.path.join(here, "bin", "linux", "libcpengine.so")
    return None


def _cache_dir():
    """Per-user folder for the loaded copy of the library (outside the plugin folder)."""
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "CloudPro", "engine")


def _loadable_copy(src):
    """A copy of the library to load, named by its content, in the per-user cache folder.

    Windows keeps a loaded DLL locked until QGIS closes; loaded from the plugin folder, it stops the plugin manager
    from upgrading or removing CloudPro. The copy is loaded instead, so the plugin folder is never locked. Copies
    of older versions are removed when they are no longer in use. Falls back to the original when the copy fails."""
    import hashlib
    import shutil
    import tempfile
    try:
        with open(src, "rb") as f:
            h = hashlib.sha1(f.read()).hexdigest()[:12]
        name, ext = os.path.splitext(os.path.basename(src))
        dst_dir = _cache_dir()
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, "%s-%s%s" % (name, h, ext))
        if not os.path.exists(dst) or os.path.getsize(dst) != os.path.getsize(src):
            fd, tmp = tempfile.mkstemp(dir=dst_dir, suffix=ext + ".tmp")
            os.close(fd)
            shutil.copyfile(src, tmp)
            os.replace(tmp, dst)
        for f in os.listdir(dst_dir):                       # older copies (fails silently while in use)
            if f != os.path.basename(dst) and f.startswith(name + "-"):
                try:
                    os.remove(os.path.join(dst_dir, f))
                except OSError:
                    pass
        return dst
    except OSError:
        return src


def _load():
    global _lib, _why
    if _lib is not None or _why:
        return _lib
    if os.environ.get("CLOUDPRO_NO_ENGINE"):
        _why = "turned off (CLOUDPRO_NO_ENGINE)"
        return None
    p = _path()
    if not p or not os.path.exists(p):
        _why = "no engine for this platform" if not p else "library missing"
        return None
    try:
        lib = C.CDLL(_loadable_copy(p))
        if lib.cp_version() != VERSION:
            _why = "engine version %d, CloudPro expects %d" % (lib.cp_version(), VERSION)
            return None
        lib.cp_georef_chunk.restype = C.c_int64
        lib.cp_georef_chunk.argtypes = [C.c_void_p, C.c_int64, C.POINTER(_Georef), C.POINTER(_Opts), C.c_void_p,
                                        C.c_void_p, C.c_int64, C.c_void_p, C.POINTER(_Stats)]
        lib.cp_mcap_payloads.restype = C.c_int64
        lib.cp_mcap_payloads.argtypes = [C.c_char_p, C.c_int64, C.c_int, C.c_int64, C.c_void_p, C.c_int64]
        lib.cp_ray_count.restype = C.c_void_p
        lib.cp_ray_count.argtypes = [C.c_void_p] * 4 + [C.c_int64, C.c_double, C.c_void_p, C.c_double, C.c_void_p,
                                                        C.c_int]
        lib.cp_table_size.restype = C.c_int64
        lib.cp_table_size.argtypes = [C.c_void_p]
        lib.cp_table_take.restype = None
        lib.cp_table_take.argtypes = [C.c_void_p, C.c_void_p, C.c_void_p]
        lib.cp_cluster.restype = None
        lib.cp_cluster.argtypes = [C.c_void_p, C.c_int64, C.c_void_p, C.c_void_p, C.c_int64, C.c_void_p, C.c_double,
                                   C.c_void_p]
        lib.cp_line_counts.restype = None
        lib.cp_line_counts.argtypes = [C.c_void_p, C.c_int64] + [C.c_void_p] * 4 + [C.c_int64, C.c_double, C.c_void_p]
        V, I, D, U32 = C.c_void_p, C.c_int, C.c_double, C.c_uint32
        lib.cp_key_range.restype = None
        lib.cp_key_range.argtypes = [V, V, V, C.c_int64, I, I, V]
        lib.cp_raster_top.restype = None
        lib.cp_raster_top.argtypes = [V, V, V, V, C.c_int64, I, I, I, D, D, V, V, V, V]
        lib.cp_merge_top.restype = None
        lib.cp_merge_top.argtypes = [V, V, V, V, V, V, C.c_int64]
        lib.cp_near_winners.restype = None
        lib.cp_near_winners.argtypes = [V, V, V, C.c_int64, C.c_int64, I, I, V, V]
        lib.cp_merge_near.restype = None
        lib.cp_merge_near.argtypes = [V, V, V, V, C.c_int64]
        lib.cp_splat_near.restype = C.c_int64
        lib.cp_splat_near.argtypes = [V, V, V, I, I, I, U32, V]
        lib.cp_in_box.restype = C.c_int64
        lib.cp_in_box.argtypes = [C.c_void_p, C.c_int64] + [C.c_double] * 8 + [C.c_void_p]
        lib.cp_eye_dome.restype = None
        lib.cp_eye_dome.argtypes = [C.c_void_p, C.c_void_p, C.c_int, C.c_int, C.c_float, C.c_int, C.c_int,
                                    C.c_void_p]
        lib.cp_gl_init.restype = C.c_int
        lib.cp_gl_init.argtypes = []
        lib.cp_gl_upload.restype = C.c_int
        lib.cp_gl_upload.argtypes = [C.c_void_p, C.c_void_p, C.c_int64, C.c_void_p]
        lib.cp_gl_renderer.restype = None
        lib.cp_gl_renderer.argtypes = [C.c_char_p, C.c_int]
        lib.cp_gl_free.restype = None
        lib.cp_gl_free.argtypes = [C.c_void_p, C.c_void_p]
        lib.cp_gl_draw.restype = C.c_int
        lib.cp_gl_draw.argtypes = [C.c_void_p, C.c_int64, C.c_void_p, C.c_void_p, C.c_int, C.c_int, C.c_float,
                                   C.c_uint32, C.c_void_p, C.c_void_p, C.c_void_p]
        lib.cp_grid_stats.restype = None
        lib.cp_grid_stats.argtypes = [C.c_void_p, C.c_int64, C.c_int64, C.c_int64, C.c_int64, C.c_int64] + \
            [C.c_void_p] * 6
        _lib = lib
    except Exception as e:                          # noqa: BLE001 - any load failure means "use Python"
        _why = "could not load (%s)" % e
    return _lib


def available():
    return _load() is not None


def status():
    """One line for the log: which code does the work."""
    return "engine %d" % VERSION if available() else "Python (%s)" % _why


def _ptr(a):
    return a.ctypes.data


class Georef:
    """One line's georeferencing in the engine (the arrays are kept alive here)."""

    def __init__(self, ctx, calib, rmin, rmax, returns, drop_duplicates, min_speed, tmin, tmax, tile, line):
        tr = ctx.traj
        f8 = lambda v: np.ascontiguousarray(v, np.float64)          # noqa: E731
        h = f8(tr.h) if ctx.geoidN is None else f8(np.asarray(tr.h, float) - ctx.geoidN)
        self._keep = [f8(tr.t), f8(ctx.E), f8(ctx.N), h, f8(tr.roll), f8(tr.pitch), f8(tr.heading), f8(ctx.speed),
                      f8(ctx.tj)] + [f8(ctx.J[:, i, j]) for i in range(2) for j in range(2)]
        k = self._keep
        g = _Georef()
        for name, a in zip(("t", "E", "N", "h", "roll", "pitch", "heading", "speed"), k[:8]):
            setattr(g, name, _ptr(a))
        g.n = len(k[0])
        for name, a in zip(("tj", "J00", "J01", "J10", "J11"), k[8:]):
            setattr(g, name, _ptr(a))
        g.nj = len(k[8])
        g.M[:] = [float(v) for v in np.asarray(ctx.M, float).ravel()]
        g.lever[:] = [float(v) for v in ctx.l]
        g.R, g.origin_z, g.latency = float(ctx.R), float(ctx.origin[2]), float(ctx.latency)
        self.g = g
        o = _Opts()
        cal = np.asarray(calib, float)
        if cal.shape != (32, 2):
            from dcs_core.hesai import DEFAULT_CALIB
            cal = DEFAULT_CALIB if len(cal) == 0 else cal
        o.calib[:] = [float(v) for v in np.asarray(cal, float).ravel()]
        o.rmin, o.rmax = float(rmin), float(rmax)
        o.returns = {"both": 0, "strongest": 1, "last": 2}[returns]
        o.drop_duplicates = int(bool(drop_duplicates))
        o.min_speed = float(min_speed or 0.0)
        o.tmin, o.tmax = float(tmin), float(tmax)
        o.tile, o.line = float(tile), int(line)
        self.o = o

    def chunk(self, P, rec_dtype):
        """(N, 1080) uint8 packets -> ([(key, records)], stopped packets, points outside the time window)."""
        lib = _load()
        P = np.ascontiguousarray(P, np.uint8)
        n = len(P)
        out = np.empty(n * 256, rec_dtype)
        tmp = np.empty(n * 256 * (33 + 8) + 8, np.uint8)
        maxt = 4096
        tiles = np.zeros((maxt, 4), np.int64)
        st = _Stats()
        m = lib.cp_georef_chunk(_ptr(P), n, C.byref(self.g), C.byref(self.o), _ptr(out), _ptr(tiles), maxt,
                                _ptr(tmp), C.byref(st))
        if m < 0:
            raise RuntimeError("CloudPro engine: chunk failed (%d)" % m)
        parts = [("%d_%d" % (int(ix), int(iy)), out[s:s + c]) for ix, iy, s, c in tiles[:st.ntiles]]
        return parts, int(st.packets_stopped), int(st.points_outside)


def read_points(rec, fh_cache, fi, ci, plen=1080):
    """The point packets of one chunk, (N, 1080) uint8: dcs_core's Recording.read_lidar_chunk, with the message
    loop in C (the decompression is zstandard's, which also runs outside Python's lock)."""
    from dcs_core.clraw import TOPIC_POINTS
    f = rec.files[fi]
    cid = f.topic_id(TOPIC_POINTS)
    fh = fh_cache.get(fi)
    if fh is None:
        fh = fh_cache[fi] = open(f.path, "rb")
    data = f.read_chunk(fh, ci)
    if data is None or cid is None:
        return np.zeros((0, plen), np.uint8)
    data = bytes(data)
    out = np.empty((len(data) // plen + 1, plen), np.uint8)
    k = _load().cp_mcap_payloads(data, len(data), int(cid), plen, _ptr(out), len(out))
    return out[:k]


def grid_stats(a, x0, y0, cmm, n):
    """Sums for Store.grid_stats over a tile's records a -> count, rej, zsum (mm), isum, zmin, zmax (mm)."""
    lib = _load()
    a = np.ascontiguousarray(a)
    N = n * n
    cnt, rej, zmin, zmax = (np.zeros(N, np.int64) for _ in range(4))
    zs, ins = np.zeros(N), np.zeros(N)
    lib.cp_grid_stats(_ptr(a), len(a), int(x0), int(y0), int(cmm), int(n), _ptr(cnt), _ptr(rej), _ptr(zs),
                      _ptr(ins), _ptr(zmin), _ptr(zmax))
    return cnt, rej, zs, ins, zmin, zmax


def threads():
    """Worker threads for the engine's parallel loops: every core but one (left for QGIS), at most 16."""
    return max(1, min(16, (os.cpu_count() or 2) - 1))


def ray_count(o, u, Lmax, line, step, lo, vs, dims, lid_bits, nthreads=None):
    """filters.DynamicObjects' free-space count -> (sorted distinct keys, counts): the same as marching every ray
    sample in numpy, de-duplicating per ray and counting with np.unique. Rays are split over threads."""
    import concurrent.futures as cf
    lib = _load()
    f8 = lambda a: np.ascontiguousarray(a, np.float64)            # noqa: E731
    o, u, Lmax = f8(o), f8(u), f8(Lmax)
    line = np.ascontiguousarray(line, np.int64)
    lo = f8(lo)
    dims = np.ascontiguousarray(dims, np.int64)
    n = len(Lmax)
    nthreads = max(1, min(nthreads or threads(), (n + 9999) // 10000))
    cuts = np.linspace(0, n, nthreads + 1).astype(np.int64)

    def one(a, b):
        h = lib.cp_ray_count(_ptr(o[a:b]), _ptr(u[a:b]), _ptr(Lmax[a:b]), _ptr(line[a:b]), b - a, float(step),
                             _ptr(lo), float(vs), _ptr(dims), int(lid_bits))
        if not h:
            raise MemoryError("CloudPro engine: not enough memory for the free-space count")
        m = lib.cp_table_size(h)
        k, c = np.empty(m, np.int64), np.empty(m, np.int64)
        lib.cp_table_take(h, _ptr(k), _ptr(c))
        return k, c

    with cf.ThreadPoolExecutor(nthreads) as ex:
        parts = list(ex.map(lambda ab: one(*ab), zip(cuts[:-1], cuts[1:])))
    keys = np.concatenate([k for k, _c in parts])
    cnts = np.concatenate([c for _k, c in parts])
    if not len(keys):
        return keys, cnts
    free, inv = np.unique(keys, return_inverse=True)
    return free, np.bincount(inv.ravel(), cnts).astype(np.int64)


def cluster(xyz, r):
    """Single-linkage clusters of points closer than r -> (label per point, size per label), with the labels
    numbered as scipy's connected_components numbers them (by the first point of each cluster)."""
    lib = _load()
    xyz = np.asarray(xyz, np.float64)
    n = len(xyz)
    if n == 0:
        return np.zeros(0, int), np.zeros(0, int)
    c = np.floor((xyz - xyz.min(0)) / r).astype(np.int64) + 1          # padded: neighbours never wrap
    dims = c.max(0) + 2
    key = (c[:, 0] * dims[1] + c[:, 1]) * dims[2] + c[:, 2]
    order = np.argsort(key, kind="stable")
    ks = key[order]
    ukeys, starts = np.unique(ks, return_index=True)
    starts = np.r_[starts, n].astype(np.int64)
    xs = np.ascontiguousarray(xyz[order])
    root = np.empty(n, np.int64)
    lib.cp_cluster(_ptr(xs), n, _ptr(np.ascontiguousarray(ukeys)), _ptr(starts), len(ukeys),
                   _ptr(np.ascontiguousarray(dims, np.int64)), float(r), _ptr(root))
    lab_o = np.empty(n, np.int64)
    lab_o[order] = order[root]                    # root as an original point index
    roots, first, inv = np.unique(lab_o, return_index=True, return_inverse=True)
    rank = np.empty(len(roots), np.int64)
    rank[np.argsort(first, kind="stable")] = np.arange(len(roots))
    lab = rank[inv.ravel()].astype(np.int32)
    return lab, np.bincount(lab)


def line_counts(P2, anchors, normals, half, nthreads=None):
    """Points of P2 ((m, 2) float32) within half of each candidate line (anchor, unit normal): the count
    int((np.abs((P2 - anchor) @ normal) < half).sum()) for every candidate, candidates split over threads."""
    import concurrent.futures as cf
    lib = _load()
    P2 = np.ascontiguousarray(P2, np.float32)
    ax = np.ascontiguousarray(anchors[:, 0], np.float32)
    ay = np.ascontiguousarray(anchors[:, 1], np.float32)
    n0 = np.ascontiguousarray(normals[:, 0], np.float64)
    n1 = np.ascontiguousarray(normals[:, 1], np.float64)
    nc = len(ax)
    out = np.zeros(nc, np.int64)
    nthreads = max(1, min(nthreads or threads(), nc))
    cuts = np.linspace(0, nc, nthreads + 1).astype(np.int64)

    def one(a, b):
        if b > a:
            lib.cp_line_counts(_ptr(P2), len(P2), _ptr(ax[a:]), _ptr(ay[a:]), _ptr(n0[a:]), _ptr(n1[a:]), b - a,
                               float(half), _ptr(out[a:]))
    with cf.ThreadPoolExecutor(nthreads) as ex:
        list(ex.map(lambda ab: one(*ab), zip(cuts[:-1], cuts[1:])))
    return out


def _f4(a):
    return np.ascontiguousarray(a, np.float32)


def _pieces(n, minimum=2_000_000):
    """Cuts of [0, n) into one piece per worker thread (none below `minimum` points per piece)."""
    k = max(1, min(threads(), n // minimum))
    return np.linspace(0, n, k + 1).astype(np.int64)


_POOL = None
_POOL_LOCK = __import__("threading").Lock()


def _pool():
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:                         # the tiled filter runs call the engine from several threads
            if _POOL is None:
                import concurrent.futures as cf
                _POOL = cf.ThreadPoolExecutor(max_workers=threads(), thread_name_prefix="CloudPro engine")
    return _POOL


def raster_top(sx, sy, key, col, w, h, size=1, bg=0xFF09100F, depth_out=None, dval=None):
    """render.rasterise on every core: the highest-key point per pixel -> (h, w) uint32 (identical at size 1)."""
    lib = _load()
    sx, sy, key = _f4(sx), _f4(sy), _f4(key)
    col = np.ascontiguousarray(col, np.uint32)
    dv = _f4(dval) if (dval is not None and depth_out is not None) else None
    n, npix = len(sx), w * h
    cuts = _pieces(n)
    rngs = np.empty((len(cuts) - 1, 2))

    def kr(j):
        a, b = cuts[j], cuts[j + 1]
        lib.cp_key_range(_ptr(sx[a:]), _ptr(sy[a:]), _ptr(key[a:]), b - a, int(w), int(h), _ptr(rngs[j]))
    list(_pool().map(kr, range(len(cuts) - 1)))
    kmin, kmax = float(rngs[:, 0].min()), float(rngs[:, 1].max())
    out = np.full(npix, int(bg) & 0xFFFFFFFF, np.uint32)
    if not kmax >= kmin:
        return out.reshape(h, w)

    def piece(j):
        a, b = cuts[j], cuts[j + 1]
        buf = np.full(npix, int(bg) & 0xFFFFFFFF, np.uint32)
        best = np.full(npix, -1, np.int32)
        dep = np.array(depth_out, np.float32) if dv is not None else None
        lib.cp_raster_top(_ptr(sx[a:]), _ptr(sy[a:]), _ptr(key[a:]), _ptr(col[a:]), b - a, int(w), int(h),
                          int(size), kmin, kmax, _ptr(buf), _ptr(best), _ptr(dv[a:]) if dv is not None else None,
                          _ptr(dep) if dep is not None else None)
        return buf, best, dep
    parts = list(_pool().map(piece, range(len(cuts) - 1)))
    buf, best, dep = parts[0]
    for b2, best2, d2 in parts[1:]:                       # in order: later points win ties, as in one pass
        lib.cp_merge_top(_ptr(buf), _ptr(best), _ptr(dep) if dep is not None else None, _ptr(b2), _ptr(best2),
                         _ptr(d2) if d2 is not None else None, npix)
    if dep is not None:
        depth_out[:] = dep
    return buf.reshape(h, w)


def raster_near(sx, sy, z, col, w, h, size=1, bg=0xFF09100F):
    """render.rasterise_nearest on every core -> (buffer (h, w), depth (h*w,) float32 inf where empty, visible)."""
    lib = _load()
    sx, sy, z = _f4(sx), _f4(sy), _f4(z)
    col = np.ascontiguousarray(col, np.uint32)
    n, npix = len(sx), w * h
    cuts = _pieces(n)

    def piece(j):
        a, b = cuts[j], cuts[j + 1]
        dep = np.full(npix, np.inf, np.float32)
        win = np.full(npix, -1, np.int64)
        lib.cp_near_winners(_ptr(sx[a:]), _ptr(sy[a:]), _ptr(z[a:]), b - a, int(a), int(w), int(h), _ptr(dep),
                            _ptr(win))
        return dep, win
    parts = list(_pool().map(piece, range(len(cuts) - 1)))
    dep, win = parts[0]
    for d2, w2 in parts[1:]:
        lib.cp_merge_near(_ptr(dep), _ptr(win), _ptr(d2), _ptr(w2), npix)
    buf = np.empty(npix, np.uint32)
    nv = lib.cp_splat_near(_ptr(win), _ptr(dep), _ptr(col), int(w), int(h), int(size), int(bg) & 0xFFFFFFFF,
                           _ptr(buf))
    return buf.reshape(h, w), dep, int(nv)


def in_box(a, cx, cy, bearing, length, width, zrange=None):
    """Indices of a tile's records inside a rotated box: PointSet.from_store's test, in one pass."""
    lib = _load()
    a = np.ascontiguousarray(a)
    b = np.radians(bearing)
    idx = np.empty(len(a), np.int64)
    z0, z1 = (zrange if zrange is not None else (1.0, 0.0))
    k = lib.cp_in_box(_ptr(a), len(a), float(cx), float(cy), float(np.sin(b)), float(np.cos(b)), length / 2.0,
                      width / 2.0, float(z0), float(z1), _ptr(idx))
    return idx[:k]


# ---------------------------------------------------------------- OpenGL (3D view on the graphics card)
def gl_init():
    """With an OpenGL context current: 1 when the engine found every OpenGL function it needs."""
    lib = _load()
    return bool(lib is not None and lib.cp_gl_init())


def gl_renderer():
    """The OpenGL renderer | vendor names (after gl_init, context current)."""
    lib = _load()
    b = C.create_string_buffer(256)
    lib.cp_gl_renderer(b, 256)
    return b.value.decode("utf-8", "replace")


def gl_upload(ids, xyz=None, col=None, n=0):
    lib = _load()
    x = np.ascontiguousarray(xyz, np.float32) if xyz is not None else None
    c = np.ascontiguousarray(col, np.uint32) if col is not None else None
    return lib.cp_gl_upload(_ptr(x) if x is not None else None, _ptr(c) if c is not None else None, int(n),
                            _ptr(ids))


def gl_free(ids, fb):
    lib = _load()
    if lib is not None:
        lib.cp_gl_free(_ptr(ids), _ptr(fb))


def gl_draw(ids, m, proj, view, w, h, size, bg, fb, want_depth=True):
    """-> (buffer (h, w) uint32 ARGB, window depth (h, w) float32 or None, status)."""
    lib = _load()
    out = np.empty(h * w, np.uint32)
    dep = np.empty(h * w, np.float32) if want_depth else None
    pr = np.ascontiguousarray(proj, np.float32)
    vw = np.ascontiguousarray(view, np.float32)
    st = lib.cp_gl_draw(_ptr(ids), int(m), _ptr(pr), _ptr(vw), int(w), int(h), float(size), int(bg) & 0xFFFFFFFF,
                        _ptr(fb), _ptr(out), _ptr(dep) if dep is not None else None)
    return out.reshape(h, w), (dep.reshape(h, w) if dep is not None else None), int(st)


def eye_dome(buf, depth, strength=1.2):
    """render.eye_dome (radius 1) compiled, rows split over the cores."""
    lib = _load()
    h, w = buf.shape
    b = np.ascontiguousarray(buf, np.uint32)
    d = np.ascontiguousarray(depth, np.float32).reshape(-1)
    out = np.empty_like(b)
    k = max(1, min(threads(), h // 64))
    cuts = np.linspace(0, h, k + 1).astype(int)
    jobs = [_pool().submit(lib.cp_eye_dome, _ptr(b), _ptr(d), w, h, float(strength), int(cuts[i]), int(cuts[i + 1]),
                           _ptr(out)) for i in range(k)]
    for j in jobs:
        j.result()
    return out
