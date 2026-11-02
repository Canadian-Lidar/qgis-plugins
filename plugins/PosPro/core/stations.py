"""Reference-station catalogues and downloads for the base-station finder.

Each network has a provider: `list_fn()` returns stations (cached for a week
in the PosPro cache folder) and `download_fn(station, day, rate, span,
out_dir, creds, log)` returns RINEX files. Networks whose data is only
available through a subscriber web portal have no download function: the
finder shows them on the map and opens their portal instead.
"""
import datetime
import gzip
import json
import math
import os
import time

from . import geo, products

RADIUS_KM = 150
PROVIDER_LABEL = {"noaa": "NOAA CORS", "igs": "IGS", "nrcan": "NRCan CACS", "rtk": "NRCan RTK list"}
NEAREST_PER_NETWORK = 3
CACHE_DAYS = 7

ACCESS_LABEL = {"public": "Free download", "login": "Your account", "portal": "Portal only"}

# map symbols: QGIS simple-marker shape names, mapper colours. The commercial
# RTK networks come from NRCan's compliance list (one entry each).
NETWORKS = {
    "noaa": {"label": "NOAA CORS", "shape": "triangle", "colour": "#3ab4f2", "size": 4.2,
             "operator": "NOAA National Geodetic Survey", "portal": "https://geodesy.noaa.gov/UFCORS/"},
    "igs": {"label": "IGS", "shape": "star", "colour": "#00e5aa", "size": 5.4,
            "operator": "International GNSS Service", "portal": "https://network.igs.org/"},
    "nrcan": {"label": "NRCan CACS", "shape": "square", "colour": "#e84855", "size": 3.8,
              "operator": "Natural Resources Canada",
              "portal": "https://webapp.csrs-scrs.nrcan-rncan.gc.ca/geod/data-donnees/cacs-scca.php?locale=en"},
    "NSACS": {"label": "NS active control", "shape": "square", "colour": "#ff8d96", "size": 3.8,
              "operator": "Province of Nova Scotia (via NRCan)",
              "portal": "https://webapp.csrs-scrs.nrcan-rncan.gc.ca/geod/data-donnees/cacs-scca.php?locale=en"},
    "NRCAN_REG": {"label": "NRCan regional", "shape": "square", "colour": "#c8102e", "size": 3.4,
                  "operator": "NRCan / provincial partners",
                  "portal": "https://webapp.csrs-scrs.nrcan-rncan.gc.ca/geod/data-donnees/cacs-scca.php?locale=en"},
    "CANNET": {"label": "Can-Net", "shape": "diamond", "colour": "#f5a623", "size": 4.2, "operator": "Trimble (Can-Net)",
               "portal": "https://www.can-net.ca/"},
    "SMARTNET": {"label": "SmartNet", "shape": "pentagon", "colour": "#9b7bff", "size": 4.2,
                 "operator": "Leica Geosystems (SmartNet)", "portal": "https://www.smartnetna.com/"},
    "BRANDT": {"label": "BrandtNet", "shape": "hexagon", "colour": "#ff7a45", "size": 4.2, "operator": "Brandt",
               "portal": "https://www.brandt.ca/Divisions/Positioning-Technology/Brandtnet"},
    "TOPNET": {"label": "TopNET", "shape": "equilateral_triangle", "colour": "#7ee0ff", "size": 4.2,
               "operator": "Topcon (TopNET Live)", "portal": "http://www.topnetlive.com/"},
    "GEODNET": {"label": "GEODNET", "shape": "circle", "colour": "#c6f36b", "size": 3.6, "operator": "GEODNET",
                "portal": "https://geodnet.com/"},
    "OTHER_RTK": {"label": "Other RTK", "shape": "cross_fill", "colour": "#b8d4ce", "size": 3.6, "operator": "",
                  "portal": "https://webapp.csrs-scrs.nrcan-rncan.gc.ca/geod/data-donnees/rtk.php?locale=en"},
}
NRCAN_RTK_STATUS = {"0": "Compliant with NAD83(CSRS) (NRCan weekly check)", "2": "Status unknown (no recent NRCan check)",
                    "1": "Not compliant (outside 2 cm H / 3 cm V, or long outage)"}


