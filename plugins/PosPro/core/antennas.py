"""Where were GPS1 (primary) and GPS2 (secondary) really? A check for antennas plugged in the wrong way.

The GPS1 -> GPS2 baseline is measured from the recorded carrier phase (moving base, no base station
needed). While the vehicle drives straight, its direction of travel (course over ground) is known
without the antennas, so the baseline can be put into the vehicle frame (forward, starboard, down)
independently of the Certus's heading, which itself comes from the antennas and is turned 180° when
they are swapped. The measured vector is compared with the installation (GPS2 - GPS1):

    ok        measured ≈ installed
    swapped   measured ≈ -installed: GPS1 is where GPS2 should be (cables PRIM/SEC swapped, or the
              offsets were entered the wrong way round)
    unclear   neither (wrong offsets, a moved antenna, too little data)

No Qt here.
"""
import bisect
import math
import struct

RT = struct.Struct("<dddd3f3f3f3fHH")
MIN_SPEED = 3.0              # m/s: course over ground is a good heading only when moving
MAX_TURN = math.radians(3)   # rad/s: and driving fairly straight (no crabbing in turns)
MAX_POINTS = 2000


def read_rt(path):
    """[(t, vn, ve, heading, yaw_rate)] from rt.bin (the Certus's own solution, packet 20)."""
    from .results import read_rt_array
    rt = read_rt_array(path)
    if rt is None or not len(rt):
        return []
    v, a, g = rt["vel"].astype(float), rt["att"].astype(float), rt["gyr"].astype(float)
    return list(zip(rt["t"].tolist(), v[:, 0].tolist(), v[:, 1].tolist(), a[:, 2].tolist(), g[:, 2].tolist()))


