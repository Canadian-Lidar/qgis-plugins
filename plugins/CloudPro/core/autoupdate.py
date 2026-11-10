"""Georeferencing in the background when the chain's inputs change (CloudPro 1.9.6, DCS Core API 11).

When PosPro or AlignPro publishes, DCS Core tells CloudPro (registry.on_publish). If this job has been
georeferenced before, its points no longer match the published inputs, and everything upstream is current
(PosPro done; AlignPro done too unless the job uses the recorded or entered mounting), CloudPro georeferences again
with the settings of the last run and carries the edits over - so the job opens ready to clean instead of
waiting on Update. Nothing is published: publishing stays the user's step.

    decide(dcs_path)  -> (run?, reason)
    run(dcs_path, progress=None, cancel=None)  -> (lines [(lid, stats)], edits carried over)
"""
import os

from . import chain, importer as IM

PLUGIN = "CloudPro"


def _project(dcs_path):
    from .project import Project
    return Project.open_any(dcs_path)


def enabled(project):
    """The project's choice (Georeference dialog: 'Update automatically ...'); on by default."""
    return bool(project.data.get("auto_update", True))


def decide(dcs_path, project=None):
    """Whether a background georeferencing should run now for this DCS project, and why (or why not)."""
    if not dcs_path or not os.path.exists(dcs_path):
        return False, "no project"
    try:
        p = project or _project(dcs_path)
    except Exception as e:                      # noqa: BLE001
        return False, "cannot open CloudPro's stage (%s)" % e
    if p.dcs is None or not p.lines:
        return False, "never georeferenced (the first georeferencing is done in CloudPro)"
    if not enabled(p):
        return False, "automatic updates are off for this job"
    dcs = p.dcs
    st, why = dcs.state("cloudpro")
    if st == "running":
        return False, "CloudPro is already working on this job"
    ps, _ = dcs.state("pospro")
    if ps != "done":
        return False, "PosPro is %s" % ps
    ov = (p.data.get("override") or {}).get("source")
    if ov not in ("recorded", "entered"):
        a, _ = dcs.state("alignpro")
        if a not in ("done", "none"):
            return False, "AlignPro is %s (its calibration will change)" % a
    try:
        reasons = chain.georef_state(p)
    except Exception as e:                      # noqa: BLE001
        return False, "cannot read the chain's inputs (%s)" % e
    if not reasons:
        return False, "the points are current"
    return True, "; ".join(reasons)


def options(project):
    """The settings of the last georeferencing (recorded with each line), as ImportOptions."""
    last = project.lines[-1] if project.lines else {}
    o = IM.ImportOptions(returns=last.get("returns", "both"), drop_duplicates=bool(last.get("drop_duplicates", True)),
                         rmin=float(last.get("rmin", 1.0)), rmax=float(last.get("rmax", 120.0)),
                         decimate=int(last.get("decimate", 1) or 1), min_speed=float(last.get("min_speed", 0.0) or 0.0),
                         bore=tuple(last.get("bore", (0.0, 0.0, 0.0))))
    gp = project.data.get("geoid_path")
    if gp:
        from .export import geoid_sampler
        o.geoid = geoid_sampler(gp)
    return o


def run(dcs_path, progress=None, cancel=None, project=None):
    """Georeference the job again with the last settings, edits carried over (Project.begin_rebuild keeps the
    previous points on any failure). Returns (lines, edits carried over)."""
    p = project or _project(dcs_path)
    inp = chain.resolve(p.dcs, override=p.data.get("override"))
    if not inp.files:
        raise ValueError("no .clraw files found in the job's captures")
    opts = options(p)
    try:
        with p.dcs.run("cloudpro", PLUGIN, _version()):
            out, rest = IM.import_chain(p, inp, opts, None, progress, cancel, keep_edits=True, settings={})
        p.run_log("georeference (automatic)", inp.summary() + ["warning: " + w for w in inp.warnings] +
                  ["L%d: %s points %s" % (l + 1, st["points"], st) for l, st in out] +
                  ["edits carried over: %d" % rest])
        p.log("Georeferenced automatically after the chain changed: %d line(s), %s points" % (
            len(out), "{:,}".format(sum(st["points"] for _, st in out))))
        p.save()
    finally:
        p.store.close()
    return out, rest


def _version():
    from .. import __version__
    return __version__
