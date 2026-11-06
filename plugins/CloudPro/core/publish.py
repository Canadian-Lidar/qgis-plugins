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
    crs_fact = dcs.shared("crs") or {}
    td = project.data.get("trajectory_datum")
    fields = {
        "calibration": (inp.get("alignment") or {}).get("calibration", "no calibration"),
        "trajectory": inp.get("trajectory") or {},
        "alignment": inp.get("alignment") or {},
        "georeferencing": {"mount_deg": inp.get("mount_deg"), "lidar_lever_m": inp.get("lidar_lever_m"),
                           "latency_s": inp.get("latency_s"), "convention": "navaq_hesai",
                           "georeferenced": inp.get("georeferenced")},
        "crs": {"horizontal": project.crs,
                "frame": (td or {}).get("to") or crs_fact.get("frame"),
                "epoch": (td or {}).get("from", {}).get("epoch") if td else crs_fact.get("epoch"),
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
    doc = handover.publish(dcs, "cloudpro", data_files, fields, PLUGIN, version, "CloudPro core")
    # built_from must name the upstream results the points were really georeferenced with, so CloudPro
    # shows as out of date when PosPro / AlignPro published again after the georeferencing.
    used = {}
    if (inp.get("trajectory") or {}).get("handover_id"):
        used["pospro"] = inp["trajectory"]["handover_id"]
    if (inp.get("alignment") or {}).get("handover_id"):
        used["alignpro"] = inp["alignment"]["handover_id"]
    bf = dict(doc["built_from"])
    if bf.get("stages") != used:
        bf["stages"] = used
        doc["built_from"] = bf
        path = os.path.join(od, "cloud.json")
        dfiles.atomic_write_json(path, {k: v for k, v in doc.items()})
        dcs.update_stage("cloudpro", by=PLUGIN, built_from=bf)
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
            raise RuntimeError("cancelled")
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
    warnings = chain.georef_state(project) + list((project.data.get("inputs") or {}).get("warnings") or [])
    return publish(project, outputs, warnings, version)
