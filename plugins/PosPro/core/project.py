"""PosPro project: settings and state saved as JSON beside the work folder.

A project folder holds:
    <name>.pospro.json   settings, base stations, results summary
    work/                extracted data, intermediate and final results
"""
import copy
import json
import os
import time

PROJECT_SUFFIX = ".pospro.json"

IMU_PRESETS = {
    "certus_evo": {
        "label": "Advanced Navigation Certus Evo",
        "arw_deg_sqrth": 0.15, "vrw_m_s_sqrth": 0.05,
        "gyro_bias_deg_h": 0.3, "gyro_bias_tau_s": 3600,
        "acc_bias_ug": 30, "acc_bias_tau_s": 3600,
        "gyro_bias0_deg_h": 3.0, "acc_bias0_mg": 0.5,
        "gyro_scale0_ppm": 300, "acc_scale0_ppm": 300,
    },
    "certus": {
        "label": "Advanced Navigation Certus",
        "arw_deg_sqrth": 0.3, "vrw_m_s_sqrth": 0.1,
        "gyro_bias_deg_h": 3.0, "gyro_bias_tau_s": 1800,
        "acc_bias_ug": 50, "acc_bias_tau_s": 1800,
        "gyro_bias0_deg_h": 50.0, "acc_bias0_mg": 5.0,
        "gyro_scale0_ppm": 500, "acc_scale0_ppm": 600,
    },
    "tactical_mems": {
        "label": "Generic tactical-grade MEMS",
        "arw_deg_sqrth": 0.2, "vrw_m_s_sqrth": 0.08,
        "gyro_bias_deg_h": 1.0, "gyro_bias_tau_s": 3600,
        "acc_bias_ug": 50, "acc_bias_tau_s": 3600,
        "gyro_bias0_deg_h": 10.0, "acc_bias0_mg": 1.0,
        "gyro_scale0_ppm": 500, "acc_scale0_ppm": 500,
    },
    "industrial_mems": {
        "label": "Generic industrial MEMS",
        "arw_deg_sqrth": 0.5, "vrw_m_s_sqrth": 0.15,
        "gyro_bias_deg_h": 10.0, "gyro_bias_tau_s": 600,
        "acc_bias_ug": 200, "acc_bias_tau_s": 600,
        "gyro_bias0_deg_h": 100.0, "acc_bias0_mg": 10.0,
        "gyro_scale0_ppm": 1000, "acc_scale0_ppm": 1000,
    },
}

# The mapper's vehicles (Settings > Hardware > Vehicle: session.VEHICLE_PRESETS), with the wheeled-vehicle
# constraint each one gets: how fast (m/s) the rear axle may slide sideways or lift. The constraint and the
# zero-velocity updates can be switched off on the Processing page for a platform without wheels.
VEHICLE_PROFILES = {
    "van": {"label": "Van", "nhc": True, "nhc_sigma": 0.1, "zupt": True},
    "truck": {"label": "Truck", "nhc": True, "nhc_sigma": 0.15, "zupt": True},
    "car": {"label": "Car", "nhc": True, "nhc_sigma": 0.1, "zupt": True},
}
# motion profiles of PosPro 1.4 and earlier (a list of its own), read when an older project opens
_OLD_PROFILES = {"car": 0.1, "truck": 0.15, "rail": 0.05, "boat": None, "other": None}

FRAMES = {
    "NAD83(CSRS)": {"crs": "EPSG:4617", "label": "NAD83(CSRS)"},
    "ITRF2020": {"crs": "EPSG:4326", "label": "ITRF2020 / IGS20"},
    "WGS 84": {"crs": "EPSG:4326", "label": "WGS 84"},
    "NAD83(2011)": {"crs": "EPSG:6318", "label": "NAD83(2011)"},
}