class Station:
    __slots__ = ("network", "sid", "name", "lat", "lon", "h", "receiver", "antenna", "rates", "access", "portal",
                 "operator", "extra", "dist_km", "nearest")

    def __init__(self, network, sid, lat, lon, h=None, name="", receiver="", antenna="", rates=(30,),
                 access="public", portal="", operator="", **extra):
        self.network, self.sid, self.lat, self.lon, self.h = network, sid, float(lat), float(lon), h
        self.name, self.receiver, self.antenna = name, receiver, antenna
        self.rates = sorted(set(rates)) if rates else []
        self.access, self.portal, self.operator, self.extra = access, portal, operator, extra
        self.dist_km = 0.0
        self.nearest = False

    @property
    def key(self):
        return "%s:%s" % (self.network, self.sid)

    def to_json(self):
        return {k: getattr(self, k) for k in self.__slots__ if k not in ("dist_km", "nearest")}

    @classmethod
    def from_json(cls, d):
        d = dict(d)
        extra = d.pop("extra", {}) or {}
        s = cls(d.pop("network"), d.pop("sid"), d.pop("lat"), d.pop("lon"), **d, **extra)
        return s


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------
def _cache_path(net):
    d = os.path.join(products.cache_dir(), "stations")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "%s.json" % net)


def _cached_list(net, fn, force, log, centre=None):
    if net in CENTRE_DEPENDENT and centre:
        net = "%s_%.1f_%.1f" % (net, round(centre[0] * 2) / 2, round(centre[1] * 2) / 2)
    path = _cache_path(net)
    if not force and os.path.exists(path) and time.time() - os.path.getmtime(path) < CACHE_DAYS * 86400:
        with open(path, encoding="utf-8") as fh:
            return [Station.from_json(d) for d in json.load(fh)]
    sts = fn(log, centre)
    if sts:
        tmp = path + ".part"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump([s.to_json() for s in sts], fh)
        os.replace(tmp, path)
    return sts


def nearby(centre_llh, days, force=False, log=None, cancelled=None, radius_km=RADIUS_KM):
    """Stations of every network within radius_km of the drive centre, with
    distances and the nearest few of each network flagged. Returns
    (stations, [networks that could not be reached])."""
    out, problems = [], []
    for prov, fn in PROVIDERS.items():
        if cancelled and cancelled():
            break
        try:
            sts = _cached_list(prov, fn, force, log, centre_llh)
        except Exception as e:      # noqa: BLE001 - one network down must not hide the rest
            # a stale cache is better than nothing
            path = _cache_path(prov)
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    sts = [Station.from_json(d) for d in json.load(fh)]
            else:
                sts = []
            problems.append(PROVIDER_LABEL.get(prov, prov))
            if log:
                log("%s station list: %s" % (PROVIDER_LABEL.get(prov, prov), str(e).splitlines()[0][:200]))
        for s in sts:
            s.dist_km = geo.great_circle_km(centre_llh[0], centre_llh[1], s.lat, s.lon)
            if s.dist_km <= radius_km:
                out.append(s)
    # an IGS station that NRCan also archives (FRDN, UNBJ, HLFX...) appears once,
    # as the NRCan station (same files, Canadian archive)
    nr = {s.sid[:4]: s for s in out if s.network in ("nrcan", "NSACS", "NRCAN_REG")}
    keep = []
    for s in out:
        if s.network == "igs" and s.sid[:4] in nr:
            nr[s.sid[:4]].extra["also"] = "IGS station %s" % s.sid
            if not nr[s.sid[:4]].receiver:
                nr[s.sid[:4]].receiver, nr[s.sid[:4]].antenna = s.receiver, s.antenna
            continue
        keep.append(s)
    out = keep
    # the nearest few of each network
    out.sort(key=lambda s: s.dist_km)
    count = {}
    for s in out:
        count[s.network] = count.get(s.network, 0) + 1
        s.nearest = count[s.network] <= NEAREST_PER_NETWORK
    return out, problems


def download(st, days, rate, span, out_dir, creds=None, log=None, cancelled=None, progress=None):
    fn = DOWNLOADERS.get(st.network)
    if fn is None:
        raise products.DownloadError("%s has no download service; use its portal" % NETWORKS[st.network]["label"])
    files = []
    for i, day in enumerate(days):
        if cancelled and cancelled():
            break
        if progress:
            progress(i / max(1, len(days)), "%s %s" % (st.sid, day))
        files += fn(st, day, rate, span, out_dir, creds, log)
    return files


