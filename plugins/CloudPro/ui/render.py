"""Numpy point rasteriser for the 2-D and 3-D views.

Points are drawn into a 32-bit ARGB buffer: per-pixel visibility uses a
16-bit depth key with numpy's linear-time radix sort (kind='stable' on
uint16), so the nearest (or highest) point wins.  Larger point sizes splat a
small square.  Optional eye-dome lighting (Boucheny 2009) shades the depth
buffer for 3-D shape perception without normals.
"""
import numpy as np

from ..core.filters import ASPRS

# ---------------------------------------------------------------------------
# colour maps (256 x 3 uint8)
# ---------------------------------------------------------------------------


def _lut(stops):
    stops = np.asarray(stops, float)
    x = np.linspace(0, 1, 256)
    pos = np.linspace(0, 1, len(stops))
    return np.stack([np.interp(x, pos, stops[:, c]) for c in range(3)], 1).astype(np.uint8)


LUTS = {
    "Rainbow": _lut([(48, 18, 59), (70, 107, 227), (40, 188, 235), (50, 241, 152), (164, 252, 60),
                     (238, 207, 58), (251, 128, 34), (208, 47, 5), (122, 4, 3)]),          # turbo-like
    "Viridis": _lut([(68, 1, 84), (59, 82, 139), (33, 145, 140), (94, 201, 98), (253, 231, 37)]),
    "Terrain": _lut([(30, 60, 120), (40, 140, 160), (80, 170, 90), (190, 190, 110), (150, 110, 80),
                     (240, 240, 240)]),
    "Mint": _lut([(9, 30, 28), (0, 120, 95), (0, 229, 170), (200, 255, 236)]),
    "Grey": _lut([(20, 20, 20), (240, 240, 240)]),
}

CLASS_COLOURS = np.zeros((256, 3), np.uint8)
CLASS_COLOURS[:] = (150, 150, 150)
for k, c in {0: (120, 120, 120), 1: (170, 170, 170), 2: (180, 120, 60), 3: (120, 200, 90), 4: (60, 170, 60),
             5: (20, 120, 40), 6: (240, 200, 80), 7: (255, 0, 255), 9: (60, 120, 230), 10: (200, 200, 60),
             11: (110, 110, 130), 13: (240, 240, 120), 14: (255, 255, 0), 15: (255, 140, 0),
             17: (200, 160, 255), 18: (255, 60, 200), 19: (0, 200, 220), 20: (130, 90, 50),
             64: (58, 180, 242), 65: (245, 166, 35), 66: (220, 240, 255),
             67: (255, 40, 40), 68: (255, 255, 255), 69: (255, 214, 102), 70: (180, 120, 255),
             71: (120, 220, 255)}.items():
    CLASS_COLOURS[k] = c

CHANNEL_COLOURS = np.array([LUTS["Rainbow"][int(i * 255 / 31)] for i in range(32)], np.uint8)
REJECT_RGB = (232, 72, 85)
PREVIEW_REJECT_RGB = (255, 120, 0)
SELECT_RGB = (255, 255, 255)


def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def pack(rgb):
    """(n,3) uint8 -> (n,) uint32 ARGB."""
    rgb = np.asarray(rgb, np.uint32)
    return (0xFF000000 | (rgb[..., 0] << 16) | (rgb[..., 1] << 8) | rgb[..., 2]).astype(np.uint32)


def scalar_colours(v, lo, hi, lut="Rainbow"):
    t = np.clip((v - lo) / max(hi - lo, 1e-9), 0, 1)
    return LUTS[lut][(t * 255).astype(np.int32)]


def auto_range(v, p=(2, 98)):
    if len(v) == 0:
        return 0.0, 1.0
    s = v if len(v) < 200000 else v[np.random.default_rng(0).integers(0, len(v), 200000)]
    lo, hi = np.percentile(s, p)
    if hi - lo < 1e-6:
        hi = lo + 1.0
    return float(lo), float(hi)


