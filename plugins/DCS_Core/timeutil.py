"""GPS / UTC time used by every DCS stage (one leap-second table). No Qt.

GPS seconds = seconds since 1980-01-06 00:00 GPS time. SBET time = GPS
seconds of the week. UTC = GPS - leap seconds (18 since 2017-01-01)."""
import datetime

GPS_EPOCH = datetime.datetime(1980, 1, 6)
WEEK = 604800.0
# GPS - UTC leap seconds (date from which they apply)
LEAPS = [(datetime.datetime(2017, 1, 1), 18), (datetime.datetime(2015, 7, 1), 17),
         (datetime.datetime(2012, 7, 1), 16), (datetime.datetime(2009, 1, 1), 15),
         (datetime.datetime(2006, 1, 1), 14), (datetime.datetime(1999, 1, 1), 13)]


def leap_seconds(utc):
    for d, n in LEAPS:
        if utc >= d:
            return n
    return 13


def gps_to_utc(gps_seconds):
    t = GPS_EPOCH + datetime.timedelta(seconds=gps_seconds)
    return t - datetime.timedelta(seconds=leap_seconds(t))


def utc_to_gps(utc):
    return (utc - GPS_EPOCH).total_seconds() + leap_seconds(utc)


def week_sow(gps_seconds):
    w = int(gps_seconds // WEEK)
    return w, gps_seconds - w * WEEK


def from_week_sow(week, sow):
    return week * WEEK + sow
