"""Zstandard decompression for .clraw chunks, from whichever backend exists.

Order of preference:
  1. the `zstandard` package, also looked for in DCS Core's ext/ folder
     (copies for the Python versions QGIS ships on Windows)
  2. `compression.zstd` (Python 3.14+ standard library)
  3. ctypes against the libzstd that ships with QGIS / GDAL
     (zstd.dll / libzstd.dll in the QGIS bin folder on Windows,
      libzstd.so.1 on Linux, libzstd.1.dylib on macOS)

.clraw chunk records carry the uncompressed size, so every backend
decompresses in one call with a known output size.
"""
import ctypes
import ctypes.util
import glob
import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
_impl = None
_name = "none"
_lock = threading.Lock()


class ZstdUnavailable(ImportError, RuntimeError):
    """No Zstandard decoder could be loaded (caught as ImportError or RuntimeError)."""


def ext_dir():
    """DCS Core's folder of bundled packages for this platform and Python, e.g. ext/win312."""
    tag = "%s%d%d" % ({"win32": "win", "darwin": "mac"}.get(sys.platform, "linux"),
                      sys.version_info[0], sys.version_info[1])
    return os.path.join(HERE, "ext", tag)


def _add_ext_path():
    p = ext_dir()
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)


def _try_zstandard():
    import zstandard
    local = threading.local()      # ZstdDecompressor objects are not thread safe

    def dec(data, size):
        d = getattr(local, "d", None)
        if d is None:
            d = local.d = zstandard.ZstdDecompressor()
        return d.decompress(data, max_output_size=int(size) or 0)
    return dec, "zstandard %s" % zstandard.__version__


def _try_stdlib():
    from compression import zstd as z  # noqa: Python 3.14+

    def dec(data, size):
        return z.decompress(data)
    return dec, "compression.zstd"


def _lib_candidates():
    names = []
    for n in ("zstd", "libzstd", "zstd-1"):
        found = ctypes.util.find_library(n)
        if found:
            names.append(found)
    exe = os.path.dirname(sys.executable)
    roots = {exe, os.path.dirname(exe)}
    for k in ("OSGEO4W_ROOT", "QGIS_PREFIX_PATH", "CONDA_PREFIX"):
        if os.environ.get(k):
            roots.add(os.environ[k])
    for p in os.environ.get("PATH", "").split(os.pathsep):
        if p:
            roots.add(p)
    pats = ("zstd.dll", "zstd*.dll", "libzstd.dll", "libzstd*.dll", "libzstd.so*", "libzstd*.dylib")
    for r in sorted(roots):
        for sub in ("", "bin", "lib", "Library/bin", "apps/qgis/bin", "apps/qgis-ltr/bin"):
            d = os.path.join(r, sub)
            if not os.path.isdir(d):
                continue
            for pat in pats:
                names.extend(sorted(glob.glob(os.path.join(d, pat))))
    names += ["libzstd.so.1", "libzstd.so", "zstd.dll", "libzstd.dll", "libzstd.1.dylib",
              "/opt/homebrew/lib/libzstd.dylib", "/usr/local/lib/libzstd.dylib"]
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
            size = int(size)
            out = ctypes.create_string_buffer(max(size, 1))
            src = ctypes.c_char_p(bytes(data))
            n = f(out, size, src, len(data))
            if iserr(n):
                raise ValueError("zstd: corrupt chunk")
            return out.raw[:n]
        return dec, "libzstd (%s)" % os.path.basename(n)
    raise ImportError("libzstd not found (%s)" % last)


def decoder():
    """(decompress(data, size) -> bytes, backend name). Raises ZstdUnavailable."""
    global _impl, _name
    if _impl is not None:
        return _impl, _name
    with _lock:
        if _impl is not None:
            return _impl, _name
        _add_ext_path()
        errs = []
        for t in (_try_zstandard, _try_stdlib, _try_ctypes):
            try:
                _impl, _name = t()
                return _impl, _name
            except Exception as e:  # noqa: BLE001 - depends on host
                errs.append("%s: %s" % (t.__name__, e))
    raise ZstdUnavailable("No Zstandard decoder is available to read .clraw files. Install it from the OSGeo4W "
                          "shell with 'python -m pip install zstandard'.\n" + "\n".join(errs))


def decompress(data, size):
    return decoder()[0](data, size)


def backend():
    """Name of the backend in use, or "none"."""
    try:
        return decoder()[1]
    except ZstdUnavailable:
        return "none"
