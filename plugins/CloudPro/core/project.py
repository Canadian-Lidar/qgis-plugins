"""CloudPro project.

In the DCS chain (the normal case) CloudPro is stage 03 of the job's shared
project (DCS Core):

    Processing/<Name>/<Name>.dcsproj
                      03_CloudPro/cloudpro.json      CloudPro's own settings (lines, view, history)
                      03_CloudPro/filters/           filter groups
                      03_CloudPro/work/store/        tiled point store (rebuildable cache)
                      03_CloudPro/work/lines/        sensor tracks per line
                      03_CloudPro/out/               delivered LAS / grids + cloud.json
                      03_CloudPro/log/               one log per run

An older stand-alone project (<name>.cloudpro.json with store/ beside it)
still opens, without the chain (dcs = None). No Qt here.
"""
import datetime as _dt
import json
import os

import numpy as np

from .store import Store

VERSION = 2
PLUGIN = "CloudPro"
STAGE = "cloudpro"
SETTINGS = "cloudpro.json"
LINE_COLOURS = ["#00e5aa", "#3ab4f2", "#9b7bff", "#f5a623", "#e84855", "#7bd389", "#ff8fab", "#4dd0e1",
                "#ffd166", "#c792ea", "#80cbc4", "#f78c6c"]


def _utc():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _atomic_json(path, obj):
    try:
        from dcs_core import files
        files.atomic_write_json(path, obj, backup=True)
        return
    except ImportError:
        pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1)
    if os.path.exists(path):
        try:
            os.replace(path, path + ".bak")
        except OSError:
            pass
    os.replace(tmp, path)


def crs_from_dcs(dcs):
    """Projected CRS for the job: the shared crs fact, else the capture's
    hardware record, else NB Stereographic."""
    c = dcs.shared("crs") or {}
    h = (c.get("horizontal") or "").strip() if isinstance(c, dict) else ""
    if h:
        return h
    hw = dcs.shared("hardware") or {}
    cs = hw.get("coordinate_system") if isinstance(hw, dict) else None
    if isinstance(cs, dict) and cs.get("definition"):
        return cs["definition"]
    return "EPSG:2953"


