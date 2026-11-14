"""Automatic filters for sonar soundings (marine jobs), after the methods of the hydrographic processing packages
and the literature, with tolerances from IHO S-44 (6th edition, 2020):

  TVU_max(d) = sqrt(a^2 + (b d)^2) at 95 %, d = depth;  Exclusive a 0.15 b 0.0075 · Special a 0.25 b 0.0075 ·
  Order 1a / 1b a 0.5 b 0.013 · Order 2 a 1.0 b 0.023.

Filters (group "Marine"):
  m_gates     Sonar gates - power, slant range, angle from vertical outboard / inboard (under the boat), depth
              below the transducer. As HYPACK MBMAX64's basic / beam-angle filters and the acquisition gates.
  m_outliers  Reject outliers (cell statistics) - soundings further than a gate from the cell median / mean,
              recomputed until nothing changes. As QPS Qimera's Reject Outliers (1D) with dynamic update (the most
              consistent method in Wlodarczyk-Sielicka et al., Sensors 2023, 23(11):5076), BeamworX AutoClean's
              Detailed filter and Guenther's statistical filter; gate = IHO TVU x factor, robust sigma x k, or
              fixed.
  m_cube      Depth hypotheses (CUBE-style) - per cell the soundings are split into depth hypotheses (gaps wider
              than the TVU), one hypothesis is kept (most soundings, CUBE's default "prior" disambiguation, or the
              one closest to the neighbouring cells), the others rejected. After Calder & Mayer 2003, G3 4(6):1048
              and HYPACK's CUBE; rejects water-column returns (fish, bubbles, turbulence) and multipath ghosts.
  m_spline    Surface spline - a robust local surface (Tukey biweight, k nearest soundings) and a gate = IHO TVU x
              factor, above / below / both. As Qimera's surface spline (presets from Very weak to Very strong) and
              BeamworX's spline filter; "Very weak" follows the seabed most closely (best in the study above).
  The general "Isolated cluster" filter is BeamworX's Coarse filter / EIVA EC-3D's neighbour methods.

The depth of a sounding is its depth below the transducer (the head's acoustic-centre track), so the TVU grows with
depth as S-44 intends; without tracks the depth is taken as 10 m.
"""
import numpy as np

from . import spatial as sp
from .filters import P, Filter, _res


def _class_mask(ctx, flt):
    from .filters import _class_mask as cm          # defined after the registry imports this module
    return cm(ctx, flt)

IHO = {"Exclusive": (0.15, 0.0075), "Special": (0.25, 0.0075), "Order 1a": (0.5, 0.013), "Order 1b": (0.5, 0.013),
       "Order 2": (1.0, 0.023)}
IHO_ORDERS = list(IHO)
DEFAULT_ORDER = "Order 1a"            # Sean, 13 Nov 2026


def tvu(depth, order=DEFAULT_ORDER):
    """IHO S-44 maximum allowable TVU (95 %) at depth d (m)."""
    a, b = IHO[order]
    d = np.asarray(depth, float)
    return np.sqrt(a * a + (b * d) ** 2)


def depth_below_transducer(ctx, idx):
    """Depth of each sounding below its head's acoustic centre (m, positive down); 10 m without tracks."""
    if not ctx.tracks:
        return np.full(len(idx), 10.0)
    E, N, Z, H = ctx.sensor(idx)
    d = Z - ctx.z[idx]
    return np.where(np.isfinite(d), np.maximum(d, 0.0), 10.0)


def outboard_angle(ctx, idx):
    """Angle from the vertical of each sounding seen from its head (deg): positive toward the side the head looks
    to (outboard), negative under the boat toward the other side (inboard). The side each line looks to is the side
    most of its soundings lie on."""
    out = np.full(len(idx), np.nan)
    if not ctx.tracks:
        return out
    E, N, Z, H = ctx.sensor(idx)
    right = (ctx.x[idx] - E) * np.cos(H) - (ctx.y[idx] - N) * np.sin(H)      # + starboard
    down = Z - ctx.z[idx]
    lines = ctx.ps.line[idx]
    for lid in np.unique(lines):
        m = lines == lid
        side = 1.0 if np.nanmedian(right[m]) >= 0 else -1.0
        out[m] = np.degrees(np.arctan2(right[m] * side, np.maximum(down[m], 1e-3)))
    return out


ORDER_P = P("order", "IHO S-44 order", "choice", DEFAULT_ORDER, choices=IHO_ORDERS,
            help="Sets the TVU the gate is based on: sqrt(a^2 + (b x depth)^2)")


