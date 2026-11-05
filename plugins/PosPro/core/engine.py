"""Run the compiled PosPro engine in a separate process.

The engine prints one message per line:
    PROGRESS <0..1> <text>
    INFO <text> / WARN <text> / ERROR <text>
    RESULT <key>=<value>
A crash or fault in processing therefore ends only the engine process, never
QGIS, and a cancel simply kills the process.
"""
import os
import queue
import subprocess
import sys
import threading
import time

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Cancelled(Exception):
    pass


class EngineError(Exception):
    pass


def engine_path():
    if sys.platform.startswith("win"):
        p = os.path.join(PLUGIN_DIR, "bin", "windows", "pospro-engine.exe")
    elif sys.platform == "darwin":
        p = os.path.join(PLUGIN_DIR, "bin", "macos", "pospro-engine")
    else:
        p = os.path.join(PLUGIN_DIR, "bin", "linux", "pospro-engine")
    override = os.environ.get("POSPRO_ENGINE")
    if override and os.path.exists(override):
        return override
    return p


class EngineResult:
    def __init__(self):
        self.code = None
        self.results = {}
        self.warnings = []
        self.infos = []
        self.errors = []
        self.stderr = ""

    def ok(self):
        return self.code == 0 and not self.errors

    def message(self):
        if self.errors:
            return "; ".join(self.errors)
        if self.code not in (0, None):
            tail = self.stderr.strip().splitlines()[-3:]
            return "engine stopped with code %s %s" % (self.code, " ".join(tail))
        return ""


def _popen(args, cwd=None):
    kw = {}
    if sys.platform.startswith("win"):
        kw["creationflags"] = 0x08000000      # CREATE_NO_WINDOW
    exe = engine_path()
    if not os.path.exists(exe):
        raise EngineError("The processing engine is missing: %s" % exe)
    if not sys.platform.startswith("win"):
        try:
            os.chmod(exe, 0o755)
        except OSError:
            pass
    return subprocess.Popen([exe] + [str(a) for a in args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, cwd=cwd,
                            universal_newlines=True, encoding="utf-8", errors="replace",
                            bufsize=1, **kw)


def run(args, on_progress=None, on_message=None, is_cancelled=None, cwd=None):
    """Run one engine command and wait. Returns EngineResult."""
    res = EngineResult()
    proc = _popen(args, cwd)
    q = queue.Queue()

    def reader(stream, tag):
        for line in stream:
            q.put((tag, line.rstrip("\n")))
        q.put((tag, None))

    threads = [threading.Thread(target=reader, args=(proc.stdout, "out"), daemon=True),
               threading.Thread(target=reader, args=(proc.stderr, "err"), daemon=True)]
    for t in threads:
        t.start()
    done = 0
    err_lines = []
    try:
        while done < 2:
            if is_cancelled and is_cancelled():
                proc.kill()
                proc.wait()
                raise Cancelled()
            try:
                tag, line = q.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                done += 1
                continue
            if tag == "err":
                err_lines.append(line)
                continue
            _handle(line, res, on_progress, on_message)
        res.code = proc.wait()
    finally:
        # close the engine's pipes (Python otherwise warns "unclosed file" for each one)
        for t in threads:
            t.join(timeout=1.0)
        for f in (proc.stdout, proc.stderr):
            try:
                f.close()
            except Exception:       # noqa: BLE001
                pass
    res.stderr = "\n".join(err_lines[-50:])
    return res


def _handle(line, res, on_progress, on_message):
    if line.startswith("PROGRESS "):
        parts = line.split(" ", 2)
        try:
            f = float(parts[1])
        except (IndexError, ValueError):
            return
        if on_progress:
            on_progress(f, parts[2] if len(parts) > 2 else "")
    elif line.startswith("RESULT "):
        kv = line[7:]
        if "=" in kv:
            k, v = kv.split("=", 1)
            res.results[k.strip()] = v.strip()
    elif line.startswith("WARN "):
        res.warnings.append(line[5:])
        if on_message:
            on_message("warning", line[5:])
    elif line.startswith("INFO "):
        res.infos.append(line[5:])
        if on_message:
            on_message("info", line[5:])
    elif line.startswith("ERROR "):
        res.errors.append(line[6:])
        if on_message:
            on_message("error", line[6:])


def run_parallel(jobs, on_progress=None, on_message=None, is_cancelled=None):
    """Run several engine commands at once (e.g. forward and backward passes).

    jobs: list of (label, args). Progress is the mean of the jobs.
    Returns {label: EngineResult}.
    """
    fr = {label: 0.0 for label, _ in jobs}
    out = {}
    errors = {}
    lock = threading.Lock()
    stop = threading.Event()

    def cancelled():
        return stop.is_set() or (is_cancelled is not None and is_cancelled())

    def work(label, args):
        def prog(f, msg):
            with lock:
                fr[label] = f
                total = sum(fr.values()) / len(fr)
            if on_progress:
                on_progress(total, msg)
        try:
            out[label] = run(args, prog, on_message, cancelled)
        except Cancelled:
            errors[label] = "cancelled"
        except Exception as e:     # noqa: BLE001 - reported to the user
            errors[label] = str(e)
            stop.set()

    threads = [threading.Thread(target=work, args=j, daemon=True) for j in jobs]
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        if is_cancelled and is_cancelled():
            stop.set()
        time.sleep(0.1)
    if is_cancelled and is_cancelled():
        raise Cancelled()
    for label, e in errors.items():
        if e != "cancelled":
            raise EngineError("%s: %s" % (label, e))
    return out


def version():
    try:
        r = run(["version"])
        return r.results.get("version"), r.results.get("rtklib")
    except Exception:          # noqa: BLE001
        return None, None
