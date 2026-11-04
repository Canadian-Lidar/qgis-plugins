"""CloudPro batch processing without the GUI (same core as the plugin).

In the DCS chain (a job's shared project):

    python -m cloudpro.cli import  D:/Job/Processing/Street/Street.dcsproj        (PosPro trajectory + AlignPro mounting)
    python -m cloudpro.cli update  D:/Job/Processing/Street/Street.dcsproj        (re-georeference, keep edits)
    python -m cloudpro.cli filter  D:/Job/Processing/Street/Street.dcsproj --group "Mobile mapping - standard clean"
    python -m cloudpro.cli publish D:/Job/Processing/Street/Street.dcsproj --dem 0.5   (LAS + grid + cloud.json)
    python -m cloudpro.cli info    D:/Job/Processing/Street/Street.dcsproj

Stand-alone projects (no chain):

    python -m cloudpro.cli create  D:/jobs MyJob --crs EPSG:2953
    python -m cloudpro.cli import  D:/jobs/MyJob/MyJob.cloudpro.json --sbet sbet_20261102.out --clraw D:/captures
    python -m cloudpro.cli filter  D:/jobs/MyJob/MyJob.cloudpro.json --group "Mobile mapping - standard clean"
    python -m cloudpro.cli export  D:/jobs/MyJob/MyJob.cloudpro.json --las out.las [--per-line] [--classes 2]
    python -m cloudpro.cli export  D:/jobs/MyJob/MyJob.cloudpro.json --grid dem.tif --cell 0.5 --classes 2
    python -m cloudpro.cli info    D:/jobs/MyJob/MyJob.cloudpro.json

Run it with the QGIS Python (OSGeo4W shell on Windows) from the folder that
holds the 'cloudpro' plugin folder, e.g. %APPDATA%/QGIS/QGIS3/profiles/default/python/plugins.
"""
import argparse
import glob
import json
import os
import sys
import time


def _progress(f, msg=None):
    sys.stdout.write("\r  %5.1f%%  %s        " % (100 * f, msg if isinstance(msg, str) else ""))
    sys.stdout.flush()


