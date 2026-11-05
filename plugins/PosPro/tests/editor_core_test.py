"""Interpolation in every mode: smooth follows the road, straight cuts across, ends are carried on."""
import math
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from PosPro.core import review  # noqa: E402
from PosPro.tests import simtraj  # noqa: E402


class P:
    def __init__(self, d):
        self.work_dir, self.cfg = d, {"processing": {"mode": "tc"}}


def read(d):
    with open(os.path.join(d, "results", "trajectory.bin"), "rb") as f:
        f.seek(8)
        n = struct.unpack("<Q", f.read(8))[0]
        f.seek(review.TRJ_HDR)
        return [review.TRJ.unpack(f.read(review.TRJ.size)) for _ in range(n)]


def err(recs, t0, a, b, comp):
    M, N = review._radii(simtraj.LAT0)
    worst = 0.0
    for r in recs:
        ts = r[0] - t0
        if a <= ts <= b:
            tn, te, tu = simtraj.truth_local(ts)
            if comp == "h":
                d = math.hypot((r[1] - simtraj.LAT0) * M - tn, (r[2] - simtraj.LON0) * N * math.cos(simtraj.LAT0) - te)
            else:
                d = abs(r[3] - simtraj.H0 - tu)
            worst = max(worst, d)
    return worst


def run():
    d = tempfile.mkdtemp()
    t0, _ = simtraj.make(d)
    p = P(d)
    review.state(p)
    before_h = err(read(d), t0, 230, 246, "h")
    before_v = err(read(d), t0, 100, 112, "v")
    # smooth across the sideways jump (horizontal only), straight across the height spike (vertical only)
    review.add_edit(p, t0 + 229.5, t0 + 246.5, "interpolate", horizontal=True, vertical=False, method="smooth")
    review.add_edit(p, t0 + 99.5, t0 + 112.5, "interpolate", horizontal=False, vertical=True, method="straight")
    review.apply(p)
    recs = read(d)
    after_h, after_v = err(recs, t0, 230, 246, "h"), err(recs, t0, 100, 112, "v")
    print("sideways jump: %.3f m -> %.3f m (smooth)" % (before_h, after_h))
    print("height spike : %.3f m -> %.3f m (straight)" % (before_v, after_v))
    assert after_h < 0.2 and after_v < 0.05   # 187 m of curving road bridged
    # straight across the curve is worse horizontally than smooth
    review.reset(p)
    review.add_edit(p, t0 + 229.5, t0 + 246.5, "interpolate", horizontal=True, vertical=False, method="straight")
    review.apply(p)
    straight_h = err(read(d), t0, 230, 246, "h")
    print("sideways jump: straight line %.3f m" % straight_h)
    assert straight_h > after_h
    flags = [r for r in read(d) if r[review.I_FLAGS] & review.FLAG_INTERP]
    assert flags and all(229.4 <= r[0] - t0 <= 246.6 for r in flags)
    # a window at the very end is carried on from its good side
    review.reset(p)
    review.add_edit(p, t0 + 355.0, t0 + 360.0, "interpolate", method="smooth")
    review.apply(p)
    tail = err(read(d), t0, 355, 360, "h")
    print("end window carried on: %.3f m" % tail)
    assert tail < 1.0      # 55 m carried on along a curve at constant velocity
    # undo brings the processed trajectory back
    review.reset(p)
    review.apply(p)
    assert abs(err(read(d), t0, 230, 246, "h") - before_h) < 1e-6
    s = review.summary(p)
    assert s["interpolated"] == []
    print("OK")


if __name__ == "__main__":
    run()
