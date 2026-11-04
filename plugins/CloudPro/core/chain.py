"""CloudPro's inputs from the DCS chain (DCS Processing Workflow Spec):

  01_PosPro/out/trajectory.json   (dcs.trajectory/1)  SBET + SMRMSG at the Certus reference point
  02_AlignPro/out/alignment.json  (dcs.alignment/1)   calibrated mounting, lever arm, latency

CloudPro may run without AlignPro: it then uses the mounting recorded in the
capture and says "no calibration" in its report and cloud.json. No Qt."""

from . import clraw
from .trajectory import read_sbet

GPS_EPOCH_UNIX = 315964800.0
CONVENTION = "navaq_hesai"


class Inputs:
    """What CloudPro will georeference with, and why."""

    def __init__(self):
        self.traj = None                 # Trajectory
        self.traj_info = {}              # source, handover_id, description, frame
        self.mount = None                # (roll, pitch, heading) deg, None = each capture's own record
        self.lever = None                # (F, S, D) m, None = capture record
        self.latency = 0.0
        self.align_info = {"calibration": "no calibration"}
        self.source_crs = None
        self.files = []
        self.warnings = []

    def summary(self):
        t = self.traj_info
        a = self.align_info
        s = ["Trajectory: %s%s" % (t.get("description") or t.get("source") or "none",
                                   ("  (%s)" % t["handover_id"][7:19]) if t.get("handover_id") else "")]
        if a.get("calibration") == "applied":
            s.append("Mounting: AlignPro calibration %.4f / %.4f / %.4f deg, lever %s m, latency %.1f ms  (%s)" % (
                self.mount[0], self.mount[1], self.mount[2], "/".join("%.3f" % v for v in self.lever),
                self.latency * 1000, (a.get("handover_id") or "")[7:19]))
        else:
            s.append("Mounting: as recorded in the capture%s - no calibration" % (
                (" (shared %.4f / %.4f / %.4f deg)" % tuple(self.mount)) if self.mount else ""))
        return s

    def record(self):
        """What is stored with the project and in cloud.json."""
        return {"trajectory": dict(self.traj_info),
                "alignment": dict(self.align_info),
                "mount_deg": list(self.mount) if self.mount else None,
                "lidar_lever_m": list(self.lever) if self.lever else None,
                "latency_s": self.latency, "override": getattr(self, "override", {})}


def frame_to_crs(frame_name):
    """Geographic CRS of the trajectory datum when it differs from the usual NAD83(CSRS); None = use the
    target CRS's own datum."""
    n = (frame_name or "").upper().replace(" ", "")
    if not n or "NAD83(CSRS)" in n or n == "NAD83CSRS":
        return None
    if "WGS84" in n or "WGS 84" in (frame_name or "").upper():
        return "EPSG:4326"
    return None


def clraw_files(dcs):
    """The .clraw files of the job's survey lines (DCS Core): the captures' folders, else the job folder; never
    anything under the Processing folder, never a mapper 2.10 navigation recording."""
    return dcs.survey_clraw_files()


