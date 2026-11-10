"""Performance log and report (CloudPro 1.9.8).

Every timed step - loading an Inspection Area, a filter run (with each filter's time), a preview, georeferencing
(manual or automatic), publishing and exporting, how the 3D view draws (graphics card or processor) and its frame
times - is appended to 03_CloudPro/log/performance.log, one line each, with the computer described at the top of
each session. MENU > Performance report... writes one text file (computer, job, this log, recent History and run
logs) to send for review.

    log(project, kind, text)            never raises
    session(project, extra=())          the computer, once per opened job
    write_report(project, path, extra)  -> path
"""
import datetime as _dt
import glob
import os
import platform
import sys

NAME = "performance.log"
_SEEN = set()


def _now():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def path(project):
    return os.path.join(project.log_dir, NAME)


def log(project, kind, text):
    """Append one line: time, kind, text. Errors are swallowed (a log must never stop the work)."""
    if project is None:
        return
    try:
        with open(path(project), "a", encoding="utf-8") as f:
            f.write("%s  %-10s %s\n" % (_now(), kind, str(text).replace("\n", " | ")))
    except Exception:                           # noqa: BLE001
        pass


def _ram_gb():
    try:
        if sys.platform.startswith("win"):
            import ctypes

            class M(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = M()
            m.dwLength = ctypes.sizeof(M)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return m.ullTotalPhys / 2 ** 30, m.ullAvailPhys / 2 ** 30
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0]) * 1024
        return info["MemTotal"] / 2 ** 30, info.get("MemAvailable", 0) / 2 ** 30
    except Exception:                           # noqa: BLE001
        return None, None


def _cpu():
    name = ""
    try:
        if sys.platform.startswith("win"):
            import winreg
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            name = winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
        elif os.path.exists("/proc/cpuinfo"):
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        name = line.split(":", 1)[1].strip()
                        break
    except Exception:                           # noqa: BLE001
        pass
    return name or platform.processor() or platform.machine()


def system_info():
    """Lines describing this computer and the software CloudPro runs on."""
    lines = []
    try:
        from ..guide import installed_version
        lines.append("CloudPro %s" % installed_version())
    except Exception:                           # noqa: BLE001
        pass
    try:
        from qgis.core import Qgis
        lines.append("QGIS %s" % Qgis.version())
    except Exception:                           # noqa: BLE001
        lines.append("QGIS: not running inside QGIS")
    try:
        import dcs_core
        lines.append("DCS Core %s (API %s)" % (getattr(dcs_core, "__version__", "?"), getattr(dcs_core, "API", "?")))
    except Exception:                           # noqa: BLE001
        pass
    try:
        from ..qt import QtCore
        lines.append("Qt %s" % QtCore.qVersion())
    except Exception:                           # noqa: BLE001
        pass
    lines.append("Python %s, %s" % (platform.python_version(), platform.platform()))
    tot, av = _ram_gb()
    lines.append("Processor: %s, %s logical cores" % (_cpu(), os.cpu_count()))
    if tot:
        lines.append("Memory: %.1f GB, %.1f GB free" % (tot, av))
    try:
        import numpy
        import scipy
        lines.append("numpy %s, scipy %s" % (numpy.__version__, scipy.__version__))
    except Exception:                           # noqa: BLE001
        pass
    try:
        from . import engine
        lines.append("Engine: %s, %d worker threads" % (engine.status(), engine.threads()))
    except Exception:                           # noqa: BLE001
        pass
    return lines


def session(project, extra=()):
    """The computer at the top of a session, once per job per QGIS session."""
    if project is None:
        return
    key = os.path.abspath(project.log_dir)
    if key in _SEEN:
        return
    _SEEN.add(key)
    try:
        with open(path(project), "a", encoding="utf-8") as f:
            f.write("\n===== %s  session  %s\n" % (_now(), getattr(project, "name", "")))
            for line in system_info() + list(extra):
                f.write("    %s\n" % line)
    except Exception:                           # noqa: BLE001
        pass


def write_report(project, out_path, extra=(), log_lines=4000, history=300, run_logs=6):
    """One text file to send: computer, job, the performance log (last lines), recent History, recent run logs."""
    L = ["CloudPro performance report", "Written %s" % _now(), "", "== Computer"]
    L += ["    " + x for x in system_info()]
    L += ["", "== Job"]
    try:
        L.append("    Project: %s" % project.root)
        st = project.store
        L.append("    Points: %s in %d tiles, %d lines" % ("{:,}".format(int(st.total)), len(st.keys()),
                                                             len(project.lines)))
    except Exception as e:                      # noqa: BLE001
        L.append("    (%s)" % e)
    L += ["    " + x for x in extra]
    L += ["", "== Performance log (%s)" % path(project)]
    try:
        with open(path(project), encoding="utf-8") as f:
            lines = f.read().splitlines()
        if len(lines) > log_lines:
            L.append("    ... %d earlier lines left out" % (len(lines) - log_lines))
        L += lines[-log_lines:]
    except OSError:
        L.append("    (no performance log yet)")
    L += ["", "== History (last %d)" % history]
    for h in project.data.get("history", [])[-history:]:
        L.append("%s  %s" % (h.get("utc", ""), h.get("text", "")))
    L += ["", "== Recent run logs"]
    logs = sorted(p for p in glob.glob(os.path.join(project.log_dir, "*.log")) if not p.endswith(NAME))
    for p in logs[-run_logs:]:
        L.append("--- %s" % os.path.basename(p))
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                L += f.read().splitlines()[:400]
        except OSError:
            pass
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    return out_path