# =============================================================================
class SonarGates(Filter):
    key, name, group, speed = "m_gates", "Sonar gates (power, range, angle, depth)", "Marine", "++"
    ref = "HYPACK MBMAX64 basic and beam-angle filters; acquisition gates"
    help = ("Rejects soundings by what the sonar measured: power above the ping's low threshold, slant range, angle "
            "from the vertical toward the side the head looks to (outboard) and under the boat toward the other side "
            "(inboard, where the two heads see the edge of their fans), and depth below the transducer.")
    params = [P("db", "Min. power above the low threshold", "float", 0.0, 0, 60, 1, " dB"),
              P("rmin", "Min. slant range", "float", 0.5, 0, 500, 0.1, " m"),
              P("rmax", "Max. slant range", "float", 200.0, 0, 500, 1, " m"),
              P("outboard", "Max. angle outboard", "float", 80.0, 0, 90, 1, " deg"),
              P("inboard", "Max. angle inboard (under the boat)", "float", 90.0, 0, 90, 1, " deg"),
              P("dmin", "Min. depth below transducer", "float", 0.0, 0, 1000, 0.1, " m"),
              P("dmax", "Max. depth below transducer", "float", 1000.0, 0, 1000, 1, " m")]
    needs_tracks = True

    def run(self, ctx, sel):
        bad = np.zeros(len(sel), bool)
        if self.v("db") > 0:
            bad |= ctx.ps.i[sel] < self.v("db") / 60.0 * 255.0
        r = ctx.ps.rng[sel] * 0.01
        bad |= (r < self.v("rmin")) | (r > self.v("rmax"))
        if ctx.tracks and (self.v("outboard") < 90 or self.v("inboard") < 90):
            a = outboard_angle(ctx, sel)
            bad |= np.isfinite(a) & ((a > self.v("outboard")) | (a < -self.v("inboard")))
        if ctx.tracks and (self.v("dmin") > 0 or self.v("dmax") < 1000):
            d = depth_below_transducer(ctx, sel)
            bad |= (d < self.v("dmin")) | (d > self.v("dmax"))
        return _res(reject=sel[bad])


def _cells(x, y, c):
    ix, iy, x0, y0 = sp.grid_index(x, y, c)
    key = iy.astype(np.int64) * (int(ix.max()) + 1) + ix
    return key, (ix, iy, x0, y0)


