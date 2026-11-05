"""Small RINEX helpers: header facts and time span of an observation file,
decompression of .gz / .Z, and the RINEX 2 short / 3 long file names."""
import datetime
import gzip
import io
import os
import re
import shutil



def open_text(path):
    if path.lower().endswith(".gz"):
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="ascii", errors="replace")
    return open(path, "r", encoding="ascii", errors="replace")


def is_hatanaka(path):
    p = path.lower()
    if p.endswith(".gz"):
        p = p[:-3]
    if p.endswith(".crx") or re.search(r"\.\d\dd$", p):
        return True
    try:
        with open_text(path) as fh:
            first = fh.readline()
        return "COMPACT RINEX" in first or "CRINEX" in first
    except OSError:
        return False


def decompress_to(path, out_dir):
    """Return a plain-text copy of a .gz file (or the path itself)."""
    if not path.lower().endswith(".gz"):
        return path
    base = os.path.basename(path)[:-3]
    out = os.path.join(out_dir, base)
    if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(path):
        return out
    os.makedirs(out_dir, exist_ok=True)
    tmp = out + ".part"
    with gzip.open(path, "rb") as fi, open(tmp, "wb") as fo:
        shutil.copyfileobj(fi, fo, 1 << 20)
    os.replace(tmp, out)
    return out


def _epoch3(line):
    # "> 2026 10 15 14 00 19.1250000  0 17"
    try:
        y, mo, d, h, mi = (int(line[2:6]), int(line[7:9]), int(line[10:12]), int(line[13:15]), int(line[16:18]))
        s = float(line[18:29])
        return datetime.datetime(y, mo, d, h, mi) + datetime.timedelta(seconds=s)
    except ValueError:
        return None


def _epoch2(line):
    # " 26 10 15 14  0 19.1250000  0 17G..."
    try:
        y = int(line[1:3]); y += 2000 if y < 80 else 1900
        mo, d, h, mi = int(line[4:6]), int(line[7:9]), int(line[10:12]), int(line[13:15])
        s = float(line[15:26])
        return datetime.datetime(y, mo, d, h, mi) + datetime.timedelta(seconds=s)
    except ValueError:
        return None


_GPS0 = datetime.datetime(1980, 1, 6)


class _Coverage:
    """Epoch coverage of an observation file as [t0, t1] runs in GPS seconds
    (a gap is more than 2.5 intervals without an epoch), plus runs with
    fewer than 5 satellites."""

    def __init__(self, interval):
        self.dt = interval
        self.seg, self.low = [], []
        self.prev = None
        self.first = []

    def add(self, t, nsat_txt):
        g = (t - _GPS0).total_seconds()
        if self.dt is None and len(self.first) < 20:
            self.first.append(g)
            if len(self.first) == 20:
                d = sorted(self.first[i + 1] - self.first[i] for i in range(19))
                self.dt = d[9] or 1.0
        gap = 2.5 * (self.dt or 30.0)
        if self.prev is None or g - self.prev > gap:
            self.seg.append([g, g])
        else:
            self.seg[-1][1] = g
        self.prev = g
        try:
            ns = int(nsat_txt)
        except ValueError:
            return
        if ns < 5:
            if self.low and g - self.low[-1][1] <= gap:
                self.low[-1][1] = g
            else:
                self.low.append([g, g])

    def done(self):
        return self.seg[:5000], self.low[:5000]


def header_info(path, scan_body=True):
    """Facts about a RINEX observation file. Times are GPS time (datetime)."""
    info = {"path": path, "version": None, "marker": "", "antenna": "", "receiver": "",
            "approx_xyz": None, "delta_hen": (0.0, 0.0, 0.0), "interval": None,
            "first": None, "last": None, "systems": "", "epochs": 0, "hatanaka": False,
            "obs_types": {}, "segments": [], "low_sats": []}
    if is_hatanaka(path):
        info["hatanaka"] = True
    times = []
    with open_text(path) as fh:
        for line in fh:
            label = line[60:80].strip()
            if label == "RINEX VERSION / TYPE":
                try:
                    info["version"] = float(line[:9])
                except ValueError:
                    pass
            elif label == "MARKER NAME":
                info["marker"] = line[:60].strip()
            elif label == "ANT # / TYPE":
                info["antenna"] = line[20:40].rstrip()
            elif label == "REC # / TYPE / VERS":
                info["receiver"] = line[20:40].strip()
            elif label == "APPROX POSITION XYZ":
                try:
                    info["approx_xyz"] = (float(line[0:14]), float(line[14:28]), float(line[28:42]))
                except ValueError:
                    pass
            elif label == "ANTENNA: DELTA H/E/N":
                try:
                    info["delta_hen"] = (float(line[0:14]), float(line[14:28]), float(line[28:42]))
                except ValueError:
                    pass
            elif label == "INTERVAL":
                try:
                    info["interval"] = float(line[:10])
                except ValueError:
                    pass
            elif label == "SYS / # / OBS TYPES":
                if line[0] != " ":
                    sysch = line[0]
                    info["systems"] += sysch
                    info["obs_types"].setdefault(sysch, [])
                info["obs_types"].setdefault(info["systems"][-1:] or "?", []).extend(line[7:60].split())
            elif label == "TIME OF FIRST OBS":
                try:
                    v = line[:43].split()
                    info["first"] = datetime.datetime(int(v[0]), int(v[1]), int(v[2]), int(v[3]), int(v[4])) + \
                        datetime.timedelta(seconds=float(v[5]))
                except (ValueError, IndexError):
                    pass
            elif label == "END OF HEADER":
                break
        if not scan_body or info["hatanaka"]:
            return info
        v3 = (info["version"] or 3) >= 3
        n = 0
        cov = _Coverage(info["interval"])
        for line in fh:
            if v3:
                if line.startswith(">"):
                    t = _epoch3(line)
                    if t:
                        n += 1
                        if len(times) < 50:
                            times.append(t)
                        info["last"] = t
                        cov.add(t, line[32:35])
            else:
                if len(line) > 29 and line[0] == " " and line[3] == " " and line[28] in "0123456" and \
                        line[1:3].strip().isdigit() and line[26:29].strip() in ("0", "1", "2", "3", "4", "5", "6"):
                    t = _epoch2(line)
                    if t:
                        n += 1
                        if len(times) < 50:
                            times.append(t)
                        info["last"] = t
                        cov.add(t, line[29:32])
        info["epochs"] = n
        info["segments"], info["low_sats"] = cov.done()
    if times and not info["first"]:
        info["first"] = times[0]
    if len(times) > 2 and not info["interval"]:
        d = sorted((times[i + 1] - times[i]).total_seconds() for i in range(len(times) - 1))
        info["interval"] = d[len(d) // 2]
    return info

