"""Convert a recorded ANPP stream into the files post-processing packages read (Inertial Explorer, Qinertia).

Taken from the mapper (mapper/export/convert.py, 2.10.0 before the exports were removed). The writers are
unchanged, so the files are the same byte for byte except the program name in the IMR header and the RINEX
"PGM / RUN BY / DATE" line, which now say RemixPro. Changes: packets are decoded by dcs_core.anpp; the
stream can also come from the /ins/anpp channel of .clraw files (ClrawAnppStream) when there is no .anpp;
convert() can be cancelled.

One pass over the .anpp file(s) feeds several writers:

  IMR         NovAtel Waypoint / Inertial Explorer raw IMU file
  QIMU        SBG Qinertia generic IMU file (ASCII, version 2)
  RINEX obs   raw GNSS observations, one file per receiver
  RINEX nav   GPS broadcast ephemeris seen during the session
  track CSV   the real-time navigation solution at 1 Hz, for a quick look

Time. The Certus stamps everything with UTC ("Unix time"). GPS time is UTC plus
the leap-second offset (18 s since 2017). Data recorded before the Certus knew
the time (stamped 2013) is dropped and reported.

Frames. Certus raw sensor data is in its aligned vehicle frame: X forward,
Y right, Z down. Qinertia uses the same convention. Inertial Explorer uses
X right, Y forward, Z up, so for the IMR file the axes are swapped
(x, y, z) -> (y, x, -z), a pure rotation, and no body-to-IMU rotation needs
entering in Inertial Explorer.
"""
from __future__ import annotations

import calendar
import math
import os
import struct
import time

from dcs_core import anpp
from dcs_core import clraw as dclraw

from . import software

GPS_EPOCH_UNIX = 315964800
WEEK = 604800
MIN_VALID_UNIX = 1577836800        # 2020-01-01

SYS_LETTER = {1: "G", 2: "R", 3: "C", 4: "E", 5: "S", 6: "J", 10: "I"}
SYS_ORDER = "GRECJSI"
# (system id, ANPP frequency value) -> RINEX 3 band + attribute
SIGNALS = {
    (1, 1): "1C", (1, 2): "1L", (1, 3): "1P", (1, 4): "1M", (1, 5): "2L",
    (1, 6): "2P", (1, 7): "2M", (1, 8): "5Q",
    (2, 1): "1C", (2, 3): "1P", (2, 5): "2C", (2, 6): "2P", (2, 8): "3Q",
    (4, 1): "1C", (4, 2): "1A", (4, 5): "6C", (4, 6): "6A", (4, 8): "5Q",
    (4, 9): "7Q", (4, 10): "8Q",
    (3, 1): "2I", (3, 5): "7I", (3, 8): "6I",
    (5, 1): "1C", (5, 8): "5I",
    (6, 1): "1C", (6, 2): "1L", (6, 3): "1Z", (6, 5): "2L", (6, 6): "6L", (6, 8): "5Q",
    (10, 8): "5A",
}


def gps_seconds(unix, leap):
    """Seconds since the GPS epoch (6 Jan 1980) for a UTC unix time."""
    return unix - GPS_EPOCH_UNIX + leap


def gps_calendar(gps_s):
    """(year, month, day, hour, minute, second_float) of a GPS-time instant."""
    whole = int(math.floor(gps_s))
    frac = gps_s - whole
    tm = time.gmtime(whole + GPS_EPOCH_UNIX)
    return tm.tm_year, tm.tm_mon, tm.tm_mday, tm.tm_hour, tm.tm_min, tm.tm_sec + frac


def geodetic_to_ecef(lat_deg, lon_deg, h):
    a, e2 = 6378137.0, 6.69437999014e-3
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    n = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    return ((n + h) * math.cos(lat) * math.cos(lon),
            (n + h) * math.cos(lat) * math.sin(lon),
            (n * (1 - e2) + h) * math.sin(lat))