class Project:
    def __init__(self, path, dcs=None):
        """path = the settings file (03_CloudPro/cloudpro.json or <name>.cloudpro.json)."""
        self.path = os.path.abspath(path)
        self.root = os.path.dirname(self.path)
        self.dcs = dcs
        self.data = {}
        self.store = None

    # ------------------------------------------------------------- folders
    @property
    def work(self):
        return os.path.join(self.root, "work") if self.dcs is not None else self.root

    @property
    def out_dir(self):
        d = os.path.join(self.root, "out")
        os.makedirs(d, exist_ok=True)
        return d

    @property
    def log_dir(self):
        d = os.path.join(self.root, "log")
        os.makedirs(d, exist_ok=True)
        return d

    @property
    def filters_dir(self):
        return os.path.join(self.root, "filters")

    def _make_dirs(self):
        for d in (self.work, os.path.join(self.work, "lines"), self.filters_dir):
            os.makedirs(d, exist_ok=True)

    def _open_store(self):
        self.store = Store(os.path.join(self.work, "store"), tile=self.data.get("tile", 50.0),
                           crs=self.data.get("crs", ""))
        # a crash or full disk during an edit or import must not surface later as odd points: check the tiles
        # against the index on every open (file sizes only, milliseconds) and repair what can be repaired
        try:
            self.store_findings = self.store.check(repair=True)
        except OSError as e:
            self.store_findings = ["the point store could not be checked: %s" % e]
        if self.store_findings:
            try:
                self.log("Point store repaired on opening: " + "; ".join(self.store_findings))
            except Exception:       # noqa: BLE001  (logging must not stop the opening)
                pass

    # ------------------------------------------------------------- create / open
    @classmethod
    def from_dcs(cls, dcs, tile=50.0):
        """CloudPro's stage of a DCS project (created on first use)."""
        root = dcs.stage_dir(STAGE)
        p = cls(os.path.join(root, SETTINGS), dcs)
        if os.path.exists(p.path):
            with open(p.path, encoding="utf-8") as f:
                p.data = json.load(f)
        else:
            p.data = {"version": VERSION, "name": dcs.name, "crs": crs_from_dcs(dcs),
                      "created": _utc(), "tile": tile, "lines": [], "inputs": {}, "geoid": "",
                      "vertical": "ellipsoidal", "bookmarks": [], "view": {}, "history": [], "filter_runs": []}
        p.data["name"] = dcs.name
        p._make_dirs()
        p._open_store()
        if not os.path.exists(p.path):
            p.save()
        return p

    @classmethod
    def create(cls, folder, name, crs="EPSG:2953", tile=50.0):
        """Stand-alone project (no DCS chain); kept for the command line and tests."""
        root = os.path.join(folder, name)
        os.makedirs(root, exist_ok=True)
        p = cls(os.path.join(root, name + ".cloudpro.json"))
        p.data = {"version": VERSION, "name": name, "crs": crs, "created": _utc(), "tile": tile, "lines": [],
                  "inputs": {}, "geoid": "", "vertical": "ellipsoidal", "bookmarks": [], "view": {}, "history": [],
                  "filter_runs": []}
        p._make_dirs()
        p._open_store()
        p.save()
        return p

    @classmethod
    def open(cls, path):
        return cls.open_any(path)

    @classmethod
    def open_any(cls, path):
        """A .dcsproj (or its folder, or any file inside it, e.g. 03_CloudPro/cloudpro.json) opens CloudPro's
        stage of the shared project; a <name>.cloudpro.json opens an older stand-alone project."""
        path = os.path.abspath(path)
        if path.endswith(".cloudpro.json"):
            p = cls(path)
            with open(path, encoding="utf-8") as f:
                p.data = json.load(f)
            p._make_dirs()
            p._open_store()
            return p
        from dcs_core.project import DcsProject
        if path.endswith(".dcsproj") or os.path.isdir(path):
            dcs = DcsProject.open(path)
        else:
            dcs = DcsProject.find_for(path)
            if dcs is None:
                raise ValueError("%s is not part of a DCS project" % path)
        return cls.from_dcs(dcs)

    def save(self):
        self.data["modified"] = _utc()
        st = getattr(self, "_staged", None)
        if st is not None:
            # a rebuild is in progress: the settings on disk keep describing the points still in work/store
            # until commit_rebuild (a crash or a cancel leaves a consistent project)
            _atomic_json(self.path, dict(self.data, **st["data"]))
            if self.store is not None:
                self.store.save_meta()          # the staging store's own meta (work/store.new)
            return
        _atomic_json(self.path, self.data)
        if self.store is not None:
            self.store.save_meta()

    # ------------------------------------------------------------- staged rebuild (georeferencing)
    STAGED_KEYS = ("lines", "inputs", "trajectory_datum", "crs", "vertical", "geoid", "geoid_path", "epoch")

    @property
    def lines_dir(self):
        return os.path.join(self.work, "lines.new" if getattr(self, "_staged", None) is not None else "lines")

    def begin_rebuild(self):
        """Start building a new point store beside the current one (work/store.new, work/lines.new). The
        current points and settings stay untouched until commit_rebuild; abort_rebuild drops the new ones."""
        import copy
        import shutil
        if getattr(self, "_staged", None) is not None:
            raise RuntimeError("a rebuild is already in progress")
        snap = {k: copy.deepcopy(self.data[k]) for k in self.STAGED_KEYS if k in self.data}
        missing = [k for k in self.STAGED_KEYS if k not in self.data]
        for d in ("store.new", "lines.new"):
            shutil.rmtree(os.path.join(self.work, d), ignore_errors=True)
        old = self.store
        if old is not None:
            old.close()
        self._staged = {"data": snap, "missing": missing, "store": old}
        os.makedirs(self.lines_dir, exist_ok=True)
        self.store = Store(os.path.join(self.work, "store.new"), tile=self.data.get("tile", 50.0),
                           crs=self.data.get("crs", ""))
        self.data["lines"] = []
        self.data["inputs"] = {}

    def _swap_dir(self, name):
        """work/<name>.new -> work/<name>; the old folder is renamed aside first, then deleted."""
        import gc
        import shutil
        cur, new = os.path.join(self.work, name), os.path.join(self.work, name + ".new")
        aside = None
        if os.path.exists(cur):
            aside = "%s.old_%s" % (cur, _dt.datetime.now().strftime("%Y%m%d%H%M%S%f"))
            try:
                os.replace(cur, aside)
            except OSError:
                gc.collect()                    # Windows: a memory map not yet released
                os.replace(cur, aside)
        os.replace(new, cur)
        if aside:
            shutil.rmtree(aside, ignore_errors=True)

    def commit_rebuild(self):
        st = self._staged
        self.store.save_meta()
        self.store.close()
        if st["store"] is not None:
            st["store"].close()
        self._swap_dir("store")
        self._swap_dir("lines")
        self._staged = None
        self._open_store()
        self.save()

    def abort_rebuild(self):
        import shutil
        st = getattr(self, "_staged", None)
        if st is None:
            return
        try:
            self.store.close()
        except Exception:       # noqa: BLE001
            pass
        for d in ("store.new", "lines.new"):
            shutil.rmtree(os.path.join(self.work, d), ignore_errors=True)
        self.data.update(st["data"])
        for k in st["missing"]:
            self.data.pop(k, None)
        self._staged = None
        self._open_store()
        self.save()

    # ------------------------------------------------------------- basics
    @property
    def name(self):
        return self.dcs.name if self.dcs is not None else self.data.get("name", "")

    @property
    def crs(self):
        return self.data.get("crs", "")

    @property
    def lines(self):
        return self.data.setdefault("lines", [])

    @property
    def qgis_project_path(self):
        if self.dcs is not None:
            return self.dcs.qgis_project_path()
        return os.path.join(self.root, self.name + ".qgz")

    def reset_points(self):
        """Forget every imported line (the store is a cache of the raw data)."""
        import gc
        import shutil
        if self.store is not None:
            self.store.close()
        stamp = _dt.datetime.now().strftime("%Y%m%d%H%M%S%f")
        for name in ("store", "lines"):
            # renamed aside first: a tile that cannot be deleted (Windows: still mapped) must never be appended to
            d = os.path.join(self.work, name)
            if os.path.exists(d):
                try:
                    os.replace(d, "%s.old_%s" % (d, stamp))
                except OSError:
                    gc.collect()
                    os.replace(d, "%s.old_%s" % (d, stamp))
                shutil.rmtree("%s.old_%s" % (d, stamp), ignore_errors=True)
        self.data["lines"] = []
        self.data["inputs"] = {}
        self._make_dirs()
        self._open_store()
        self.save()

    def add_line(self, info):
        lid = len(self.lines)
        info = dict(info)
        info["id"] = lid
        info.setdefault("colour", LINE_COLOURS[lid % len(LINE_COLOURS)])
        info.setdefault("enabled", True)
        self.lines.append(info)
        return lid

    def track_path(self, lid):
        return os.path.join(self.lines_dir, "line_%03d_track.npz" % lid)

    def save_track(self, lid, t, E, N, Z, heading):
        np.savez(self.track_path(lid), t=t, E=E, N=N, Z=Z, heading=heading)

    def track(self, lid):
        p = self.track_path(lid)
        if not os.path.exists(p):
            return None
        with np.load(p) as d:
            return {k: d[k] for k in d.files}

    def tracks(self):
        out = {}
        for ln in self.lines:
            tr = self.track(ln["id"])
            if tr is not None:
                out[ln["id"]] = tr
        return out

    def log(self, text):
        h = self.data.setdefault("history", [])
        h.append({"utc": _utc(), "text": text})
        if len(h) > 5000:
            del h[:1000]

    def record_filter(self, group_dict, scope, changed):
        """Remember a filter group that changed points (for the report and cloud.json)."""
        if not changed:
            return
        self.data.setdefault("filter_runs", []).append({"utc": _utc(), "group": group_dict, "scope": scope,
                                                        "changed": int(changed)})

    def run_log(self, title, lines):
        """One log file per run in log/, named by its start time."""
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
        p = os.path.join(self.log_dir, "%s_%s.log" % (stamp, title.replace(" ", "_")))
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return p

    def to_world(self, x, y, z=None):
        o = self.store.origin
        if z is None:
            return x + o[0], y + o[1]
        return x + o[0], y + o[1], z + o[2]

    def to_local(self, E, N, Z=None):
        o = self.store.origin
        if Z is None:
            return E - o[0], N - o[1]
        return E - o[0], N - o[1], Z - o[2]