def _group_stats(key, z, how="Median"):
    """Per point: its cell's median (or mean) and robust sigma (1.4826 MAD) and count."""
    o = np.lexsort((z, key))
    ks, zs = key[o], z[o]
    brk = np.flatnonzero(np.diff(ks)) + 1
    st = np.r_[0, brk]
    en = np.r_[brk, len(ks)]
    cnt = en - st
    if how == "Mean":
        cen = np.add.reduceat(zs, st) / cnt
    else:
        cen = 0.5 * (zs[st + (cnt - 1) // 2] + zs[st + cnt // 2])
    cen_p = np.repeat(cen, cnt)
    mad = np.empty(len(st))
    ad = np.abs(zs - cen_p)
    for i, (a, b) in enumerate(zip(st, en)):           # cells are small: a sort per cell is fine
        mad[i] = np.median(ad[a:b])
    out_c = np.empty(len(z))
    out_s = np.empty(len(z))
    out_n = np.empty(len(z), np.int64)
    out_c[o] = cen_p
    out_s[o] = np.repeat(1.4826 * mad, cnt)
    out_n[o] = np.repeat(cnt, cnt)
    return out_c, out_s, out_n


class RejectOutliers(Filter):
    key, name, group, speed = "m_outliers", "Reject outliers (cell statistics, IHO TVU)", "Marine", "+"
    ref = ("QPS Qimera Reject Outliers (1D); Wlodarczyk-Sielicka et al. 2023, Sensors 23(11):5076; BeamworX "
           "AutoClean Detailed; Guenther et al. 1982")
    help = ("Grids the soundings and rejects those further than the gate from their cell's median (or mean); the "
            "cells are recomputed after each pass until nothing more is rejected. Gate: the IHO TVU at the "
            "sounding's depth x factor, the cell's robust sigma x k, or a fixed distance.")
    params = [P("cell", "Cell size", "float", 1.0, 0.1, 50, 0.1, " m"), ORDER_P,
              P("mode", "Gate", "choice", "IHO TVU x factor", choices=["IHO TVU x factor", "Robust sigma x k", "Fixed"]),
              P("factor", "Factor / k", "float", 1.0, 0.1, 10, 0.1),
              P("fixed", "Fixed gate", "float", 0.5, 0.01, 50, 0.01, " m"),
              P("ref", "Cell value", "choice", "Median", choices=["Median", "Mean"]),
              P("nmin", "Min. soundings per cell", "int", 5, 2, 1000),
              P("passes", "Max. passes", "int", 5, 1, 20)]

    def run(self, ctx, sel):
        live = np.ones(len(sel), bool)
        bad = np.zeros(len(sel), bool)
        d_all = depth_below_transducer(ctx, sel)
        x, y, z = ctx.x[sel], ctx.y[sel], ctx.z[sel]
        key_all, _g = _cells(x, y, self.v("cell"))
        for _ in range(int(self.v("passes"))):
            if ctx.cancel():
                break
            j = np.flatnonzero(live)
            if not len(j):
                break
            cen, sig, cnt = _group_stats(key_all[j], z[j], self.v("ref"))
            mode = self.v("mode")
            if mode == "IHO TVU x factor":
                gate = tvu(d_all[j], self.v("order")) * self.v("factor")
            elif mode == "Robust sigma x k":
                gate = np.maximum(sig * self.v("factor"), 0.01)
            else:
                gate = np.full(len(j), self.v("fixed"))
            new = (cnt >= self.v("nmin")) & (np.abs(z[j] - cen) > gate)
            if not new.any():
                break
            bad[j[new]] = True
            live[j[new]] = False
        return _res(reject=sel[bad])


class DepthHypotheses(Filter):
    key, name, group, speed = "m_cube", "Depth hypotheses (CUBE-style)", "Marine", "+"
    ref = "Calder & Mayer 2003, Geochem. Geophys. Geosyst. 4(6):1048 (CUBE); HYPACK MBMAX64 CUBE"
    help = ("Per cell, the soundings are split into depth hypotheses wherever two neighbouring depths are further "
            "apart than the IHO TVU x factor. One hypothesis is kept - the one with most soundings (CUBE's 'prior' "
            "disambiguation) or the one closest to the neighbouring cells - and the soundings of the others are "
            "rejected. Removes water-column returns and multipath below the seabed.")
    params = [P("cell", "Cell size", "float", 1.0, 0.1, 50, 0.1, " m"), ORDER_P,
              P("factor", "TVU factor", "float", 1.0, 0.1, 10, 0.1),
              P("choose", "Keep", "choice", "Most soundings", choices=["Most soundings", "Closest to neighbours"]),
              P("nmin", "Min. soundings per cell", "int", 4, 2, 1000),
              P("share", "Keep a second hypothesis with at least", "float", 0.0, 0, 1, 0.05,
                help="Share of the cell's soundings (0 = keep only one hypothesis)")]

    def run(self, ctx, sel):
        x, y, z = ctx.x[sel], ctx.y[sel], ctx.z[sel]
        d = depth_below_transducer(ctx, sel)
        h = tvu(d, self.v("order")) * self.v("factor")
        key, (ix, iy, x0, y0) = _cells(x, y, self.v("cell"))
        o = np.lexsort((z, key))
        ks, zs, hs = key[o], z[o], h[o]
        brk = np.flatnonzero(np.diff(ks)) + 1
        st, en = np.r_[0, brk], np.r_[brk, len(ks)]
        # neighbour reference: median of the 3 x 3 block of cell medians (for "Closest to neighbours")
        nb_ref = None
        if self.v("choose") == "Closest to neighbours":
            med = np.array([np.median(zs[a:b]) for a, b in zip(st, en)])
            cell_key = ks[st]
            W = int(ix.max()) + 1
            cy, cx = cell_key // W, cell_key % W
            G = np.full((int(iy.max()) + 1, W), np.nan)
            G[cy, cx] = med
            from scipy import ndimage
            Gf = np.where(np.isnan(G), 0, G)
            C = ndimage.convolve((~np.isnan(G)).astype(float), np.ones((3, 3)), mode="constant")
            S = ndimage.convolve(Gf, np.ones((3, 3)), mode="constant")
            with np.errstate(invalid="ignore", divide="ignore"):
                nb_ref = ((S - Gf) / np.maximum(C - 1, 1))[cy, cx]
        badsorted = np.zeros(len(zs), bool)
        nmin, share = int(self.v("nmin")), float(self.v("share"))
        for ci, (a, b) in enumerate(zip(st, en)):
            if b - a < nmin:
                continue
            gaps = np.flatnonzero(np.diff(zs[a:b]) > hs[a + 1:b]) + 1
            if not len(gaps):
                continue
            hb = np.r_[0, gaps, b - a]
            sizes = np.diff(hb)
            if nb_ref is not None and np.isfinite(nb_ref[ci]):
                means = np.array([zs[a + p:a + q].mean() for p, q in zip(hb[:-1], hb[1:])])
                keep = {int(np.argmin(np.abs(means - nb_ref[ci])))}
            else:
                keep = {int(np.argmax(sizes))}
            if share > 0:
                keep |= {i for i, s in enumerate(sizes) if s >= share * (b - a)}
            for i, (p, q) in enumerate(zip(hb[:-1], hb[1:])):
                if i not in keep:
                    badsorted[a + p:a + q] = True
        bad = np.zeros(len(sel), bool)
        bad[o] = badsorted
        return _res(reject=sel[bad])


STIFFNESS = {"Very weak": (12, "Quadric"), "Weak": (20, "Quadric"), "Medium": (32, "Quadric"),
             "Strong": (48, "Plane"), "Very strong": (80, "Plane")}


class SurfaceSpline(Filter):
    key, name, group, speed = "m_spline", "Surface spline (robust surface, IHO TVU)", "Marine", "-"
    ref = ("QPS Qimera surface spline; BeamworX AutoClean spline; robust local fitting (Tukey biweight), cf. "
           "Debese et al. and Canepa et al. as reviewed in Le Deunf et al. 2020, Geosciences 10(7):254")
    help = ("Fits a robust local surface through each sounding's nearest neighbours (the sounding itself left out, "
            "Tukey biweight) and rejects the sounding when it lies further from it than the IHO TVU x factor. "
            "Stiffness: 'Very weak' follows the seabed most closely, 'Very strong' smooths most. Side: reject only "
            "above the surface (water column), only below (multipath), or both.")
    params = [P("stiff", "Stiffness", "choice", "Very weak", choices=list(STIFFNESS)), ORDER_P,
              P("factor", "TVU factor", "float", 1.0, 0.1, 10, 0.1),
              P("side", "Reject", "choice", "Above and below", choices=["Above and below", "Above only", "Below only"])]

    def run(self, ctx, sel):
        acc = np.flatnonzero(ctx.accepted & _class_mask(ctx, self))
        if len(acc) < 10:
            return _res()
        k, order = STIFFNESS[self.v("stiff")]
        k = min(k, len(acc) - 1)
        tr = sp.tree(np.column_stack([ctx.x[acc], ctx.y[acc]]))
        gate_all = tvu(depth_below_transducer(ctx, sel), self.v("order")) * self.v("factor")
        out = np.zeros(len(sel), bool)
        for s in range(0, len(sel), 100000):
            if ctx.cancel():
                break
            ss = sel[s:s + 100000]
            _d, i = tr.query(np.column_stack([ctx.x[ss], ctx.y[ss]]), k=k + 1, workers=-1)
            i = i[:, 1:]
            gx = ctx.x[acc][i] - ctx.x[ss][:, None]
            gy = ctx.y[acc][i] - ctx.y[ss][:, None]
            gz = ctx.z[acc][i]
            A = (np.stack([np.ones_like(gx), gx, gy], -1) if order == "Plane" else
                 np.stack([np.ones_like(gx), gx, gy, gx * gx, gx * gy, gy * gy], -1))
            w = np.ones_like(gx)
            for _ in range(4):
                Aw = A * w[..., None]
                N = np.einsum("nki,nkj->nij", Aw, A) + np.eye(A.shape[-1]) * 1e-6
                b = np.einsum("nki,nk->ni", Aw, gz)
                c = np.linalg.solve(N, b[..., None])[..., 0]
                res = gz - np.einsum("nki,ni->nk", A, c)
                sc = 1.4826 * np.median(np.abs(res), 1, keepdims=True) + 1e-3
                u = res / (4.685 * sc)
                w = np.where(np.abs(u) < 1, (1 - u * u) ** 2, 0.0) + 1e-9
            dz = ctx.z[ss] - c[:, 0]
            g = gate_all[s:s + len(ss)]
            side = self.v("side")
            out[s:s + len(ss)] = (dz > g) if side == "Above only" else (dz < -g) if side == "Below only" else \
                (np.abs(dz) > g)
        return _res(reject=sel[out])


MARINE_FILTERS = (SonarGates, RejectOutliers, DepthHypotheses, SurfaceSpline)


def marine_groups(make, FilterGroup):
    """The ready-made marine groups (IHO Order 1a unless named otherwise)."""
    G = []
    g = FilterGroup("Marine - standard clean (IHO Order 1a)")
    g.filters = [make("reset"), make("m_gates", inboard=20.0), make("isolated", interval=1.0, min_pts=10),
                 make("m_cube"), make("m_outliers"), make("m_spline")]
    G.append(g)
    g = FilterGroup("Marine - light clean (reconnaissance)")
    g.filters = [make("reset"), make("m_gates"), make("isolated", interval=1.5, min_pts=5),
                 make("m_outliers", factor=2.0)]
    G.append(g)
    g = FilterGroup("Marine - strict (IHO Special Order)")
    g.filters = [make("reset"), make("m_gates", inboard=10.0, outboard=70.0),
                 make("isolated", interval=0.75, min_pts=10), make("m_cube", order="Special"),
                 make("m_outliers", order="Special"), make("m_spline", order="Special")]
    G.append(g)
    g = FilterGroup("Marine - water column and multipath")
    g.filters = [make("m_cube"), make("m_spline", side="Above only"), make("m_spline", side="Below only", factor=2.0)]
    G.append(g)
    return G
