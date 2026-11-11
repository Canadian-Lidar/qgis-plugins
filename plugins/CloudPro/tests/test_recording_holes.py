"""Recording holes (CloudPro 2.4.0, DCS Core 1.13.0): when PosPro's trajectory has no records for a share of the
lines' time (mapper 2.9.0 captures lost about a third of the INS data), the line separation check says so first,
turns calibration / trajectory findings into notes (offsets measured across holes follow the holes), tags each
problem area with the longest hole in its times, and writes both to the feedback files.

    python3 CloudPro/tests/test_recording_holes.py      (from the plugins folder)
"""
import json
import os
import sys
import tempfile

import numpy as np

PLUGINS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PLUGINS)

from CloudPro.core import separation as S              # noqa: E402
from CloudPro.tests import test_calibration_check as TC  # noqa: E402
from dcs_core import sbet as dsbet                      # noqa: E402


def holes_for(T0, every=2.4, gap=0.8, span=(0, 240)):
    """Holes like Shirley's: gap s missing every `every` s, plus one long hole over the bump of Line 2."""
    starts = np.arange(T0 + span[0] + 1.0, T0 + span[1], every)
    sp = [[a, a + gap] for a in starts]
    b0 = T0 + 100 + (TC.L - TC.BUMP[1]) / TC.SPEED
    sp = [s for s in sp if not (b0 - 1 < s[0] < b0 + 4)] + [[b0, b0 + 3.1]]
    sp.sort()
    t = np.arange(T0, T0 + span[1], 0.005)
    keep = ~dsbet.in_holes(t, sp)
    return dsbet.holes_summary(t[keep], spans=True)


def main():
    # DCS Core: holes from record times
    t = np.r_[np.arange(0, 1, 0.005), np.arange(1.8, 3, 0.005)]
    h = dsbet.holes_summary(t)
    assert h["count"] == 1 and abs(h["longest_s"] - 0.805) < 0.01 and dsbet.holes_matter(h), h
    assert list(dsbet.in_holes([0.5, 1.2, 2.0], h["spans"])) == [False, True, False]
    print("dcs_core.sbet.holes: %d hole, %.2f s, %.0f %% missing" % (h["count"], h["longest_s"], h["missing_pct"]))

    p = TC.build(tempfile.mkdtemp())
    rh = holes_for(TC.T0)
    orig = S.recording_holes
    S.recording_holes = lambda project: rh
    try:
        r = S.run(p)
    finally:
        S.recording_holes = orig
    rec = r.data["recording"]
    assert rec and rec["missing_pct"] > 20 and rec["longest_s"] >= 3.0, rec
    q = r.pairs[0]
    assert q["in_holes"] > 0.2, q["in_holes"]
    f = S.pair_findings(q, 0.03)
    assert f[0][0] == "recording" and "not calibration or trajectory findings" in f[0][1], f
    assert not any(k in ("calibration", "trajectory", "either") for k, _ in f), f
    assert not S.calibration_pairs(r, 0.03)
    v = S.verdict(r, 0.03)
    assert "Recording holes" in v and "Calibration (AlignPro)" not in v, v
    print("pair: %.0f %% of patches in holes; finding: %s" % (100 * q["in_holes"], f[0][1][:90]))
    a = r.problem_areas(0.03)
    assert a and a[0]["hole_s"] >= 3.0, [(x["id"], x.get("hole_s")) for x in a]
    print("problem area #%d: in a %.1f s recording hole" % (a[0]["id"], a[0]["hole_s"]))

    # saved and loaded: the findings stay; the feedback files carry the holes
    r2 = S.load(p)
    assert S.pair_findings(r2.pairs[0], 0.03)[0][0] == "recording"
    assert r2.problem_areas(0.03)[0]["hole_s"] == a[0]["hole_s"]
    S.write_feedback(p, r2, 0.03)
    fb = json.load(open(os.path.join(p.out_dir, "problem_areas.json")))
    cc = json.load(open(os.path.join(p.out_dir, "calibration_check.json")))
    assert fb["recording_holes"]["count"] == rec["count"] and fb["areas"][0]["recording_hole_s"] >= 3.0
    assert cc["pairs"][0]["findings"][0]["kind"] == "recording" and not cc["pairs"][0]["calibration"]
    assert "in a 3.1 s recording hole" in "\n".join(S.report_lines(r2, 0.03))
    print("feedback files and report carry the holes")

    # without holes nothing changes
    r3 = S.run(p)
    assert r3.data["recording"] is None and S.pair_findings(r3.pairs[0], 0.03)[0][0] != "recording"
    assert S.calibration_pairs(r3, 0.03)
    print("OK")


if __name__ == "__main__":
    main()
