"""Downloads with a shared cache: broadcast ephemeris, precise orbits and
clocks, antenna calibrations, and base-station RINEX from NOAA CORS and IGS.

All network access goes through QGIS (proxy and authentication settings
apply) when QGIS is available, otherwise through urllib.
"""
import datetime
import gzip
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import zlib

from . import geo
from .engine import PLUGIN_DIR

BKG = "https://igs.bkg.bund.de/root_ftp/IGS"
IGN = "https://igs.ign.fr/pub/igs"
IGS_FILES = "https://files.igs.org/pub/station/general"
ESA = "http://navigation-office.esa.int/products/gnss-products"
AIUB = "https://www.aiub.unibe.ch/download/CODE"
NOAA = "https://geodesy.noaa.gov"
NOAA_S3 = "https://noaa-cors-pds.s3.amazonaws.com"



class DownloadError(Exception):
    pass


def cache_dir():
    try:
        from qgis.core import QgsApplication
        root = QgsApplication.qgisSettingsDirPath()
    except Exception:          # noqa: BLE001 - outside QGIS
        root = os.path.expanduser("~")
    d = os.path.join(root, "pospro_cache")
    os.makedirs(d, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# HTTP. PosPro does not use QGIS's network manager for downloads: on a TLS
# problem QGIS opens its certificate dialog from the background job, and
# opening "Connection Certificates" there crashes QGIS 4.2 (QCA). Instead each
# request runs on a private QNetworkAccessManager with QGIS's proxy; an
# incomplete certificate chain (a server that does not send its intermediate
# certificate, as igs.bkg.bund.de) is completed the way browsers do it: the
# missing issuer is fetched from the certificate's "CA Issuers" address and
# kept in the PosPro cache. Anything still wrong fails the request (the next
# mirror is tried); certificate checks are never switched off.
# ---------------------------------------------------------------------------
class TlsError(Exception):
    def __init__(self, msg, issuers=()):
        super().__init__(msg)
        self.issuers = list(issuers)


_EXTRA_CAS = None


def _extra_cas():
    """Intermediate certificates learnt earlier (PEM file in the cache)."""
    global _EXTRA_CAS
    if _EXTRA_CAS is None:
        _EXTRA_CAS = []
        try:
            from qgis.PyQt.QtNetwork import QSslCertificate
            path = os.path.join(cache_dir(), "intermediate_cas.pem")
            if os.path.exists(path):
                with open(path, "rb") as fh:
                    _EXTRA_CAS = list(QSslCertificate.fromData(fh.read()))
        except Exception:           # noqa: BLE001
            _EXTRA_CAS = []
    return _EXTRA_CAS


def _learn_ca(der_or_pem):
    from qgis.PyQt.QtNetwork import QSsl, QSslCertificate
    certs = list(QSslCertificate.fromData(der_or_pem, QSsl.EncodingFormat.Der)) or \
        list(QSslCertificate.fromData(der_or_pem, QSsl.EncodingFormat.Pem))
    if not certs:
        return False
    cas = _extra_cas()
    known = {bytes(c.digest()) for c in cas}
    new = [c for c in certs if bytes(c.digest()) not in known]
    if not new:
        return False
    cas.extend(new)
    try:
        with open(os.path.join(cache_dir(), "intermediate_cas.pem"), "ab") as fh:
            for c in new:
                fh.write(bytes(c.toPem()))
    except OSError:
        pass
    return True


def _aia_issuers(cert):
    """'CA Issuers' URLs of a certificate's Authority Information Access."""
    out = []
    try:
        for ext in cert.extensions():
            if ext.oid() == "1.3.6.1.5.5.7.1.1" or ext.name() == "authorityInfoAccess":
                v = ext.value()
                if isinstance(v, dict):
                    for k, u in v.items():
                        if "issuer" in str(k).lower():
                            out += [str(x) for x in (u if isinstance(u, (list, tuple)) else [u])]
    except Exception:               # noqa: BLE001
        pass
    return [u for u in out if u.startswith("http")]


_HOOK = threading.local()


class progress_hook:
    """with progress_hook(cb): ... -- cb(url, received, total) is called as
    bytes arrive on this thread; returning True aborts the transfer."""

    def __init__(self, cb):
        self.cb = cb

    def __enter__(self):
        self.prev = getattr(_HOOK, "cb", None)
        _HOOK.cb = self.cb
        return self

    def __exit__(self, *a):
        _HOOK.cb = self.prev
        return False


def _qt_request(url, body=None, ctype=None, timeout=120, headers=None, sink=None):
    """sink: a file opened for writing; the body is streamed into it (not held in memory)."""
    from qgis.PyQt.QtCore import QByteArray, QEventLoop, QTimer, QUrl
    from qgis.PyQt.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest, QSslConfiguration
    nam = QNetworkAccessManager()
    try:
        from qgis.core import QgsNetworkAccessManager
        nam.setProxy(QgsNetworkAccessManager.instance().fallbackProxy())
    except Exception:               # noqa: BLE001
        pass
    req = QNetworkRequest(QUrl(url))
    req.setRawHeader(b"User-Agent", b"PosPro QGIS plugin")
    for k, v in (headers or {}).items():
        req.setRawHeader(k.encode(), v.encode())
    try:
        req.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                         QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy)
    except AttributeError:
        pass
    if url.startswith("https") and _extra_cas():
        cfg = QSslConfiguration.defaultConfiguration()
        cfg.addCaCertificates(_extra_cas())
        req.setSslConfiguration(cfg)
    if body is None:
        reply = nam.get(req)
    else:
        req.setRawHeader(b"Content-Type", (ctype or "application/octet-stream").encode())
        reply = nam.post(req, QByteArray(body))
    ssl = {"errors": [], "issuers": []}

    def on_ssl(errors):
        for e in errors:
            ssl["errors"].append(e.errorString())
            c = e.certificate()
            if c is not None and not c.isNull():
                ssl["issuers"] += _aia_issuers(c)
        try:
            for c in reply.sslConfiguration().peerCertificateChain():
                ssl["issuers"] += _aia_issuers(c)
        except Exception:           # noqa: BLE001
            pass
    reply.sslErrors.connect(on_ssl)
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(reply.abort)
    cb = getattr(_HOOK, "cb", None)

    def on_progress(rec, tot):
        timer.start(int(timeout * 1000))        # data is flowing: a stall timeout, not a total limit
        if cb is not None:
            try:
                if cb(url, int(rec), int(tot)):
                    reply.abort()
            except Exception:       # noqa: BLE001
                pass
    reply.downloadProgress.connect(on_progress)
    if sink is not None:
        def on_ready():
            sink.write(bytes(reply.readAll()))
        reply.readyRead.connect(on_ready)
    reply.finished.connect(loop.quit)
    timer.start(int(timeout * 1000))
    if not reply.isFinished():
        loop.exec()
    timer.stop()
    code = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
    err = reply.error()
    data = bytes(reply.readAll())
    if sink is not None:
        sink.write(data)
        data = None
    msg = reply.errorString()
    reply.deleteLater()
    if ssl["errors"]:
        raise TlsError("%s: %s" % (url, "; ".join(dict.fromkeys(ssl["errors"]))), dict.fromkeys(ssl["issuers"]))
    if err != QNetworkReply.NetworkError.NoError or (code is not None and int(code) >= 400):
        raise DownloadError("%s: %s" % (url, msg if err != QNetworkReply.NetworkError.NoError else "HTTP %s" % code))
    return data