DEFAULTS = {
    "format": 1,
    "name": "",
    "created": None,
    "rover": {
        "files": [],
        "rinex_obs": "",          # alternative input: rover RINEX + IMU from ANPP
        "extract": {},            # summary from the engine
        "extract_files": [],      # files the summary belongs to (path, size, mtime)
        "time_start": None,       # GPS seconds, crop
        "time_end": None,
        "hardware": {},           # from .clraw hardware.json
    },
    "nav_files": [],
    "auto_ephemeris": True,
    "bases": [],
    "base_selected": None,
    "frame": {"name": "NAD83(CSRS)", "epoch": "1997.0"},
    "installation": {
        "lever_antenna": [0.0, 0.0, 0.0],   # IMU -> GNSS antenna, X fwd, Y stbd, Z down (m)
        "lever_output": [0.0, 0.0, 0.0],    # IMU -> output point
        "output_name": "IMU centre",
        "lever_nhc": [0.0, 0.0, 0.0],       # IMU -> rear axle centre
        "lever_antenna2": [0.0, 0.0, 0.0],  # IMU -> secondary antenna (receiver 1)
        "use_antenna2": True,               # tight coupling with both antennas when recorded
        "time_offset_ms": 0.0,
        "source": "",
    },
    "imu": {"preset": "certus_evo", "custom": {}},
    "vehicle": {"length": 5.2, "width": 2.0, "type": "van", "outline": {}},
    "processing": {
        "mode": "tc",             # tc | lc | gnss
        "positioning": "ppk",     # ppk | ppp
        "passes": "both",         # both | forward
        "systems": "GREJ",
        "frequencies": 2,
        "elmask": 12.0,
        "ar": True,
        "ar_ratio": 3.0,
        "glonass_ar": False,
        "nhc": True,
        "zupt": True,
        "nhc_sigma": None,        # None = the vehicle's (VEHICLE_PROFILES)
        "align_mode": 0,          # 0 auto, 1 real-time attitude, 2 GNSS course
        "align_speed": 4.0,
        "output_rate_hz": 0,      # 0 = every IMU sample
        "gate_chi2": 9.0,
        "lc_use_float": True,
        "lc_use_dgps": True,
        "base_max_dt": 60.0,
        "atmosphere": "auto",     # auto | on | off: relative ionosphere/troposphere (long baselines)
        "snr_weighting": True,    # weak signals count less
        "advanced": {},           # extra engine keys (expert use)
    },
    "calibration": {"levers": {}},
    "csrs": {"email": "", "epoch": "1997-01-01", "vdatum": "cgvd2013"},
    "results": {},
    "export": {
        "folder": "",
        "rate_hz": 0,
        "point": "imu",
        "sbet": True,
        "ascii": False,
        "ascii_cols": "date_time_utc,lat,lon,h",
        "ascii_sep": ",",
        "ascii_crs": "",
        "kml": False,
        "gpkg": False,
    },
}


def _merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


STAGE_FILE = "pospro.json"


