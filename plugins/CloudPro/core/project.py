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
        _atomic_json(self.path, self.data)
        if self.store is not None:
            self.store.save_meta()

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
        import shutil
        if self.store is not None:
            self.store.close()
        shutil.rmtree(os.path.join(self.work, "store"), ignore_errors=True)
        shutil.rmtree(os.path.join(self.work, "lines"), ignore_errors=True)
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
        return os.path.join(self.work, "lines", "line_%03d_track.npz" % lid)

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