def colour_points(ps, mode, lut="Rainbow", rng=None, line_colours=None, idx=None, shade=None):
    """Per-point uint32 colours for a colour mode."""
    sl = slice(None) if idx is None else idx
    n = len(ps.x) if idx is None else len(idx)
    if n == 0:
        return np.zeros(0, np.uint32)
    if mode == "Height":
        v = ps.z[sl]
        lo, hi = rng or auto_range(v)
        rgb = scalar_colours(v, lo, hi, lut)
    elif mode == "Intensity":
        v = ps.i[sl].astype(float)
        lo, hi = rng or auto_range(v, (1, 99))
        rgb = scalar_colours(v, lo, hi, "Grey" if lut == "Rainbow" else lut)
    elif mode == "Intensity (range normalised)":
        r = np.maximum(ps.rng[sl] * 0.01, 0.5)
        v = ps.i[sl] * (r / 10.0) ** 2
        lo, hi = rng or auto_range(v, (1, 99))
        rgb = scalar_colours(v, lo, hi, "Grey" if lut == "Rainbow" else lut)
    elif mode == "Classification":
        rgb = CLASS_COLOURS[ps.cls[sl]]
    elif mode == "Line":
        lc = line_colours if line_colours is not None else np.array([LUTS["Rainbow"][(i * 47) % 256]
                                                                     for i in range(256)])
        rgb = lc[np.minimum(ps.line[sl], len(lc) - 1)]
    elif mode == "Return":
        rgb = np.where((ps.ret[sl] == 1)[:, None], np.array([[245, 166, 35]], np.uint8),
                       np.array([[0, 229, 170]], np.uint8))
    elif mode == "Channel":
        rgb = CHANNEL_COLOURS[ps.ch[sl] % 32]
    elif mode == "Time":
        v = ps.t[sl]
        lo, hi = rng or auto_range(v, (0, 100))
        rgb = scalar_colours(v, lo, hi, "Viridis")
    elif mode == "Range":
        v = ps.rng[sl] * 0.01
        lo, hi = rng or auto_range(v, (1, 99))
        rgb = scalar_colours(v, lo, hi, lut)
    else:
        rgb = np.full((n, 3), 180, np.uint8)
    if shade is not None:
        rgb = np.clip(rgb.astype(np.float32) * shade[:, None], 0, 255).astype(np.uint8)
    return pack(rgb)


