"""Antenna check: installed, swapped (with the Certus heading turned 180°), and standing still."""
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from PosPro.core import antennas  # noqa: E402

L1, L2 = [0.082, 0.495, -0.112], [0.082, -0.493, -0.112]       # GPS1 starboard, GPS2 port (F, S, D)


def drive(swapped=False, moving=True, n=600):
    random.seed(1)
    sols, rt = [], []
    for i in range(n):
        t = 1000.0 + i * 0.125
        course = math.radians(30 + 20 * math.sin(i / 400.0)) if moving else math.radians(75)
        v = 12.0 if moving else 0.0
        # true baseline GPS1 -> GPS2 in the body frame; swapped cables: receiver 0 sits on the port antenna
        b = [L2[k] - L1[k] for k in range(3)]
        if swapped:
            b = [-x for x in b]
        bn = b[0] * math.cos(course) - b[1] * math.sin(course)
        be = b[0] * math.sin(course) + b[1] * math.cos(course)
        sols.append((t, be + random.gauss(0, 0.004), bn + random.gauss(0, 0.004), -b[2], 1))
        certus_heading = course + (math.pi if swapped else 0.0)
        rt.append((t, v * math.cos(course), v * math.sin(course), certus_heading, 0.001))
    return sols, rt


for name, kw, want in (("installed", {}, "ok"), ("swapped", {"swapped": True}, "swapped"),
                       ("standing, installed", {"moving": False}, "unclear")):
    sols, rt = drive(**kw)
    r = antennas.analyse(sols, rt, L1, L2)
    print("%-20s %-8s measured %s  basis %s  certus off %s" % (name, r["verdict"], r["measured"], r["basis"],
                                                                 r.get("certus_heading_off_deg")))
    assert r["verdict"] == want, r["text"]
sols, rt = drive(swapped=True)
r = antennas.analyse(sols, rt, L1, L2)
assert "180" in r["text"] and "starboard" in r["text"]
print(r["text"])
# swapped while standing: the Certus heading is flipped too, so standing alone cannot tell -> must not say ok
sols, rt = drive(swapped=True, moving=False)
r = antennas.analyse(sols, rt, L1, L2)
print("standing, swapped    ", r["verdict"], "(", r["text"][-80:], ")")
assert "straight stretch" in r["text"]
print("OK")
