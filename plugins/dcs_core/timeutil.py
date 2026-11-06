"""GPS / UTC time used by every DCS stage (one leap-second table). No Qt.

GPS seconds = seconds since 1980-01-06 00:00 GPS time. SBET time = GPS
seconds of the week. UTC = GPS - leap seconds (18 since 2017-01-01).
LEAP is the current value; sbet and las take it from here (one source)."""
import datetime

GPS_EPOCH = datetime.datetime(1980, 1, 6)
WEEK = 604800.0
# GPS - UTC leap seconds (UTC date from which they apply), newest first
LEAPS = [(datetime.datetime(2017, 1, 1), 18), (datetime.datetime(2015, 7, 1), 17),
         (datetime.datetime(2012, 7, 1), 16), (datetime.datetime(2009, 1, 1), 15),
         (datetime.datetime(2006, 1, 1), 14), (datetime.datetime(1999, 1, 1), 13),
         (datetime.datetime(1997, 7, 1), 12), (datetime.datetime(1996, 1, 1), 11),
         (datetime.datetime(1994, 7, 1), 10), (datetime.datetime(1993, 7, 1), 9),
         (datetime.datetime(1992, 7, 1), 8), (datetime.datetime(1991, 1, 1), 7),
         (datetime.datetime(1990, 1, 1), 6), (datetime.datetime(1988, 1, 1), 5),
         (datetime.datetime(1985, 7, 1), 4), (datetime.datetime(1983, 7, 1), 3),
         (datetime.datetime(1982, 7, 1), 2), (datetime.datetime(1981, 7, 1), 1)]
LEAP = LEAPS[0][1]


def leap_seconds(utc):
    """GPS - UTC at a UTC instant (naive datetime)."""
    for d, n in LEAPS:
        if utc >= d:
            return n
    return 0


def gps_to_utc(gps_seconds):
    """GPS seconds since the GPS epoch -> naive UTC datetime (the leap count is looked up at the UTC instant)."""
    t = GPS_EPOCH + datetime.timedelta(seconds=gps_seconds)
    for d, n in LEAPS:
        if t - datetime.timedelta(seconds=n) >= d:
            return t - datetime.timedelta(seconds=n)
    return t


def utc_to_gps(utc):
    return (utc - GPS_EPOCH).total_seconds() + leap_seconds(utc)


def week_sow(gps_seconds):
    w = int(gps_seconds // WEEK)
    return w, gps_seconds - w * WEEK


def from_week_sow(week, sow):
    return week * WEEK + sow
