"""The shared DCS project (.dcsproj): one per job, used by every DCS plugin.

    JobFolder/Processing/<Name>/<Name>.dcsproj
                                01_PosPro/ 02_AlignPro/ 03_CloudPro/  (work/ out/ log/)

Every change goes through modify(): the file is re-read under a short lock,
the change applied to the fresh copy and written back atomically, so two
plugins (or two QGIS windows) never overwrite each other's sections. No Qt.
"""
import datetime
import hashlib
import json
import os
import re
import time

from . import files, locks

SCHEMA = "dcs.project/1"
SUFFIX = ".dcsproj"

# (key, folder, label, schema of its handover file, handover file name)
STAGES = [
    ("pospro", "01_PosPro", "PosPro", "dcs.trajectory/1", "trajectory.json"),
    ("alignpro", "02_AlignPro", "AlignPro", "dcs.alignment/1", "alignment.json"),
    # optional (DCS Core 1.13.0): the trajectory corrected from the point cloud; CloudPro uses it when finalized
    ("strippro", "02_StripPro", "StripPro", "dcs.trajectory/1", "trajectory.json"),
    ("cloudpro", "03_CloudPro", "CloudPro", "dcs.cloud/1", "cloud.json"),
]
STAGE = {s[0]: s for s in STAGES}
UPSTREAM = {"pospro": [], "alignpro": ["pospro"], "strippro": ["pospro", "alignpro"],
            "cloudpro": ["pospro", "alignpro", "strippro"]}
OPTIONAL = ("strippro",)        # a stage a job can do without: the chain shows it, nothing waits for it
# shared facts a stage's results depend on (an edit makes the stage stale)
DEPENDS = {"pospro": ["crs", "base_station", "antennas"],
           "alignpro": ["crs", "lidar_mounting"],
           "strippro": ["crs", "lidar_mounting"],
           "cloudpro": ["crs", "lidar_mounting"]}
SHARED_KEYS = ("crs", "base_station", "antennas", "lidar_mounting", "vehicle", "hardware")
# stages that read the job's survey-line .clraw themselves (built_from records them as "inputs", 1.8.0)
READS_SURVEY = ("alignpro", "strippro", "cloudpro")
MODIFY_WAIT_S = 10.0            # how long modify() waits for another writer of the project file
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {"COM%d" % i for i in range(1, 10)} | {"LPT%d" % i for i in range(1, 10)}


class ProjectError(Exception):
    pass


def now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_name(name):
    n = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", (name or "").strip()).strip(". ")
    if not n:
        raise ProjectError("enter a project name")
    n = n[:80].rstrip(". ")
    if n.split(".")[0].upper() in _RESERVED:        # Windows device names
        n = "_" + n
    return n


def value_hash(v):
    return hashlib.sha256(json.dumps(v, sort_keys=True, default=str).encode()).hexdigest()[:16]


_FP_CACHE = {}          # abs path -> (size, mtime_ns, fingerprint): the latest only, so it cannot grow


def _stat(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns]


def cached_fingerprint(path):
    st = _stat(path)
    if st is None:
        return None
    p = os.path.abspath(path)
    c = _FP_CACHE.get(p)
    if c is None or c[0] != st[0] or c[1] != st[1]:
        c = _FP_CACHE[p] = (st[0], st[1], files.fingerprint(path))
    return c[2]


def _raw_reasons(recorded, stats, base, kind="raw file"):
    """Reasons a set of recorded raw-file fingerprints no longer holds. stats = {rel: [size, mtime_ns]} recorded
    beside them (1.8.0): a file with the same size and time is not hashed again."""
    out = []
    for rel, fp in (recorded or {}).items():
        path = files.resolve(rel, base)
        st = _stat(path)
        if st is None:
            out.append("%s not found: %s (relocate the job folder?)" % (kind, os.path.basename(rel)))
            continue
        if not fp or (stats or {}).get(rel) == st:
            continue
        if not files.fingerprint_matches(path, fp, cached_fingerprint(path)):
            out.append("%s changed: %s" % (kind, os.path.basename(rel)))
    return out