def download_products(days, log=None):
    """Broadcast ephemeris, precise orbit + clock (final, else rapid) and the
    IGS antenna file for the days; failures are logged, not fatal."""
    got = []
    for d in days:
        for fn in (products.brdc, products.precise):
            try:
                r = fn(d, log)
                got += list(r) if isinstance(r, tuple) else [r]
            except products.DownloadError as e:
                if log:
                    log(str(e).splitlines()[0])
    try:
        got.append(products.antex(log))
    except products.DownloadError as e:
        if log:
            log(str(e).splitlines()[0])
    return got


def credentials(network, parent=None):
    """User name and password for a subscription network, kept in the QGIS
    authentication database (encrypted) under 'PosPro <network>'."""
    from qgis.core import QgsApplication, QgsAuthMethodConfig
    from qgis.PyQt.QtWidgets import QLineEdit
    from ..ui import theme
    name = "PosPro %s" % NETWORKS[network]["label"]
    am = QgsApplication.authManager()
    try:
        for cid, cfg in am.availableAuthMethodConfigs().items():
            if cfg.name() == name:
                full = QgsAuthMethodConfig()
                am.loadAuthenticationConfig(cid, full, True)
                return full.config("username"), full.config("password")
    except Exception:               # noqa: BLE001 - no auth DB: just ask
        pass
    user, ok = theme.get_text(parent, NETWORKS[network]["label"], "User name:")
    if not ok or not user:
        return None
    pw, ok = theme.get_text(parent, NETWORKS[network]["label"], "Password:", QLineEdit.EchoMode.Password)
    if not ok:
        return None
    try:
        cfg = QgsAuthMethodConfig("Basic")
        cfg.setName(name)
        cfg.setConfig("username", user)
        cfg.setConfig("password", pw)
        am.storeAuthenticationConfig(cfg)
    except Exception:               # noqa: BLE001
        pass
    return user, pw


# ---------------------------------------------------------------------------
# providers
# ---------------------------------------------------------------------------
NRCAN = "https://webapp.csrs-scrs.nrcan-rncan.gc.ca"


def _get_json(url):
    return json.loads(products.http_get(url).decode("utf-8", "replace"))


def nrcan_rtk_list(log, centre=None, bbox=(41.5, -84.0, 63.0, -47.5)):
    """Commercial RTK stations registered with NRCan (Can-Net, SmartNet,
    BrandtNet, TopNET, GEODNET, ...), with NRCan's compliance status. The same
    service drives NRCan's 'RTK networks' map page. Eastern Canada by default."""
    nets = _get_json(NRCAN + "/CSRS/tools/networks")
    out = []
    for n in nets:
        nid = n.get("id")
        if not nid:
            continue
        url = "%s/CSRS/tools/networks/stations?id=%s&map=%s&minlat=%.3f&maxlat=%.3f&minlon=%.3f&maxlon=%.3f" % (
            NRCAN, nid, nid, bbox[0], bbox[2], bbox[1], bbox[3])
        try:
            data = _get_json(url)
        except products.DownloadError as e:
            if log:
                log("NRCan RTK list %s: %s" % (nid, str(e)[:120]))
            continue
        groups = (data.get("networks") or {}).get("stations") or []
        net = nid if nid in NETWORKS else "OTHER_RTK"
        for grp in groups:
            for d in grp:
                try:
                    out.append(Station(net, d["id"], float(d["lat"]), float(d["lon"]), None, name=d.get("n", ""),
                                       rates=(), access="portal", portal=n.get("link", ""),
                                       operator=n.get("en", nid),
                                       status=NRCAN_RTK_STATUS.get(str(d.get("status")), ""),
                                       report=NRCAN + "/geod/data-donnees/station/report-rapport.php?id=" + d["id"],
                                       note="Commercial network: RINEX through the operator's subscriber portal."))
                except (KeyError, ValueError):
                    continue
    return out


# ---------------------------------------------------------------------------
# NRCan: CACS and the provincial active-control stations it archives
# (getStations.php drives NRCan's CACS map page; files are on the anonymous
# archive https://cacs.nrcan.gc.ca, 30 s daily and 1 s 15-minute files)
# ---------------------------------------------------------------------------
NRCAN_NETS = {"CSRS": "nrcan", "NSACS": "NSACS", "REGIONAL": "NRCAN_REG", "RACM": "NRCAN_REG", "WCDA": "NRCAN_REG"}
CACS = "https://cacs.nrcan.gc.ca/gps/data"


