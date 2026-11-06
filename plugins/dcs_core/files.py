"""Safe file writing, fingerprints and portable paths. No Qt."""
import hashlib
import json
import os
import tempfile
import time

MB = 1 << 20
# os.replace on Windows fails (WinError 5 / 32) while another process has the target open (a reader in another
# QGIS, an antivirus scanner, OneDrive, an SMB oplock); such holds are short, so the rename is retried.
REPLACE_TRIES_S = 3.0


def replace(src, dst, timeout=REPLACE_TRIES_S):
    """os.replace, retried for up to `timeout` seconds while the target is held open (Windows)."""
    t0 = time.monotonic()
    delay = 0.01
    while True:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if time.monotonic() - t0 >= timeout:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.2)


def atomic_write_bytes(path, data, backup=False):
    """Write data to path through a temporary file in the same folder and an
    atomic rename, so a crash leaves either the old file or the new one."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", dir=folder)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if backup and os.path.exists(path):
            try:
                with open(path, "rb") as fi:
                    old = fi.read()
                atomic_write_bytes(path + ".bak", old)
            except OSError:
                pass
        replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path, obj, backup=False, indent=1):
    atomic_write_bytes(path, (json.dumps(obj, indent=indent, ensure_ascii=False) + "\n").encode("utf-8"), backup)


def read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def read_json_retry(path, timeout=2.0):
    """read_json, retried for up to `timeout` s on PermissionError (Windows: the file is being replaced) and on
    an incomplete read; FileNotFoundError is raised at once."""
    t0 = time.monotonic()
    delay = 0.01
    while True:
        try:
            return read_json(path)
        except FileNotFoundError:
            raise
        except (OSError, ValueError):
            if time.monotonic() - t0 >= timeout:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.2)


def _fingerprint_v1(path, size):
    """1.0-1.7 fingerprint: size + sha256 of the first MB and (files over 2 MB) the last MB."""
    h = hashlib.sha256()
    h.update(str(size).encode())
    with open(path, "rb") as fh:
        h.update(fh.read(MB))
        if size > 2 * MB:
            fh.seek(size - MB)
            h.update(fh.read(MB))
    return "fp:" + h.hexdigest()[:32]


def fingerprint(path):
    """Size + sha256 of the first and last megabyte: fast on multi-gigabyte
    captures and enough to notice a replaced, re-copied or truncated file.
    Files of 2 MB or less are hashed whole ("fp2:", 1.8.0); larger files give the
    same "fp:" value as before. None when the file does not exist."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size > 2 * MB:
        return _fingerprint_v1(path, size)
    h = hashlib.sha256()
    h.update(str(size).encode())
    with open(path, "rb") as fh:
        h.update(fh.read())
    return "fp2:" + h.hexdigest()[:32]


def fingerprint_matches(path, recorded, current=None):
    """True when the file still has the recorded fingerprint. A fingerprint recorded before 1.8.0 ("fp:" for a
    file of 2 MB or less) is compared with the old method, so old projects do not go stale."""
    if not recorded:
        return True
    cur = current if current is not None else fingerprint(path)
    if cur is None:
        return False
    if cur == recorded:
        return True
    if recorded.startswith("fp:") and cur.startswith("fp2:"):
        try:
            return _fingerprint_v1(path, os.path.getsize(path)) == recorded
        except OSError:
            return False
    return False


def content_id(paths):
    """sha256 over the full content of the files, in the given order (the
    names do not take part), used as a handover id."""
    h = hashlib.sha256()
    for p in paths:
        with open(p, "rb") as fh:
            while True:
                b = fh.read(4 * MB)
                if not b:
                    break
                h.update(b)
        h.update(b"\x00")
    return "sha256:" + h.hexdigest()


def portable(path, base):
    """path relative to base (forward slashes) when on the same drive, else
    absolute."""
    path = os.path.abspath(path)
    try:
        rel = os.path.relpath(path, base)
    except ValueError:          # another drive on Windows
        return path.replace("\\", "/")
    return rel.replace("\\", "/")


def resolve(path, base):
    if not path:
        return path
    p = path.replace("/", os.sep)
    return os.path.normpath(p if os.path.isabs(p) else os.path.join(base, p))
