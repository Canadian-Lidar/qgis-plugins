"""Hesai PandarXT-32 packets (protocol 6.1), vectorised with numpy.

One decoder, decode_packets(), serves every plugin; decode() (AlignPro's
form) and decode_hesai() (CloudPro's form) are thin wrappers around it.
Byte layouts follow the project's ".clraw & Sensor Reference for Plugin
Developers" (mapper 2.9.0, format version 1).
"""
import numpy as np

PKT = HESAI_LEN = 1080
STATUS_LEN = 512
N_CH = 32
N_BLK = 8
RETURN_MODES = {0x33: "First", 0x37: "Strongest", 0x38: "Last", 0x39: "Last + Strongest",
                0x3B: "Last + First", 0x3C: "First + Strongest"}
DUAL_MODES = (0x39, 0x3B, 0x3C)

# (elevation, azimuth offset) degrees per channel, used when a recording has no lidar_calibration.csv
DEFAULT_CALIB = np.array([
    (14.916, 0.506), (13.899, 0.503), (12.891, 0.520), (11.886, 0.519), (10.882, 0.509), (9.882, 0.506),
    (8.882, 0.502), (7.879, 0.495), (6.881, 0.490), (5.881, 0.484), (4.884, 0.482), (3.890, 0.471),
    (2.896, 0.465), (1.900, 0.459), (0.909, 0.454), (-0.089, 0.447), (-1.084, 0.441), (-2.080, 0.437),
    (-3.079, 0.432), (-4.074, 0.427), (-5.071, 0.417), (-6.074, 0.416), (-7.077, 0.416), (-8.079, 0.412),
    (-9.080, 0.402), (-10.081, 0.401), (-11.087, 0.397), (-12.096, 0.391), (-13.100, 0.372), (-14.112, 0.364),
    (-15.132, 0.361), (-16.160, 0.356)])
DEFAULT_CAL = [tuple(r) for r in DEFAULT_CALIB.tolist()]


# ----------------------------------------------------------------------------- packets
def packets_to_array(payloads):
    """Message payloads -> (N, 1080) uint8 array of the point packets (others skipped)."""
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
    """UDP sequence number of each packet (N,)."""
    return a[:, 1076:1080].copy().view("<u4").ravel()