def nrcan_cacs_list(log, centre=None):
    body = "map=cacs&maxlatitude=90&minlatitude=-90&minlongitude=-180&maxlongitude=180"
    data = json.loads(products.http_post(NRCAN + "/geod/process/getStations.php", body).decode("utf-8", "replace"))
    nets = (data.get("networks") or {})
    out = []
    for name, grp in zip(nets.get("names") or [], nets.get("stations") or []):
        key = NRCAN_NETS.get(name)
        if not key:
            continue                    # DISCONTINUED
        for d in grp:
            try:
                sid = str(d["n"]).strip().upper()
                rates = []
                if d.get("ic3_obs") or d.get("ic_obs"):
                    rates.append(30)
                if d.get("r3_1sec") or d.get("r2_1sec"):
                    rates.append(1)
                xyz = [float(str(d[k]).strip()) for k in ("x", "y", "z")]
                h = geo.ecef2llh(*xyz)[2]
                out.append(Station(key, sid, float(d["lat"]), float(d["lon"]), h, name=(d.get("town") or "").title(),
                                   rates=rates or (30,), access="public", operator="Natural Resources Canada",
                                   xyz=xyz, network_name=name, report=NRCAN + "/geod/data-donnees/station/"
                                   "report-rapport.php?id=%s" % d.get("id", "")))
            except (KeyError, ValueError, TypeError):
                continue
    return out


def _listing(url):
    """File names in an Apache directory listing."""
    import re
    html = products.http_get(url).decode("utf-8", "replace")
    return re.findall(r'href="([^"?/][^"]*)"', html)


def _pick(names, sid, tail):
    """The RINEX 3 long name of a station in a listing (country code unknown)."""
    sid = sid.upper()
    for n in names:
        if n.upper().startswith(sid) and n.endswith(tail):
            return n
    return None


def _fetch_crx(url, out_dir, log):
    """Download a .crx.gz (or .d.gz/.d.Z) and return the RINEX observation file."""
    name = url.rsplit("/", 1)[1]
    local = os.path.join(out_dir, name)
    if not os.path.exists(local[:-3] if local.endswith(".gz") else local):
        data = products.http_get(url)
        os.makedirs(out_dir, exist_ok=True)
        with open(local, "wb") as fh:
            fh.write(data)
    return products.crx2rnx(local, log)


def _hours(day, span, margin_s=600):
    """UTC (hour, minute) 15-minute slots of `day` that overlap the drive."""
    t0 = geo.gps_to_utc(span[0] - margin_s)
    t1 = geo.gps_to_utc(span[1] + margin_s)
    start = datetime.datetime(day.year, day.month, day.day)
    slots = []
    for k in range(96):
        a = start + datetime.timedelta(minutes=15 * k)
        b = a + datetime.timedelta(minutes=15)
        if b > t0 and a < t1:
            slots.append((a.hour, a.minute))
    return slots


def nrcan_download(st, day, rate, span, out_dir, creds, log):
    y, d = day.year, day.timetuple().tm_yday
    yy = y % 100
    if rate >= 30:
        url = "%s/gpsdata/%02d%03d/%02dd/" % (CACS, yy, d, yy)
        n = _pick(_listing(url), st.sid, "_01D_30S_MO.crx.gz")
        if not n:
            raise products.DownloadError("no 30 s file for %s on %s at NRCan" % (st.sid, day))
        if log:
            log("NRCan %s %s" % (st.sid, n))
        return [_fetch_crx(url + n, out_dir, log)]
    files = []
    for hh, mm in _hours(day, span):
        url = "%s/hrdata/%02d%03d/%02dd/%02d/" % (CACS, yy, d, yy, hh)
        n = _pick(_listing(url), st.sid, "%02d%02d_15M_01S_MO.crx.gz" % (hh, mm))
        if n:
            files.append(_fetch_crx(url + n, out_dir, log))
        elif log:
            log("NRCan %s: no 1 s file for %02d:%02d" % (st.sid, hh, mm))
    if not files:
        raise products.DownloadError("no 1 s files for %s on %s at NRCan (1 s data are kept for a limited "
                                     "time; try 30 s)" % (st.sid, day))
    return files


