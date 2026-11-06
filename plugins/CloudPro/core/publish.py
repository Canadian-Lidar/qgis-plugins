"""Publish CloudPro's results (stage 03 of the DCS chain): the delivered
files in 03_CloudPro/out/ and cloud.json (dcs.cloud/1) last, all or nothing.

cloud.json records the trajectory and alignment handover ids the points
were georeferenced with (or "no calibration"), the filters applied with
their settings, the classes, the output files, CRS and heights. No Qt."""
import datetime
import os

import numpy as np

from . import export as EX
from .filters import ASPRS
from .store import REJ_AUTO, REJ_MANUAL, REJECTED

PLUGIN = "CloudPro"


def point_stats(project):
    tot = acc = rman = raut = 0
    cls = np.zeros(256, np.int64)
    per_line = {}
    for k in project.store.keys():
        a = project.store.tile_array(k)
        if len(a) == 0:
            continue
        rej = (a["flag"] & REJECTED) != 0
        tot += len(a)
        acc += int((~rej).sum())
        rman += int((rej & ((a["flag"] & REJ_MANUAL) != 0)).sum())
        raut += int((rej & ((a["flag"] & REJ_AUTO) != 0)).sum())
        cls += np.bincount(a["cls"][~rej], minlength=256)
        for l, c in zip(*np.unique(a["line"], return_counts=True)):
            per_line[int(l)] = per_line.get(int(l), 0) + int(c)
    return {"total": tot, "accepted": acc, "rejected": tot - acc, "rejected_manual": rman,
            "rejected_automatic": raut,
            "classes": {str(i): {"name": ASPRS.get(i, "class %d" % i), "points": int(c)}
                        for i, c in enumerate(cls) if c}}


def report_text(project, stats, outputs, warnings):
    inp = project.data.get("inputs") or {}
    t = inp.get("trajectory") or {}
    a = inp.get("alignment") or {}
    L = ["CloudPro report - %s" % project.name,
         "Created %s UTC" % datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), "",
         "INPUTS",
         "  Trajectory   %s  %s" % (t.get("description", "?"), t.get("handover_id") or ""),
         "  Calibration  %s  %s" % (a.get("calibration", "no calibration"), a.get("handover_id") or ""),
         "  Mounting     %s deg (navaq_hesai)" % (inp.get("mount_deg") or "as recorded in each capture"),
         "  Lever        %s m" % (inp.get("lidar_lever_m") or "as recorded in each capture"),
         "  Latency      %.4f s" % float(inp.get("latency_s") or 0.0),
         "  CRS          %s, heights %s" % (project.crs, project.data.get("vertical", "ellipsoidal"))]
    td = project.data.get("trajectory_datum")
    if td:
        L.append("  Datum        %s" % td.get("note"))
    L += ["", "LINES"]
    for ln in project.lines:
        L.append("  L%-3d %-24s %12s points  %s" % (ln["id"] + 1, ln.get("name", ""), "{:,}".format(ln.get("points", 0)),
                                                    ", ".join(os.path.basename(f) for f in ln.get("files", []))))
    L += ["", "POINTS",
          "  total %s, accepted %s, rejected %s (manual %s, automatic %s)" % tuple(
              "{:,}".format(stats[k]) for k in ("total", "accepted", "rejected", "rejected_manual",
                                               "rejected_automatic")), "  classes (accepted points):"]
    for k, v in stats["classes"].items():
        L.append("    %3s  %-24s %s" % (k, v["name"], "{:,}".format(v["points"])))
    L += ["", "FILTERS APPLIED"]
    for r in project.data.get("filter_runs", []):
        L.append("  %s  %s on %s: %s points changed" % (r.get("utc", ""), r.get("group", {}).get("name", "?"),
                                                         r.get("scope", "?"), "{:,}".format(r.get("changed", 0))))
        for f in r.get("group", {}).get("filters", []):
            if f.get("enabled", True):
                L.append("      - %s %s" % (f["key"], {k: v for k, v in f.get("values", {}).items() if v != ""}))
    L += ["", "OUTPUTS"] + ["  %s: %s" % (k, v["file"]) for k, v in outputs.items()]
    if warnings:
        L += ["", "WARNINGS"] + ["  - " + w for w in warnings]
    return "\n".join(L) + "\n"