# --------------------------------------------------------------------------
# IMU writers
# --------------------------------------------------------------------------
IMR_HEADER = struct.Struct("<8sbdiidddiid32s4s32s12sbiii354s")
IMR_RECORD = struct.Struct("<diiiiii")
assert IMR_HEADER.size == 512 and IMR_RECORD.size == 32
IMR_GYRO_SCALE = 1e-6      # int32 counts -> degrees per second
IMR_ACCEL_SCALE = 1e-6     # int32 counts -> metres per second squared


def imr_header(rate_hz, lever_arm_ie_mm=None, imu_name="Certus Evo OEM"):
    lv = lever_arm_ie_mm
    return IMR_HEADER.pack(
        b"$IMURAW\x00", 0, 8.90,
        0,                      # bDeltaTheta: 0 = angular rates
        0,                      # bDeltaVelocity: 0 = accelerations
        float(rate_hz), IMR_GYRO_SCALE, IMR_ACCEL_SCALE,
        2,                      # time tags are GPS seconds of week
        2,                      # corrected for receiver clock bias
        0.0,
        imu_name.encode()[:31].ljust(32, b"\x00"), b"\x00" * 4,
        software().encode()[:31].ljust(32, b"\x00"),
        b"\x00" * 12,
        1 if lv else 0,
        int(round(lv[0])) if lv else 0, int(round(lv[1])) if lv else 0,
        int(round(lv[2])) if lv else 0,
        b"\x00" * 354)


def _clamp32(v):
    return -2147483648 if v < -2147483648 else (2147483647 if v > 2147483647 else v)


class ImuWriter:
    """Writes the IMR and QIMU files as samples arrive."""

    def __init__(self, imr_path, qimu_path, leap, lever_arm_vehicle_m=None):
        self.leap = leap
        self.lever = lever_arm_vehicle_m
        self.imr = open(imr_path, "wb") if imr_path else None
        self.qimu = open(qimu_path, "w") if qimu_path else None
        self.imr_path = imr_path
        if self.imr:
            self.imr.write(imr_header(200.0, self._lever_ie_mm()))
        if self.qimu:
            self.qimu.write("$qimu\n$version:2\n$timeSource:gps\n"
                            "$deltaVelScaleFactor:1.0\n$deltaAngleScaleFactor:1.0\n")
        self.week = None
        self.n = 0
        self.first = None
        self.last = None
        self.dropped_no_time = 0
        self.dropped_backwards = 0
        self.gaps = 0
        self.longest_gap = 0.0
        self._dts = {}

    def _lever_ie_mm(self):
        if not self.lever:
            return None
        x, y, z = self.lever
        return (y * 1000.0, x * 1000.0, -z * 1000.0)

    def add(self, unix_s, micros, s):
        """unix_s, micros: UTC time of the sample, or unix_s None when the
        Certus did not yet know the time.
        s: raw sensors tuple (ax, ay, az, gx, gy, gz, ..., temp at index 9)."""
        if unix_s is None:
            self.dropped_no_time += 1
            return
        gi = unix_s - GPS_EPOCH_UNIX + self.leap
        g = gi + micros * 1e-6
        if self.last is not None:
            dt = g - self.last
            if dt <= 0:
                self.dropped_backwards += 1
                return
            key = round(dt, 4)
            self._dts[key] = self._dts.get(key, 0) + 1
            if dt > 0.0125:
                self.gaps += 1
                if dt > self.longest_gap:
                    self.longest_gap = dt
        else:
            self.first = g
            self.week = gi // WEEK
        self.last = g
        self.n += 1
        ax, ay, az, gx, gy, gz = s[0], s[1], s[2], s[3], s[4], s[5]
        if self.imr:
            sow = (gi - self.week * WEEK) + micros * 1e-6
            k = 180.0 / math.pi / IMR_GYRO_SCALE
            self.imr.write(IMR_RECORD.pack(
                sow,
                _clamp32(int(round(gy * k))), _clamp32(int(round(gx * k))),
                _clamp32(int(round(-gz * k))),
                _clamp32(int(round(ay / IMR_ACCEL_SCALE))),
                _clamp32(int(round(ax / IMR_ACCEL_SCALE))),
                _clamp32(int(round(-az / IMR_ACCEL_SCALE)))))
        if self.qimu:
            self.qimu.write("%d.%06d;%.7f;%.7f;%.7f;%.9f;%.9f;%.9f;%.2f;3\n"
                            % (gi, micros, ax, ay, az, gx, gy, gz, s[9]))

    def close(self):
        rate = 200.0
        if self._dts:
            dt = max(self._dts.items(), key=lambda kv: kv[1])[0]
            if dt > 0:
                rate = round(1.0 / dt, 3)
        if self.imr:
            self.imr.seek(0)
            self.imr.write(imr_header(rate, self._lever_ie_mm()))
            self.imr.close()
        if self.qimu:
            self.qimu.close()
        return {
            "samples": self.n, "rate_hz": rate,
            "gps_week": self.week,
            "first_gps_seconds": self.first, "last_gps_seconds": self.last,
            "duration_s": round(self.last - self.first, 3) if self.n else 0,
            "dropped_before_time_valid": self.dropped_no_time,
            "dropped_out_of_order": self.dropped_backwards,
            "gaps_over_2_samples": self.gaps, "longest_gap_s": round(self.longest_gap, 4),
        }


