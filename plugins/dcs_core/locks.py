"""Lock files: one writer per stage folder (and briefly for the project file).
A lock left by a process that no longer runs on this computer, or older than
the time limit, is taken over. No Qt."""
import json
import os
import socket
import time

from .files import atomic_write_json

STALE_S = 24 * 3600


class Locked(Exception):
    """Someone else is working on this stage."""

    def __init__(self, info):
        self.info = info
        super().__init__("%s is being processed by %s on %s since %s" % (
            info.get("stage", "this stage"), info.get("plugin", "another program"), info.get("host", "?"),
            time.strftime("%H:%M", time.localtime(info.get("time", 0)))))


def _alive(pid):
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)     # QUERY_LIMITED_INFORMATION
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(h)
            return bool(ok) and code.value == 259                          # STILL_ACTIVE
        except Exception:       # noqa: BLE001
            return True
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def read(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def is_live(info):
    if not info:
        return False
    if time.time() - float(info.get("time", 0)) > STALE_S:
        return False
    if info.get("host") == socket.gethostname():
        return _alive(int(info.get("pid", 0)))
    return True


class Lock:
    """with Lock(folder, stage, plugin): ... Raises Locked when another live
    process holds it. Re-entrant within one process (same pid)."""

    def __init__(self, folder, stage, plugin, name=".lock"):
        self.path = os.path.join(folder, name)
        self.stage, self.plugin = stage, plugin
        self.taken_over = None
        self.mine = False

    def acquire(self):
        info = read(self.path)
        if info and info.get("pid") == os.getpid() and info.get("host") == socket.gethostname():
            self.mine = False           # held by us already (nested): leave it to the outer holder
            return self
        if info and is_live(info):
            raise Locked(info)
        if info:
            self.taken_over = info
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        atomic_write_json(self.path, {"stage": self.stage, "plugin": self.plugin, "host": socket.gethostname(),
                                      "pid": os.getpid(), "time": time.time()})
        self.mine = True
        return self

    def release(self):
        if self.mine:
            try:
                os.unlink(self.path)
            except OSError:
                pass
            self.mine = False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False