def _request(url, body=None, ctype=None, timeout=120, headers=None, sink=None):
    """One retry after a short pause for transient network errors (TLS chains are completed separately)."""
    for attempt in (0, 1):
        if sink is not None:
            sink.seek(0)
            sink.truncate()
        try:
            return _request1(url, body, ctype, timeout, headers, sink)
        except DownloadError as e:
            txt = str(e).lower()
            permanent = any(k in txt for k in ("http 4", "not found", "certificate", "host not found",
                                                 "unknown host", "empty file"))
            if attempt or permanent:
                raise
            time.sleep(3)


def _request1(url, body=None, ctype=None, timeout=120, headers=None, sink=None):
    try:
        import qgis.PyQt.QtNetwork      # noqa: F401
        have_qt = True
    except ImportError:
        have_qt = False
    if have_qt:
        for attempt in range(3):
            try:
                return _qt_request(url, body, ctype, timeout, headers, sink)
            except TlsError as e:
                learnt = False
                for u in e.issuers:
                    try:
                        learnt |= _learn_ca(_qt_request(u, timeout=30))
                    except (TlsError, DownloadError):
                        continue
                if not learnt:
                    raise DownloadError(str(e))
                if sink is not None:
                    sink.seek(0)
                    sink.truncate()
        raise DownloadError("%s: certificate chain could not be completed" % url)
    import urllib.request
    import urllib.error
    try:
        h = {"User-Agent": "PosPro"}
        h.update(headers or {})
        if body is not None:
            h["Content-Type"] = ctype or "application/octet-stream"
        rq = urllib.request.Request(url, data=body, headers=h)
        with urllib.request.urlopen(rq, timeout=timeout) as fh:
            if sink is not None:
                shutil.copyfileobj(fh, sink, 1 << 20)
                return None
            return fh.read()
    except (urllib.error.URLError, OSError) as e:
        raise DownloadError("%s: %s" % (url, e))


