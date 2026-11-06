"""How much memory a point density needs, and how much this computer has (no psutil needed)."""
import os
import sys

from . import engine

# bytes per point, measured on the simulated patch test (6 Nov 2026)
LINE_BYTES = 55          # cached points: t, p, intensity, channel (22) + georeferenced copy, index, mask (33)
VIEW_BYTES = 110         # Data View: t, p, intensity, line + one pose cache (pos f8, C f4) + shown and ghost copies
GEOREF_BATCH_BYTES = 1000000 * 250     # georef_times holds one batch of 1 M points' poses at a time
WARN_FRACTION = 0.40
REFUSE_FRACTION = 0.80


def physical_ram():
    """Bytes of physical memory, or None when unknown."""
    try:
        import psutil  # noqa: PLC0415
        return int(psutil.virtual_memory().total)
    except Exception:  # noqa: BLE001
        pass
    if sys.platform.startswith("win"):
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return int(st.ullTotalPhys)
        except Exception:  # noqa: BLE001
            return None
        return None
    try:
        return int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))
    except (ValueError, OSError, AttributeError):
        return None


def estimate(settings, n_lines, n_boxes=3):
    """Peak bytes AlignPro holds for these settings with n_lines recorded lines:
    -> dict(lines, solve, view, total)."""
    s = dict(engine.DEFAULT_SETTINGS)
    s.update(settings or {})
    lines = int(s["points_per_line"]) * max(1, int(n_lines)) * LINE_BYTES + GEOREF_BATCH_BYTES
    _n, solve = engine.solve_points_estimate(s)
    view = int(s["fine_points_per_box"]) * n_boxes * VIEW_BYTES
    return {"lines": lines, "solve": solve, "view": view, "total": lines + solve + view}


def check(settings, n_lines, ram=None):
    """-> (level, text): level "ok" | "warn" (above WARN_FRACTION of the RAM) | "refuse" (above REFUSE_FRACTION)."""
    est = estimate(settings, n_lines)
    ram = physical_ram() if ram is None else ram
    gb = est["total"] / 1e9
    if not ram:
        return "ok", "about %.1f GB of memory" % gb
    frac = est["total"] / ram
    txt = "about %.1f GB of memory (%.0f %% of this computer's %.0f GB)" % (gb, 100 * frac, ram / 1e9)
    if frac > REFUSE_FRACTION:
        return "refuse", txt
    if frac > WARN_FRACTION:
        return "warn", txt
    return "ok", txt
