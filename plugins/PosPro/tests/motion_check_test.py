"""The marine motion check (PosPro 2.9.0) on a simulated boat: rolling and pitching in waves, turning now and
then; two heads mounted tilted 40 deg to port and starboard, whose own attitude takes 70 % of the turning
acceleration for tilt and whose clock runs 0.12 s off the autopilot's. The check must find the mounting itself,
the time offset, the error per m/s2 in turns, that the heads agree, and the autopilot's heading offset."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from PosPro.core import marine as M      # noqa: E402

G = 9.81


def rot_x(a):
    c, s = np.cos(a), np.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])


class FakeRec:
    def __init__(self, att):
        self._att = att

    def vehicle(self):
        return {"att": self._att, "boot_fit": (1.0, 0.0, 0.002)}


def simulate():
    t = np.arange(0, 1200, 0.04)                                   # 25 Hz autopilot
    rng = np.random.default_rng(5)
    roll = np.radians(2.0) * np.sin(2 * np.pi * t / 4.3) + np.radians(0.8) * np.sin(2 * np.pi * t / 2.1 + 1)
    pitch = np.radians(1.2) * np.sin(2 * np.pi * t / 3.7 + 0.4)
    rate = np.where((t % 300) > 240, np.radians(10.0), 0.0)        # a 60 s turn every 5 min
    rate = np.convolve(rate, np.ones(50) / 50, "same")
    yaw = np.cumsum(rate) * 0.04
    speed = 2.0
    att = np.rec.fromarrays([t, roll.astype("f4"), pitch.astype("f4"), yaw.astype("f4")], names="t,roll,pitch,yaw")
    # the boat's course: its heading plus 3 deg of crab
    cog = yaw + np.radians(3.0)
    tp = t[::5]                                                     # 5 Hz positions
    vn, ve = speed * np.cos(np.interp(tp, t, cog)), speed * np.sin(np.interp(tp, t, cog))
    pos = np.zeros(len(tp), M.POS)
    pos["t"] = tp
    # heads: up vector in the head frame = R_mount^T . (boat up, with 70 % of the turn acceleration taken as tilt)
    alat = speed * rate
    roll_seen = roll + 0.7 * np.arctan(alat / G)
    heads = {}
    for did, m in ((1, np.radians(40.0)), (2, np.radians(-40.0))):
        th = np.arange(5, 1195, 0.15) - 0.12                          # head clock 0.12 s early
        Ub = M._up_from_rp(np.interp(th + 0.12, t, roll_seen), np.interp(th + 0.12, t, pitch))
        Ub += rng.normal(0, 0.0004, Ub.shape)
        Uh = Ub @ rot_x(m)                                            # = (R^T Ub^T)^T with R = rot_x(m)
        A = np.zeros(len(th), dtype=[("t", "f8"), ("up", "f4", 3)])
        A["t"], A["up"] = th, Uh
        heads[did] = A
    return FakeRec(att), heads, pos, vn, ve


def test_motion_check():
    rec, heads, pos, vn, ve = simulate()
    m = M.motion_check(rec, heads, pos, vn, ve, cog_min=1.0)
    h = m["heads"]["1"]
    assert abs(h["offset_s"] - 0.12) < 0.02, h
    R = np.array(h["mount_rotation"])
    ang = np.degrees(np.arccos(np.clip((np.trace(R @ rot_x(np.radians(40.0)).T) - 1) / 2, -1, 1)))
    assert ang < 0.5, ang                                          # the head's mounting found
    expect = 0.7 * np.degrees(1 / G)                               # deg of roll error per m/s2
    assert abs(abs(h["turn_slope_deg_per_ms2"]) - expect) < 0.6, (h, expect)
    assert h["straight_deg"] < 0.1 and h["turning_deg"] > h["straight_deg"], h
    assert m["heads_deg"] < 0.1, m                                 # the two heads agree
    assert abs(abs(m["heading_offset_deg"]) - 3.0) < 0.3, m        # the crab: course - heading
    s = M.motion_summary({"rec": m})
    assert any("turns" in f for f in s["flags"]) and any("clock" in f for f in s["flags"]), s["flags"]
    assert M.motion_summary({"rec": {}}) == {}
    print("motion check: offset %.3f s, slope %.2f deg per m/s2 (%.2f expected), heads %.3f deg, heading %+.1f deg; "
          "flags %s" % (h["offset_s"], h["turn_slope_deg_per_ms2"], expect, m["heads_deg"], m["heading_offset_deg"],
                        s["flags"]))


if __name__ == "__main__":
    test_motion_check()
    print("ok")