def _days_from_civil(y, m, d):
    y = y - (m <= 2)
    era = np.floor_divide(y, 400)
    yoe = y - era * 400
    mp = (m + 9) % 12
    doy = (153 * mp + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def packet_times(P):
    """UTC Unix seconds of each packet (N,) from the packet tails (date, time, microseconds)."""
    t = P[:, 1065:1071].astype(np.int64)
    days = _days_from_civil(t[:, 0] + 1900, t[:, 1], t[:, 2])
    us = P[:, 1071:1075].copy().view("<u4")[:, 0].astype(np.float64)
    return days * 86400.0 + t[:, 3] * 3600.0 + t[:, 4] * 60.0 + t[:, 5] + us * 1e-6


hesai_packet_times = packet_times


def is_dual(P):
    """True per packet when it carries two returns (blocks 0/1, 2/3 ... are pairs)."""
    return (P[:, 10] == 2) | np.isin(P[:, 1062], DUAL_MODES)


def parse_status(payload):
    """512-byte GPS/status packet -> (nmea_valid, pps_locked), or None."""
    b = bytes(payload)
    if len(b) < 508:
        return None
    return b[506] == 0x41, b[507] == 1


# ----------------------------------------------------------------------------- decoding
def decode_packets(P, calib=None, t_pkt=None, returns="both", rmin=0.5, rmax=200.0, imin=0,
                   drop_duplicates=False, keep=None, rng=None):
    """Decode an (N, 1080) uint8 array of point packets.

    calib    (32, 2) elevation and azimuth offset in degrees (None = DEFAULT_CALIB)
    t_pkt    (N,) packet times in seconds; None = from the packet tails (UTC Unix)
    returns  'both' | 'strongest' | 'last' (dual return: block pairs are last, strongest)
    imin     minimum intensity kept
    drop_duplicates  with 'both': drop the second block of a pair when it repeats the first
    keep     fraction of the gated returns kept at random (None = all); rng = numpy Generator

    Returns None for no packets, else a dict of flat arrays in packet, block, channel order:
    t (s), x, y, z (sensor frame, m), range (m), intensity, channel (0-31),
    ret (0 = last/first block of a pair, 1 = strongest/second, 2 = single return),
    azimuth (deg, calibrated, 0-360), az_block (deg, the block's raw azimuth), packet (index into P).
    """
    N = P.shape[0]
    if N == 0:
        return None
    calib = np.asarray(DEFAULT_CALIB if calib is None or len(calib) == 0 else calib, float)
    if t_pkt is None:
        t_pkt = packet_times(P)
    u9 = P[:, 9].astype(np.float64)
    unit = np.where(u9 > 0, u9, 4.0) * 1e-3                                   # mm -> m (4 on the mapper)
    dual = is_dual(P)
    blocks = np.arange(N_BLK)
    base = 12 + 130 * blocks                                                   # (8,)
    az = (P[:, base].astype(np.uint16) | (P[:, base + 1].astype(np.uint16) << 8)) * 0.01   # (N,8)
    k = np.arange(N_CH)
    off = base[:, None] + 2 + 4 * k[None, :]                                   # (8,32)
    dist = P[:, off].astype(np.uint16) | (P[:, off + 1].astype(np.uint16) << 8)  # (N,8,32)
    inten = P[:, off + 2]
    rng_m = dist * unit[:, None, None]
    ret = np.where(dual[:, None], (blocks % 2)[None, :], 2)                    # (N,8)

    m = (dist > 0) & (rng_m >= rmin) & (rng_m <= rmax)
    if imin:
        m &= inten >= imin
    if returns == "strongest":
        m &= (ret != 0)[:, :, None]
    elif returns == "last":
        m &= (ret != 1)[:, :, None]
    elif drop_duplicates:
        d0 = dist[:, 0::2, :].astype(np.int32)
        d1 = dist[:, 1::2, :].astype(np.int32)
        dup = (np.abs(d0 - d1) <= 1) & dual[:, None, None]
        m[:, 1::2, :] &= ~dup
    idx = np.nonzero(m)
    if keep is not None and keep < 1.0 and len(idx[0]):
        rng = rng or np.random.default_rng(0)
        sel = rng.random(len(idx[0])) < keep
        idx = tuple(x[sel] for x in idx)
    ip, ib, ic = idx
    elev = np.radians(calib[ic, 0])
    azc = az[ip, ib] + calib[ic, 1]
    alpha = np.radians(azc)
    r = rng_m[ip, ib, ic]
    ce = np.cos(elev)
    # firing time (Hesai XT32 manual): s = firing sequence 1..S of the block, S = 4 dual, 8 single
    s_tot = np.where(dual[ip], 4, 8)
    s = np.where(dual[ip], ib // 2 + 1, ib + 1)
    t = t_pkt[ip] + (3.28 - 50.0 * (s_tot - s) + 1.512 * ic + 0.28) * 1e-6
    return {"t": t, "x": r * ce * np.sin(alpha), "y": r * ce * np.cos(alpha), "z": r * np.sin(elev),
            "range": r, "intensity": inten[ip, ib, ic], "channel": ic.astype(np.uint8),
            "ret": ret[ip, ib].astype(np.uint8), "azimuth": np.mod(azc, 360.0), "az_block": az[ip, ib],
            "packet": ip}


def decode(a, t_pkt, cal, returns="strongest", rmin=1.0, rmax=80.0, imin=0, keep=None, rng=None):
    """AlignPro's form. Returns dict: t (f8), p (N,3 f4 sensor frame m), r (f4), i (u1), ch (u1),
    az (f4 deg, the block's raw azimuth)."""
    d = decode_packets(a, cal, t_pkt, returns, rmin, rmax, imin, False, keep, rng)
    if d is None:
        return None
    return {"t": d["t"], "p": np.stack([d["x"], d["y"], d["z"]], -1).astype(np.float32),
            "r": d["range"].astype(np.float32), "i": d["intensity"], "ch": d["channel"],
            "az": d["az_block"].astype(np.float32)}


def decode_hesai(P, calib, rmin=0.5, rmax=200.0, returns="both", drop_duplicates=True):
    """CloudPro's form: times from the packet tails (UTC s); dict of t, x, y, z, range,
    intensity, channel, ret, azimuth, packet."""
    d = decode_packets(P, calib, None, returns, rmin, rmax, 0, drop_duplicates)
    if d is not None:
        d.pop("az_block")
    return d
