"""Zstandard decompression for .clraw chunks, from whichever backend exists.

Order: Python 3.14 ``compression.zstd`` -> the ``zstandard`` package (also
looked for in the plugin's ``ext`` folder) -> the zstd shared library through
ctypes (QGIS ships one with GDAL on Windows, Linux and macOS have libzstd).
MCAP chunk records carry the uncompressed size, so the plain one-shot
``ZSTD_decompress`` call is all that is needed.
"""
import ctypes
import ctypes.util
import glob
import os
import sys

_impl = None
_name = "none"


def _try_stdlib():
    from compression import zstd as z  # Python >= 3.14

    def dec(data, size):
        return z.decompress(data)
    return dec


def _try_zstandard():
    import threading
    import zstandard
    local = threading.local()      # ZstdDecompressor objects are not thread safe

    def dec(data, size):
        d = getattr(local, "d", None)
        if d is None:
            d = local.d = zstandard.ZstdDecompressor()
        return d.decompress(data, max_output_size=int(size) or 0)
    return dec


def _candidate_libs():
    names = ["zstd", "libzstd", "zstd-1", "libzstd.so.1"]
    out = []
    for n in names:
        p = ctypes.util.find_library(n)
        if p:
            out.append(p)
    exe_dir = os.path.dirname(sys.executable)
    roots = [exe_dir, os.path.join(exe_dir, "..", "bin"), os.path.join(exe_dir, "..", "lib"),
             os.environ.get("OSGEO4W_ROOT", ""), os.path.join(os.environ.get("OSGEO4W_ROOT", ""), "bin"),
             os.environ.get("QGIS_PREFIX_PATH", "")]
    for r in roots:
        if r and os.path.isdir(r):
            out += glob.glob(os.path.join(r, "zstd*.dll")) + glob.glob(os.path.join(r, "libzstd*.dll"))
            out += glob.glob(os.path.join(r, "libzstd*.dylib"))
    out += ["libzstd.so.1", "libzstd.so", "libzstd.1.dylib", "/opt/homebrew/lib/libzstd.dylib",
            "/usr/local/lib/libzstd.dylib"]
    return out


def _try_ctypes():
    lib = None
    for p in _candidate_libs():
        try:
            lib = ctypes.CDLL(p)
            lib.ZSTD_decompress
            break
        except (OSError, AttributeError):
            lib = None
    if lib is None:
        raise ImportError("no zstd shared library")
    f = lib.ZSTD_decompress
    f.restype = ctypes.c_size_t
    f.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
    iserr = lib.ZSTD_isError
    iserr.restype = ctypes.c_uint
    iserr.argtypes = [ctypes.c_size_t]

    def dec(data, size):
        size = int(size)
        dst = ctypes.create_string_buffer(max(size, 1))
        src = ctypes.c_char_p(bytes(data))
        n = f(dst, size, src, len(data))
        if iserr(n):
            raise ValueError("zstd: corrupt chunk")
        return dst.raw[:n]
    return dec


def _add_ext_path():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tag = "%s%d%d" % ({"win32": "win", "darwin": "mac"}.get(sys.platform, "linux"),
                      sys.version_info[0], sys.version_info[1])
    p = os.path.join(here, "ext", tag)
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)


def backend():
    """Return (decompress(data, size) -> bytes, backend name)."""
    global _impl, _name
    if _impl is not None:
        return _impl, _name
    _add_ext_path()
    for name, fn in (("compression.zstd", _try_stdlib), ("zstandard", _try_zstandard), ("libzstd", _try_ctypes)):
        try:
            _impl = fn()
            _name = name
            return _impl, _name
        except Exception:
            continue
    raise RuntimeError("CloudPro needs Zstandard to read .clraw files: none of compression.zstd, "
                       "the zstandard package or a zstd library was found")


def decompress(data, size):
    return backend()[0](data, size)