def main(argv=None):
    from .core import clraw, importer, export as EX, filters as F
    from .core.edits import EditManager
    from .core.project import Project
    from .core.runner import run_group_tiled
    from .core.spatial import parse_set
    ap = argparse.ArgumentParser(prog="cloudpro")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("folder")
    c.add_argument("name")
    c.add_argument("--crs", default="EPSG:2953")
    c.add_argument("--tile", type=float, default=50.0)
    i = sub.add_parser("import")
    i.add_argument("project")
    i.add_argument("--clraw", nargs="+", help=".clraw files or folders (default in a DCS project: the job's captures)")
    i.add_argument("--sbet", help="PosPro SBET; omit to use the Certus real-time track")
    i.add_argument("--returns", default="both", choices=["both", "last", "strongest"])
    i.add_argument("--rmin", type=float, default=1.0)
    i.add_argument("--rmax", type=float, default=120.0)
    i.add_argument("--mount", nargs=3, type=float, help="override roll pitch heading (mapper convention, deg)")
    i.add_argument("--lever", nargs=3, type=float, help="override lever F S D (m)")
    i.add_argument("--bore", nargs=3, type=float, default=(0, 0, 0), help="boresight dR dP dH (deg, AlignPro)")
    f = sub.add_parser("filter")
    f.add_argument("project")
    f.add_argument("--group", required=True, help="group name (project or default groups) or a JSON file")
    f.add_argument("--lines", default="", help="1-based line list, e.g. 1,3-4")
    e = sub.add_parser("export")
    e.add_argument("project")
    e.add_argument("--las")
    e.add_argument("--xyz")
    e.add_argument("--grid")
    e.add_argument("--cell", type=float, default=0.5)
    e.add_argument("--stat", default="mean", choices=["mean", "min", "max", "count", "intensity"])
    e.add_argument("--classes", default="")
    e.add_argument("--lines", default="")
    e.add_argument("--per-line", action="store_true")
    e.add_argument("--include-rejected", action="store_true")
    n = sub.add_parser("info")
    n.add_argument("project")
    n.add_argument("--target", type=float, default=100.0, help="coverage target density, pts/m2 (default 100)")
    n.add_argument("--cell", type=float, default=1.0, help="coverage cell size, m (default 1)")
    u = sub.add_parser("update")
    u.add_argument("project")
    pb = sub.add_parser("publish")
    pb.add_argument("project")
    pb.add_argument("--dem", type=float, default=0.0, help="ground grid cell (m), 0 = none")
    pb.add_argument("--classes", default="")
    pb.add_argument("--include-rejected", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "create":
        p = Project.create(a.folder, a.name, a.crs, a.tile)
        print(p.path)
        return 0
    prj = Project.open_any(a.project)
    if prj.dcs is not None and a.cmd in ("import", "update", "filter", "publish"):
        return _dcs_cmd(a, prj)
    if a.cmd == "info":
        print("%s  %s  %d lines  %s points  origin %s" % (prj.name, prj.crs, len(prj.lines),
                                                           "{:,}".format(prj.store.total), prj.store.origin))
        for ln in prj.lines:
            print("  L%d %-20s %12s pts  %s" % (ln["id"] + 1, ln.get("name", ""), "{:,}".format(ln.get("points", 0)),
                                                ln.get("trajectory", "")))
        if prj.store.total:
            from .core.coverage import coverage_summary
            c = coverage_summary(prj.store, a.cell, a.target)
            print("  coverage  %.0f m2 with points (%g m cells), median %.0f pts/m2, %.0f%% at or above %g pts/m2"
                  % (c["area_m2"], a.cell, c["median"], 100 * c["ok_share"], a.target))
        if prj.dcs is not None:
            from .core import chain
            for k, label, st, why in prj.dcs.chain():
                print("  %-9s %s%s" % (label, st, ("  (" + "; ".join(why) + ")") if why else ""))
            for w in chain.georef_state(prj):
                print("  ! " + w)
        return 0
    if a.cmd == "import":
        paths = []
        for c_ in a.clraw:
            paths += glob.glob(os.path.join(c_, "**", "*.clraw"), recursive=True) if os.path.isdir(c_) else [c_]
        opts = importer.ImportOptions(returns=a.returns, rmin=a.rmin, rmax=a.rmax, bore=tuple(a.bore),
                                      mount=tuple(a.mount) if a.mount else None,
                                      lever=tuple(a.lever) if a.lever else None)
        traj = importer.load_trajectory(a.sbet) if a.sbet else None
        for key, ps in clraw.group_recordings(paths).items():          # in recording order
            rec = clraw.Recording(ps)
            tr = traj or importer.load_trajectory(rec)
            t = time.time()
            lid, st = importer.import_recording(prj, rec, tr, opts, progress=lambda f_, s: _progress(f_, "%d pts" %
                                                                                                    s["points"]))
            print("\rline %s -> L%d  %s points  %.1f s" % (rec.line, lid + 1, "{:,}".format(st["points"]),
                                                          time.time() - t))
        importer.rebuild_grids(prj)
        prj.save()
        return 0
    if a.cmd == "filter":
        groups = F.default_groups()
        gp = os.path.join(prj.root, "filters", "groups.json")
        if os.path.exists(gp):
            with open(gp) as fh:
                groups = [F.FilterGroup.from_dict(d) for d in json.load(fh)] + groups
        if os.path.exists(a.group):
            with open(a.group) as fh:
                d = json.load(fh)
            g = F.FilterGroup.from_dict(d[0] if isinstance(d, list) else d)
        else:
            m = [x for x in groups if x.name == a.group]
            if not m:
                print("no group %r; known: %s" % (a.group, ", ".join(x.name for x in groups)))
                return 1
            g = m[0]
        lines = [l - 1 for l in parse_set(a.lines)] or None
        ed = EditManager(prj)
        t = time.time()
        k = run_group_tiled(prj, g, ed, lines=lines, progress=_progress)
        from .core.importer import rebuild_grids
        rebuild_grids(prj, list(getattr(prj.store, "dirty_tiles", [])))
        prj.save()
        print("\r'%s': %s points changed in %.0f s" % (g.name, "{:,}".format(k), time.time() - t))
        return 0
    if a.cmd == "export":
        classes = parse_set(a.classes) or None
        lines = [l - 1 for l in parse_set(a.lines)] or None
        if a.las:
            k = EX.export_las(prj, a.las, None, lines, a.include_rejected, classes, a.per_line, _progress)
            print("\rLAS: %s points -> %s" % ("{:,}".format(k), a.las))
        if a.xyz:
            k = EX.export_xyz(prj, a.xyz, None, lines, a.include_rejected, classes, _progress)
            print("\rXYZ: %s points -> %s" % ("{:,}".format(k), a.xyz))
        if a.grid:
            G, x0, yt = EX.grid_surface(prj, a.cell, a.stat, None, lines, classes, 0, _progress)
            p = EX.write_grid(a.grid, G, x0, yt, a.cell, prj.crs)
            print("\rgrid -> %s" % p)
        return 0


def _dcs_cmd(a, prj):
    from .core import chain, importer, filters as F, publish
    from .core.edits import EditManager
    from .core.runner import run_group_tiled
    from .core.spatial import parse_set
    from .core.project import PLUGIN
    from . import __version__ as V
    dcs = prj.dcs
    with dcs.run("cloudpro", PLUGIN, V):
        if a.cmd in ("import", "update"):
            inp = chain.resolve(dcs, override=prj.data.get("override"))
            for line in inp.summary() + ["! " + w for w in inp.warnings]:
                print(line)
            opts = importer.ImportOptions()
            if a.cmd == "import":
                opts.returns, opts.rmin, opts.rmax = a.returns, a.rmin, a.rmax
                if a.mount:
                    inp.mount = tuple(a.mount)
                    inp.align_info = {"calibration": "no calibration", "source": "entered", "handover_id": None}
                if a.lever:
                    inp.lever = tuple(a.lever)
            files = None
            if a.cmd == "import" and a.clraw:
                files = []
                for c_ in a.clraw:
                    files += glob.glob(os.path.join(c_, "**", "*.clraw"), recursive=True) if os.path.isdir(c_) else [c_]
            out, rest = importer.import_chain(prj, inp, opts, files, progress=_progress, keep_edits=True)
            prj.run_log(a.cmd, inp.summary() + ["warning: " + w for w in inp.warnings] +
                        ["L%d: %d points" % (l + 1, st["points"]) for l, st in out] + ["edits carried over: %d" % rest])
            print("\r%d line(s), %s points%s" % (len(out), "{:,}".format(sum(s["points"] for _, s in out)),
                                               ("; %s edited points carried over" % "{:,}".format(rest)) if rest else ""))
        elif a.cmd == "filter":
            groups = F.default_groups()
            gp = os.path.join(prj.filters_dir, "groups.json")
            if os.path.exists(gp):
                with open(gp) as fh:
                    groups = [F.FilterGroup.from_dict(d) for d in json.load(fh)] + groups
            m = [x for x in groups if x.name == a.group]
            if not m:
                print("no group %r; known: %s" % (a.group, ", ".join(x.name for x in groups)))
                return 1
            lines = [l - 1 for l in parse_set(a.lines)] or None
            k = run_group_tiled(prj, m[0], EditManager(prj), lines=lines, progress=_progress)
            prj.record_filter(m[0].to_dict(), "selected lines" if lines else "entire project", k)
            importer.rebuild_grids(prj, list(getattr(prj.store, "dirty_tiles", [])))
            prj.save()
            prj.run_log("filter", ["group %s: %d points changed" % (m[0].name, k), json.dumps(m[0].to_dict())])
            print("\r'%s': %s points changed" % (m[0].name, "{:,}".format(k)))
        elif a.cmd == "publish":
            for w in chain.georef_state(prj):
                print("! " + w)
            doc = publish.deliver(prj, las=True, dem_cell=a.dem, include_rejected=a.include_rejected,
                                  classes=parse_set(a.classes) or None, progress=_progress, version=V)
            prj.run_log("publish", ["cloud.json %s (%s)" % (doc["id"], doc["calibration"])] + doc.get("warnings", []))
            print("\rpublished cloud.json %s (%s): %s" % (doc["id"][:19], doc["calibration"],
                                                         ", ".join(sorted(doc["files"]))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
