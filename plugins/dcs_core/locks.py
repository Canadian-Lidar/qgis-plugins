"""Lock files: one writer per stage folder (and briefly for the project file).

The lock file is created exclusively (O_CREAT | O_EXCL), so two processes can never both hold it. A lock left
by a process that no longer runs on this computer, or older than the time limit, is taken over safely: it is
renamed aside first and checked to be the one judged stale, so a fresh lock taken meanwhile is never lost.
Within one process a lock is re-entrant (nested holders share it). release() removes the file only while it
still carries this holder's token. thread_lock(path) gives a per-path threading.RLock for code that must also
keep threads of one process apart (DcsProject.modify). No Qt."""
import json
import os
import socket
import threading
import time
import uuid

STALE_S = 24 * 3600
UNREADABLE_GRACE_S = 30         # a lock file being written (not yet valid JSON) counts as live this long


class Locked(Exception):
    """Someone else is working on this stage."""

    def __init__(self, info):
        self.info = info or {}
        super().__init__("%s is being processed by %s on %s since %s" % (
            self.info.get("stage", "this stage"), self.info.get("plugin", "another program"),
            self.info.get("host", "?"), time.strftime("%H:%M", time.localtime(self.info.get("time", 0)))))


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


# ----------------------------------------------------------------------------- in-process bookkeeping
_MUTEX = threading.Lock()
_HELD = {}              # abs lock path -> number of nested holders in this process (beyond the owner)
_ACQ = {}               # abs lock path -> threading.Lock serialising acquire() within this process
_RLOCKS = {}            # abs path -> threading.RLock (thread_lock)


def thread_lock(path):
    """The process-wide threading.RLock for path (created on first use)."""
    p = os.path.abspath(path)
    with _MUTEX:
        lk = _RLOCKS.get(p)
        if lk is None:
            lk = _RLOCKS[p] = threading.RLock()
        return lk


def _acq_lock(p):
    with _MUTEX:
        lk = _ACQ.get(p)
        if lk is None:
            lk = _ACQ[p] = threading.Lock()
        return lk


def _same(a, b):
    """The same lock record (by token, else pid + time for locks written before 1.8.0)."""
    if not a or not b:
        return False
    if a.get("token") or b.get("token"):
        return a.get("token") == b.get("token")
    return a.get("pid") == b.get("pid") and a.get("time") == b.get("time") and a.get("host") == b.get("host")


def _unlink(path, timeout=2.0):
    t0 = time.monotonic()
    while True:
        try:
            os.unlink(path)
            return True
        except FileNotFoundError:
            return True
        except PermissionError:             # Windows: someone is reading it right now
            if time.monotonic() - t0 > timeout:
                return False
            time.sleep(0.02)
        except OSError:
            return False


class Lock:
    """with Lock(folder, stage, plugin): ... Raises Locked when another live
    process holds it (after waiting up to `wait` seconds). Re-entrant within one process."""

    def __init__(self, folder, stage, plugin, name=".lock", wait=0.0):
        self.path = os.path.abspath(os.path.join(folder, name))
        self.stage, self.plugin = stage, plugin
        self.wait = float(wait)
        self.taken_over = None
        self.mine = False
        self._nested = False
        self.token = None

    # ------------------------------------------------------------------
    def _take_over(self, seen):
        """Move a stale lock aside and remove it. True when the path is free to try again."""
        aside = "%s.stale-%s" % (self.path, uuid.uuid4().hex[:12])
        try:
            os.rename(self.path, aside)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        moved = read(aside)
        if seen is not None and not _same(moved, seen):
            # someone replaced the stale lock with a fresh one between our read and the rename: put it back
            try:
                os.rename(aside, self.path)
            except OSError:
                pass
            return False
        _unlink(aside)
        self.taken_over = seen
        return True

    def acquire(self):
        with _MUTEX:
            if self.path in _HELD:
                _HELD[self.path] += 1
                self.mine, self._nested = False, True
                return self
        deadline = time.monotonic() + max(0.0, self.wait)
        acq = _acq_lock(self.path)
        if not acq.acquire(timeout=max(0.5, self.wait)):
            raise Locked(read(self.path) or {"stage": self.stage, "plugin": "another task in this QGIS"})
        try:
            with _MUTEX:                    # another thread of this process may have taken it while we waited
                if self.path in _HELD:
                    _HELD[self.path] += 1
                    self.mine, self._nested = False, True
                    return self
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            delay = 0.02
            while True:
                token = uuid.uuid4().hex
                rec = {"stage": self.stage, "plugin": self.plugin, "host": socket.gethostname(),
                       "pid": os.getpid(), "time": time.time(), "token": token}
                try:
                    fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                except FileExistsError:
                    cur = read(self.path)
                    if cur is None:
                        try:
                            age = time.time() - os.path.getmtime(self.path)
                        except OSError:
                            continue            # gone already: try again
                        live = age < UNREADABLE_GRACE_S
                    elif cur.get("pid") == os.getpid() and cur.get("host") == socket.gethostname():
                        live = False            # left by this process (not held now): stale
                    else:
                        live = is_live(cur)
                    if not live and self._take_over(cur):
                        continue
                    if time.monotonic() >= deadline:
                        raise Locked(cur or {"stage": self.stage})
                    time.sleep(delay)
                    delay = min(delay * 2, 0.25)
                    continue
                except PermissionError:         # Windows: a pending delete of the old lock file
                    if time.monotonic() >= deadline:
                        raise Locked(read(self.path) or {"stage": self.stage})
                    time.sleep(delay)
                    continue
                try:
                    os.write(fd, json.dumps(rec).encode("utf-8"))
                    os.fsync(fd)
                finally:
                    os.close(fd)
                self.token = token
                self.mine = True
                with _MUTEX:
                    _HELD[self.path] = 0
                return self
        finally:
            acq.release()

    def release(self):
        if self._nested:
            with _MUTEX:
                if self.path in _HELD and _HELD[self.path] > 0:
                    _HELD[self.path] -= 1
            self._nested = False
            return
        if self.mine:
            cur = read(self.path)
            for _ in range(20):                 # Windows: unreadable for a moment while another process reads it
                if cur is not None or not os.path.exists(self.path):
                    break
                time.sleep(0.02)
                cur = read(self.path)
            if cur is not None and cur.get("token") == self.token:
                _unlink(self.path)
            with _MUTEX:
                _HELD.pop(self.path, None)
            self.mine = False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False
