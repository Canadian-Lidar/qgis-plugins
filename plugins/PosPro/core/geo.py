"""Geodesy, time and datum helpers (pure Python, no numpy needed)."""
import calendar
import datetime
import math

A = 6378137.0
F_GRS80 = 1 / 298.257222101
F_WGS84 = 1 / 298.257223563
GPS_EPOCH = datetime.datetime(1980, 1, 6)
GPS_EPOCH_UNIX = 315964800

# GPS - UTC leap seconds (date from which they apply)
_LEAPS = [(datetime.datetime(2017, 1, 1), 18), (datetime.datetime(2015, 7, 1), 17),
          (datetime.datetime(2012, 7, 1), 16), (datetime.datetime(2009, 1, 1), 15),
          (datetime.datetime(2006, 1, 1), 14), (datetime.datetime(1999, 1, 1), 13)]


def leap_seconds(utc):
    for d, n in _LEAPS:
        if utc >= d:
            return n
    return 13


def gps_to_utc(gps_seconds):
    t = GPS_EPOCH + datetime.timedelta(seconds=gps_seconds)
    # the leap-second table is in UTC: look it up at the UTC time (iterated once), not the GPS time
    u = t - datetime.timedelta(seconds=leap_seconds(t))
    return t - datetime.timedelta(seconds=leap_seconds(u))


def utc_to_gps(utc):
    return (utc - GPS_EPOCH).total_seconds() + leap_seconds(utc)


def gps_week_sow(gps_seconds):
    w = int(gps_seconds // 604800)
    return w, gps_seconds - w * 604800


def decimal_year(utc):
    y = utc.year
    start = datetime.datetime(y, 1, 1)
    end = datetime.datetime(y + 1, 1, 1)
    return y + (utc - start).total_seconds() / (end - start).total_seconds()


def doy(utc):
    return utc.timetuple().tm_yday


def llh2ecef(lat_deg, lon_deg, h, f=F_GRS80):
    e2 = f * (2 - f)
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    n = A / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    return ((n + h) * math.cos(lat) * math.cos(lon), (n + h) * math.cos(lat) * math.sin(lon),
            (n * (1 - e2) + h) * math.sin(lat))


def ecef2llh(x, y, z, f=F_GRS80):
    e2 = f * (2 - f)
    p = math.hypot(x, y)
    lon = math.atan2(y, x)
    lat = math.atan2(z, p * (1 - e2))
    h = 0.0
    for _ in range(10):
        n = A / math.sqrt(1 - e2 * math.sin(lat) ** 2)
        h = p / math.cos(lat) - n if abs(lat) < math.radians(89) else z / math.sin(lat) - n * (1 - e2)
        lat = math.atan2(z, p * (1 - e2 * n / (n + h)))
    return math.degrees(lat), math.degrees(lon), h


def distance_m(a, b):
    return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(3)))


# ---------------------------------------------------------------------------
# ITRF -> NAD83(CSRS), 14-parameter Helmert, position-vector convention,
# reference epoch 2010.0 (EPSG:10415 for ITRF2020 -> NAD83(CSRS)v8,
# EPSG:8265 for ITRF2014 -> NAD83(CSRS)v7). Rotations in mas, scale in ppb.
# ---------------------------------------------------------------------------
HELMERT = {
    "ITRF2020": dict(t=(1.0039, -1.90961, -0.54117), r=(-26.78138, 0.42027, -10.93206), s=-0.05109,
                     dt=(0.00079, -0.00070, -0.00124), dr=(-0.06667, 0.75744, 0.05133), ds=-0.07201),
    "ITRF2014": dict(t=(1.0053, -1.90921, -0.54157), r=(-26.78138, 0.42027, -10.93206), s=0.36891,
                     dt=(0.00079, -0.00060, -0.00144), dr=(-0.06667, 0.75744, 0.05133), ds=-0.07201),
}
MAS = math.pi / (180 * 3600 * 1000)


def itrf_to_nad83csrs(xyz, epoch, itrf="ITRF2020"):
    """Transform ITRF coordinates at `epoch` (decimal year) to NAD83(CSRS) at
    the same epoch. Velocities are NOT applied: the result is NAD83(CSRS) at
    the observation epoch."""
    p = HELMERT[itrf]
    dt = epoch - 2010.0
    tx, ty, tz = [p["t"][i] + p["dt"][i] * dt for i in range(3)]
    rx, ry, rz = [(p["r"][i] + p["dr"][i] * dt) * MAS for i in range(3)]
    s = (p["s"] + p["ds"] * dt) * 1e-9
    x, y, z = xyz
    # position vector convention
    return (tx + (1 + s) * x - rz * y + ry * z,
            ty + rz * x + (1 + s) * y - rx * z,
            tz - ry * x + rx * y + (1 + s) * z)


def fmt_duration(s):
    s = int(round(s))
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    if h:
        return "%d h %02d min" % (h, m)
    if m:
        return "%d min %02d s" % (m, sec)
    return "%d s" % sec


def unix_from_utc(t):
    return calendar.timegm(t.timetuple()) + t.microsecond * 1e-6


def destination(lat_deg, lon_deg, bearing_deg, dist_m, R=6371008.8):
    """Point reached from (lat, lon) along a bearing on a sphere (degrees)."""
    la, lo, b = math.radians(lat_deg), math.radians(lon_deg), math.radians(bearing_deg)
    d = dist_m / R
    la2 = math.asin(math.sin(la) * math.cos(d) + math.cos(la) * math.sin(d) * math.cos(b))
    lo2 = lo + math.atan2(math.sin(b) * math.sin(d) * math.cos(la), math.cos(d) - math.sin(la) * math.sin(la2))
    return math.degrees(la2), (math.degrees(lo2) + 540) % 360 - 180


def great_circle_km(lat1, lon1, lat2, lon2, R=6371.0088):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))