def used_frame(project, dcs=None):
    """(frame, epoch) of the coordinates of the points: the datum the trajectory was moved to (ITRF2020 PPP
    trajectories), else the frame of the trajectory actually used, as georeferenced (not the shared crs fact,
    which follows PosPro's latest run)."""
    td = project.data.get("trajectory_datum")
    if td:
        return td.get("to") or None, (td.get("from") or {}).get("epoch")
    t = (project.data.get("inputs") or {}).get("trajectory") or {}
    fr = t.get("frame") or {}
    if t.get("source") == "pospro" and fr:
        return fr.get("name"), fr.get("epoch")
    if t.get("source") == "realtime":
        return "Certus real-time solution (frame of the RTK corrections)", None
    c = (dcs.shared("crs") if dcs is not None else None) or {}
    return c.get("frame"), c.get("epoch")


def used_stages(inp):
    """built_from.stages: the handover ids the points were georeferenced with. "ignored" for AlignPro when
    the project properties chose the recorded or entered mounting on purpose."""
    used = {}
    if (inp.get("trajectory") or {}).get("handover_id"):
        used["pospro"] = inp["trajectory"]["handover_id"]
    if (inp.get("alignment") or {}).get("handover_id"):
        used["alignpro"] = inp["alignment"]["handover_id"]
    elif (inp.get("override") or {}).get("source") in ("recorded", "entered"):
        used["alignpro"] = "ignored"
    return used


def _publish_api8(dcs, od, data_files, fields, version, used):
    """DCS Core API 8: publish, then rewrite built_from.stages (without the "ignored" marker, which API 8
    does not know)."""
    from dcs_core import files as dfiles
    from dcs_core import handover
    used = {k: v for k, v in used.items() if v != "ignored"}
    doc = handover.publish(dcs, "cloudpro", data_files, fields, PLUGIN, version, "CloudPro core")
    bf = dict(doc["built_from"])
    if bf.get("stages") != used:
        bf["stages"] = used
        doc["built_from"] = bf
        dfiles.atomic_write_json(os.path.join(od, "cloud.json"), dict(doc))
        dcs.update_stage("cloudpro", by=PLUGIN, built_from=bf)
    return doc