# ---------------------------------------------------------------------------
# IGS: station list from files.igs.org, data from BKG (IGN as fallback)
# ---------------------------------------------------------------------------
def igs_list(log, centre=None):
    data = _get_json("https://files.igs.org/pub/station/general/IGSNetwork.json")
    out = []
    for sid9, d in data.items():
        try:
            xyz = [float(d["X"]), float(d["Y"]), float(d["Z"])]
            la, lo, h = geo.ecef2llh(*xyz)
            rec = (d.get("Receiver") or {}).get("Name", "")
            ant = (d.get("Antenna") or {})
            out.append(Station("igs", sid9, la, lo, h, name=sid9, receiver=rec,
                               antenna=("%s %s" % (ant.get("Name", ""), ant.get("Radome", ""))).strip(),
                               rates=(1, 30), access="public", operator="International GNSS Service",
                               xyz=xyz, last_data=d.get("LastData", ""),
                               note="1 s data exist only for IGS high-rate stations."))
        except (KeyError, ValueError, TypeError):
            continue
    return out


def igs_download(st, day, rate, span, out_dir, creds, log):
    y, d = day.year, day.timetuple().tm_yday
    if rate >= 30:
        return [products.igs_rinex(st.sid, day, log)]
    files = []
    for hh, mm in _hours(day, span):
        name = "%s_R_%04d%03d%02d%02d_15M_01S_MO.crx.gz" % (st.sid.upper(), y, d, hh, mm)
        got = None
        for base in ("%s/highrate/%04d/%03d/%02d/" % (products.BKG, y, d, hh),
                     "%s/data/highrate/%04d/%03d/%02dd/%02d/" % (products.IGN, y, d, y % 100, hh)):
            try:
                got = _fetch_crx(base + name, out_dir, log)
                break
            except (products.DownloadError, OSError):
                continue
        if got:
            files.append(got)
    if not files:
        raise products.DownloadError("no 1 s files for %s on %s (not a high-rate station, or not yet "
                                     "archived); try 30 s" % (st.sid, day))
    return files


# ---------------------------------------------------------------------------
# NOAA CORS: the nine nearest from the NCN API, details per station, rates
# from dates_sites.txt; files from the AWS bucket (corsdata/ retired 1 Nov 2026)
# ---------------------------------------------------------------------------
def _noaa_rates():
    import re
    rates = {}
    try:
        txt = products.http_get(products.NOAA + "/CORS/dates_sites.txt").decode("utf-8", "replace")
    except products.DownloadError:
        return rates
    pat = re.compile(r"\s([A-Z0-9]{4})\s+(\d{7})\s+(\S+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(.*)$")
    for line in txt.splitlines():
        m = pat.search(line)
        if m:
            rates[m.group(1).lower()] = (int(m.group(4)), m.group(7).strip(), m.group(6))
    return rates


def noaa_list(log, centre=None):
    if not centre:
        return []
    xyz = geo.llh2ecef(centre[0], centre[1], centre[2] if len(centre) > 2 else 0.0)
    near = _get_json("%s/api/nde/ncors?x=%.3f&y=%.3f&z=%.3f" % (products.NOAA, xyz[0], xyz[1], xyz[2]))
    rates = _noaa_rates()
    out = []
    for item in near if isinstance(near, list) else []:
        sid = str(item.get("corsId", "")).lower()
        if not sid:
            continue
        try:
            d = _get_json("%s/api/nde/cors?id=%s" % (products.NOAA, sid))
            d = d[0] if isinstance(d, list) and d else d
            r, status, agency = rates.get(sid, (30, "", ""))
            out.append(Station("noaa", sid.upper(), float(d["lat"]), float(d["lon"]), float(d.get("ellipHeight") or 0),
                               name=d.get("name", "").replace(" CORS ARP", "").title(),
                               rates=sorted({30, r}) if r < 30 else (30,), access="public",
                               operator=agency or "NOAA NGS", status=status,
                               note="Files carry the station's own rate for 30 days, then 30 s."))
        except (products.DownloadError, KeyError, ValueError, TypeError) as e:
            if log:
                log("NOAA %s: %s" % (sid, str(e)[:100]))
    return out


def noaa_download(st, day, rate, span, out_dir, creds, log):
    age = (datetime.date.today() - day).days
    if rate < 30 and age > 30 and log:
        log("NOAA keeps the full rate for 30 days only; %s will be 30 s" % day)
    return [products.noaa_rinex(st.sid, day, log)]


PROVIDERS = {"nrcan": nrcan_cacs_list, "igs": igs_list, "noaa": noaa_list, "rtk": nrcan_rtk_list}
DOWNLOADERS = {"nrcan": nrcan_download, "NSACS": nrcan_download, "NRCAN_REG": nrcan_download, "igs": igs_download,
               "noaa": noaa_download}
CENTRE_DEPENDENT = {"noaa"}