# --------------------------------------------------------------------------
# RINEX
# --------------------------------------------------------------------------
def _hl(content, label):
    return "%-60s%-20s\n" % (content[:60], label)


def _sat_id(sysid, prn):
    letter = SYS_LETTER.get(sysid)
    if not letter:
        return None
    if letter == "J" and prn >= 193:
        prn -= 192
    elif letter == "S" and prn >= 100:
        prn -= 100
    if not 0 < prn < 100:
        return None
    return "%s%02d" % (letter, prn)


class RinexObsWriter:
    """RINEX 3.04 observation file for one receiver. The body is written to a
    temporary file first because the header needs the list of signals."""

    def __init__(self, path, leap, receiver, marker="MAPPER", rec_info=None):
        self.path = path
        self.leap = leap
        self.receiver = receiver
        self.marker = marker
        self.rec_info = rec_info or {}
        self.tmp = open(path + ".body", "w")
        self.codes = {}                 # letter -> ordered list of band+attr
        self.epochs = 0
        self.first = None
        self.last = None
        self.approx = None
        self.glonass_slots = {}
        self._dts = {}
        self._cur_key = None
        self._cur = {}
        self.sat_sum = 0
        self.unknown_signals = 0

    def add_packet(self, ut, ns, sats):
        """Add the satellites of one packet-60 fragment."""
        key = (ut, ns)
        if key != self._cur_key:
            self._flush()
            self._cur_key = key
        cur = self._cur
        for sysid, prn, _el, _az, freqs in sats:
            sid = _sat_id(sysid, prn)
            if not sid:
                continue
            d = cur.setdefault(sid, {})
            for freq, track, cp, pr, dop, snr in freqs:
                code = SIGNALS.get((sysid, freq))
                if not code:
                    self.unknown_signals += 1
                    continue
                d[code] = (track, cp, pr, dop, snr)
                lst = self.codes.setdefault(sid[0], [])
                if code not in lst:
                    lst.append(code)

    def _flush(self):
        # A satellite whose signals are all of kinds this writer does not map
        # has nothing to print: leave it out rather than write an empty line.
        self._cur = {sid: obs for sid, obs in self._cur.items() if obs}
        if self._cur_key is None or not self._cur:
            self._cur = {}
            return
        ut, ns = self._cur_key
        if ut < MIN_VALID_UNIX:
            self._cur = {}
            return
        # Keep whole seconds and nanoseconds apart: a float cannot hold both
        # to the precision RINEX prints.
        g = (ut - GPS_EPOCH_UNIX + self.leap, ns)
        if self.last is not None:
            if g <= self.last:
                self._cur = {}
                return
            key = round((g[0] - self.last[0]) + (g[1] - self.last[1]) * 1e-9, 3)
            self._dts[key] = self._dts.get(key, 0) + 1
        else:
            self.first = g
        self.last = g
        self.epochs += 1
        y, mo, d, h, mi, s = gps_calendar(g[0])
        s += g[1] * 1e-9
        sats = sorted(self._cur.items(), key=lambda kv: (SYS_ORDER.index(kv[0][0]), kv[0]))
        self.sat_sum += len(sats)
        w = self.tmp.write
        w("> %04d %02d %02d %02d %02d%11.7f  0%3d\n" % (y, mo, d, h, mi, s, len(sats)))
        for sid, obs in sats:
            # Fields are written keyed by code; the final layout is fixed when
            # the header is known, so use a tagged form here.
            parts = [sid]
            for code, (track, cp, pr, dop, snr) in obs.items():
                parts.append("%s,%d,%.3f,%.3f,%.3f,%.1f" % (code, track, cp, pr, dop, snr))
            w(" ".join(parts) + "\n")
        self._cur = {}

    def set_approx(self, lat, lon, h):
        if self.approx is None:
            self.approx = geodetic_to_ecef(lat, lon, h)

    def close(self):
        self._flush()
        self.tmp.close()
        interval = 0.0
        if self._dts:
            interval = max(self._dts.items(), key=lambda kv: kv[1])[0]
        systems = [s for s in SYS_ORDER if s in self.codes]
        for s in systems:
            self.codes[s].sort()
        with open(self.path, "w") as out:
            out.write(_hl("%9.2f%-11s%-20s%-20s" % (3.04, "", "OBSERVATION DATA", "M"),
                          "RINEX VERSION / TYPE"))
            out.write(_hl("%-20s%-20s%-20s" % (software(), "",
                                               time.strftime("%Y%m%d %H%M%S UTC", time.gmtime())),
                          "PGM / RUN BY / DATE"))
            out.write(_hl("Converted from Advanced Navigation ANPP packet 60", "COMMENT"))
            out.write(_hl("Receiver %d (0 = primary antenna, 1 = secondary)" % self.receiver,
                          "COMMENT"))
            out.write(_hl(self.marker, "MARKER NAME"))
            out.write(_hl("GROUND_CRAFT", "MARKER TYPE"))
            out.write(_hl("%-20s%-40s" % ("", ""), "OBSERVER / AGENCY"))
            out.write(_hl("%-20s%-20s%-20s" % (str(self.rec_info.get("serial", ""))[:20],
                                               str(self.rec_info.get("type", "ADVNAV CERTUS"))[:20],
                                               str(self.rec_info.get("version", ""))[:20]),
                          "REC # / TYPE / VERS"))
            out.write(_hl("%-20s%-20s" % ("", ""), "ANT # / TYPE"))
            x, y, z = self.approx or (0.0, 0.0, 0.0)
            out.write(_hl("%14.4f%14.4f%14.4f" % (x, y, z), "APPROX POSITION XYZ"))
            out.write(_hl("%14.4f%14.4f%14.4f" % (0, 0, 0), "ANTENNA: DELTA H/E/N"))
            for s in systems:
                types = []
                for code in self.codes[s]:
                    types += ["C" + code, "L" + code, "D" + code, "S" + code]
                for i in range(0, len(types), 13):
                    chunk = types[i:i + 13]
                    lead = "%s  %3d" % (s, len(types)) if i == 0 else "      "
                    out.write(_hl(lead + "".join(" " + t for t in chunk), "SYS / # / OBS TYPES"))
            if interval:
                out.write(_hl("%10.3f" % interval, "INTERVAL"))
            for g, label in ((self.first, "TIME OF FIRST OBS"), (self.last, "TIME OF LAST OBS")):
                if g is not None:
                    yy, mo, d, h, mi, sec = gps_calendar(g[0])
                    sec += g[1] * 1e-9
                    out.write(_hl("%6d%6d%6d%6d%6d%13.7f     GPS" % (yy, mo, d, h, mi, sec), label))
            if self.glonass_slots:
                items = sorted(self.glonass_slots.items())
                for i in range(0, len(items), 8):
                    lead = "%3d " % len(items) if i == 0 else "    "
                    out.write(_hl(lead + "".join("R%02d %2d " % kv for kv in items[i:i + 8]),
                                  "GLONASS SLOT / FRQ #"))
            out.write(_hl("%6d" % self.leap, "LEAP SECONDS"))
            out.write(_hl("", "END OF HEADER"))
            with open(self.path + ".body") as body:
                for line in body:
                    if line.startswith(">"):
                        out.write(line)
                        continue
                    parts = line.split()
                    sid = parts[0]
                    obs = {}
                    for p in parts[1:]:
                        code, track, cp, pr, dop, snr = p.split(",")
                        obs[code] = (int(track), float(cp), float(pr), float(dop), float(snr))
                    fields = [sid]
                    for code in self.codes.get(sid[0], []):
                        o = obs.get(code)
                        if o is None:
                            fields.append(" " * 64)
                            continue
                        track, cp, pr, dop, snr = o
                        ssi = ""
                        if track & 0x20 and snr > 0:
                            ssi = "%d" % min(max(int(snr / 6.0), 1), 9)
                        c = "%14.3f%1s%1s" % (pr, "", ssi) if track & 0x08 and pr else " " * 16
                        if track & 0x01 and cp:
                            lli = (1 if track & 0x02 else 0) | (2 if track & 0x04 else 0)
                            l = "%14.3f%1s%1s" % (cp, lli if lli else "", ssi)
                        else:
                            l = " " * 16
                        dd = "%14.3f  " % dop if track & 0x10 else " " * 16
                        sn = "%14.3f  " % snr if track & 0x20 else " " * 16
                        fields.append(c + l + dd + sn)
                    out.write("".join(fields).rstrip() + "\n")
        os.unlink(self.path + ".body")
        return {
            "receiver": self.receiver, "epochs": self.epochs,
            "interval_s": interval,
            "first_gps_seconds": (self.first[0] + self.first[1] * 1e-9) if self.first else None,
            "last_gps_seconds": (self.last[0] + self.last[1] * 1e-9) if self.last else None,
            "mean_satellites": round(self.sat_sum / self.epochs, 1) if self.epochs else 0,
            "signals": {s: self.codes[s] for s in systems},
            "unmapped_signal_records": self.unknown_signals,
        }