def http_get(url, timeout=120):
    """Return the body of url (bytes). Raises DownloadError."""
    return _request(url, timeout=timeout)


def http_post(url, body, ctype="application/x-www-form-urlencoded; charset=UTF-8", timeout=120, headers=None):
    """POST and return the body (bytes). Raises DownloadError."""
    if isinstance(body, str):
        body = body.encode()
    h = {"X-Requested-With": "XMLHttpRequest"}
    h.update(headers or {})
    return _request(url, body, ctype, timeout, h)


def _save(data, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def complete(path, day=None, min_size=1):
    """A cached file that can be used as it is. A daily file (day given) fetched before that day was over
    (plus 3 h for the archives to fill) may be partial and is fetched again."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    if st.st_size < min_size:
        return False
    if day is not None:
        end = datetime.datetime(day.year, day.month, day.day, tzinfo=datetime.timezone.utc) + \
            datetime.timedelta(days=1, hours=3)
        if st.st_mtime < end.timestamp():
            return False
    return True


def fetch(urls, out_path, log=None, min_size=100, timeout=120, day=None):
    """Try each URL; store the uncompressed file at out_path (cached). The download is streamed to disk and
    unpacked from there, so a 1 s daily RINEX does not sit in memory. With day: see complete()."""
    if complete(out_path, day, min_size):
        return out_path
    have_old = os.path.exists(out_path) and os.path.getsize(out_path) >= min_size
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    dl, part = out_path + ".dl", out_path + ".part"
    errors = []
    for u in urls:
        try:
            if log:
                log("downloading %s" % u)
            with open(dl, "w+b") as fh:
                _request(u, timeout=timeout, sink=fh)
            if u.endswith(".Z"):
                raise DownloadError("Unix .Z compression is not supported (%s)" % u)
            if u.endswith(".gz"):
                with gzip.open(dl, "rb") as fi, open(part, "wb") as fo:
                    shutil.copyfileobj(fi, fo, 1 << 20)
            else:
                os.replace(dl, part)
            if os.path.getsize(part) < min_size:
                raise DownloadError("%s: empty file" % u)
            os.replace(part, out_path)
            return out_path
        except (DownloadError, OSError, zlib.error, EOFError) as e:
            errors.append(str(e))
        finally:
            for f in (dl, part):
                try:
                    os.remove(f)
                except OSError:
                    pass
    if have_old:            # the refresh of a same-day file failed: the earlier copy is still usable
        if log:
            log("using the copy of %s fetched earlier (could not refresh it)" % os.path.basename(out_path))
        return out_path
    raise DownloadError("could not download %s:\n  %s" % (os.path.basename(out_path), "\n  ".join(errors)))


def days_between(t0_gps, t1_gps):
    d0 = geo.gps_to_utc(t0_gps).date()
    d1 = geo.gps_to_utc(t1_gps).date()
    out = []
    d = d0
    while d <= d1:
        out.append(d)
        d += datetime.timedelta(days=1)
    return out


def _wk(day):
    t = datetime.datetime(day.year, day.month, day.day)
    return int((t - geo.GPS_EPOCH).days // 7)


def brdc(day, log=None):
    """Merged multi-GNSS broadcast ephemeris for a UTC day."""
    y, d = day.year, day.timetuple().tm_yday
    name = "BRDC00WRD_S_%04d%03d0000_01D_MN.rnx" % (y, d)
    out = os.path.join(cache_dir(), "brdc", "%04d" % y, name)
    urls = ["%s/BRDC/%04d/%03d/%s.gz" % (BKG, y, d, name),
            "%s/BRDC/%04d/%03d/BRDC00IGS_R_%04d%03d0000_01D_MN.rnx.gz" % (BKG, y, d, y, d),
            "%s/data/%04d/%03d/BRDC00IGS_R_%04d%03d0000_01D_MN.rnx.gz" % (IGN, y, d, y, d),
            "%s/data/%04d/%03d/%s.gz" % (IGN, y, d, name)]
    return fetch(urls, out, log, day=day)


def precise(day, log=None):
    """Precise orbit (SP3) and clock (CLK) for a UTC day.

    Finals first (ESA, IGS, CODE), then rapids.  ESA's archive is the most
    complete; BKG and IGN are often missing finals or slow to answer."""
    y, d, w = day.year, day.timetuple().tm_yday, _wk(day)
    wk = "%04d" % w

    def bkg_ign(n):
        return ["%s/products/%s/%s.gz" % (BKG, wk, n), "%s/products/%s/%s.gz" % (IGN, wk, n)]

    combos = [
        ("ESA0OPSFIN", "05M", "30S", lambda n: ["%s/%s/%s.gz" % (ESA, wk, n)]),
        ("IGS0OPSFIN", "15M", "30S", bkg_ign),
        ("COD0OPSFIN", "05M", "30S", lambda n: ["%s/%04d/%s.gz" % (AIUB, y, n)] + bkg_ign(n)),
        ("ESA0OPSRAP", "05M", "30S", lambda n: ["%s/%s/%s.gz" % (ESA, wk, n)]),
        ("IGS0OPSRAP", "15M", "05M", bkg_ign),
        ("COD0OPSRAP", "05M", "30S", lambda n: ["%s/%s.gz" % (AIUB, n), "%s/%04d/%s.gz" % (AIUB, y, n)]),
    ]
    errors = []
    missing_f = os.path.join(cache_dir(), "products", "missing.json")
    try:
        with open(missing_f, "r", encoding="utf-8") as fh:
            missing = json.load(fh)
    except (OSError, ValueError):
        missing = {}
    now = time.time()
    missing = {k: v for k, v in missing.items() if now - v < 6 * 3600}     # asked again after 6 h
    for ac, orb, clk, urls in combos:
        sp3n = "%s_%04d%03d0000_01D_%s_ORB.SP3" % (ac, y, d, orb)
        clkn = "%s_%04d%03d0000_01D_%s_CLK.CLK" % (ac, y, d, clk)
        sp3o = os.path.join(cache_dir(), "products", wk, sp3n)
        clko = os.path.join(cache_dir(), "products", wk, clkn)
        key = "%s_%04d%03d" % (ac, y, d)
        if key in missing and not (os.path.exists(sp3o) and os.path.exists(clko)):
            errors.append("%s: not available when last asked (%s); asked again after 6 h" % (
                ac, time.strftime("%H:%M", time.localtime(missing[key]))))
            continue
        try:
            fetch(urls(sp3n), sp3o, log, timeout=40)
            fetch(urls(clkn), clko, log, timeout=60)
            if log:
                log("precise products: %s" % ac)
            return sp3o, clko, ac
        except DownloadError as e:
            errors.append(str(e))
            missing[key] = now
            try:
                os.makedirs(os.path.dirname(missing_f), exist_ok=True)
                with open(missing_f + ".tmp", "w", encoding="utf-8") as fh:
                    json.dump(missing, fh)
                os.replace(missing_f + ".tmp", missing_f)
            except OSError:
                pass
    raise DownloadError("no precise orbit/clock found for %s (final products appear ~2 weeks after "
                        "the day, rapid ~1 day):\n%s" % (day, "\n".join(errors)))


def antex(log=None, max_age_days=60):
    out = os.path.join(cache_dir(), "igs20.atx")
    if os.path.exists(out) and time.time() - os.path.getmtime(out) > max_age_days * 86400:
        try:
            os.replace(out, out + ".old")
        except OSError:
            pass
    try:
        return fetch(["%s/igs20.atx" % IGS_FILES, "%s/station/general/igs20.atx" % IGN], out, log, 100000)
    except DownloadError:
        if os.path.exists(out + ".old"):
            os.replace(out + ".old", out)
            return out
        raise


# ---------------------------------------------------------------------------
# base stations
# ---------------------------------------------------------------------------
def crx2rnx(path, log=None):
    """Hatanaka-decompress a compact RINEX file; returns the RINEX path. Skipped when the RINEX file is
    already there and newer than the compact one."""
    exe = os.path.join(PLUGIN_DIR, "bin", "windows" if sys.platform.startswith("win") else "linux",
                       "crx2rnx.exe" if sys.platform.startswith("win") else "crx2rnx")
    src = path
    if src.lower().endswith(".gz"):
        plain = src[:-3]
        if not (os.path.exists(plain) and os.path.getmtime(plain) >= os.path.getmtime(src)):
            with gzip.open(src, "rb") as fi, open(plain + ".part", "wb") as fo:
                shutil.copyfileobj(fi, fo, 1 << 20)
            os.replace(plain + ".part", plain)
        src = plain
    low = src.lower()
    if low.endswith(".crx"):
        out = src[:-4] + ".rnx"
    elif low[-1:] == "d":
        out = src[:-1] + "o"
    else:
        out = src + ".rnx"
    if os.path.exists(out) and os.path.getsize(out) > 0 and os.path.getmtime(out) >= os.path.getmtime(src):
        return out
    if not os.path.exists(exe):
        raise DownloadError("the compact RINEX decompressor is missing (%s); reinstall PosPro" % exe)
    kw = {"creationflags": 0x08000000} if sys.platform.startswith("win") else {}
    try:
        with open(src, "rb") as fi, open(out + ".part", "wb") as fo:
            r = subprocess.run([exe, "-s"], stdin=fi, stdout=fo, stderr=subprocess.PIPE, **kw)
        if r.returncode not in (0, 2):
            raise DownloadError("compact RINEX could not be decompressed: %s" %
                                r.stderr.decode(errors="replace")[:300])
        os.replace(out + ".part", out)
    finally:
        if os.path.exists(out + ".part"):
            try:
                os.remove(out + ".part")
            except OSError:
                pass
    return out


def noaa_station(sid):
    """Station facts from the NOAA NCN API (position is ARP)."""
    data = json.loads(http_get("%s/api/nde/cors?id=%s" % (NOAA, sid.lower())).decode("utf-8", "replace"))
    if isinstance(data, list):
        data = data[0] if data else {}
    return data


def noaa_nearest(xyz):
    data = json.loads(http_get("%s/api/nde/ncors?x=%.3f&y=%.3f&z=%.3f" % (NOAA, xyz[0], xyz[1], xyz[2]))
                      .decode("utf-8", "replace"))
    ids = []
    if isinstance(data, dict):
        data = data.get("stations") or data.get("ncors") or list(data.values())
    for item in data if isinstance(data, list) else []:
        if isinstance(item, str):
            ids.append(item.upper())
        elif isinstance(item, dict):
            sid = item.get("siteId") or item.get("id") or item.get("site") or item.get("stationId")
            if sid:
                ids.append(str(sid).upper())
    return ids


def noaa_rinex(sid, day, log=None):
    s = sid.lower()
    y, d = day.year, day.timetuple().tm_yday
    yy = y % 100
    folder = os.path.join(cache_dir(), "cors", s, "%04d%03d" % (y, d))
    obs = os.path.join(folder, "%s%03d0.%02do" % (s, d, yy))
    if complete(obs, day, 1000):
        return obs
    # NOAA retired the corsdata/ paths on 1 Nov 2026; the AWS bucket is the archive
    for base in ("%s/rinex" % NOAA_S3, "%s/corsdata/rinex" % NOAA):
        try:
            return fetch(["%s/%04d/%03d/%s/%s%03d0.%02do.gz" % (base, y, d, s, s, d, yy)], obs, log, 1000,
                         day=day)
        except DownloadError:
            pass
        try:
            crx = os.path.join(folder, "%s%03d0.%02dd" % (s, d, yy))
            fetch(["%s/%04d/%03d/%s/%s%03d0.%02dd.gz" % (base, y, d, s, s, d, yy)], crx, log, 1000, day=day)
            return crx2rnx(crx, log)
        except DownloadError:
            pass
    raise DownloadError("no RINEX for CORS %s on %s" % (sid.upper(), day))


def igs_rinex(sid9, day, log=None):
    """Daily 30 s RINEX 3 of an IGS station (9-character id, e.g. FRDN00CAN)."""
    y, d = day.year, day.timetuple().tm_yday
    name = "%s_R_%04d%03d0000_01D_30S_MO" % (sid9.upper(), y, d)
    folder = os.path.join(cache_dir(), "igs", sid9.upper())
    crx = os.path.join(folder, name + ".crx")
    rnx = os.path.join(folder, name + ".rnx")
    if complete(rnx, day, 1000):
        return rnx
    fetch(["%s/obs/%04d/%03d/%s.crx.gz" % (BKG, y, d, name), "%s/data/%04d/%03d/%s.crx.gz" % (IGN, y, d, name)],
          crx, log, 1000, day=day)
    return crx2rnx(crx, log)
