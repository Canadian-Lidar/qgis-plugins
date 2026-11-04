"""AlignPro project.

In the DCS chain AlignPro is stage 02 of a shared project (DCS Core):

    JobFolder/Processing/<Name>/<Name>.dcsproj
                                02_AlignPro/alignpro.json   AlignPro's own settings and results
                                02_AlignPro/work/           line caches, grids, screenshots (rebuildable)
                                02_AlignPro/out/            alignment.json + its data files, reports
                                02_AlignPro/log/            one log per run

It reads the captures recorded in the shared project, the trajectory PosPro published
(01_PosPro/out/trajectory.json) and the Lidar mounting from the shared hardware facts, and
publishes alignment.json for CloudPro. Older stand-alone <name>.alignpro.json projects still open
(self.dcs is None).

Everything the UI does goes through this class, so the whole calibration can also be run
headless (see tools/alignpro_cli.py)."""
import copy
import datetime as dt
import json
import math
import os

import numpy as np

from . import clraw, engine, geo, lines as lines_mod, trajectory

VERSION = "1.2.0"
STAGE_FILE = "alignpro.json"
PLUGIN = "AlignPro"
CONVENTION = {"name": "navaq_hesai", "matrix": "M = Rz(heading) Ry(pitch) Rx(-roll) Z0",
              "angles": "roll + starboard down, pitch + bow up, heading + clockwise (degrees)",
              "frame": "vehicle frame X forward, Y starboard, Z down, from the Certus reference point"}