def _e(v):
    """RINEX navigation number, 19 characters."""
    return "%19.12E" % v


class RinexNavWriter:
    """RINEX 3.04 navigation file, GPS only (what packet 61 provides)."""

    def __init__(self, path, leap):
        self.path = path
        self.leap = leap
        self.eph = {}
        self.other = 0

    def add(self, payload):
        if len(payload) < 132 or payload[4] != 1:
            self.other += 1
            return
        e = anpp.gps_ephemeris(payload)
        if not 1 <= e["prn"] <= 32 or e["sqrt_a"] < 4000 or e["sqrt_a"] > 6000:
            self.other += 1
            return
        week = e["week"]
        if week < 1500:           # a truncated (10-bit) week number
            ref = gps_seconds(e["unix"], self.leap) // WEEK
            week = int(week + 1024 * round((ref - week) / 1024.0))
        e["full_week"] = week
        self.eph[(e["prn"], e["iode"], e["toe"], week)] = e

    def close(self):
        with open(self.path, "w") as out:
            out.write(_hl("%9.2f%-11s%-20s%-20s" % (3.04, "", "N: GNSS NAV DATA", "G: GPS"),
                          "RINEX VERSION / TYPE"))
            out.write(_hl("%-20s%-20s%-20s" % (software(), "",
                                               time.strftime("%Y%m%d %H%M%S UTC", time.gmtime())),
                          "PGM / RUN BY / DATE"))
            out.write(_hl("GPS ephemeris from Advanced Navigation ANPP packet 61", "COMMENT"))
            out.write(_hl("%6d" % self.leap, "LEAP SECONDS"))
            out.write(_hl("", "END OF HEADER"))
            for key in sorted(self.eph, key=lambda k: (k[3], k[2], k[0])):
                e = self.eph[key]
                toc = e["full_week"] * WEEK + e["toe"]
                tm = time.gmtime(toc + GPS_EPOCH_UNIX)
                fl = e["flags"]
                out.write("G%02d %04d %02d %02d %02d %02d %02d%s%s%s\n" % (
                    e["prn"], tm.tm_year, tm.tm_mon, tm.tm_mday, tm.tm_hour, tm.tm_min,
                    tm.tm_sec, _e(e["af0"]), _e(e["af1"]), _e(e["af2"])))
                rows = [
                    (e["iode"], e["crs"], e["delta_n"], e["m0"]),
                    (e["cuc"], e["e"], e["cus"], e["sqrt_a"]),
                    (e["toe"], e["cic"], e["omega0"], e["cis"]),
                    (e["i0"], e["crc"], e["omega"], e["omega_dot"]),
                    (e["idot"], (fl >> 1) & 3, e["full_week"], fl & 1),
                    (e["ura"], (fl >> 4) & 0x3F, e["tgd"], e["iodc"]),
                    (e["tx_time"], 6.0 if fl & 0x400 else 4.0),
                ]
                for row in rows:
                    out.write("    " + "".join(_e(float(v)) for v in row) + "\n")
        return {"gps_ephemerides": len(self.eph), "other_ephemeris_packets": self.other,
                "satellites": sorted({k[0] for k in self.eph})}


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------
class _JoinedFiles:
    """Several files read as if they were one."""

    def __init__(self, paths):
        self.paths = list(paths)
        self.i = 0
        self.f = None
        self.pos = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.f:
            self.f.close()
            self.f = None

    def tell(self):
        return self.pos

    def read(self, n):
        while True:
            if self.f is None:
                if self.i >= len(self.paths):
                    return b""
                self.f = open(self.paths[self.i], "rb")
                self.i += 1
            data = self.f.read(n)
            if data:
                self.pos += len(data)
                return data
            self.f.close()
            self.f = None


