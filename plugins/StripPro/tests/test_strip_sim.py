"""StripPro's solver on the simulated survey (tests/sim.py): after the adjustment the points must be much closer
to where they really are, inside the recording holes above all, and the lines must agree.

    python3 StripPro/tests/test_strip_sim.py      (from the plugins folder)
"""
import os
import sys
import time

import numpy as np

PLUGINS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PLUGINS)

from StripPro.core import strip as ST    # noqa: E402
from StripPro.tests import sim          # noqa: E402


def err(p, q):
    return np.linalg.norm(p - q, axis=1)


def main():
    t0 = time.time()
    d = sim.make()
    t, p, s, line, pt = d["t"], d["p"], d["s"], d["line"], d["p_true"]
    inh = ST._in_holes_pad(t, d["holes"], 0.0)
    e0 = err(p, pt)
    print("simulated %d points in %.0f s; %d holes; error before: median %.1f cm, in holes %.1f cm, 95 %% %.1f cm"
          % (len(t), time.time() - t0, len(d["holes"]), 100 * np.median(e0), 100 * np.median(e0[inh]),
             100 * np.percentile(e0, 95)))
    cor = {l: ST.Correction(ST.knots_for(a - 1, b + 1, holes=d["holes"])) for l, (a, b) in d["spans"].items()}
    sv = ST.Solver(cor, d["holes"])
    t1 = time.time()
    hist = sv.solve(line, t, p, s, progress=lambda f, h: print("  iteration %d: %d pairs (%d between lines), median "
                                                                 "%.2f cm, step %.2f cm" % (
                                                                     h["iteration"], h["pairs"], h["inter"],
                                                                     h["median_before_cm"], h["step_max_cm"])))
    p2, _s2 = sv.corrected(line, t, p, s)
    e1 = err(p2, pt)
    print("solved in %.0f s; error after: median %.1f cm, in holes %.1f cm, 95 %% %.1f cm" % (
        time.time() - t1, 100 * np.median(e1), 100 * np.median(e1[inh]), 100 * np.percentile(e1, 95)))
    r0 = ST.residuals(line, t, p, s)
    r1 = ST.residuals(line, t, p, s, sv)
    print("plane separations, median cm: same line %.2f -> %.2f, between lines %.2f -> %.2f" % (
        100 * np.median(r0["same"]), 100 * np.median(r1["same"]), 100 * np.median(r0["inter"]),
        100 * np.median(r1["inter"])))
    assert np.median(e1[inh]) < 0.5 * np.median(e0[inh]), "holes not improved enough"
    assert np.percentile(e1, 95) < 0.6 * np.percentile(e0, 95)
    assert np.median(r1["inter"]) <= np.median(r0["inter"])
    good = ~ST._in_holes_pad(t, d["holes"], 0.3)
    assert np.percentile(e1[good], 90) < np.percentile(e0[good], 90) + 0.01, "good data made worse"
    print("where the INS had data: 90 %% %.1f -> %.1f cm" % (100 * np.percentile(e0[good], 90),
                                                            100 * np.percentile(e1[good], 90)))
    print("OK")


if __name__ == "__main__":
    main()
