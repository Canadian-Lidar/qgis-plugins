"""Zstandard decompression with no hard dependency.

Order of preference:
  1. the `zstandard` package (pip install zstandard)
  2. `compression.zstd` (Python 3.14+ standard library)
  3. ctypes against the libzstd that ships with QGIS / GDAL
     (zstd.dll / libzstd.dll in the QGIS bin folder on Windows,
      libzstd.so.1 on Linux, libzstd.1.dylib on macOS)

.clraw chunk records carry the uncompressed size, so every path can
decompress in one call with a known output size.
"""
import ctypes
import ctypes.util
import glob
import os
import sys

_impl = None
_name = "none"


def _try_zstandard():
    import zstandard
    dctx = zstandard.ZstdDecompressor()

    def dec(data, size):
        return dctx.decompress(data, max_output_size=size)
    return dec, "zstandard %s" % zstandard.__version__


def _try_stdlib():
    from compression import zstd  # noqa: Python 3.14+

    def dec(data, size):
        return zstd.decompress(data)
    return dec, "compression.zstd"


def _lib_candidates():
    names = []
    found = ctypes.util.find_library("zstd")
    if found:
        names.append(found)
    roots = {os.path.dirname(sys.executable), os.path.dirname(os.path.dirname(sys.executable))}
    for k in ("OSGEO4W_ROOT", "QGIS_PREFIX_PATH", "CONDA_PREFIX"):
        if os.environ.get(k):
            roots.add(os.environ[k])
    for p in os.environ.get("PATH", "").split(os.pathsep):
        if p:
            roots.add(p)
    pats = ("zstd.dll", "libzstd.dll", "libzstd*.dll", "libzstd.so*", "libzstd*.dylib")
    for r in roots:
        for sub in ("", "bin", "lib", "Library/bin", "apps/qgis/bin", "apps/qgis-ltr/bin"):
            d = os.path.join(r, sub)
            for pat in pats:
                names.extend(glob.glob(os.path.join(d, pat)))
    names += ["libzstd.so.1", "libzstd.so", "zstd.dll", "libzstd.dll", "libzstd.1.dylib"]
    seen = []
    for n in names:
        if n not in seen:
            seen.append(n)
    return seen


def _try_ctypes():
    last = None
    for n in _lib_candidates():
        try:
            lib = ctypes.CDLL(n)
            f = lib.ZSTD_decompress
        except (OSError, AttributeError) as e:
            last = e
            continue
        f.restype = ctypes.c_size_t
        f.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
        iserr = lib.ZSTD_isError
        iserr.restype = ctypes.c_uint
        iserr.argtypes = [ctypes.c_size_t]

        def dec(data, size, f=f, iserr=iserr):
            out = ctypes.create_string_buffer(max(int(size), 1))
            src = ctypes.c_char_p(bytes(data))
            n = f(out, size, src, len(data))
            if iserr(n):
                raise ValueError("zstd: corrupt chunk")
            return out.raw[:n]
        return dec, "libzstd (%s)" % os.path.basename(n)
    raise ImportError("libzstd not found (%s)" % last)


def _init():
    global _impl, _name
    if _impl is not None:
        return
    errs = []
    for t in (_try_zstandard, _try_stdlib, _try_ctypes):
        try:
            _impl, _name = t()
            return
        except Exception as e:  # pragma: no cover - depends on host
            errs.append("%s: %s" % (t.__name__, e))
    raise ImportError("No Zstandard decoder available. Install it from the OSGeo4W shell with "
                      "'python -m pip install zstandard'.\n" + "\n".join(errs))


def decompress(data, size):
    _init()
    return _impl(data, size)


def backend():
    try:
        _init()
    except ImportError:
        return "none"
    return _name