def publish(project, outputs, warnings=(), version=""):
    """outputs: {key: {"path": abs path in out/, "format": ..., ...}} already written there.
    Writes cloud_report.txt, then cloud.json through DCS Core. Returns the handover."""
    from dcs_core import files as dfiles
    from dcs_core import handover
    dcs = project.dcs
    if dcs is None:
        raise ValueError("publishing needs a DCS project")
    od = handover.out_dir(dcs, "cloudpro")
    stats = point_stats(project)
    inp = project.data.get("inputs") or {}
    warnings = list(warnings)
    if (inp.get("alignment") or {}).get("calibration") != "applied":
        warnings.append("no calibration: the Lidar mounting recorded in the capture was used")
    outs = {k: dict({kk: vv for kk, vv in v.items() if kk != "path"}, file=os.path.basename(v["path"]))
            for k, v in outputs.items()}
    rep = os.path.join(od, "cloud_report.txt")
    dfiles.atomic_write_bytes(rep, report_text(project, stats, outs, warnings).encode("utf-8"))
    data_files = {k: v["path"] for k, v in outputs.items()}
    data_files["report"] = rep
    td = project.data.get("trajectory_datum")
    frame, epoch = used_frame(project, dcs)
    fields = {
        "calibration": (inp.get("alignment") or {}).get("calibration", "no calibration"),
        "trajectory": inp.get("trajectory") or {},
        "alignment": inp.get("alignment") or {},
        "georeferencing": {"mount_deg": inp.get("mount_deg"), "lidar_lever_m": inp.get("lidar_lever_m"),
                           "latency_s": inp.get("latency_s"), "convention": "navaq_hesai",
                           "georeferenced": inp.get("georeferenced")},
        "crs": {"horizontal": project.crs, "frame": frame, "epoch": epoch,
                "trajectory_datum": (td or {}).get("note"),
                "height": project.data.get("vertical", "ellipsoidal"), "geoid": project.data.get("geoid") or None},
        "lines": [{"id": l["id"] + 1, "name": l.get("name"), "points": l.get("points"),
                   "files": [dfiles.portable(f, dcs.job_folder) for f in l.get("files", [])],
                   "returns": l.get("returns"), "range_m": [l.get("rmin"), l.get("rmax")]} for l in project.lines],
        "points": stats,
        "filters": project.data.get("filter_runs", []),
        "outputs": outs,
        "warnings": warnings,
    }
    used = used_stages(inp)
    try:
        # DCS Core API 9: built_from.stages = the upstream results really used ("ignored" = AlignPro left
        # out on purpose: Project properties > recorded or entered mounting)
        # inputs: the survey-line files the points were really made from
        doc = handover.publish(dcs, "cloudpro", data_files, fields, PLUGIN, version, "CloudPro core", used=used,
                               inputs=sorted({f for l in project.lines for f in l.get("files", [])}))
    except TypeError:
        doc = _publish_api8(dcs, od, data_files, fields, version, used)
    project.data.setdefault("published", []).append({"utc": doc["created"], "id": doc["id"],
                                                     "outputs": list(outs)})
    project.log("Published cloud.json (%s) with %s" % (doc["id"][:19], ", ".join(outs)))
    project.save()
    return doc


def deliver(project, las=True, per_line=False, dem_cell=0.0, include_rejected=False, classes=None,
            progress=None, cancel=None, version=""):
    """Write the standard deliverables to out/ (all or nothing: temporary names, then renamed) and publish."""
    od = project.out_dir
    base = project.name
    tmp_paths = {}
    outputs = {}
    try:
        if las:
            p = os.path.join(od, ".tmp_%s.las" % base)
            n = EX.export_las(project, p, None, None, include_rejected, classes, False,
                              (lambda f, m: progress(0.7 * f, m)) if progress else None, cancel)
            tmp_paths["las"] = (p, os.path.join(od, "%s.las" % base))
            outputs["las"] = {"format": "LAS 1.4 PDRF 6", "points": n, "classes": sorted(classes) if classes else
                              "all", "rejected": "withheld" if include_rejected else "excluded"}
        if dem_cell and dem_cell > 0:
            G, x0, yt = EX.grid_surface(project, dem_cell, "mean", None, None, {2}, 2,
                                        (lambda f, m: progress(0.7 + 0.25 * f, "ground grid")) if progress else None)
            p = EX.write_grid(os.path.join(od, ".tmp_%s_ground.tif" % base), G, x0, yt, dem_cell, project.crs)
            ext = os.path.splitext(p)[1]
            tmp_paths["ground_grid"] = (p, os.path.join(od, "%s_ground%s" % (base, ext)))
            outputs["ground_grid"] = {"format": "GeoTIFF" if ext == ".tif" else "ESRI ASCII grid",
                                      "cell_m": dem_cell, "statistic": "mean of class 2"}
        if cancel and cancel():
            raise EX.Cancelled("cancelled: nothing was published")
        # the published cloud.json goes first: a publish that fails from here on reads as "not published",
        # never as a cloud.json describing other files
        try:
            os.unlink(os.path.join(od, "cloud.json"))
        except OSError:
            pass
        for k, (a, b) in tmp_paths.items():
            os.replace(a, b)
            outputs[k]["path"] = b
    except BaseException:
        for a, _b in tmp_paths.values():
            try:
                os.unlink(a)
            except OSError:
                pass
        raise
    from . import chain
    warnings = chain.georef_state(project) + list((project.data.get("inputs") or {}).get("warnings") or []) + \
        list(getattr(project, "last_export_warnings", []) or [])
    return publish(project, outputs, warnings, version)