def _wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def _median(v):
    s = sorted(v)
    return s[len(s) // 2] if s else 0.0


def analyse(sols, rt, lever1, lever2):
    """sols: [(t, e, n, u, q)] GPS1 -> GPS2 baseline; rt: read_rt(). Returns the result dict."""
    fixed = [s for s in sols if s[4] == 1]
    ts = [r[0] for r in rt]
    moving, still = [], []
    head_diff = []
    for t, e, n, u, _q in fixed:
        k = bisect.bisect_left(ts, t)
        k = min(max(k, 0), len(rt) - 1)
        if k > 0 and abs(ts[k - 1] - t) < abs(ts[k] - t):
            k -= 1
        if not rt or abs(ts[k] - t) > 0.1:
            continue
        _t, vn, ve, hd, yr = rt[k]
        if math.hypot(vn, ve) >= MIN_SPEED and abs(yr) <= MAX_TURN:
            c = math.atan2(ve, vn)
            moving.append((t, n * math.cos(c) + e * math.sin(c), -n * math.sin(c) + e * math.cos(c), -u))
            head_diff.append(_wrap(hd - c))
        else:
            # standing or turning: the Certus heading is all there is (and it depends on the antennas)
            still.append((t, n * math.cos(hd) + e * math.sin(hd), -n * math.sin(hd) + e * math.cos(hd), -u))
    expected = [lever2[i] - lever1[i] for i in range(3)]
    use, basis = (moving, "course") if len(moving) >= 20 else (still, "certus")
    res = {"fixed": len(fixed), "epochs": len(sols), "moving": len(moving), "still": len(still), "basis": basis,
           "expected": [round(x, 3) for x in expected], "lever1": list(lever1), "lever2": list(lever2)}
    if len(use) < 20:
        res.update(verdict="unclear", measured=None, points=[],
                   text="Too few fixed GPS1–GPS2 epochs (%d fixed of %d) to tell where the antennas were. "
                        "Use a recording with some open-sky driving." % (len(fixed), len(sols)))
        return res
    m = [_median([p[i] for p in use]) for i in range(1, 4)]
    sd = [math.sqrt(sum((p[i] - m[i - 1]) ** 2 for p in use) / max(1, len(use) - 1)) for i in range(1, 4)]
    step = max(1, len(use) // MAX_POINTS)
    res.update(measured=[round(x, 3) for x in m], sd=[round(x, 3) for x in sd],
               length=round(math.sqrt(sum(x * x for x in m[:2])), 3), points=[p for p in use[::step]])
    if head_diff:
        sx = sum(math.sin(d) for d in head_diff)
        cx = sum(math.cos(d) for d in head_diff)
        res["certus_heading_off_deg"] = round(math.degrees(math.atan2(sx, cx)), 1)
    exp_h = math.hypot(expected[0], expected[1])
    d_ok = math.hypot(m[0] - expected[0], m[1] - expected[1])
    d_sw = math.hypot(m[0] + expected[0], m[1] + expected[1])
    side = lambda v: "%.2f m %s" % (abs(v), "to starboard" if v > 0 else "to port")       # noqa: E731
    fwd = lambda v: "%.2f m %s" % (abs(v), "ahead" if v > 0 else "behind")               # noqa: E731
    def where(v):
        parts = [side(v[1])] if abs(v[1]) >= 0.05 else []
        if abs(v[0]) >= 0.05:
            parts.append(fwd(v[0]))
        return " and ".join(parts) or "at the same place as GPS1"
    meas_txt = "GPS2 was measured %s of GPS1" % where(m)
    exp_txt = "the installation says %s" % where(expected)
    tol = max(0.15, 0.15 * exp_h)
    if exp_h < 0.1:
        res.update(verdict="unclear", text="%s, but no GPS2 offset is entered on the Installation page, so "
                                           "there is nothing to compare with." % meas_txt)
    elif d_ok <= tol:
        res.update(verdict="ok", text="The antennas are where the installation says: %s (%s)." % (meas_txt, exp_txt))
    elif d_sw <= tol:
        res.update(verdict="swapped", text="The antennas look swapped: %s, but %s. GPS1 (primary) is where "
                                           "GPS2 should be. Either the PRIM and SEC cables were plugged in the "
                                           "wrong way round, or the offsets were entered the wrong way round."
                                           % (meas_txt, exp_txt))
    else:
        res.update(verdict="unclear", text="%s; %s. That is neither the installation nor the antennas swapped: "
                                           "check the antenna offsets (or whether an antenna was moved)."
                                           % (meas_txt, exp_txt))
    off = res.get("certus_heading_off_deg")
    if off is not None and abs(abs(off) - 180) < 30:
        res["text"] += (" The Certus's own heading was about 180° off the direction of travel during the drive, "
                        "which is what a dual-antenna heading does with swapped antennas.")
    if basis == "certus":
        # the Certus heading comes from the antennas themselves: swapped antennas turn it 180°, which hides the swap
        res["verdict"] = "unclear"
        res["text"] = ("Not enough straight driving to tell: %s using the Certus's heading, but that heading comes "
                       "from the antennas themselves, so swapped antennas would look the same. Drive a straight "
                       "stretch above 11 km/h for a firm answer." % meas_txt)
    return res


def check(project, ctx):
    """Run the baseline and analyse it against the installation."""
    from . import pipeline
    sols, rt_path = pipeline.baseline_solutions(project, ctx)
    inst = project.cfg["installation"]
    return analyse(sols, read_rt(rt_path), inst.get("lever_antenna") or [0, 0, 0],
                   inst.get("lever_antenna2") or [0, 0, 0])


def offsets_ne(roll, pitch, heading, lever):
    """North and east offsets (arrays, m) of a body-frame lever [F, S, D] for numpy attitude arrays (rad)."""
    import numpy as np
    cr, sr, cp, sp, ch, sh = np.cos(roll), np.sin(roll), np.cos(pitch), np.sin(pitch), np.cos(heading), np.sin(heading)
    x, y, z = lever
    n = cp * ch * x + (sr * sp * ch - cr * sh) * y + (cr * sp * ch + sr * sh) * z
    e = cp * sh * x + (sr * sp * sh + cr * ch) * y + (cr * sp * sh - sr * ch) * z
    return n, e