# ---------------------------------------------------------------------------
# rasteriser
# ---------------------------------------------------------------------------
def rasterise(px, py, key, col, w, h, size=1, bg=0xFF09100F, depth_out=None, dval=None):
    """Draw points at pixel coords (float) into a (h, w) uint32 buffer.
    key: larger = drawn on top (uint16 after scaling). Returns (buffer, pix index per drawn point)."""
    buf = np.full(h * w, bg, np.uint32)
    if len(px) == 0:
        return buf.reshape(h, w)
    ix = px.astype(np.int32)
    iy = py.astype(np.int32)
    m = (ix >= 0) & (iy >= 0) & (ix < w) & (iy < h)
    if not m.all():
        ix, iy, key, col = ix[m], iy[m], key[m], col[m]
        if dval is not None:
            dval = dval[m]
    if len(ix) == 0:
        return buf.reshape(h, w)
    k = np.asarray(key, np.float64)
    kmin, kmax = k.min(), k.max()
    kq = ((k - kmin) * (65535.0 / max(kmax - kmin, 1e-12))).astype(np.uint16)
    order = np.argsort(kq, kind="stable")              # radix sort on uint16
    ix, iy, col = ix[order], iy[order], col[order]
    dv = None if dval is None else dval[order]
    if size <= 1:
        pix = iy * w + ix
        buf[pix] = col
        if depth_out is not None and dv is not None:
            depth_out[pix] = dv
    else:
        r0 = -(size // 2)
        for dy in range(r0, r0 + size):
            for dx in range(r0, r0 + size):
                jx = ix + dx
                jy = iy + dy
                mm = (jx >= 0) & (jy >= 0) & (jx < w) & (jy < h)
                pix = jy[mm] * w + jx[mm]
                buf[pix] = col[mm]
                if depth_out is not None and dv is not None:
                    depth_out[pix] = dv[mm]
    return buf.reshape(h, w)


def rasterise_nearest(sx, sy, z, col, w, h, size=1, bg=0xFF09100F):
    """3-D drawing: the nearest point per pixel, then only those points splatted at the point size.

    sx, sy: pixel coordinates (float); z: camera depth (> 0 in front); col: uint32 ARGB.
    Returns (buffer (h, w) uint32, depth (h*w,) float32 with inf where empty, number drawn).
    Faster than rasterise() for big clouds: one depth sort and one scatter over all points, and
    the splat passes only over the visible points (at most one per pixel), in a padded buffer so
    no per-pass bounds masks are needed."""
    pad = max(1, size)
    wp, hp = w + 2 * pad, h + 2 * pad
    buf = np.full(hp * wp, bg, np.uint32)
    dep = np.full(hp * wp, np.inf, np.float32)

    def crop(a):
        return np.ascontiguousarray(a.reshape(hp, wp)[pad:pad + h, pad:pad + w])
    if len(sx) == 0:
        return crop(buf), crop(dep).ravel(), 0
    ix = sx.astype(np.int32)
    iy = sy.astype(np.int32)
    m = (ix >= 0) & (iy >= 0) & (ix < w) & (iy < h) & (z > 0.05)
    if not m.all():
        ix, iy, z, col = ix[m], iy[m], z[m], col[m]
    if len(ix) == 0:
        return crop(buf), crop(dep).ravel(), 0
    pix = iy * w + ix
    z = np.asarray(z, np.float32)
    zmin, zmax = float(z.min()), float(z.max())
    dq = ((z - zmin) * (65535.0 / max(zmax - zmin, 1e-9))).astype(np.uint16)
    far_first = np.argsort(np.uint16(65535) - dq, kind="stable")     # radix sort, nearest last
    win = np.full(w * h, -1, np.int32)
    win[pix[far_first]] = far_first.astype(np.int32)                # the last write per pixel wins
    vis = win[win >= 0]
    if size > 1:                                                    # splats: far ones first again
        vis = vis[np.argsort(np.uint16(65535) - dq[vis], kind="stable")]
    pv = pix[vis]
    base = (pv // w + pad) * wp + (pv % w + pad)
    cv, zv = col[vis], z[vis]
    r0 = -(size // 2)
    for dy in range(r0, r0 + max(1, size)):
        for dx in range(r0, r0 + max(1, size)):
            q = base + (dy * wp + dx)
            buf[q] = cv
            dep[q] = zv
    return crop(buf), crop(dep).ravel(), len(vis)


def eye_dome(buf, depth, strength=1.2, radius=1):
    """Eye-dome lighting on a depth buffer (inf = empty)."""
    h, w = buf.shape
    D = depth.reshape(h, w)
    valid = np.isfinite(D)
    if not valid.any():
        return buf
    L = np.where(valid, np.log2(np.maximum(D, 1e-3)), np.nan)
    acc = np.zeros((h, w), np.float32)
    for dx, dy in ((radius, 0), (-radius, 0), (0, radius), (0, -radius)):
        S = np.full((h, w), np.nan, np.float32)
        ys = slice(max(dy, 0), h + min(dy, 0))
        yd = slice(max(-dy, 0), h + min(-dy, 0))
        xs = slice(max(dx, 0), w + min(dx, 0))
        xd = slice(max(-dx, 0), w + min(-dx, 0))
        S[yd, xd] = L[ys, xs]
        diff = L - np.where(np.isnan(S), L + 0.5, S)
        acc += np.maximum(0, np.nan_to_num(diff, nan=0.0))
    shade = np.exp(-strength * 300.0 * acc / 4.0)
    shade = np.where(valid, shade, 1.0)
    r = ((buf >> 16) & 0xFF).astype(np.float32) * shade
    g = ((buf >> 8) & 0xFF).astype(np.float32) * shade
    b = (buf & 0xFF).astype(np.float32) * shade
    out = (0xFF000000 | (r.astype(np.uint32) << 16) | (g.astype(np.uint32) << 8) | b.astype(np.uint32))
    return np.where(valid, out, buf).astype(np.uint32)