class DcsProject:
    def __init__(self, path, data):
        self.path = os.path.abspath(path)
        self.folder = os.path.dirname(self.path)
        self.data = data

    # ------------------------------------------------------------- create/open
    @classmethod
    def create(cls, job_folder, name, processing_root=None, plugin="DCS"):
        """New project for the captures in job_folder. The project folder is
        <processing_root or job_folder/Processing>/<name>/."""
        name = safe_name(name)
        job_folder = os.path.abspath(job_folder)
        if not os.path.isdir(job_folder):
            raise ProjectError("the job folder %s does not exist" % job_folder)
        root = os.path.abspath(processing_root) if processing_root else os.path.join(job_folder, "Processing")
        folder = os.path.join(root, name)
        path = os.path.join(folder, name + SUFFIX)
        if os.path.exists(path):
            raise ProjectError("a project called %s already exists in %s; open it instead" % (name, root))
        os.makedirs(folder, exist_ok=True)
        data = {"schema": SCHEMA, "name": name, "created": now(), "modified": now(), "created_by": plugin,
                "raw_root": files.portable(job_folder, folder), "captures": [], "shared": {}, "shared_meta": {},
                "stages": {}}
        files.atomic_write_json(path, data)
        return cls(path, data)

    @classmethod
    def open(cls, path):
        if os.path.isdir(path):
            cands = [f for f in os.listdir(path) if f.endswith(SUFFIX)]
            if not cands:
                raise ProjectError("no %s file in %s" % (SUFFIX, path))
            path = os.path.join(path, sorted(cands)[0])
        try:
            data = files.read_json_retry(path)
        except (OSError, ValueError) as e:
            bak = path + ".bak"
            if os.path.exists(bak):
                data = files.read_json(bak)
            else:
                raise ProjectError("cannot read %s: %s" % (path, e))
        if not str(data.get("schema", "")).startswith("dcs.project/"):
            raise ProjectError("%s is not a DCS project" % path)
        if int(str(data["schema"]).split("/")[1]) > 1:
            raise ProjectError("%s was written by a newer DCS Core; update the plugins" % path)
        for k in ("captures", "shared", "shared_meta", "stages"):
            data.setdefault(k, [] if k == "captures" else {})
        return cls(path, data)

    @classmethod
    def find_for(cls, path):
        """The project whose folder contains path (any file or folder in it), or None."""
        p = os.path.abspath(path)
        if os.path.isfile(p):
            p = os.path.dirname(p)
        for _ in range(6):
            try:
                c = [f for f in os.listdir(p) if f.endswith(SUFFIX)]
            except OSError:
                c = []
            if c:
                return cls.open(os.path.join(p, sorted(c)[0]))
            up = os.path.dirname(p)
            if up == p:
                break
            p = up
        return None

    def reload(self):
        self.data = DcsProject.open(self.path).data
        return self

    def modify(self, fn, plugin="DCS"):
        """Apply fn(data) to a freshly read copy under the project lock and
        write it back atomically (previous version kept as .bak)."""
        lk = locks.Lock(self.folder, "project file", plugin, name=".dcsproj.lock", wait=MODIFY_WAIT_S)
        tl = locks.thread_lock(lk.path)
        if not tl.acquire(timeout=MODIFY_WAIT_S):
            raise locks.Locked({"stage": "project file", "plugin": "another task in this QGIS"})
        try:
            with lk:
                return self._modify_locked(fn)
        finally:
            tl.release()

    def _modify_locked(self, fn):
        """modify() body, run while holding both the thread lock and the project lock file."""
        backup = True
        try:
            fresh = files.read_json_retry(self.path)
        except FileNotFoundError:
            fresh = self.data
        except (OSError, ValueError) as e:
            backup = False              # keep the good .bak; the damaged file is replaced
            # never write back the in-memory copy over someone else's changes: use the last good version
            try:
                fresh = files.read_json(self.path + ".bak")
            except (OSError, ValueError):
                raise ProjectError("cannot read %s (%s); not changed" % (self.path, e))
        for k in ("captures", "shared", "shared_meta", "stages"):
            fresh.setdefault(k, [] if k == "captures" else {})
        out = fn(fresh)
        fresh["modified"] = now()
        files.atomic_write_json(self.path, fresh, backup=backup)
        self.data = fresh
        return out

    # ------------------------------------------------------------- basics
    @property
    def name(self):
        return self.data.get("name", "")

    @property
    def job_folder(self):
        return files.resolve(self.data.get("raw_root", ".."), self.folder)

    def qgis_project_path(self):
        return os.path.join(self.folder, self.name + ".qgz")

    def stage_dir(self, stage, sub=None, create=True):
        d = os.path.join(self.folder, STAGE[stage][1])
        if sub:
            d = os.path.join(d, sub)
        if create:
            os.makedirs(d, exist_ok=True)
        return d

    # ------------------------------------------------------------- captures
    def captures(self):
        """[{"path": abs folder, "files": [abs file, ...], "fingerprints": {...}}]"""
        out = []
        for c in self.data.get("captures", []):
            folder = files.resolve(c["path"], self.folder)
            out.append({"path": folder, "files": [files.resolve(f, folder) for f in c.get("files", [])],
                        "fingerprints": dict(c.get("fingerprints") or {})})
        return out

    def capture_files(self):
        return [f for c in self.captures() for f in c["files"]]

    def set_capture_files(self, paths, plugin="DCS"):
        """Record the raw files used (grouped by their capture folder) with
        their fingerprints."""
        groups = {}
        for p in paths:
            p = os.path.abspath(p)
            folder = os.path.dirname(p)
            # the capture folder is the one holding lidar/ ins/ (mapper layout)
            if os.path.basename(folder).lower() in ("ins", "lidar", "processed", "beamworx"):
                folder = os.path.dirname(folder)
            groups.setdefault(folder, []).append(p)

        def fn(d):
            d["captures"] = [{"path": files.portable(folder, self.folder),
                              "files": [files.portable(f, folder) for f in fs],
                              "fingerprints": {files.portable(f, folder): cached_fingerprint(f) for f in fs}}
                             for folder, fs in groups.items()]
        self.modify(fn, plugin)

    def survey_clraw_files(self, max_age=0.0):
        """The .clraw files of the job's survey lines: those in the captures' folders (and the captures'
        own .clraw files), else (no captures, or captures that are only mapper 2.10 navigation recordings)
        the whole job folder. Never anything under the Processing folder, never a navigation recording.
        max_age: reuse the previous answer when it is younger than this many seconds (state() polling)."""
        from . import clraw
        c = getattr(self, "_survey_cache", None)
        if max_age and c and time.monotonic() - c[0] < max_age:
            return list(c[1])

        proc = os.path.normcase(os.path.abspath(self.folder)) + os.sep

        def walk(paths):
            found = clraw.find_clraw([p for p in paths if os.path.exists(p)])
            out = []
            for f in found:
                a = os.path.normcase(os.path.abspath(f))
                if a.startswith(proc) or "processing" in [x.lower() for x in a.split(os.sep)[:-1]]:
                    continue
                out.append(f)
            return out
        paths = []
        for c in self.captures():
            paths.append(c["path"])
            paths.extend(f for f in c["files"] if f.lower().endswith(".clraw"))
        lines = clraw.split_navigation(walk(paths))[0] if paths else []
        if not lines:
            lines = clraw.split_navigation(walk([self.job_folder]))[0]
        out = sorted(set(lines), key=lambda x: os.path.basename(x))
        self._survey_cache = (time.monotonic(), list(out))
        return out

    def sonar_clraw_files(self):
        """The job's marine sonar recordings (.clraw of kind "sonar", 1.5.0), in recording order: in the captures'
        folders, else the job folder; never under Processing."""
        from . import clraw
        roots = sorted({os.path.dirname(f) for f in self.capture_files()}) or [self.job_folder]
        found = []
        for r in roots:
            for root, dirs, fs in os.walk(r):
                dirs[:] = [d for d in dirs if d.lower() != "processing"]
                found += [os.path.join(root, f) for f in fs if f.lower().endswith(".clraw")]
        found = [f for f in sorted(set(found)) if clraw.is_sonar(f)]
        groups = clraw.group_recordings(found, sonar=True)
        return [p for ps in groups.values() for p in ps]

    def missing_captures(self):
        return [f for f in self.capture_files() if not os.path.exists(f)]

    def relocate(self, new_job_folder, plugin="DCS"):
        """Point the project at a moved job folder. Every capture is looked up
        by its path relative to the old job folder; returns the files still
        missing (nothing is changed when any is missing)."""
        old_root = self.job_folder
        new_root = os.path.abspath(new_job_folder)
        moved = []
        for c in self.data.get("captures", []):
            old = files.resolve(c["path"], self.folder)
            try:
                rel = os.path.relpath(old, old_root)
            except ValueError:
                rel = os.path.basename(old)
            moved.append((c, os.path.normpath(os.path.join(new_root, rel))))
        missing = [os.path.join(n, f) for c, n in moved for f in c.get("files", [])
                   if not os.path.exists(os.path.join(n, f))]
        if missing:
            return missing

        def fn(d):
            d["raw_root"] = files.portable(new_root, self.folder)
            for c, (_, n) in zip(d.get("captures", []), moved):
                c["path"] = files.portable(n, self.folder)
        self.modify(fn, plugin)
        return []

    # ------------------------------------------------------------- shared facts
    def shared(self, key, default=None):
        return self.data.get("shared", {}).get(key, default)

    def set_shared(self, key, value, plugin="DCS"):
        """Change a shared fact. Returns True when it changed (later stages
        that depend on it become stale)."""
        if key not in SHARED_KEYS:
            raise ProjectError("unknown shared fact %s" % key)
        h = value_hash(value)
        if self.data.get("shared_meta", {}).get(key, {}).get("hash") == h and key in self.data.get("shared", {}):
            return False

        def fn(d):
            d["shared"][key] = value
            d["shared_meta"][key] = {"hash": h, "modified": now(), "by": plugin}
        self.modify(fn, plugin)
        return True

    # ------------------------------------------------------------- stages
    def stage(self, key):
        return dict(self.data.get("stages", {}).get(key) or {})

    def update_stage(self, key, by="DCS", **fields):
        def fn(d):
            s = d["stages"].setdefault(key, {"folder": STAGE[key][1]})
            s.update(fields)
        self.modify(fn, by)

    def run(self, key, plugin, version=""):
        """Context manager for a processing run of a stage: holds the stage
        lock, marks it running, and marks it failed if the block raises (a
        successful run ends with handover.publish, which marks it done)."""
        return _Run(self, key, plugin, version)

    def built_from_now(self, key, used=None, inputs=None):
        """What a stage would record as built_from if it published now.

        used (1.8.0): {upstream stage: handover id} the stage really used (read from the handover it loaded);
        replaces the ids currently in the project for those stages. inputs (1.8.0, AlignPro / CloudPro): the raw
        survey-line .clraw files the stage read; default = survey_clraw_files(). They are recorded with their
        fingerprints ("inputs"), and the job's survey files at that moment ("survey"), so a changed or added
        survey line makes the stage stale even when PosPro used navigation recordings."""
        up = {u: self.stage(u).get("handover_id") for u in UPSTREAM[key] if self.stage(u).get("handover_id")}
        if used is not None:
            up = {u: h for u, h in dict(used).items() if u in UPSTREAM[key] and h}
        job = self.job_folder
        caps = self.capture_files()
        shared = {k: self.data.get("shared_meta", {}).get(k, {}).get("hash") for k in DEPENDS[key]
                  if k in self.data.get("shared", {})}
        # raw files keyed relative to the job folder, so moving the job folder (relocate) is not a change
        out = {"stages": up, "captures": {files.portable(f, job): cached_fingerprint(f) for f in caps},
               "captures_stat": {files.portable(f, job): _stat(f) for f in caps}, "shared": shared}
        if key in READS_SURVEY:
            survey = self.survey_clraw_files()
            ins = survey if inputs is None else [os.path.abspath(p) for p in inputs]
            out["inputs"] = {files.portable(f, job): cached_fingerprint(f) for f in ins}
            out["inputs_stat"] = {files.portable(f, job): _stat(f) for f in ins}
            out["survey"] = sorted(files.portable(f, job) for f in survey)
        return out

    def state(self, key):
        """(state, reasons): state is none, running, failed, done or stale."""
        s = self.stage(key)
        st = s.get("status", "none")
        lk = locks.read(os.path.join(self.folder, STAGE[key][1], ".lock"))
        if lk and locks.is_live(lk):
            return "running", ["%s is running on %s" % (lk.get("plugin", "a plugin"), lk.get("host", "?"))]
        if st == "running":
            # the run ended without finishing (QGIS closed or crashed): earlier results still count
            st = "done" if s.get("handover_id") else "failed"
        if st != "done":
            return st, []
        reasons = []
        ho = s.get("handover")
        if not ho or not os.path.exists(files.resolve(ho, self.folder)):
            return "failed", ["the finalized results are missing"]
        bf = s.get("built_from") or {}
        for u, hid in (bf.get("stages") or {}).items():
            if hid == "ignored":        # deliberately not used (CloudPro with the recorded/entered mounting)
                continue
            us = self.stage(u)
            if us.get("status") != "done" or us.get("handover_id") != hid:
                reasons.append("%s results changed since this was computed" % STAGE[u][2])
        for u in UPSTREAM[key]:
            if u not in (bf.get("stages") or {}) and self.stage(u).get("status") == "done":
                reasons.append("%s results were added since this was computed" % STAGE[u][2])
        job = self.job_folder
        reasons += _raw_reasons(bf.get("captures"), bf.get("captures_stat"), job)
        # 1.8.0: the survey lines AlignPro / CloudPro read (absent in older projects: nothing to check)
        reasons += _raw_reasons(bf.get("inputs"), bf.get("inputs_stat"), job, "survey line")
        if bf.get("survey") is not None:
            had = {os.path.normcase(r) for r in bf["survey"]} | {os.path.normcase(r) for r in (bf.get("inputs") or {})}
            try:
                now_files = self.survey_clraw_files(max_age=5.0)
            except OSError:
                now_files = []
            added = [f for f in now_files if os.path.normcase(files.portable(f, job)) not in had]
            if added:
                reasons.append("survey line added: %s%s" % (os.path.basename(added[0]),
                                                             (" and %d more" % (len(added) - 1)) if len(added) > 1 else ""))
        for k, h in (bf.get("shared") or {}).items():
            if self.data.get("shared_meta", {}).get(k, {}).get("hash") != h:
                m = self.data.get("shared_meta", {}).get(k, {})
                reasons.append("%s edited%s" % (k.replace("_", " "), (" by %s" % m["by"]) if m.get("by") else ""))
        return ("stale" if reasons else "done"), reasons

    def chain(self):
        return [(k, STAGE[k][2]) + self.state(k) for k, *_ in STAGES]