class Cancelled(Exception):
    """The user pressed Cancel."""


class ClrawAnppStream:
    """The /ins/anpp packets of one recording's .clraw files, read as one ANPP byte stream (each message is a
    complete packet, header included). tell() is the position in the .clraw files, for progress. Damaged
    chunks are skipped and counted in damaged_chunks."""

    def __init__(self, paths):
        self.paths = sorted(paths, key=os.path.basename)
        self.files = [dclraw.ClrawFile(p) for p in self.paths]
        self.total = sum(f.size for f in self.files) or 1
        self._it = self._gen()
        self._buf = bytearray()
        self.pos = 0
        self._done = 0

    def _gen(self):
        for cf in self.files:
            for chunk in cf.iter_chunks({dclraw.CH_ANPP}, progress=lambda f, cf=cf: self._at(cf, f)):
                yield b"".join(p.tobytes() if isinstance(p, memoryview) else bytes(p) for _c, _l, _t, p in chunk)
            self._done += cf.size
            self.pos = self._done

    def _at(self, cf, frac):
        self.pos = self._done + int(frac * cf.size)

    @property
    def damaged_chunks(self):
        return sum(f.bad_chunks for f in self.files)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def tell(self):
        return self.pos

    def read(self, n):
        while len(self._buf) < n:
            try:
                self._buf += next(self._it)
            except StopIteration:
                break
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out


