"""RemixPro from the command line (no QGIS needed): the same conversions as the window.

    python -m RemixPro.cli PATH [PATH ...] [--out DIR] [--beamworx] [--ie] [--qinertia] [--list] [--force]

SonarView recordings (.svlz / .svlog) and CLAM TidalCal projects give .clraw (kind "sonar"); measured offsets
from the GNSS antenna to each sonar's acoustic centre go in sonar_offsets.json beside the recordings
({"1": [F, S, D], "2": [F, S, D]} by SonarView device id, or "port" / "starboard").
Run it from the folder that holds RemixPro and dcs_core (the QGIS plugins folder, e.g.
%APPDATA%/QGIS/QGIS3/profiles/default/python/plugins). PATH: a mapper project folder, a USB copy, an unzipped
download, or single .clraw / .anpp files. Without --beamworx / --ie / --qinertia everything each recording
allows is made. --out defaults to <mapper project>/Processing/RemixPro, else a RemixPro folder beside the
inputs. Outputs already made from the same inputs are not made again (--force: make them again)."""
import argparse
import os
import sys

if __package__ in (None, ""):                                       # python RemixPro/cli.py
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "RemixPro"                                        # noqa: A001

from RemixPro.core import __version__, make, sources               # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(prog="RemixPro", description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--out", help="output folder")
    ap.add_argument("--beamworx", action="store_true", help="BeamworX .bwxraw + .xml")
    ap.add_argument("--ie", action="store_true", help="Inertial Explorer IMR + RINEX")
    ap.add_argument("--qinertia", action="store_true", help="Qinertia $qimu + RINEX")
    ap.add_argument("--clraw", action="store_true", help="SonarView .svlz/.svlog (or TidalCal projects) to .clraw")
    ap.add_argument("--list", action="store_true", help="only list what was found")
    ap.add_argument("--force", action="store_true", help="make again even when up to date")
    ap.add_argument("--include-incomplete", action="store_true", help="also lines still being copied")
    a = ap.parse_args(argv)
    items = sources.scan(a.paths)
    out = a.out or sources.default_output_root(a.paths)
    want = [k for k, on in ((sources.BEAMWORX, a.beamworx), (sources.IE, a.ie), (sources.QINERTIA, a.qinertia), (sources.CLRAW, a.clraw)) if on]
    print("RemixPro %s: %d recording(s) found; output folder %s" % (__version__, len(items), out))
    for it in items:
        print("  %-11s %-40s %s" % (sources.KIND_LABELS[it.kind], it.name,
                                     ", ".join(sources.OUTPUT_LABELS[k] for k in it.outputs) or "nothing to make"))
        for n in it.notes:
            print("              " + n)
    if a.list:
        return 0
    failed = 0
    for it in items:
        kinds = [k for k in (want or it.outputs) if k in it.outputs]
        if not kinds:
            continue
        if not it.complete and not a.include_incomplete:
            print("skipped %s: incomplete (--include-incomplete to convert it anyway)" % it.name)
            continue
        last = [-1]

        def prog(f, name=it.name):
            pct = int(f * 100)
            if pct // 10 != last[0]:
                last[0] = pct // 10
                print("  %s %3d%%" % (name, pct), flush=True)
        try:
            r = make.run(it, kinds, out, progress=prog, force=a.force)
        except Exception as e:                      # one bad recording does not stop the others
            failed += 1
            print("FAILED %s: %s" % (it.name, e))
            continue
        print("\n".join(make.describe_result(it, r)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