class Project:
    """PosPro's settings. In a DCS project (the normal case since 1.2) they
    live in Processing/<Name>/01_PosPro/pospro.json and self.dcs is the shared
    project; a stand-alone .pospro.json from earlier versions still opens
    (self.dcs is None)."""

    def __init__(self, path=None):
        self.path = path
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg["created"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.dirty = False
        self.dcs = None
        self._synced = {}

    @classmethod
    def from_dcs(cls, dcs):
        """PosPro's part of a shared DCS project (created when missing)."""
        path = os.path.join(dcs.stage_dir("pospro"), STAGE_FILE)
        if os.path.exists(path):
            p = cls.load(path, dcs=dcs)
        else:
            p = cls(path)
            p.cfg["name"] = dcs.name
            p.dcs = dcs
            os.makedirs(p.work_dir, exist_ok=True)
            p.save()
        p.dcs = dcs
        if not dcs.stage("pospro").get("settings"):
            from dcs_core import files
            dcs.update_stage("pospro", "PosPro", settings=files.portable(path, dcs.folder))
        return p

    @classmethod
    def open_any(cls, path):
        """A .dcsproj, a folder holding one, PosPro's pospro.json inside a DCS
        project, or an old stand-alone .pospro.json."""
        from dcs_core.project import SUFFIX, DcsProject
        if path.endswith(SUFFIX) or os.path.isdir(path):
            return cls.from_dcs(DcsProject.open(path))
        if os.path.basename(path) == STAGE_FILE:
            dcs = DcsProject.find_for(path)
            if dcs is not None:
                return cls.from_dcs(dcs)
        return cls.load(path)

    @property
    def project_folder(self):
        """The folder the user thinks of as the project (the DCS project folder)."""
        return self.dcs.folder if self.dcs else self.folder

    @property
    def out_dir(self):
        d = os.path.join(self.folder, "out")
        os.makedirs(d, exist_ok=True)
        return d

    def sync_dcs(self):
        """Write the shared facts PosPro owns into the DCS project: raw files,
        coordinate frame, base station, antenna offsets, vehicle and the
        capture's hardware record. Only what changed is written."""
        if not self.dcs:
            return
        c = self.cfg
        inst = c["installation"]
        b = self.base()
        fr_ = c.get("frame") or {} if c["processing"].get("mode") == "rt" else self.trajectory_frame()
        facts = {
            "crs": {"frame": fr_.get("name"), "epoch": fr_.get("epoch"),
                    "height": "ellipsoidal",
                    "horizontal": (c.get("export") or {}).get("ascii_crs") or ""},
            "base_station": None if not b else {
                "name": b.get("name"), "files": b.get("files"), "xyz_m": b.get("xyz"),
                "position_source": b.get("pos_source"), "frame": b.get("frame"), "epoch": b.get("epoch"),
                "antenna": b.get("antenna"), "delta_hen_m": b.get("delta_hen")},
            "antennas": {"gnss1_m": inst.get("lever_antenna"), "gnss2_m": inst.get("lever_antenna2"),
                         "use_gnss2": inst.get("use_antenna2"), "source": inst.get("source", "")},
            "vehicle": {"type": c["vehicle"].get("type"), "outline": c["vehicle"].get("outline"),
                        "rear_axle_m": inst.get("lever_nhc"), "constraints": c["vehicle"].get("type")},
        }
        hw = c["rover"].get("hardware")
        if hw:
            facts["hardware"] = hw
        for k, v in facts.items():
            if v is None:
                continue
            h = json.dumps(v, sort_keys=True, default=str)
            if self._synced.get(k) != h:
                self.dcs.set_shared(k, v, "PosPro")
                self._synced[k] = h
        files_now = list(c["rover"].get("files") or [])
        if self._synced.get("captures") != files_now:
            existing = [f for f in files_now if os.path.exists(f)]
            if existing and sorted(existing) != sorted(self.dcs.capture_files()):
                self.dcs.set_capture_files(existing, "PosPro")
            self._synced["captures"] = files_now

    # ------------------------------------------------------------------
    @classmethod
    def create(cls, folder, name):
        os.makedirs(folder, exist_ok=True)
        p = cls(os.path.join(folder, name + PROJECT_SUFFIX))
        p.cfg["name"] = name
        os.makedirs(p.work_dir, exist_ok=True)
        p.save()
        return p

    @classmethod
    def load(cls, path, dcs=None):
        p = cls(path)
        p.dcs = dcs
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or "rover" not in data:
            raise ValueError("%s is not a PosPro project" % path)
        p.cfg = _merge(copy.deepcopy(DEFAULTS), data)
        p._upgrade_vehicle()
        os.makedirs(p.work_dir, exist_ok=True)
        return p

    def _upgrade_vehicle(self):
        """PosPro 1.4 and earlier had a motion-constraint list of its own beside the mapper's vehicle. Now the
        vehicle sets the constraints; an older project keeps what it processed with: no wheel constraint for
        boat / other (and no zero-velocity updates for boat), 0.05 m/s for rail."""
        veh = self.cfg["vehicle"]
        if veh.get("type") not in VEHICLE_PROFILES:
            veh["type"] = "van"
        old = veh.pop("profile", None)
        proc = self.cfg["processing"]
        if old is None:
            return
        if old in ("boat", "other"):
            proc["nhc"] = False
            if old == "boat":
                proc["zupt"] = False
        if old == "rail":
            proc["nhc_sigma"] = 0.05
        elif proc.get("nhc_sigma") == 0.1:
            proc["nhc_sigma"] = None            # the old fixed default: the vehicle's value from now on

    def vehicle_profile(self):
        return VEHICLE_PROFILES.get(self.cfg["vehicle"].get("type"), VEHICLE_PROFILES["van"])

    def save(self):
        if not self.path:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.cfg, fh, indent=1, sort_keys=False)
        os.replace(tmp, self.path)
        self.dirty = False
        try:
            self.sync_dcs()
        except Exception:           # noqa: BLE001 - the stage settings are saved; the shared file retries next save
            pass

    def __deepcopy__(self, memo):
        q = Project(self.path)
        q.cfg = copy.deepcopy(self.cfg, memo)
        q.dirty = self.dirty
        q.dcs = copy.deepcopy(self.dcs, memo) if self.dcs else None
        q._synced = dict(self._synced)
        return q

    # ------------------------------------------------------------------
    @property
    def name(self):
        if self.dcs:
            return self.dcs.name
        return self.cfg.get("name") or os.path.basename(self.path or "").replace(PROJECT_SUFFIX, "")

    @property
    def folder(self):
        return os.path.dirname(self.path) if self.path else ""

    @property
    def work_dir(self):
        return os.path.join(self.folder, "work")

    def wpath(self, *parts):
        p = os.path.join(self.work_dir, *parts)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        return p

    def run_frame(self):
        """The frame the trajectory is in, worked out from the settings of the run rather than stored:
        PPP gives ITRF2020 at the observation date; PPK takes the base position's frame; real time is the
        Certus solution in the frame of its RTK corrections."""
        c = self.cfg
        p = c["processing"]
        fr = c.get("frame") or {}
        if p.get("mode") == "rt":
            return {"name": "Certus real-time solution (frame of the RTK corrections)", "epoch": ""}
        if p.get("positioning") == "ppp":
            from . import geo
            span = self.rover_span()
            ep = geo.decimal_year(geo.gps_to_utc(span[0])) if span else None
            return {"name": "ITRF2020", "epoch": "%.3f" % ep if ep else ""}
        b = self.base() or {}
        return {"name": b.get("frame") or fr.get("name") or "NAD83(CSRS)",
                "epoch": (b.get("epoch") if b.get("frame") else fr.get("epoch")) or ""}

    def trajectory_frame(self):
        """The frame of the last processed trajectory (as it was run), else what the settings would give."""
        return (self.cfg.get("results") or {}).get("frame_used") or self.run_frame()

    # ------------------------------------------------------------------
    def base(self, bid=None):
        bid = bid or self.cfg.get("base_selected")
        for b in self.cfg["bases"]:
            if b.get("id") == bid:
                return b
        return None

    def add_base(self, entry):
        ids = {b.get("id") for b in self.cfg["bases"]}
        k = 1
        while "B%d" % k in ids:
            k += 1
        entry = dict(entry)
        entry["id"] = "B%d" % k
        entry.setdefault("use", True)
        self.cfg["bases"].append(entry)
        if not self.cfg.get("base_selected"):
            self.cfg["base_selected"] = entry["id"]
        self.dirty = True
        return entry

    def remove_base(self, bid):
        self.cfg["bases"] = [b for b in self.cfg["bases"] if b.get("id") != bid]
        if self.cfg.get("base_selected") == bid:
            self.cfg["base_selected"] = self.cfg["bases"][0]["id"] if self.cfg["bases"] else None
        self.dirty = True

    def imu_params(self):
        preset = self.cfg["imu"].get("preset", "certus_evo")
        p = dict(IMU_PRESETS.get(preset, IMU_PRESETS["certus_evo"]))
        p.update({k: v for k, v in self.cfg["imu"].get("custom", {}).items() if v is not None})
        p.pop("label", None)
        return p

    def capture_span(self):
        """(t0, t1) GPS seconds of the rover data: the raw IMU's span, else (captures without raw IMU,
        processed in the real-time mode) the span of the Certus real-time solution. None before reading."""
        ex = self.cfg["rover"].get("extract") or {}
        try:
            t0, t1 = float(ex.get("imu_t0")), float(ex.get("imu_t1"))
            if t1 > t0 > 0:
                return t0, t1
        except (TypeError, ValueError):
            pass
        if not ex:
            return None
        from . import results
        return results.rt_span(os.path.join(self.work_dir, "extract", "rt.bin"))

    def capture_span_source(self):
        ex = self.cfg["rover"].get("extract") or {}
        try:
            if float(ex.get("imu_t1")) > float(ex.get("imu_t0")) > 0:
                return "imu"
        except (TypeError, ValueError):
            pass
        return "rt" if self.capture_span() else None

    def rover_span(self):
        span = self.capture_span()
        if span is None:
            return None
        t0, t1 = span
        s = self.cfg["rover"].get("time_start") or t0
        e = self.cfg["rover"].get("time_end") or t1
        s, e = max(s, t0), min(e, t1)
        if e <= s:          # a crop outside the recording (or reversed): use the whole recording
            return t0, t1
        return s, e

    def approx_xyz(self):
        ex = self.cfg["rover"].get("extract") or {}
        try:
            v = [float(x) for x in ex.get("approx_xyz", "").split(",")]
            if len(v) == 3 and sum(q * q for q in v) > 1e12:
                return tuple(v)
        except ValueError:
            pass
        return None
