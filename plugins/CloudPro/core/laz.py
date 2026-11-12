"""LAZ (compressed LAS) writing.

LAZ holds exactly the points of a LAS file, compressed without loss (typically 5-10 times smaller); every common
point cloud program reads it. The compressor is the open-source LASzip-compatible library bundled in CloudPro/ext/
for each platform and Python version QGIS ships (MIT licence, ext/LICENSE-lazrs.txt).

Like the engine, the library is loaded from a per-user copy, never from the plugin folder: Windows locks a loaded
library until QGIS closes, which would stop the plugin manager from upgrading CloudPro.
"""
import hashlib
import os
import shutil
import struct
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_lock = threading.Lock()
_mod = None
_why = ""


def ext_tag():
    return "%s%d%d" % ({"win32": "win", "darwin": "mac"}.get(sys.platform, "linux"),
                       sys.version_info[0], sys.version_info[1])


def _cache_dir():
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "CloudPro", "ext")


def _copy_of(src):
    """Per-user copy of the bundled ext/<tag> folder, named by its content; older copies removed when free."""
    h = hashlib.sha1()
    files = []
    for root, _d, fs in os.walk(src):
        for f in sorted(fs):
            p = os.path.join(root, f)
            files.append(os.path.relpath(p, src))
            with open(p, "rb") as fh:
                h.update(fh.read())
    tag = os.path.basename(src)
    base = _cache_dir()
    dst = os.path.join(base, "%s-%s" % (tag, h.hexdigest()[:12]))
    if not os.path.isdir(dst):
        os.makedirs(base, exist_ok=True)
        tmp = tempfile.mkdtemp(dir=base, prefix=".tmp_")
        for rel in files:
            os.makedirs(os.path.dirname(os.path.join(tmp, rel)), exist_ok=True)
            shutil.copyfile(os.path.join(src, rel), os.path.join(tmp, rel))
        try:
            os.replace(tmp, dst)
        except OSError:                     # another QGIS made it first
            shutil.rmtree(tmp, ignore_errors=True)
    for f in os.listdir(base):
        if f.startswith(tag + "-") and os.path.join(base, f) != dst:
            shutil.rmtree(os.path.join(base, f), ignore_errors=True)
    return dst


def module():
    """The compressor module, or None (available() / why() say why)."""
    global _mod, _why
    with _lock:
        if _mod is not None or _why:
            return _mod
        src = os.path.join(HERE, "ext", ext_tag())
        try:
            if os.path.isdir(src):
                try:
                    p = _copy_of(src)
                except OSError:
                    p = src
                if p not in sys.path:
                    sys.path.insert(0, p)
            import lazrs
            lazrs.LazVlr.new_for_compression(6, 0)
            _mod = lazrs
        except Exception as e:      # noqa: BLE001
            _why = ("LAZ compression is not available for this computer (%s, Python %d.%d): %s"
                    % (sys.platform, sys.version_info[0], sys.version_info[1], e))
        return _mod


def available():
    return module() is not None


def why():
    module()
    return _why


def laszip_vlr(record_data):
    """The 'laszip encoded' VLR (record 22204) describing the compression."""
    return struct.pack("<H16sHH32s", 0, b"laszip encoded", 22204, len(record_data),
                       b"http://laszip.org") + record_data