def convert(anpp_paths, out_dir, base_name, leap=18, lever_arm_vehicle_m=None,
            want_imr=True, want_qimu=True, want_rinex=True, want_track=True,
            progress=None, ephemeris_paths=(), clraw_paths=None, cancelled=None):
    """Read the given .anpp files in order (or, with clraw_paths, the /ins/anpp channel of those .clraw
    files) and write the requested outputs into out_dir. Returns a summary dict (also listing the files
    written). cancelled() returning True stops it with Cancelled (the caller removes out_dir)."""
    os.makedirs(out_dir, exist_ok=True)
    files = {}
    imr_path = os.path.join(out_dir, base_name + ".imr") if want_imr else None
    qimu_path = os.path.join(out_dir, base_name + "_imu.qimu") if want_qimu else None
    imu = ImuWriter(imr_path, qimu_path, leap, lever_arm_vehicle_m) if (imr_path or qimu_path) else None
    obs = {}
    nav = RinexNavWriter(os.path.join(out_dir, base_name + ".nav"), leap) if want_rinex else None
    track = None
    if want_track:
        track_path = os.path.join(out_dir, base_name + "_realtime_track.csv")
        track = open(track_path, "w")
        track.write("utc_iso,gps_week,gps_sow,latitude_wgs84_deg,longitude_wgs84_deg,ellipsoidal_height_wgs84_m,"
                    "roll_deg,pitch_deg,heading_deg,speed_mps,fix,sd_north_m,sd_east_m,sd_height_m\n")
    if clraw_paths:
        source = ClrawAnppStream(clraw_paths)
        total = source.total
        anpp_paths = list(source.paths)
    else:
        source = _JoinedFiles(anpp_paths)
        total = sum(os.path.getsize(p) for p in anpp_paths) or 1
    done = 0
    pend = None
    pend_time = None
    counts = {}
    last_track_second = None
    first_valid = None
    last_valid = None
    invalid_states = 0
    device = None
    glonass_slots = {}
    track_rows = 0
    S20, S28, S60, S61 = anpp.SYSTEM_STATE, anpp.RAW_SENSORS, anpp.RAW_SAT_DATA, anpp.RAW_SAT_EPHEMERIS
    if nav:
        for path in ephemeris_paths:          # ephemeris held before recording began
            try:
                with open(path, "rb") as f:
                    for pid, b in anpp.iter_packets(f):
                        if pid == S61:
                            if len(b) >= 94 and b[4] == 2:
                                glonass_slots[b[5]] = struct.unpack_from("<b", b, 91)[0]
                            nav.add(b)
            except OSError:
                pass
    # The files of one capture are read as ONE stream: when the recorder has to
    # move to a new file in the middle of a packet (storage error), the two
    # halves join up again here and nothing is lost.
    try:
        for joined in (source,):
            with joined as f:
                n_since = 0
                for pid, b in anpp.iter_packets(f):
                    counts[pid] = counts.get(pid, 0) + 1
                    if pid == S20:
                        if len(b) < 100:
                            continue
                        ut, us, fs = anpp.state_time(b)
                        if fs & 0x08 and ut > MIN_VALID_UNIX:
                            pend = (ut, us)
                            pend_time = ut + us * 1e-6
                            if first_valid is None:
                                first_valid = pend_time
                            last_valid = pend_time
                            if ut != last_track_second:
                                last_track_second = ut
                                st = anpp.system_state(b)
                                if obs and (st["fix"] >= 2):
                                    for w in obs.values():
                                        w.set_approx(st["lat"], st["lon"], st["height"])
                                if track:
                                    g = gps_seconds(pend_time, leap)
                                    track.write("%s.%03dZ,%d,%.3f,%.9f,%.9f,%.3f,%.3f,%.3f,%.3f,%.3f,%d,%.3f,%.3f,%.3f\n" % (
                                        time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ut)), us // 1000,
                                        g // WEEK, g % WEEK, st["lat"], st["lon"], st["height"],
                                        st["roll"], st["pitch"], st["heading"], st["speed"], st["fix"],
                                        st["sd_lat"], st["sd_lon"], st["sd_height"]))
                                    track_rows += 1
                        else:
                            pend = None
                            invalid_states += 1
                    elif pid == S28:
                        if imu and len(b) >= 48:
                            if pend:
                                imu.add(pend[0], pend[1], anpp.raw_sensors(b))
                            else:
                                imu.add(None, 0, None)
                            # one IMU sample per state packet: a second packet 28
                            # without a new time stamp must not reuse the old one
                            pend = None
                    elif pid == S60:
                        if want_rinex and len(b) >= 16:
                            ut, ns, _clk, rx, _pn, _tot, sats = anpp.raw_sat_data(b)
                            w = obs.get(rx)
                            if w is None:
                                suffix = "" if rx == 0 else "_rx%d" % (rx + 1)
                                p = os.path.join(out_dir, "%s%s.obs" % (base_name, suffix))
                                w = obs[rx] = RinexObsWriter(p, leap, rx, marker=base_name[:60])
                            w.add_packet(ut, ns, sats)
                    elif pid == S61:
                        if nav:
                            if len(b) >= 94 and b[4] == 2:
                                glonass_slots[b[5]] = struct.unpack_from("<b", b, 91)[0]
                            nav.add(b)
                    elif pid == anpp.DEVICE_INFO and device is None:
                        device = anpp.device_info(b)
                    n_since += 1
                    if n_since >= 50000:
                        n_since = 0
                        if cancelled is not None and cancelled():
                            raise Cancelled()
                        if progress:
                            progress(min(0.99, (done + f.tell()) / total))
    except BaseException:
        # cancelled or failed: close every file so the work folder can be removed (Windows keeps open files)
        for fh in ([imu.imr, imu.qimu] if imu else []) + [w.tmp for w in obs.values()] + [track]:
            try:
                if fh is not None:
                    fh.close()
            except Exception:       # noqa: BLE001
                pass
        raise
    summary = {
        "software": software(),
        "source_files": [os.path.basename(p) for p in anpp_paths],
        "source_kind": "clraw" if clraw_paths else "anpp",
        "source_bytes": total, "leap_seconds": leap,
        "packet_counts": {str(k): v for k, v in sorted(counts.items())},
        "device": device,
        "first_valid_utc": _iso(first_valid), "last_valid_utc": _iso(last_valid),
        "valid_duration_s": round(last_valid - first_valid, 3) if first_valid else 0,
        "state_packets_before_time_valid": invalid_states,
        "lever_arm_vehicle_m": lever_arm_vehicle_m,
    }
    if clraw_paths:
        summary["damaged_chunks"] = source.damaged_chunks
    if imu:
        summary["imu"] = imu.close()
        if imr_path:
            files["imr"] = os.path.basename(imr_path)
        if qimu_path:
            files["qimu"] = os.path.basename(qimu_path)
    if want_rinex:
        summary["rinex_obs"] = []
        for rx in sorted(obs):
            w = obs[rx]
            w.glonass_slots = glonass_slots
            if device:
                w.rec_info = {"serial": device["serial"][:20], "type": "ADVNAV CERTUS EVO",
                              "version": "%.3f" % (device["software_version"] / 1000.0)}
            summary["rinex_obs"].append(w.close())
            files["obs" if rx == 0 else "obs_rx%d" % (rx + 1)] = os.path.basename(w.path)
        summary["rinex_nav"] = nav.close()
        files["nav"] = os.path.basename(nav.path)
    if track:
        track.close()
        summary["track_rows"] = track_rows
        files["track"] = os.path.basename(track_path)
    summary["files"] = files
    if progress:
        progress(1.0)
    return summary


def _iso(t):
    if t is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".%03dZ" % int((t % 1) * 1000)
