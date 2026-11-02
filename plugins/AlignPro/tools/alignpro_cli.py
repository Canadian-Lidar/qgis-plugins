#!/usr/bin/env python3
"""Headless AlignPro: create a project from .clraw files, calibrate, print the result, write the
ASCII report. Same engine as the QGIS plugin.

  python3 alignpro_cli.py PROJECT_DIR NAME CLRAW_OR_FOLDER... [--sbet FILE [--date YYYY-MM-DD]]
                          [--apply] [--sweep PARAM CENTER HALF STEPS]
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from alignpro.core.project import Project  # noqa: E402
from alignpro.core import report  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("name")
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--sbet")
    ap.add_argument("--date")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--sweep", nargs=4)
    ap.add_argument("--set", nargs=2, action="append", default=[], metavar=("KEY", "VALUE"),
                    help="override a setting, e.g. --set voxel_m 1.5")
    a = ap.parse_args()
    traj = {"type": "sbet", "path": os.path.abspath(a.sbet), "date": a.date or ""} if a.sbet else None
    p = Project.create(a.folder, a.name, a.inputs, traj)
    for k, v in a.set:
        cur = p.settings.get(k)
        p.settings[k] = type(cur)(v) if cur is not None and not isinstance(cur, bool) else (v.lower() == "true")
    t = time.time()

    def prog(f, msg):
        sys.stderr.write("\r%5.1f%% %-60s" % (f * 100, msg[:60]))
    p.load(prog)
    sys.stderr.write("\n")
    print("loaded %d lines in %.1f s; trajectory: %s" % (len(p.lines), time.time() - t, p.traj.source))
    for w in p.warnings:
        print("WARNING:", w)
    for ln in p.lines:
        s = ln.stats
        print("  %s  cog %5.1f  sog %4.1f  len %5.0f m  pts %8d  pps %5.0f" % (
            ln.name, s.get("cog", 0), s.get("sog", 0), s.get("length", 0), ln.n, s.get("lidar_pps", 0)))
    k = p.P.kind
    print("patches: %d (walls %d, sloped %d, flat %d), points %d" % (len(p.P), (k == 2).sum(), (k == 1).sum(),
                                                                       (k == 0).sum(), len(p.P.pt_line)))
    for pr in p.pair_list:
        print("  pair %s-%s %-14s dHdg %5.1f ov %3.0f%% shared %d sep %.1f" % (
            p.lines[pr["a"]].name, p.lines[pr["b"]].name, pr["type"], pr["dhdg"], pr["overlap"], pr["shared"],
            pr["separation"]))
    t = time.time()
    r = p.calibrate(prog)
    sys.stderr.write("\n")
    print("calibrated in %.1f s" % (time.time() - t))
    for row in r["combis"]:
        if row.get("ok"):
            print("  %-16s %9.4f %9.4f %9.4f   %.2f -> %.2f cm  (%d pts, %d it, rej %d)" % (
                row["label"], row["roll"], row["pitch"], row["heading"], row["before_cm"], row["after_cm"],
                row["points"], row["iterations"], row["rejected"]))
    a_ = r["advised"]
    s_ = r["sdev"]
    print("ADVISED %.4f %.4f %.4f   SDEV %.4f %.4f %.4f" % (a_["roll"], a_["pitch"], a_["heading"],
                                                           s_["roll"], s_["pitch"], s_["heading"]))
    for w in r["warnings"]:
        print("WARNING:", w)
    if a.apply:
        p.apply_mount([a_["roll"], a_["pitch"], a_["heading"]])
        print("error with advised angles: %.2f cm" % p.current_error())
    if a.sweep:
        par, c, h, n = a.sweep
        for v, e in p.sweep(par, float(c), float(h), int(n)):
            print("  %s %9.4f  %.3f cm" % (par, v, e))
    path = report.default_name(p, "txt")
    with open(path, "w") as f:
        f.write(report.ascii_report(p))
    print("report:", path)


if __name__ == "__main__":
    main()