class Project:
    def __init__(self, path=None):
        self.path = path
        self.cfg = {
            "name": "", "version": VERSION, "created": dt.datetime.utcnow().isoformat() + "Z",
            "files": [], "trajectory": {"type": "realtime", "path": "", "date": "", "leap": 18},
            "mount_source": "capture", "published": None,
            "settings": dict(engine.DEFAULT_SETTINGS),
            "mount_preset": None, "mount": None, "lever_preset": None, "lever": None,
            "latency_ms": 0.0, "line_use": {}, "aoi": None, "boxes": {}, "frame": None,
            "results": None, "report": {"vehicle": "", "operator": "", "client": "", "location": "",
                                        "notes": ""}, "log": [],
        }
        self.lines = []
        self.traj = None
        self.frame = None
        self.P = None
        self.sd = None
        self.sd_key = None
        self.pair_list = []
        self.warnings = []
        self.dcs = None
        self.traj_info = {}
        if path:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            st = dict(engine.DEFAULT_SETTINGS)
            st.update(data.get("settings", {}))
            self.cfg.update(data)
            self.cfg["settings"] = st

    # --------------------------------------------------------------- file
    @classmethod
    def create(cls, folder, name, files, traj=None):
        os.makedirs(folder, exist_ok=True)
        p = cls()
        p.path = os.path.join(folder, "%s.alignpro.json" % name)
        p.cfg["name"] = name
        p.cfg["files"] = [os.path.abspath(f) for f in files]
        if traj:
            p.cfg["trajectory"].update(traj)
        p.save()
        return p

    @classmethod
    def from_dcs(cls, dcs):
        """AlignPro's part of a shared DCS project (created when missing)."""
        path = os.path.join(dcs.stage_dir("alignpro"), STAGE_FILE)
        if os.path.exists(path):
            p = cls(path)
        else:
            p = cls()
            p.path = path
            p.cfg["name"] = dcs.name
            # PosPro's trajectory when it has published one, else the real-time one for a quick look
            p.cfg["trajectory"]["type"] = "pospro"
        p.dcs = dcs
        p.save()
        if not dcs.stage("alignpro").get("settings"):
            from dcs_core import files
            dcs.update_stage("alignpro", PLUGIN, settings=files.portable(path, dcs.folder), version=VERSION)
        return p

    @classmethod
    def open_any(cls, path):
        """A .dcsproj, a folder holding one, alignpro.json inside a DCS project, or an older
        stand-alone <name>.alignpro.json."""
        from dcs_core.project import SUFFIX, DcsProject
        if path.endswith(SUFFIX) or os.path.isdir(path):
            return cls.from_dcs(DcsProject.open(path))
        if os.path.basename(path) == STAGE_FILE:
            dcs = DcsProject.find_for(path)
            if dcs is not None:
                return cls.from_dcs(dcs)
        return cls(path)

    @property
    def folder(self):
        return os.path.dirname(self.path)

    @property
    def project_folder(self):
        """The folder the user thinks of as the project (the DCS project folder)."""
        return self.dcs.folder if self.dcs else self.folder

    @property
    def project_path(self):
        return self.dcs.path if self.dcs else self.path

    @property
    def out_dir(self):
        d = os.path.join(self.folder, "out")
        os.makedirs(d, exist_ok=True)
        return d

    @property
    def report_dir(self):
        return self.out_dir if self.dcs else self.folder

    @property
    def log_dir(self):
        d = os.path.join(self.folder, "log")
        os.makedirs(d, exist_ok=True)
        return d

    @property
    def work(self):
        d = os.path.join(self.folder, "work")
        os.makedirs(d, exist_ok=True)
        return d

    @property
    def name(self):
        return self.dcs.name if self.dcs else self.cfg["name"]

    @property
    def settings(self):
        return self.cfg["settings"]

    def save(self):
        d = copy.deepcopy(self.cfg)
        d["line_use"] = {l.name: l.use for l in self.lines} or d.get("line_use", {})
        text = json.dumps(d, indent=1, default=_jsonable)
        os.makedirs(self.folder, exist_ok=True)
        try:
            from dcs_core import files
            files.atomic_write_bytes(self.path, (text + "\n").encode("utf-8"), backup=True)
        except ImportError:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, self.path)

    def log(self, msg):
        line = "%s  %s" % (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
        self.cfg["log"].append(line)
        self.cfg["log"] = self.cfg["log"][-500:]
        run = getattr(self, "_run_log", None)
        if run:
            try:
                with open(run, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass

    def start_run_log(self, what):
        """log/<start time>_<what>.log, one per run (DCS layout)."""
        self._run_log = os.path.join(self.log_dir, "%s_%s.log" % (
            dt.datetime.now().strftime("%Y%m%d_%H%M%S"), what.replace(" ", "_")))
        return self._run_log

    # --------------------------------------------------------------- load
    def input_paths(self):
        """Where the .clraw files come from: the captures of the shared project (their folders, so
        a capture recorded by PosPro as .anpp still gives its .clraw), else the job folder, plus
        any files added in AlignPro."""
        out = []
        if self.dcs is not None:
            caps = self.dcs.captures()
            if caps:
                for c in caps:
                    out.append(c["path"])
                    out.extend(f for f in c["files"] if f.lower().endswith(".clraw"))
            else:
                out.append(self.dcs.job_folder)
        for f in self.cfg["files"]:
            if not os.path.isabs(f):
                f = os.path.join(self.folder, f)
            out.append(f)
        return out

    def resolve_files(self):
        files = clraw.find_clraw(self.input_paths())
        if self.dcs is not None:        # never read a stage's own output as raw data
            proc = os.path.abspath(self.dcs.folder)
            files = [f for f in files if not os.path.abspath(f).startswith(proc + os.sep)
                     and os.sep + "Processing" + os.sep not in os.path.abspath(f)]
        return files

    def load(self, progress=None, cancelled=None):
        """Read all lines (cached), build the trajectory, split passes, georeference."""
        self.warnings = []
        files = self.resolve_files()
        if not files:
            raise ValueError("No .clraw files found in the project")
        groups = clraw.group_lines(files)
        datas = []
        n = len(groups)
        for gi, (key, cfs) in enumerate(groups.items()):
            def prog(fr, msg, gi=gi):
                if progress:
                    progress((gi + fr) / n * 0.7, msg)
            datas.append(lines_mod.read_line(cfs, self.settings, self.work, prog, cancelled))
        hw = datas[0]["info"]["hardware"] or {}
        if self.dcs is not None and self.dcs.shared("hardware"):
            hw = dict(hw)
            hw.update(self.dcs.shared("hardware") or {})
        mp = hw.get("lidar_mount_navaq_deg") or {}
        preset = [float(mp.get("roll", 0)), float(mp.get("pitch", 0)), float(mp.get("heading", 0))]
        lever = [float(v) for v in hw.get("ins_to_lidar_m", [0, 0, 0])]
        lm = self.dcs.shared("lidar_mounting") if self.dcs is not None else None
        if lm and lm.get("mount_deg"):
            # the starting mounting entered for this job (shared fact owned by AlignPro)
            preset = [float(v) for v in lm["mount_deg"]]
            self.cfg["mount_source"] = lm.get("source", "entered")
        for d in datas[1:]:
            h2 = d["info"]["hardware"] or {}
            if h2.get("lidar_mount_navaq_deg") != hw.get("lidar_mount_navaq_deg") or \
                    h2.get("ins_to_lidar_m") != hw.get("ins_to_lidar_m"):
                self.warnings.append("Line %03d was recorded with a different Lidar mounting or lever arm "
                                     "than the first line; the first line's values are used for all."
                                     % d["info"]["line"])
        if self.cfg["mount_preset"] is None:
            self.cfg["mount_preset"] = preset
        if self.cfg["mount"] is None:
            self.cfg["mount"] = list(preset)
        if self.cfg["lever_preset"] is None:
            self.cfg["lever_preset"] = lever
        if self.cfg["lever"] is None:
            self.cfg["lever"] = list(lever)
        if lever == [0.0, 0.0, 0.0]:
            self.warnings.append("The Lidar lever arm recorded in the capture is 0, 0, 0. Enter the measured "
                                 "lever arm on the Installation page before calibrating.")
        if self.dcs is not None and not self.dcs.shared("lidar_mounting"):
            self.share_mounting(preset, "capture")
        for d in datas:
            st = d["info"]["status"]
            if st["count"] and st["pps_locked"] < st["count"]:
                self.warnings.append("Line %03d: Lidar PPS lock lost in %d of %d status packets."
                                     % (d["info"]["line"], st["count"] - st["pps_locked"], st["count"]))
            if d["info"].get("bad_chunks"):
                self.warnings.append("Line %03d: %d damaged chunk(s) skipped." % (d["info"]["line"],
                                                                                  d["info"]["bad_chunks"]))
        self.datas = datas
        if progress:
            progress(0.72, "Trajectory")
        self.build_trajectory()
        self.make_lines()
        if progress:
            progress(0.8, "Georeferencing")
        self.georeference()
        if progress:
            progress(0.9, "Planar patches and pairs")
        self.analyse()
        if progress:
            progress(1.0, "Ready")
        self.save()

    def build_trajectory(self):
        tcfg = self.cfg["trajectory"]
        rt = None
        nav = {}
        for d in self.datas:
            for k, v in d["nav"].items():
                nav.setdefault(k, []).append(v)
        nav = {k: np.concatenate(v) for k, v in nav.items()}
        if len(nav.get("t", [])):
            rt = trajectory.from_nav(nav)
        self.traj_rt = rt
        self.traj_info = {"source": "realtime", "handover_id": None}
        typ = tcfg.get("type")
        if typ == "pospro":
            ho = self.pospro_trajectory()
            if ho is not None:
                t = ho.get("time") or {}
                self.traj = trajectory.read_sbet(ho["paths"]["sbet"], leap=int(t.get("leap_seconds", 18)),
                                                 week=t.get("week"), near_unix=self.datas[0]["info"].get("t_start"))
                self.traj.source = "PosPro trajectory (%s)" % ((ho.get("frame") or {}).get("name") or "trajectory.json")
                self.traj_info = {"source": "pospro", "handover_id": ho.get("id"), "frame": ho.get("frame"),
                                  "quality": ho.get("quality"), "path": ho.get("handover_path")}
                for d in self.datas:
                    i = d["info"]
                    if i["t_start"] and not self.traj.covers(i["t_start"], i["t_stop"]):
                        self.warnings.append("The PosPro trajectory does not cover line %03d; process that part "
                                             "in PosPro." % i["line"])
                st, why = self.dcs.state("pospro")
                if st == "stale":
                    self.warnings.append("PosPro's trajectory is out of date (%s); re-run PosPro, then AlignPro."
                                         % "; ".join(why))
            else:
                typ = "realtime"
                self.warnings.append("PosPro has not published a trajectory for this project yet: using the Certus "
                                     "real-time trajectory for a quick look. Run PosPro before the final "
                                     "calibration.")
        if typ == "pospro":
            pass
        elif typ == "sbet" and tcfg.get("path"):
            date = None
            if tcfg.get("date"):
                date = dt.date.fromisoformat(tcfg["date"])
            near = self.datas[0]["info"].get("t_start")
            self.traj = trajectory.read_sbet(tcfg["path"], date, int(tcfg.get("leap", 18)), near_unix=near)
            self.traj_info = {"source": "sbet", "handover_id": None, "path": tcfg["path"]}
            for d in self.datas:
                i = d["info"]
                if i["t_start"] and not self.traj.covers(i["t_start"], i["t_stop"]):
                    self.warnings.append("SBET does not cover line %03d; check the SBET date and file."
                                         % i["line"])
        else:
            if rt is None:
                raise ValueError("No Certus packet 20 in the .clraw files")
            self.traj = rt
            if rt.fix is not None and np.mean(rt.fix == 7) < 0.95:
                self.warnings.append("Real-time trajectory is RTK fixed only %.0f%% of the time; use the "
                                     "PosPro trajectory for the patch test." % (100 * np.mean(rt.fix == 7)))
        if self.cfg["frame"] is None:
            ts = [d["info"]["t_start"] for d in self.datas if d["info"]["t_start"]]
            te = [d["info"]["t_stop"] for d in self.datas if d["info"]["t_stop"]]
            tm = np.array(ts + te)
            lat, lon, h, *_ = self.traj.interp(tm if len(tm) else self.traj.t[:1])
            self.cfg["frame"] = {"lat": float(np.degrees(np.mean(lat))), "lon": float(np.degrees(np.mean(lon))),
                                 "h": float(np.mean(h))}
        f = self.cfg["frame"]
        self.frame = geo.LocalFrame(f["lat"], f["lon"], f["h"])

    def pospro_trajectory(self):
        """PosPro's published trajectory.json (with absolute paths), or None."""
        if self.dcs is None:
            return None
        from dcs_core import handover
        try:
            self.dcs.reload()
            return handover.read(self.dcs, "pospro", required=False)
        except handover.HandoverError as e:
            self.warnings.append(str(e))
            return None

    def share_mounting(self, mount, source):
        """The starting Lidar mounting for this job, as a shared fact (AlignPro owns it). The solved
        mounting and the corrected lever arm go to alignment.json only."""
        if self.dcs is None:
            return False
        v = {"mount_deg": [float(x) for x in mount], "convention": CONVENTION["name"], "source": source}
        return self.dcs.set_shared("lidar_mounting", v, PLUGIN)

    def make_lines(self):
        self.lines = []
        use = self.cfg.get("line_use", {})
        for d in self.datas:
            i = d["info"]
            t0, t1 = i["t_start"], i["t_stop"]
            if t0 is None:
                continue
            wins = engine.split_passes(self.traj, t0, t1, self.settings)
            for k, w in enumerate(wins):
                name = "%03d" % i["line"] + ("" if len(wins) == 1 else "abcdefghijklmnopqrstuvwxyz"[k % 26])
                ln = engine.Line(name, d, w, len(self.lines))
                ln.use = bool(use.get(name, True))
                self.lines.append(ln)

    # --------------------------------------------------------- georeference
    @property
    def M(self):
        return geo.lidar_matrix(*self.cfg["mount"])

    def georeference(self, mount=None, lever=None, latency_ms=None):
        mount = mount or self.cfg["mount"]
        lever = lever if lever is not None else self.cfg["lever"]
        lat_s = (self.cfg["latency_ms"] if latency_ms is None else latency_ms) * 1e-3
        M = geo.lidar_matrix(*mount)
        for ln in self.lines:
            t = ln.data["t"][ln.idx]
            if not len(t):
                ln.x = np.zeros((0, 3))
                continue
            inside = (t >= self.traj.t[0]) & (t <= self.traj.t[-1])
            pos, C = engine.pose(self.traj, self.frame, t, lat_s)
            q = ln.data["p"][ln.idx].astype(float) @ M.T
            ln.x = engine.georef(pos, C, q, lever)
            ln.x[~inside] = np.nan
            if (~inside).any():
                keep = inside
                ln.idx = ln.idx[keep]
                ln.x = ln.x[keep]
            engine.line_stats(ln, self.traj, self.frame, self.settings)
        self.sd = None

    def aoi_func(self):
        poly = self.cfg.get("aoi")
        if not poly or not self.settings.get("use_aoi"):
            return None
        pts = np.array(poly, float)
        lat, lon = np.radians(pts[:, 1]), np.radians(pts[:, 0])
        xy = self.frame.from_geodetic(lat, lon, np.zeros(len(pts)))[:, :2]

        def inside(q):
            x, y = q[:, 0], q[:, 1]
            res = np.zeros(len(q), bool)
            j = len(xy) - 1
            for i in range(len(xy)):
                xi, yi = xy[i]
                xj, yj = xy[j]
                c = ((yi > y) != (yj > y)) & (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi)
                res ^= c
                j = i
            return res
        return inside

    def analyse(self):
        """Patches, pairs and inspection boxes for the current georeference."""
        self.P = engine.build_patches(self.lines, self.settings, self.aoi_func())
        self.pair_list = engine.pairs(self.lines, self.P, self.settings)
        if not self.cfg.get("boxes"):
            self.cfg["boxes"] = engine.auto_boxes(self.lines, self.P)
        self.sd = None

    def solve_data(self):
        key = (tuple(self.cfg["mount"]), self.cfg["latency_ms"], id(self.P))
        if self.sd is None or self.sd_key != key:
            self.sd = engine.SolveData(self.lines, self.P, self.traj, self.frame, self.M,
                                       self.cfg["latency_ms"] * 1e-3)
            self.sd_key = key
        return self.sd

    # ----------------------------------------------------------- calibrate
    def calibrate(self, progress=None, cancelled=None):
        """Auto calibration: all-lines solve + leave-one-line-out combinations."""
        if self.P is None or len(self.P) < 6:
            raise ValueError("Not enough planar surfaces seen by two or more lines (%d). Check that the "
                             "passes overlap, the range gate and the area of interest."
                             % (0 if self.P is None else len(self.P)))
        sd = self.solve_data()
        used = np.array([l.use for l in self.lines])
        nl = len(self.lines)
        mount = list(self.cfg["mount"])
        M = self.M
        combis = [("All lines", used.copy())]
        uidx = np.nonzero(used)[0]
        if len(uidx) >= 3:
            for i in uidx:
                m = used.copy()
                m[i] = False
                combis.append(("Without %s" % self.lines[i].name, m))
        rows = []
        main = None
        for ci, (label, m) in enumerate(combis):
            if cancelled and cancelled():
                break
            if progress:
                progress(ci / len(combis), "Combination %d of %d" % (ci + 1, len(combis)))
            r = engine.solve(sd, self.cfg["lever"], self.settings, m)
            if r is None:
                rows.append({"label": label, "ok": False})
                continue
            ang = geo.navaq_from_lidar_matrix(r["B"] @ M, near=mount)
            row = {"label": label, "ok": True, "lines": [self.lines[i].name for i in np.nonzero(m)[0]],
                   "roll": ang[0], "pitch": ang[1], "heading": ang[2],
                   "d_roll": ang[0] - mount[0], "d_pitch": ang[1] - mount[1], "d_heading": ang[2] - mount[2],
                   "bore": engine.bore_angles(r["B"]), "before_cm": r["before"] * 100,
                   "after_cm": r["after"] * 100, "rms_after_cm": r["rms_after"] * 100,
                   "formal_sd": r["formal_sd_deg"], "points": r["points"], "patches": r["patches"],
                   "rejected": r["rejected"], "iterations": r["iterations"],
                   "lever": [float(v) for v in r["lever"]]}
            rows.append(row)
            if ci == 0:
                main = r
        if main is None:
            raise ValueError("The calibration did not converge (too few common surfaces)")
        ok = [r for r in rows[1:] if r.get("ok")]
        adv = {k: rows[0][k] for k in ("roll", "pitch", "heading")}
        sdev = {}
        for k in ("roll", "pitch", "heading"):
            v = np.array([r[k] for r in ok])
            if len(v) >= 2:
                sdev[k] = float(math.sqrt((len(v) - 1) / len(v) * np.sum((v - v.mean()) ** 2)))
            else:
                sdev[k] = rows[0]["formal_sd"][["roll", "pitch", "heading"].index(k)]
        bore = list(rows[0]["bore"])
        bv = np.array([r["bore"] for r in ok]) if ok else np.zeros((0, 3))
        if len(bv) >= 2:
            bore_sd = [float(math.sqrt((len(bv) - 1) / len(bv) * np.sum((bv[:, k] - bv[:, k].mean()) ** 2)))
                       for k in range(3)]
        else:
            bore_sd = list(rows[0]["formal_sd"])
        pl = engine.pair_errors(sd, main, [dict(p) for p in self.pair_list], "e_before")
        pl = engine.pair_errors(sd, main, pl, "e_after")
        for q in pl:
            for k in ("e_before", "e_after"):
                if q.get(k) is not None:
                    q[k] *= 100.0          # cm
        le_b = engine.line_errors(sd, main, nl, "e_before")
        le_a = engine.line_errors(sd, main, nl, "e_after")
        kinds = {}
        for k, nm in ((0, "flat"), (1, "sloped"), (2, "walls")):
            kinds[nm] = int(np.sum(self.P.kind == k))
        warn = []
        hd_in = mount[2]
        if abs(hd_in) > 0.5 and abs(adv["heading"] + hd_in) < 0.25 * abs(hd_in):
            warn.append("The solved heading is close to minus the entered heading. The heading sign of the "
                        "Lidar mounting convention has not been confirmed; check it before applying.")
        if abs(abs(mount[1]) - 90.0) < 5.0:
            warn.append("The mounting pitch is near 90 deg, where the mounting convention is singular: roll and heading "
                        "trade against each other (only their sum or difference is defined), so they can move a lot "
                        "for a tiny correction. The three angles still give the right rotation - enter all three "
                        "exactly. Judge the quality on the boresight correction and its SDEV.")
            sdev_chk = bore_sd
        else:
            sdev_chk = list(sdev.values())
        if max(sdev_chk) > 0.05:
            warn.append("Combination spread above 0.05 deg: check the line table, pairs and slice views.")
        if kinds["walls"] < 20:
            warn.append("Few vertical surfaces (%d): heading is weakly determined." % kinds["walls"])
        self.cfg["results"] = {
            "time": dt.datetime.utcnow().isoformat() + "Z", "mount_in": mount,
            "lever": list(self.cfg["lever"]), "latency_ms": self.cfg["latency_ms"],
            "trajectory": self.traj.source, "trajectory_info": dict(self.traj_info),
            "advised": adv, "sdev": sdev, "combis": rows,
            "bore": bore, "bore_sdev": bore_sd,
            "pairs": [{k: v for k, v in p.items()} for p in pl],
            "line_err_before_cm": [None if v is None else v * 100 for v in le_b],
            "line_err_after_cm": [None if v is None else v * 100 for v in le_a],
            "lines": [l.name for l in self.lines], "used": [bool(l.use) for l in self.lines],
            "patches": kinds, "warnings": warn,
            "error_before_cm": rows[0]["before_cm"], "error_after_cm": rows[0]["after_cm"],
        }
        self.log("Calibration: advised %.4f %.4f %.4f (sdev %.4f %.4f %.4f), error %.2f -> %.2f cm" % (
            adv["roll"], adv["pitch"], adv["heading"], sdev["roll"], sdev["pitch"], sdev["heading"],
            rows[0]["before_cm"], rows[0]["after_cm"]))
        self.save()
        if progress:
            progress(1.0, "Done")
        return self.cfg["results"]

    def apply_mount(self, angles, reanalyse=False):
        self.cfg["mount"] = [float(a) for a in angles]
        self.log("Applied mounting %.4f %.4f %.4f" % tuple(angles))
        self.georeference()
        if reanalyse:
            self.analyse()
        self.save()

    # ------------------------------------------------------------- publish
    def publish(self):
        """Publish the applied mounting for CloudPro: alignment.json (dcs.alignment/1) with its data
        files (ASCII report and combinations table) in 02_AlignPro/out. The caller holds the stage
        run (DcsProject.run). Returns the handover dict."""
        if self.dcs is None:
            raise ValueError("Publishing needs a DCS project (this is an older stand-alone AlignPro project)")
        from dcs_core import handover
        from . import report
        c = self.cfg
        r = c.get("results") or {}
        od = self.out_dir
        mount = [float(v) for v in c["mount"]]
        adv = r.get("advised")
        manual = not (adv and np.allclose(mount, [adv[k] for k in ("roll", "pitch", "heading")], atol=1e-7))
        sd = r.get("sdev") or {}
        err_now = self.current_error()
        err_rec = self.current_error(c["mount_preset"])
        used = [l.name for l in self.lines if l.use]
        rep = os.path.join(od, "alignment_report.txt")
        com = os.path.join(od, "alignment_combinations.csv")
        from dcs_core import files
        files.atomic_write_bytes(rep, report.ascii_report(self).encode("utf-8"))
        rows = ["combination,lines,roll_deg,pitch_deg,heading_deg,error_before_cm,error_after_cm,points,patches"]
        for row in r.get("combis", []):
            if row.get("ok"):
                rows.append("%s,%s,%.6f,%.6f,%.6f,%.3f,%.3f,%d,%d" % (
                    row["label"], " ".join(row["lines"]), row["roll"], row["pitch"], row["heading"],
                    row["before_cm"], row["after_cm"], row["points"], row["patches"]))
        files.atomic_write_bytes(com, ("\n".join(rows) + "\n").encode("utf-8"))
        lever = [float(v) for v in c["lever"]]
        lev_src = "capture" if np.allclose(lever, c["lever_preset"]) else (
            "solved" if (r.get("combis") and r["combis"][0].get("lever") and
                         np.allclose(lever, r["combis"][0]["lever"]) and self.settings.get("solve_lever"))
            else "measured")
        bore = geo.boresight_from_mounts(geo.lidar_matrix(*c["mount_preset"]), geo.lidar_matrix(*mount))
        fields = {
            "convention": dict(CONVENTION),
            "mount_deg": {"roll": mount[0], "pitch": mount[1], "heading": mount[2],
                          "sd": {k: sd.get(k) for k in ("roll", "pitch", "heading")} if sd else None,
                          "source": "manual adjustment" if manual else "calibration"},
            "mount_recorded_deg": {"roll": c["mount_preset"][0], "pitch": c["mount_preset"][1],
                                   "heading": c["mount_preset"][2], "source": c.get("mount_source", "capture")},
            "lidar_lever_m": {"value": lever, "sd": (r.get("combis") or [{}])[0].get("formal_sd_lever"),
                              "source": lev_src, "recorded": list(c["lever_preset"])},
            "latency_s": float(c["latency_ms"]) / 1000.0,
            "boresight_deg": {"roll": bore[0], "pitch": bore[1], "heading": bore[2],
                              "relative_to": "mounting recorded in the capture",
                              "sd": r.get("bore_sdev")},
            "quality": {"avg_error_cm": err_now, "avg_error_recorded_cm": err_rec,
                        "sdev_deg": sd or None, "lines_used": used, "lines_total": len(self.lines),
                        "patches": r.get("patches"), "warnings": r.get("warnings", [])},
            "trajectory": {"source": self.traj_info.get("source"), "handover_id": self.traj_info.get("handover_id"),
                           "description": self.traj.source if self.traj is not None else ""},
        }
        doc = handover.publish(self.dcs, "alignpro", {"report": rep, "combinations": com}, fields, PLUGIN, VERSION,
                               "point-to-plane boresight")
        c["published"] = {"time": doc["created"], "id": doc["id"], "mount": mount, "lever": lever,
                          "latency_ms": float(c["latency_ms"])}
        self.log("Published alignment.json: %.4f %.4f %.4f (%s)" % (mount[0], mount[1], mount[2], doc["id"][:19]))
        self.save()
        return doc

    def published_state(self):
        """(state, reasons, differs): the chain state of AlignPro and whether the applied values
        differ from what was last published."""
        if self.dcs is None:
            return "none", [], False
        try:
            self.dcs.reload()
        except Exception:  # noqa: BLE001
            pass
        st, why = self.dcs.state("alignpro")
        pub = self.cfg.get("published")
        differs = bool(pub) and not (np.allclose(pub["mount"], self.cfg["mount"], atol=1e-9) and
                                     np.allclose(pub["lever"], self.cfg["lever"], atol=1e-9) and
                                     abs(pub["latency_ms"] - self.cfg["latency_ms"]) < 1e-9)
        return st, why, differs

    def current_error(self, mount=None):
        """AvgERROR (cm) and per-line errors for the given/current mounting, planes re-fitted."""
        if self.P is None or not len(self.P):
            return None
        sd = self.solve_data()
        mount = mount or self.cfg["mount"]
        B = geo.lidar_matrix(*mount) @ self.M.T
        used = np.array([l.use for l in self.lines])
        e, m = engine.residuals(sd, B, self.cfg["lever"], used)
        w = engine.patch_weights(sd.patch, m, len(self.P))
        return engine.error_metric(e[m], w[m]) * 100

    # ------------------------------------------------------------- analyze
    PARAMS = {"latency_ms": "Lidar latency (ms)", "lever_f": "Lever forward (m)",
              "lever_s": "Lever starboard (m)", "lever_d": "Lever down (m)",
              "roll": "Roll (deg)", "pitch": "Pitch (deg)", "heading": "Heading (deg)"}

    def sweep(self, param, center, half_range, steps, progress=None, cancelled=None):
        """Error (cm) against one parameter with everything else fixed. Planes re-fitted."""
        vals = np.linspace(center - half_range, center + half_range, int(steps))
        used = np.array([l.use for l in self.lines])
        out = []
        base_sd = self.solve_data()
        for k, v in enumerate(vals):
            if cancelled and cancelled():
                break
            mount = list(self.cfg["mount"])
            lever = list(self.cfg["lever"])
            sd = base_sd
            if param == "latency_ms":
                sd = engine.SolveData(self.lines, self.P, self.traj, self.frame, self.M, v * 1e-3)
            elif param.startswith("lever_"):
                lever["fsd".index(param[-1])] = v
            else:
                mount[("roll", "pitch", "heading").index(param)] = v
            B = geo.lidar_matrix(*mount) @ self.M.T
            e, m = engine.residuals(sd, B, lever, used)
            w = engine.patch_weights(sd.patch, m, len(self.P))
            out.append((float(v), engine.error_metric(e[m], w[m]) * 100))
            if progress:
                progress((k + 1) / len(vals), "%s = %.4f" % (self.PARAMS[param], v))
        return out

    def current_value(self, param):
        if param == "latency_ms":
            return float(self.cfg["latency_ms"])
        if param.startswith("lever_"):
            return float(self.cfg["lever"]["fsd".index(param[-1])])
        return float(self.cfg["mount"][("roll", "pitch", "heading").index(param)])

    def set_value(self, param, v):
        if param == "latency_ms":
            self.cfg["latency_ms"] = float(v)
        elif param.startswith("lever_"):
            self.cfg["lever"]["fsd".index(param[-1])] = float(v)
        else:
            self.cfg["mount"][("roll", "pitch", "heading").index(param)] = float(v)
        self.log("Set %s = %.5f" % (self.PARAMS[param], v))
        self.georeference()
        self.save()

    def reset(self):
        self.cfg["mount"] = list(self.cfg["mount_preset"])
        self.cfg["lever"] = list(self.cfg["lever_preset"])
        self.cfg["latency_ms"] = 0.0
        self.cfg["boxes"] = {}
        self.cfg["results"] = None
        for l in self.lines:
            l.use = True
        self.log("Project reset to the values recorded in the .clraw files")
        self.georeference()
        self.analyse()
        self.save()

    # ------------------------------------------------------------- slices
    def slice(self, box, mount=None, line_mask=None, max_per_line=40000):
        """Points inside an inspection box, optionally re-georeferenced with other angles.
        -> [(line index, along, across, z)]"""
        if not box:
            return []
        mount = list(mount) if mount else list(self.cfg["mount"])
        same = np.allclose(mount, self.cfg["mount"], atol=1e-9)
        wide = dict(box)
        wide["width"] = box["width"] + (0 if same else 2.0)
        if not same:
            wide["zmin"] = None
        rng = np.random.default_rng(5)
        p0, p1 = np.array(box["p0"], float), np.array(box["p1"], float)
        L = float(np.linalg.norm(p1 - p0))
        if L < 1e-3:
            return []
        u = (p1 - p0) / L
        v = np.array([-u[1], u[0]])
        M_new = geo.lidar_matrix(*mount)
        lat_s = self.cfg["latency_ms"] * 1e-3
        out = []
        for i, ln in enumerate(self.lines):
            if not ln.use or ln.x is None or not len(ln.x):
                continue
            if line_mask is not None and not line_mask[i]:
                continue
            rel = ln.x[:, :2] - p0
            al, ac = rel @ u, rel @ v
            m = (al >= -1) & (al <= L + 1) & (np.abs(ac) <= wide["width"] / 2)
            idx = np.nonzero(m)[0]
            if not len(idx):
                continue
            if same:
                x = ln.x[idx]
            else:
                di = ln.idx[idx]
                pos, C = engine.pose(self.traj, self.frame, ln.data["t"][di], lat_s)
                x = engine.georef(pos, C, ln.data["p"][di].astype(float) @ M_new.T, self.cfg["lever"])
            rel = x[:, :2] - p0
            al, ac = rel @ u, rel @ v
            k = (al >= 0) & (al <= L) & (np.abs(ac) <= box["width"] / 2)
            if box.get("zmin") is not None:
                k &= (x[:, 2] >= box["zmin"]) & (x[:, 2] <= box["zmax"])
            sel = np.nonzero(k)[0]
            if len(sel) > max_per_line:
                sel = rng.choice(sel, max_per_line, replace=False)
            out.append((i, al[sel], ac[sel], x[sel, 2]))
        return out

    # ------------------------------------------------------------- live view
    def view_center(self):
        """A good default centre for the data view: the calibration boxes, else the wall patches."""
        boxes = self.cfg.get("boxes") or {}
        pts = [np.array(b["p0"]) for b in boxes.values()] + [np.array(b["p1"]) for b in boxes.values()]
        if pts:
            return np.mean(pts, axis=0)
        if self.P is not None and len(self.P):
            w = self.P.center[self.P.kind == 2]
            return (w if len(w) else self.P.center)[:, :2].mean(0)
        tr = np.concatenate([l.stats["track"][:, :2] for l in self.lines if l.stats.get("track") is not None])
        return tr.mean(0)

    def view_subset(self, center, half, max_pts=400000, used_only=True):
        """Points of every line inside a square (local ENU) for the live view. Keeps the raw sensor
        vectors so the view can be re-georeferenced with any angles, lever arm or latency."""
        cx, cy = float(center[0]), float(center[1])
        rng = np.random.default_rng(11)
        parts = []
        for i, ln in enumerate(self.lines):
            if (used_only and not ln.use) or ln.x is None or not len(ln.x):
                continue
            m = (np.abs(ln.x[:, 0] - cx) <= half) & (np.abs(ln.x[:, 1] - cy) <= half)
            idx = np.nonzero(m)[0]
            parts.append((i, idx))
        tot = sum(len(ix) for _, ix in parts)
        frac = min(1.0, max_pts / max(tot, 1))
        line, t, p, inten = [], [], [], []
        for i, idx in parts:
            if frac < 1.0:
                idx = np.sort(rng.choice(idx, int(len(idx) * frac), replace=False))
            ln = self.lines[i]
            di = ln.idx[idx]
            line.append(np.full(len(di), i, np.int16))
            t.append(ln.data["t"][di])
            p.append(ln.data["p"][di].astype(np.float32))
            inten.append(ln.data["i"][di])
        if not line:
            return None
        sub = {"line": np.concatenate(line), "t": np.concatenate(t), "p": np.concatenate(p),
               "i": np.concatenate(inten), "center": (cx, cy), "half": half, "poses": {}}
        order = rng.permutation(len(sub["t"]))            # mixed draw order so no line hides another
        for k in ("line", "t", "p", "i"):
            sub[k] = sub[k][order]
        return sub

    def georef_subset(self, sub, mount, lever, latency_ms):
        key = round(float(latency_ms), 6)
        if key not in sub["poses"]:
            if len(sub["poses"]) > 4:
                sub["poses"].clear()
            pos, C = engine.pose(self.traj, self.frame, sub["t"], key * 1e-3)
            sub["poses"][key] = (pos, C.astype(np.float32))
        pos, C = sub["poses"][key]
        M = geo.lidar_matrix(*mount).astype(np.float32)
        a = sub["p"] @ M.T + np.asarray(lever, np.float32)
        return pos + np.einsum("nij,nj->ni", C, a)

    def live_error(self, mount, lever, latency_ms):
        """AvgERROR (cm) for any trial values, planes re-fitted. Poses cached per latency."""
        if self.P is None or not len(self.P):
            return None
        cache = getattr(self, "_live_sd", {})
        key = (id(self.P), round(float(latency_ms), 6), tuple(self.cfg["mount"]))
        if key not in cache:
            cache = {key: engine.SolveData(self.lines, self.P, self.traj, self.frame, self.M, latency_ms * 1e-3)}
            self._live_sd = cache
        sd = cache[key]
        B = geo.lidar_matrix(*mount) @ self.M.T
        used = np.array([l.use for l in self.lines])
        e, m = engine.residuals(sd, B, lever, used)
        w = engine.patch_weights(sd.patch, m, len(self.P))
        return engine.error_metric(e[m], w[m]) * 100

    def ground_level(self, center, half=30.0):
        zs = []
        for ln in self.lines:
            if ln.x is None or not len(ln.x):
                continue
            m = (np.abs(ln.x[:, 0] - center[0]) <= half) & (np.abs(ln.x[:, 1] - center[1]) <= half)
            if m.any():
                zs.append(ln.x[m, 2][::max(1, m.sum() // 20000)])
        return float(np.percentile(np.concatenate(zs), 5)) if zs else 0.0

    # ------------------------------------------------------------- export
    def points_geodetic(self, line, max_pts=None):
        x = line.x
        if max_pts and len(x) > max_pts:
            x = x[np.random.default_rng(0).choice(len(x), max_pts, replace=False)]
        lat, lon, h = self.frame.to_geodetic(x)
        return np.degrees(lat), np.degrees(lon), h


def _jsonable(o):
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    raise TypeError(type(o))