def resolve(dcs, use_alignpro=True, override=None):
    """Inputs from the shared project. Raises nothing for a missing stage; reasons go to warnings.
    override (CloudPro's project properties): {"source": "alignpro" | "recorded" | "entered", mount, lever,
    latency_s} - "recorded" ignores AlignPro, "entered" uses the values typed in CloudPro."""
    override = override or {}
    if override.get("source") in ("recorded", "entered"):
        use_alignpro = False
    from dcs_core import handover
    inp = Inputs()
    try:
        dcs.reload()
    except Exception:          # noqa: BLE001
        pass
    inp.files = clraw_files(dcs)
    # trajectory
    try:
        doc = handover.read(dcs, "pospro", required=False)
    except handover.HandoverError as e:
        doc = None
        inp.warnings.append(str(e))
    if doc is not None:
        tm = doc.get("time") or {}
        week = int(tm.get("week") or 0)
        leap = float(tm.get("leap_seconds", 18))
        ws = GPS_EPOCH_UNIX + week * 604800.0 if week else None
        inp.traj = read_sbet(doc["paths"]["sbet"], week_start=ws, leap=leap, smrmsg=doc["paths"].get("smrmsg"))
        fr = doc.get("frame") or {}
        inp.source_crs = frame_to_crs(fr.get("name"))
        st, why = dcs.state("pospro")
        inp.traj_info = {"source": "pospro", "handover_id": doc.get("id"),
                         "description": "PosPro trajectory (%s %s, %s)" % (fr.get("name") or "?", fr.get("epoch") or "",
                                                                          (doc.get("quality") or {}).get("mode") or "?"),
                         "frame": fr, "state": st}
        if st == "stale":
            inp.warnings.append("PosPro results are out of date: " + "; ".join(why))
    else:
        inp.traj_info = {"source": "realtime", "handover_id": None,
                         "description": "Certus real-time track (PosPro has not published a trajectory)"}
        inp.warnings.append("PosPro has not published a trajectory for this project: the Certus real-time track "
                            "is used (metres of drift without RTK). Run PosPro, then update the georeferencing.")
    # mounting
    al = None
    if use_alignpro:
        try:
            al = handover.read(dcs, "alignpro", required=False)
        except handover.HandoverError as e:
            inp.warnings.append(str(e))
    if al is not None:
        conv = (al.get("convention") or {}).get("name", CONVENTION)
        if conv != CONVENTION:
            inp.warnings.append("AlignPro used the %s convention, CloudPro expects %s: calibration not used" %
                                (conv, CONVENTION))
            al = None
    if al is not None:
        m = al.get("mount_deg") or {}
        inp.mount = (float(m["roll"]), float(m["pitch"]), float(m["heading"]))
        lv = (al.get("lidar_lever_m") or {}).get("value")
        inp.lever = tuple(float(v) for v in lv) if lv else None
        inp.latency = float(al.get("latency_s") or 0.0)
        st, why = dcs.state("alignpro")
        inp.align_info = {"calibration": "applied", "source": "alignpro", "handover_id": al.get("id"),
                          "mount_source": m.get("source"), "lever_source": (al.get("lidar_lever_m") or {}).get("source"),
                          "boresight_deg": al.get("boresight_deg"), "quality": al.get("quality"), "state": st,
                          "solved_on": (al.get("trajectory") or {}).get("handover_id")}
        if st == "stale":
            inp.warnings.append("AlignPro results are out of date: " + "; ".join(why))
        if inp.traj_info.get("handover_id") and inp.align_info["solved_on"] and \
                inp.align_info["solved_on"] != inp.traj_info["handover_id"]:
            inp.warnings.append("The AlignPro calibration was solved on an earlier PosPro trajectory")
    else:
        lm = dcs.shared("lidar_mounting") or {}
        md = lm.get("mount_deg") if isinstance(lm, dict) else None
        if md and (lm.get("convention") or CONVENTION) == CONVENTION:
            inp.mount = tuple(float(v) for v in md)
        inp.align_info = {"calibration": "no calibration", "source": "shared lidar mounting" if inp.mount else
                          "capture hardware record", "handover_id": None}
    if override.get("source") == "entered":
        inp.mount = tuple(float(v) for v in override["mount"])
        inp.lever = tuple(float(v) for v in override["lever"])
        inp.latency = float(override.get("latency_s") or 0.0)
        inp.align_info = {"calibration": "entered values", "source": "entered in CloudPro", "handover_id": None}
        inp.warnings.append("Mounting, lever arm and latency entered in CloudPro (not an AlignPro calibration)")
    elif override.get("source") == "recorded":
        inp.warnings.append("AlignPro's calibration is not used (project properties: recorded mounting)")
    inp.override = dict(override)
    return inp


def georef_state(project, inp=None):
    """Reasons why the georeferenced points no longer match the chain's current inputs ([] = current)."""
    if project.dcs is None or not project.lines:
        return []
    used = project.data.get("inputs") or {}
    if inp is None:
        inp = resolve(project.dcs, override=project.data.get("override"))
    why = []
    if (used.get("override") or {}) != (project.data.get("override") or {}):
        why.append("The georeferencing values in Project properties changed")
    ut = (used.get("trajectory") or {})
    if ut.get("handover_id") != inp.traj_info.get("handover_id"):
        if inp.traj_info.get("handover_id") and not ut.get("handover_id"):
            why.append("PosPro has published a trajectory since the points were georeferenced (real-time track used)")
        elif inp.traj_info.get("handover_id"):
            why.append("PosPro's trajectory changed since the points were georeferenced")
    ua = (used.get("alignment") or {})
    if ua.get("handover_id") != inp.align_info.get("handover_id"):
        if inp.align_info.get("handover_id") and not ua.get("handover_id"):
            why.append("AlignPro has published a calibration since the points were georeferenced")
        elif inp.align_info.get("handover_id"):
            why.append("AlignPro's calibration changed since the points were georeferenced")
        else:
            why.append("The AlignPro calibration the points used is no longer published")
    return why


def group(files):
    return clraw.group_recordings(files)


def quick_state(project):
    """Like georef_state but without reading the trajectory: compares the handover ids recorded at
    georeferencing with the ids the upstream stages have published now (for the 5 s header refresh)."""
    if project.dcs is None or not project.lines:
        return []
    try:
        project.dcs.reload()
    except Exception:          # noqa: BLE001
        return []
    used = project.data.get("inputs") or {}
    why = []
    ov = project.data.get("override") or {}
    if (used.get("override") or {}) != ov:
        why.append("the georeferencing values in Project properties changed")
    tid = (used.get("trajectory") or {}).get("handover_id")
    cur = project.dcs.stage("pospro").get("handover_id") if project.dcs.stage("pospro").get("status") == "done" else None
    if cur and cur != tid:
        why.append("PosPro has published a new trajectory since the points were georeferenced" if tid else
                   "PosPro has published a trajectory (the points use the real-time track)")
    aid = (used.get("alignment") or {}).get("handover_id")
    s = project.dcs.stage("alignpro")
    cur = s.get("handover_id") if s.get("status") == "done" else None
    if ov.get("source") in ("recorded", "entered"):
        cur = None
    if cur and cur != aid:
        why.append("AlignPro has published a new calibration since the points were georeferenced" if aid else
                   "AlignPro has published a calibration (the points use the recorded mounting)")
    return why
