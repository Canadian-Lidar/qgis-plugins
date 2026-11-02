"""Safe file writing, fingerprints and portable paths. No Qt."""
import hashlib
import json
import os
import tempfile

MB = 1 << 20


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
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path, obj, backup=False):
    atomic_write_bytes(path, (json.dumps(obj, indent=1, ensure_ascii=False) + "\n").encode("utf-8"), backup)


def read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def fingerprint(path):
    """Size + sha256 of the first and last megabyte: fast on multi-gigabyte
    captures and enough to notice a replaced, re-copied or truncated file.
    None when the file does not exist."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    h = hashlib.sha256()
    h.update(str(size).encode())
    with open(path, "rb") as fh:
        h.update(fh.read(MB))
        if size > 2 * MB:
            fh.seek(size - MB)
            h.update(fh.read(MB))
    return "fp:" + h.hexdigest()[:32]


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