class _Run:
    def __init__(self, project, key, plugin, version):
        self.p, self.key, self.plugin, self.version = project, key, plugin, version
        self.lock = locks.Lock(project.stage_dir(key), STAGE[key][2], plugin)

    def __enter__(self):
        self.lock.acquire()
        try:
            self.p.update_stage(self.key, self.plugin, status="running", plugin=self.plugin, version=self.version,
                                started=now())
        except BaseException:
            self.lock.release()         # __exit__ does not run when __enter__ raises
            raise
        return self

    def __exit__(self, et, ev, tb):
        try:
            try:
                self.p.reload()         # the run may have published from another thread / copy
            except ProjectError:
                pass
            if et is not None:
                # earlier published results stay valid; the failed run is recorded beside them
                prev = "done" if self.p.stage(self.key).get("handover_id") else "failed"
                self.p.update_stage(self.key, self.plugin, status=prev, last_run="failed",
                                    error=str(ev)[:500], finished=now())
            elif self.p.stage(self.key).get("status") == "running":
                # finished without publishing (e.g. cancelled): leave the previous results as they were
                prev = "done" if self.p.stage(self.key).get("handover_id") else "none"
                self.p.update_stage(self.key, self.plugin, status=prev)
        finally:
            self.lock.release()
        return False
