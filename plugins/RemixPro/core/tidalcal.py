"""CLAM TidalCal projects (project.tidalcal + project_settings.json) as RemixPro inputs.

A TidalCal project folder:
    project.tidalcal          {"format": "clam-tidalcal-project", "version": 1, name, horizontal_crs,
                               base_rinex_files, surveyed_base_llh, base_hi_m, dataset_paths, patch_test_profile}
    project_settings.json     {"schema_version": 1, devices: [{device_id, nickname, forward_m, starboard_m, down_m,
                               roll_deg, pitch_deg, yaw_deg, fan_qc_*}], sonar_latency_s, qc_geometry,
                               rover_rinex_for_recording, batch_ppk_files, dataset_paths, ...}
    01_Raw_Sonar/ (.svlz)  02_GNSS_Rover_Base/ (RINEX, .LLH)  03_PPK_Trajectory/ (.pos)  04_Calibration/ ...
Paths saved by TidalCal are absolute on the PC that saved them; a missing one is looked up by name in the
project's own folders. Nothing in the project is changed."""
import glob
import json
import os

FORMAT = "clam-tidalcal-project"
FOLDERS = ("01_Raw_Sonar", "02_GNSS_Rover_Base", "03_PPK_Trajectory", "04_Calibration")


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def is_project(folder):
    return os.path.isfile(os.path.join(folder, "project.tidalcal"))


def find_projects(paths):
    out = []
    for p in paths:
        if os.path.isfile(p) and p.lower().endswith(".tidalcal"):
            out.append(os.path.dirname(os.path.abspath(p)))
        elif os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() != "processing"]
                if "project.tidalcal" in files:
                    out.append(os.path.abspath(root))
    return sorted(set(out))


class Project:
    def __init__(self, folder):
        self.folder = os.path.abspath(folder)
        self.manifest = _read(os.path.join(folder, "project.tidalcal"))
        if self.manifest.get("format") != FORMAT:
            raise ValueError("%s is not a TidalCal project" % folder)
        self.settings = _read(os.path.join(folder, "project_settings.json"))
        self.name = self.manifest.get("name") or os.path.basename(self.folder)

    def resolve(self, path):
        """A path TidalCal saved, here: as saved, else by file name in the project's folders, else None."""
        if not path:
            return None
        if os.path.isfile(path):
            return os.path.abspath(path)
        name = os.path.basename(str(path).replace("\\", "/"))
        for sub in ("",) + FOLDERS:
            cand = os.path.join(self.folder, sub, name)
            if os.path.isfile(cand):
                return cand
        hits = glob.glob(os.path.join(self.folder, "**", name), recursive=True)
        return hits[0] if hits else None

    def recordings(self):
        paths = list(self.settings.get("dataset_paths") or self.manifest.get("dataset_paths") or [])
        found = [self.resolve(p) for p in paths]
        found = [p for p in found if p]
        raw = os.path.join(self.folder, "01_Raw_Sonar")
        if os.path.isdir(raw):
            found += [os.path.join(raw, n) for n in sorted(os.listdir(raw)) if n.lower().endswith((".svlz", ".svlog"))]
        return list(dict.fromkeys(os.path.abspath(p) for p in found))

    def _per_recording(self, key, recording):
        m = self.settings.get(key) or {}
        for k, v in m.items():
            if os.path.basename(str(k).replace("\\", "/")) == os.path.basename(recording):
                vals = v if isinstance(v, list) else [v]
                return [p for p in (self.resolve(x if isinstance(x, str) else (x or {}).get("path")) for x in vals) if p]
        return []

    def gnss_for(self, recording):
        """(rinex paths, llh paths, ppk .pos paths) for one recording."""
        rinex = self._per_recording("rover_rinex_for_recording", recording)
        rinex += [p for p in (self.resolve(b) for b in (self.manifest.get("base_rinex_files") or [])) if p]
        rinex += self._per_recording("batch_ppk_base_for_recording", recording)
        ppk = self._per_recording("batch_ppk_files", recording)
        gdir = os.path.join(self.folder, "02_GNSS_Rover_Base")
        llh = sorted(glob.glob(os.path.join(gdir, "**", "*.[lL][lL][hH]"), recursive=True))
        if not ppk:
            ppk = sorted(glob.glob(os.path.join(self.folder, "03_PPK_Trajectory", "**", "*.pos"), recursive=True))
        return list(dict.fromkeys(rinex)), llh, ppk

    def offsets(self):
        """{device id: [F, S, D]} from TidalCal's vessel table."""
        out = {}
        for d in self.settings.get("project_vessel_devices") or self.settings.get("devices") or []:
            try:
                out[str(int(d["device_id"]))] = [float(d["forward_m"]), float(d["starboard_m"]), float(d["down_m"])]
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def calibration(self):
        """What TidalCal holds about calibration and timing, kept in hardware.json for reference."""
        s = self.settings
        return {"tidalcal": {"project": self.name, "sonar_latency_s": s.get("sonar_latency_s"),
                             "qc_geometry": s.get("qc_geometry"), "devices": s.get("devices"),
                             "horizontal_crs": self.manifest.get("horizontal_crs"),
                             "vertical_datum": self.manifest.get("vertical_datum"),
                             "surveyed_base_llh": self.manifest.get("surveyed_base_llh"),
                             "base_hi_m": self.manifest.get("base_hi_m"),
                             "note": "Values as TidalCal saved them. The vessel table offsets are relative to "
                                     "TidalCal's position reference; check them against your measured offsets."}}

    def attachments(self):
        out = []
        for name in ("project.tidalcal", "project_settings.json"):
            p = os.path.join(self.folder, name)
            if os.path.isfile(p):
                with open(p, "rb") as f:
                    out.append(("tidalcal/" + name, "application/json", f.read()))
        prof = self.resolve(self.manifest.get("patch_test_profile"))
        if prof:
            with open(prof, "rb") as f:
                out.append(("tidalcal/" + os.path.basename(prof), "application/json", f.read()))
        return out
