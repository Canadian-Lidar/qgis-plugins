"""Hesai PandarXT-32 point packets (protocol 6.1, 1080 bytes), vectorised with numpy."""
import numpy as np

PKT = 1080
N_CH = 32
N_BLK = 8
RETURN_MODES = {0x33: "First", 0x37: "Strongest", 0x38: "Last", 0x39: "Last + Strongest",
                0x3B: "Last + First", 0x3C: "First + Strongest"}

DEFAULT_CAL = [(14.916 - i * (31.076 / 31), 0.0) for i in range(32)]


def packets_to_array(payloads):
    good = [bytes(p) for p in payloads if len(p) == PKT]
    if not good:
        return np.zeros((0, PKT), np.uint8)
    return np.frombuffer(b"".join(good), np.uint8).reshape(-1, PKT)


def header_info(a):
    """Summary of the first packet: laser count, unit, returns, mode, rpm."""
    if len(a) == 0:
        return {}
    p = a[0]
    return {"lasers": int(p[6]), "blocks": int(p[7]), "dist_unit_mm": int(p[9]) or 4,
            "returns": int(p[10]), "mode": RETURN_MODES.get(int(p[1062]), hex(p[1062])),
            "rpm": int(p[1063]) | (int(p[1064]) << 8)}


def sequence(a):
    return a[:, 1076:1080].copy().view("<u4").ravel()


def decode(a, t_pkt, cal, returns="strongest", rmin=1.0, rmax=80.0, imin=0, keep=None, rng=None):
    """Decode packets to points.

    a      (N,1080) uint8 packets, t_pkt (N,) packet times in seconds (sensor time)
    cal    [(elev_deg, az_offset_deg)] * 32
    returns 'strongest' | 'last' | 'both'   (dual return: blocks 0/1 pair = last/strongest)
    keep   fraction of the gated returns kept (random), None = all
    Returns dict of arrays: t (f8), p (N,3 f4 sensor frame m), r (f4), i (u1), ch (u1), az (f4 deg).
    """
    n = len(a)
    if n == 0:
        return None
    unit = (int(a[0, 9]) or 4) * 1e-3
    dual = int(a[0, 10]) == 2
    s_tot = 4 if dual else 8
    if dual and returns == "strongest":
        blocks = [1, 3, 5, 7]
    elif dual and returns == "last":
        blocks = [0, 2, 4, 6]
    else:
        blocks = list(range(8))
    blocks = np.array(blocks)
    base = 12 + 130 * blocks                                   # (B,)
    az = (a[:, base].astype(np.uint16) | (a[:, base + 1].astype(np.uint16) << 8)) * 0.01   # (N,B)
    k = np.arange(N_CH)
    off = (base[:, None] + 2 + 4 * k[None, :])                  # (B,32)
    dist = a[:, off].astype(np.uint16) | (a[:, off + 1].astype(np.uint16) << 8)   # (N,B,32)
    inten = a[:, off + 2]
    rng_m = dist * unit
    m = (dist > 0) & (rng_m >= rmin) & (rng_m <= rmax)
    if imin:
        m &= inten >= imin
    idx = np.nonzero(m)
    if keep is not None and keep < 1.0 and len(idx[0]):
        rng = rng or np.random.default_rng(0)
        sel = rng.random(len(idx[0])) < keep
        idx = tuple(x[sel] for x in idx)
    ip, ib, ic = idx
    cal = np.asarray(cal if cal else DEFAULT_CAL, float)
    elev = np.radians(cal[ic, 0])
    alpha = np.radians(az[ip, ib] + cal[ic, 1])
    r = rng_m[ip, ib, ic]
    ce = np.cos(elev)
    p = np.stack([r * ce * np.sin(alpha), r * ce * np.cos(alpha), r * np.sin(elev)], -1).astype(np.float32)
    s = (blocks[ib] // 2 + 1) if dual else (blocks[ib] + 1)
    t = (t_pkt[ip] + (3.28 - 50.0 * (s_tot - s) + 1.512 * ic + 0.28) * 1e-6)
    return {"t": t, "p": p, "r": r.astype(np.float32), "i": inten[ip, ib, ic],
            "ch": ic.astype(np.uint8), "az": az[ip, ib].astype(np.float32)}


def parse_status(payload):
    """512-byte GPS/status packet -> (nmea_valid, pps_locked)."""
    b = bytes(payload)
    if len(b) < 508:
        return None
    return b[506] == 0x41, b[507] == 1
