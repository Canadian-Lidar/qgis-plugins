"""CloudPro automatic filters.

Filters are organised in groups: the filters of a
group run in order, each on the result of the previous one, and the group's
result can be previewed before it is accepted.  Every filter works on the
*accepted* points that meet its input conditions (classes, lines); the
neighbourhood filters use all accepted points as neighbours.

A filter returns proposed modifications:
    reject  - indices to reject (reason: automatic)
    accept  - indices to re-accept
    cls     - (indices, class values)
    dz      - (indices, height shift in metres, added to the current shift)

Sources for the published methods are given in each filter's
``ref`` and listed in docs/REFERENCES.md.
"""
import math

import numpy as np

from . import engine
from . import spatial as sp
from .store import REASONS, REJ_AUTO, REJ_MANUAL, REJ_ORIG, REJECTED

ASPRS = {0: "Created, never classified", 1: "Unclassified", 2: "Ground", 3: "Low vegetation",
         4: "Medium vegetation", 5: "High vegetation", 6: "Building", 7: "Low point (noise)",
         9: "Water", 10: "Rail", 11: "Road surface", 13: "Wire - guard", 14: "Wire - conductor",
         15: "Transmission tower", 17: "Bridge deck", 18: "High noise", 19: "Overhead structure",
         20: "Ignored ground", 64: "Vehicle (dynamic)", 65: "Pole", 66: "Snow / rain", 67: "Traffic sign",
         68: "Road marking", 69: "Curb", 70: "Manhole cover", 71: "Guardrail"}


class P:
    """Parameter spec."""

    def __init__(self, key, label, kind, default, lo=None, hi=None, step=None, unit="", choices=None, help=""):
        self.key, self.label, self.kind, self.default = key, label, kind, default
        self.lo, self.hi, self.step, self.unit, self.choices, self.help = lo, hi, step, unit, choices, help


COMMON = [P("classes", "Only classes", "text", "", help="Comma list of ASPRS classes to edit, e.g. 1,2. Empty = all"),
          P("lines", "Only lines", "text", "", help="Comma list of line ids (1-based) to edit. Empty = all")]


class Filter:
    key = ""
    name = ""
    group = ""
    mod = "R"                 # R reject, C classify, Z height, A accept
    speed = "++"
    ref = ""
    help = ""
    params = []
    needs_tracks = False

    def __init__(self, **values):
        self.enabled = True
        self.values = {p.key: p.default for p in self.all_params()}
        self.values.update({k: v for k, v in values.items() if k in self.values})

    @classmethod
    def all_params(cls):
        return list(cls.params) + COMMON

    def v(self, k):
        return self.values[k]

    def summary(self):
        bits = []
        for p in self.params[:3]:
            val = self.values[p.key]
            if p.kind == "float":
                bits.append("%s %g%s" % (p.label.split(" (")[0], val, p.unit))
            elif p.kind in ("int", "choice", "text") and val != "":
                bits.append("%s %s" % (p.label, val))
        return ", ".join(bits)

    def to_dict(self):
        return {"key": self.key, "enabled": self.enabled, "values": dict(self.values)}

    def run(self, ctx, sel):          # pragma: no cover - interface
        raise NotImplementedError


class Ctx:
    """State a filter group works on (copies; the store is untouched until
    the preview is accepted)."""

    def __init__(self, ps, tracks=None, reference=None, scope_mask=None, cancel=None, origin=(0.0, 0.0, 0.0)):
        self.ps = ps
        self.origin = np.asarray(origin, float)
        self.ps_origin_z = float(self.origin[2])
        self.group_rejected = np.zeros(len(ps), bool)
        self.group_modified = np.zeros(len(ps), bool)
        self.x, self.y = ps.x, ps.y
        self.z = ps.z.copy()
        self.flag = ps.flag.copy()
        self.cls = ps.cls.copy()
        self.dz = ps.dz.astype(np.float64) * 1e-3
        self.tracks = tracks or {}
        self.reference = reference
        self.scope = np.ones(len(ps), bool) if scope_mask is None else scope_mask
        self.cancel = cancel or (lambda: False)
        self.messages = []
        self._tree = None
        self._tree_mask = None

    @property
    def accepted(self):
        return (self.flag & REJECTED) == 0

    def xyz(self, idx=None):
        if idx is None:
            return np.column_stack([self.x, self.y, self.z])
        return np.column_stack([self.x[idx], self.y[idx], self.z[idx]])

    def neighbours(self):
        """(tree over accepted points, their indices) - rebuilt when the
        accepted set changes."""
        acc = np.flatnonzero(self.accepted)
        if self._tree is None or self._tree_mask is None or len(self._tree_mask) != len(acc) or \
                not np.array_equal(self._tree_mask, acc):
            self._tree = sp.tree(self.xyz(acc))
            self._tree_mask = acc
        return self._tree, self._tree_mask

    def sensor(self, idx):
        """Sensor origin (E, N, Z) and heading at the firing time of points idx."""
        n = len(idx)
        E = np.full(n, np.nan)
        N = np.full(n, np.nan)
        Z = np.full(n, np.nan)
        H = np.full(n, np.nan)
        lines = self.ps.line[idx]
        for lid in np.unique(lines):
            tr = self.tracks.get(int(lid))
            if tr is None:
                continue
            m = lines == lid
            t = self.ps.t[idx[m]]
            E[m] = np.interp(t, tr["t"], tr["E"])
            N[m] = np.interp(t, tr["t"], tr["N"])
            Z[m] = np.interp(t, tr["t"], tr["Z"])
            H[m] = np.interp(t, tr["t"], tr["heading"])
        return E, N, Z, H


def _res(reject=None, accept=None, cls=None, dz=None):
    return {"reject": reject, "accept": accept, "cls": cls, "dz": dz}


def _action(ctx, idx, mode, cls_value):
    """Common 'reject / classify / both' output."""
    idx = np.asarray(idx, np.int64)
    r = _res()
    if mode in ("Reject", "Reject + classify"):
        r["reject"] = idx
    if mode in ("Classify", "Reject + classify"):
        r["cls"] = (idx, int(cls_value))
    return r


ACTION = lambda default="Reject", cls=1: [
    P("action", "Action", "choice", default, choices=["Reject", "Classify", "Reject + classify"]),
    P("cls", "Class to set", "int", cls, 0, 255, help="ASPRS class used by Classify")]


# =============================================================================
# 1. Reset / edit
# =============================================================================
class ResetModifications(Filter):
    key, name, group, mod = "reset", "Reset modifications", "Edit", "R/Z/C"
    help = "Re-accepts rejected points (by reason), removes height shifts and/or resets the classification."
    params = [P("auto", "Re-accept automatic rejections", "bool", True),
              P("manual", "Re-accept manual rejections", "bool", False),
              P("orig", "Re-accept original rejections", "bool", False),
              P("dz", "Remove height shifts", "bool", False),
              P("cls", "Reset classification to 1", "bool", False)]

    def run(self, ctx, sel_unused):
        scope = np.flatnonzero(ctx.scope & self._cond(ctx))
        reasons = (REJ_AUTO if self.v("auto") else 0) | (REJ_MANUAL if self.v("manual") else 0) | \
            (REJ_ORIG if self.v("orig") else 0)
        f = ctx.flag[scope]
        rej = (f & REJECTED) != 0
        # re-accept when every reason of the point is among the selected ones
        ok = rej & (((f & REASONS) & np.uint8(~reasons & 0xFF)) == 0) & ((f & REASONS) != 0)
        r = _res(accept=scope[ok])
        if self.v("dz"):
            m = ctx.dz[scope] != 0
            r["dz"] = (scope[m], -ctx.dz[scope[m]])
        if self.v("cls"):
            r["cls"] = (scope, 1)
        return r

    def _cond(self, ctx):
        return _condition(ctx, self, include_rejected=True)


class EditClassification(Filter):
    key, name, group, mod = "edit_class", "Edit classification", "Edit", "R/C"
    help = ("Sets class and/or status of points that meet the conditions. Put it last in a group with "
            "'Rejected by this group' to mark which group rejected a point.")
    params = [P("when", "Only when point", "choice", "Any",
                choices=["Any", "Is accepted", "Is rejected", "Rejected - automatic", "Rejected - manual",
                         "Rejected - original", "Rejected by this group", "Modified by this group"]),
              P("set_cls", "Set class (-1 = keep)", "int", -1, -1, 255),
              P("set_status", "Set status", "choice", "Keep", choices=["Keep", "Accept", "Reject"])]

    def run(self, ctx, sel_unused):
        idx = np.flatnonzero(ctx.scope & _condition(ctx, self, include_rejected=True))
        f = ctx.flag[idx]
        w = self.v("when")
        if w == "Is accepted":
            idx = idx[(f & REJECTED) == 0]
        elif w == "Is rejected":
            idx = idx[(f & REJECTED) != 0]
        elif w == "Rejected - automatic":
            idx = idx[((f & REJECTED) != 0) & ((f & REJ_AUTO) != 0)]
        elif w == "Rejected - manual":
            idx = idx[((f & REJECTED) != 0) & ((f & REJ_MANUAL) != 0)]
        elif w == "Rejected - original":
            idx = idx[((f & REJECTED) != 0) & ((f & REJ_ORIG) != 0)]
        elif w == "Rejected by this group":
            g = getattr(ctx, "group_rejected", None)
            idx = idx[g[idx]] if g is not None else idx[:0]
        elif w == "Modified by this group":
            g = getattr(ctx, "group_modified", None)
            idx = idx[g[idx]] if g is not None else idx[:0]
        r = _res()
        if self.v("set_cls") >= 0:
            r["cls"] = (idx, int(self.v("set_cls")))
        if self.v("set_status") == "Accept":
            r["accept"] = idx
        elif self.v("set_status") == "Reject":
            r["reject"] = idx
        return r


class ShiftHeight(Filter):
    key, name, group, mod = "shift", "Shift height", "Height", "Z"
    help = "Shifts the selected points up (+) or down (-); added to any existing shift."
    params = [P("dz", "Shift", "float", 0.0, -100, 100, 0.001, " m")]

    def run(self, ctx, sel):
        return _res(dz=(sel, float(self.v("dz"))))


# =============================================================================
# 2. Clip filters
# =============================================================================
class ClipHeight(Filter):
    key, name, group = "clip_height", "Clip absolute height", "Clip"
    help = "Rejects points outside a height range (project vertical datum)."
    params = [P("zmin", "Minimum height", "float", -100.0, -10000, 10000, 0.1, " m"),
              P("zmax", "Maximum height", "float", 1000.0, -10000, 10000, 0.1, " m"),
              P("mode", "Mode", "choice", "Clip outside", choices=["Clip outside", "Clip inside"])]

    def run(self, ctx, sel):
        Z = ctx.z[sel] + ctx.ps_origin_z
        inside = (Z >= self.v("zmin")) & (Z <= self.v("zmax"))
        return _res(reject=sel[~inside] if self.v("mode") == "Clip outside" else sel[inside])


class ClipIntensity(Filter):
    key, name, group = "clip_intensity", "Clip intensity", "Clip"
    help = ("Absolute: reject outside [min, max]. Relative modes: per-cell mean and SD of the intensity "
            "over the cell and its neighbour rings; reject outside mean +/- k*SD (high, low or both).")
    params = [P("mode", "Mode", "choice", "Absolute - clip outside",
                choices=["Absolute - clip outside", "Relative - clip high", "Relative - clip low",
                         "Relative - clip outside"]),
              P("imin", "Minimum", "int", 0, 0, 255), P("imax", "Maximum", "int", 255, 0, 255),
              P("cell", "Cell size", "float", 1.0, 0.05, 50, 0.05, " m"),
              P("rings", "Neighbour rings", "int", 1, 0, 5),
              P("k", "SD multiplier", "float", 3.0, 0.5, 10, 0.1),
              P("range_norm", "Range-normalise intensity (I*(r/10 m)^2)", "bool", False)]

    def run(self, ctx, sel):
        I = ctx.ps.i[sel].astype(float)
        if self.v("range_norm"):
            r = np.maximum(ctx.ps.rng[sel] * 0.01, 0.5)
            I = I * (r / 10.0) ** 2
        m = self.v("mode")
        if m.startswith("Absolute"):
            return _res(reject=sel[(I < self.v("imin")) | (I > self.v("imax"))])
        cell = self.v("cell")
        ix, iy, _, _ = sp.grid_index(ctx.x[sel], ctx.y[sel], cell)
        nx, ny = ix.max() + 1, iy.max() + 1
        S = np.zeros((ny, nx))
        S2 = np.zeros((ny, nx))
        C = np.zeros((ny, nx))
        np.add.at(S, (iy, ix), I)
        np.add.at(S2, (iy, ix), I * I)
        np.add.at(C, (iy, ix), 1)
        k = self.v("rings")
        if k > 0:
            from scipy import ndimage
            fp = np.ones((2 * k + 1, 2 * k + 1))
            S = ndimage.convolve(S, fp, mode="constant")
            S2 = ndimage.convolve(S2, fp, mode="constant")
            C = ndimage.convolve(C, fp, mode="constant")
        with np.errstate(invalid="ignore", divide="ignore"):
            mu = S / C
            sd = np.sqrt(np.maximum(S2 / C - mu * mu, 0))
        mu, sd = mu[iy, ix], sd[iy, ix]
        hi = I > mu + self.v("k") * sd
        lo = I < mu - self.v("k") * sd
        bad = hi if m.endswith("high") else lo if m.endswith("low") else (hi | lo)
        return _res(reject=sel[bad])


class ClipRange(Filter):
    key, name, group = "clip_range", "Clip range / sensor geometry", "Clip"
    help = ("Rejects returns by their geometry relative to the Lidar: range, height above or below the "
            "sensor, side of the vehicle, azimuth window.")
    params = [P("rmin", "Minimum range", "float", 0.0, 0, 500, 0.1, " m"),
              P("rmax", "Maximum range", "float", 200.0, 0, 500, 0.5, " m"),
              P("above", "Max. height above sensor", "float", 100.0, -50, 500, 0.1, " m"),
              P("below", "Max. depth below sensor", "float", 100.0, -50, 500, 0.1, " m"),
              P("side", "Side", "choice", "Both", choices=["Both", "Port only", "Starboard only"]),
              P("az0", "Azimuth window from", "float", 0.0, 0, 360, 1, " deg"),
              P("az1", "Azimuth window to", "float", 360.0, 0, 360, 1, " deg")]
    needs_tracks = True

    def run(self, ctx, sel):
        r = ctx.ps.rng[sel] * 0.01
        bad = (r < self.v("rmin")) | (r > self.v("rmax"))
        a0, a1 = self.v("az0"), self.v("az1")
        if not (a0 <= 0 and a1 >= 360):
            az = ctx.ps.az[sel] * 0.01
            inwin = (az >= a0) & (az <= a1) if a0 <= a1 else (az >= a0) | (az <= a1)
            bad |= ~inwin
        if ctx.tracks and (self.v("above") < 100 or self.v("below") < 100 or self.v("side") != "Both"):
            E, N, Z, H = ctx.sensor(sel)
            dzs = ctx.z[sel] - Z
            bad |= (dzs > self.v("above")) | (-dzs > self.v("below"))
            if self.v("side") != "Both":
                right = (ctx.x[sel] - E) * np.cos(H) - (ctx.y[sel] - N) * np.sin(H)
                bad |= (right > 0) if self.v("side") == "Port only" else (right < 0)
        return _res(reject=sel[bad])


class ClipChannelReturn(Filter):
    key, name, group = "clip_quality", "Clip laser channel / return", "Clip"
    help = ("Reject by laser id (1-32) and/or return type. "
            "Laser ids as a list, e.g. 1,2,30-32.")
    params = [P("channels", "Reject laser ids", "text", ""),
              P("returns", "Reject return", "choice", "None", choices=["None", "Last (1st block)",
                                                                        "Strongest (2nd block)"])]

    def run(self, ctx, sel):
        bad = np.zeros(len(sel), bool)
        ch = sp.parse_set(self.v("channels"), offset=1)
        if ch:
            bad |= np.isin(ctx.ps.ch[sel], list(ch))
        rv = self.v("returns")
        if rv.startswith("Last"):
            bad |= ctx.ps.ret[sel] == 0
        elif rv.startswith("Strongest"):
            bad |= ctx.ps.ret[sel] == 1
        return _res(reject=sel[bad])


class ClipTime(Filter):
    key, name, group = "clip_time", "Clip time", "Clip"
    help = ("Newest per cell: per cell keep only points newer than (newest - max age), e.g. to keep the "
            "latest pass over a changed area. Window: reject points outside a UTC time window "
            "(seconds of day).")
    params = [P("mode", "Mode", "choice", "Newest per cell", choices=["Newest per cell", "Window (s of day)"]),
              P("age", "Max. age", "float", 60.0, 0, 1e7, 1, " s"),
              P("cell", "Cell size", "float", 1.0, 0.05, 50, 0.05, " m"),
              P("t0", "Window from (s of day UTC)", "float", 0.0, 0, 86400, 1),
              P("t1", "Window to (s of day UTC)", "float", 86400.0, 0, 86400, 1)]

    def run(self, ctx, sel):
        t = ctx.ps.t[sel]
        if self.v("mode").startswith("Window"):
            sod = np.mod(t, 86400.0)
            return _res(reject=sel[(sod < self.v("t0")) | (sod > self.v("t1"))])
        ix, iy, _, _ = sp.grid_index(ctx.x[sel], ctx.y[sel], self.v("cell"))
        ids, n = sp.compact_ids(ix, iy)
        newest = np.full(n, -np.inf)
        np.maximum.at(newest, ids, t)
        return _res(reject=sel[t < newest[ids] - self.v("age")])


class VehicleBox(Filter):
    key, name, group = "vehicle_box", "Vehicle self-hits", "Clip"
    help = ("Rejects returns inside a box around the Lidar in the vehicle frame (roof rack, mast, the "
            "vehicle itself, people walking around a parked car).")
    params = [P("fwd", "Forward extent", "float", 2.5, 0, 30, 0.1, " m"),
              P("back", "Backward extent", "float", 3.0, 0, 30, 0.1, " m"),
              P("half_w", "Half width", "float", 1.2, 0, 15, 0.1, " m"),
              P("up", "Above sensor", "float", 0.5, -5, 10, 0.1, " m"),
              P("down", "Below sensor", "float", 1.6, -5, 10, 0.1, " m",
                help="Keep this above the road: Lidar height above the road minus ~0.4 m")]
    needs_tracks = True

    def run(self, ctx, sel):
        if not ctx.tracks:
            ctx.messages.append("Vehicle self-hits: no sensor track for these lines")
            return _res()
        E, N, Z, H = ctx.sensor(sel)
        dx, dy = ctx.x[sel] - E, ctx.y[sel] - N
        f = dx * np.sin(H) + dy * np.cos(H)
        rgt = dx * np.cos(H) - dy * np.sin(H)
        u = ctx.z[sel] - Z
        m = (f <= self.v("fwd")) & (f >= -self.v("back")) & (np.abs(rgt) <= self.v("half_w")) & \
            (u <= self.v("up")) & (u >= -self.v("down"))
        return _res(reject=sel[m & np.isfinite(E)])


class ClipReference(Filter):
    key, name, group = "clip_reference", "Clip reference surface", "Clip"
    help = ("Rejects points outside a gate around a reference surface: a raster layer from QGIS (DEM, "
            "earlier survey, design) chosen in the Filter panel. Cells without reference are ignored.")
    params = [P("above", "Max. above reference", "float", 0.3, 0, 1000, 0.01, " m"),
              P("below", "Max. below reference", "float", 0.3, 0, 1000, 0.01, " m")]

    def run(self, ctx, sel):
        if ctx.reference is None:
            ctx.messages.append("Clip reference surface: no reference raster selected")
            return _res()
        zr = ctx.reference(ctx.x[sel], ctx.y[sel])
        d = ctx.z[sel] + ctx.ps_origin_z - zr
        ok = np.isfinite(zr)
        return _res(reject=sel[ok & ((d > self.v("above")) | (-d > self.v("below")))])


# =============================================================================
# 3. Cell statistics
# =============================================================================
class Confidence95(Filter):
    key, name, group, speed = "conf95", "95% confidence level", "Cell statistics", "++"
    help = ("Per cell, iteratively rejects the point furthest from the cell median until 1.96*SD is "
            "below the limit or the minimum count is reached. For surfaces (use 'Only classes' = 2 "
            "after ground classification, or on road surveys).")
    params = [P("cell", "Cell size", "float", 0.5, 0.05, 20, 0.05, " m"),
              P("limit", "Max. 95% confidence", "float", 0.05, 0.001, 5, 0.005, " m"),
              P("nmin", "Min. points in cell", "int", 5, 2, 1000),
              P("iters", "Max. iterations", "int", 50, 1, 500)]

    def run(self, ctx, sel):
        ix, iy, _, _ = sp.grid_index(ctx.x[sel], ctx.y[sel], self.v("cell"))
        ids, n = sp.compact_ids(ix, iy)
        z = ctx.z[sel]
        alive = np.ones(len(sel), bool)
        for _ in range(int(self.v("iters"))):
            a = np.flatnonzero(alive)
            if len(a) == 0:
                break
            ia = ids[a]
            cnt, mean, sd = sp.group_stats(ia, n, z[a])
            med = sp.group_median(ia, n, z[a])
            bad_cell = (1.96 * sd > self.v("limit")) & (cnt > self.v("nmin"))
            if not bad_cell.any():
                break
            dev = np.abs(z[a] - med[ia])
            dev[~bad_cell[ia]] = -1
            # worst point per bad cell
            order = np.lexsort((-dev, ia))
            first = np.r_[True, np.diff(ia[order]) != 0]
            worst = order[first]
            worst = worst[dev[worst] >= 0]
            if len(worst) == 0:
                break
            alive[a[worst]] = False
        return _res(reject=sel[~alive])


class LowHitCount(Filter):
    key, name, group = "low_hit", "Low hit count", "Cell statistics"
    help = "Rejects all points of a cell (2-D) or voxel (3-D) holding fewer accepted points than the minimum."
    params = [P("cell", "Cell size", "float", 0.5, 0.02, 50, 0.05, " m"),
              P("nmin", "Min. points", "int", 3, 1, 10000),
              P("mode", "Cells", "choice", "3-D voxels", choices=["2-D cells", "3-D voxels"])]

    def run(self, ctx, sel):
        c = self.v("cell")
        acc = np.flatnonzero(ctx.accepted)
        pts = ctx.xyz(acc)
        v = np.floor(pts / c).astype(np.int64)
        if self.v("mode") == "2-D cells":
            v = v[:, :2]
        ids, n = sp.compact_ids(*[v[:, j] for j in range(v.shape[1])])
        cnt = np.bincount(ids, minlength=n)
        pos = np.full(len(ctx.x), -1)
        pos[acc] = ids
        s_ids = pos[sel]
        return _res(reject=sel[(s_ids >= 0) & (cnt[np.maximum(s_ids, 0)] < self.v("nmin"))])


class ClipMeanHeight(Filter):
    key, name, group = "clip_mean", "Clip mean height", "Cell statistics"
    help = ("Rejects points further than the gate from the cell mean (over the cell and its neighbour "
            "rings). 'Point centred' builds a virtual cell around each point, excluding the point.")
    params = [P("cell", "Cell size", "float", 1.0, 0.05, 50, 0.05, " m"),
              P("gate", "Reject outside mean +/-", "float", 0.3, 0.005, 100, 0.01, " m"),
              P("rings", "Neighbour rings", "int", 1, 0, 5),
              P("centred", "Point centred", "bool", False)]

    def run(self, ctx, sel):
        c = self.v("cell")
        acc = np.flatnonzero(ctx.accepted)
        x, y, z = ctx.x[acc], ctx.y[acc], ctx.z[acc]
        if self.v("centred"):
            tr = sp.tree(np.column_stack([x, y]))
            r = c * (0.5 + self.v("rings"))
            nb = tr.query_ball_point(np.column_stack([ctx.x[sel], ctx.y[sel]]), r, workers=-1)
            cnt = np.array([len(b) for b in nb])
            flat = np.concatenate([np.asarray(b, np.int64) for b in nb]) if len(nb) else np.zeros(0, np.int64)
            owner = np.repeat(np.arange(len(sel)), cnt)
            s = np.bincount(owner, z[flat], minlength=len(sel)) - ctx.z[sel]
            with np.errstate(invalid="ignore", divide="ignore"):
                mean = s / (cnt - 1)
            return _res(reject=sel[(cnt > 1) & (np.abs(ctx.z[sel] - mean) > self.v("gate"))])
        ix, iy, x0, y0 = sp.grid_index(x, y, c)
        nx, ny = ix.max() + 1, iy.max() + 1
        S = np.zeros((ny, nx))
        C = np.zeros((ny, nx))
        np.add.at(S, (iy, ix), z)
        np.add.at(C, (iy, ix), 1)
        k = self.v("rings")
        if k:
            from scipy import ndimage
            fp = np.ones((2 * k + 1, 2 * k + 1))
            S = ndimage.convolve(S, fp, mode="constant")
            C = ndimage.convolve(C, fp, mode="constant")
        with np.errstate(invalid="ignore", divide="ignore"):
            M = S / C
        jx = np.clip(np.floor((ctx.x[sel] - x0) / c).astype(int), 0, nx - 1)
        jy = np.clip(np.floor((ctx.y[sel] - y0) / c).astype(int), 0, ny - 1)
        return _res(reject=sel[np.abs(ctx.z[sel] - M[jy, jx]) > self.v("gate")])


class LonelyCells(Filter):
    key, name, group = "lonely_cells", "Lonely cells", "Cell statistics"
    help = "Rejects points in 2-D cells not connected (by a side) to the main survey cells."
    params = [P("cell", "Cell size", "float", 0.5, 0.05, 50, 0.05, " m"),
              P("min_cells", "Keep groups of at least (cells)", "int", 0, 0, 1000000,
                help="0 = keep only the largest group")]

    def run(self, ctx, sel):
        from scipy import ndimage
        acc = np.flatnonzero(ctx.accepted)
        ix, iy, x0, y0 = sp.grid_index(ctx.x[acc], ctx.y[acc], self.v("cell"))
        occ = np.zeros((iy.max() + 1, ix.max() + 1), bool)
        occ[iy, ix] = True
        lab, n = ndimage.label(occ)
        if n <= 1:
            return _res()
        sizes = np.bincount(lab.ravel())
        sizes[0] = 0
        keep = np.zeros(n + 1, bool)
        if self.v("min_cells") > 0:
            keep = sizes >= self.v("min_cells")
        else:
            keep[np.argmax(sizes)] = True
        keep[0] = False
        c = self.v("cell")
        jx = np.floor((ctx.x[sel] - x0) / c).astype(int)
        jy = np.floor((ctx.y[sel] - y0) / c).astype(int)
        ok = (jx >= 0) & (jy >= 0) & (jx < occ.shape[1]) & (jy < occ.shape[0])
        bad = np.zeros(len(sel), bool)
        bad[ok] = ~keep[lab[jy[ok], jx[ok]]]
        return _res(reject=sel[bad])


class VoxelReducer(Filter):
    key, name, group, speed = "voxel_reduce", "Reducer (voxel)", "Reduce", "+"
    help = ("Keeps the point closest to each voxel centre and rejects the others. Also removes duplicate "
            "points (sizes of a few mm).")
    params = [P("h", "Horizontal voxel size", "float", 0.05, 0.001, 50, 0.005, " m"),
              P("v", "Vertical voxel size", "float", 0.05, 0.001, 50, 0.005, " m")]

    def run(self, ctx, sel):
        h, vv = self.v("h"), self.v("v")
        q = np.column_stack([ctx.x[sel] / h, ctx.y[sel] / h, ctx.z[sel] / vv])
        v = np.floor(q).astype(np.int64)
        ids, n = sp.compact_ids(v[:, 0], v[:, 1], v[:, 2])
        d = ((q - v - 0.5) ** 2).sum(1)
        order = np.lexsort((d, ids))
        first = np.r_[True, np.diff(ids[order]) != 0]
        keep = np.zeros(len(sel), bool)
        keep[order[first]] = True
        return _res(reject=sel[~keep])


class ScanReducer(Filter):
    key, name, group = "scan_reduce", "Reducer (scan based)", "Reduce"
    help = ("Thins in an organised way: keep every Nth rotation of the scanner (10 Hz) and/or every Nth "
            "laser channel.")
    params = [P("rot", "Keep every Nth rotation", "int", 1, 1, 100),
              P("chan", "Keep every Nth channel", "int", 1, 1, 32)]

    def run(self, ctx, sel):
        bad = np.zeros(len(sel), bool)
        if self.v("rot") > 1:
            rot = np.floor(ctx.ps.t[sel] * 10.0).astype(np.int64)
            bad |= (rot % self.v("rot")) != 0
        if self.v("chan") > 1:
            bad |= (ctx.ps.ch[sel].astype(int) % self.v("chan")) != 0
        return _res(reject=sel[bad])


# =============================================================================
# 4. Neighbourhood / statistical outliers
# =============================================================================
class AreaOutlier(Filter):
    key, name, group, speed = "sor", "Statistical outlier (SOR)", "Outliers", "+"
    ref = "Rusu et al. 2008, Robotics and Autonomous Systems 56(11):927-941, doi:10.1016/j.robot.2008.08.005"
    help = ("Statistical outlier removal: mean distance to the k nearest neighbours; reject when "
            "above mean + alpha*SD of all points. 'Range adaptive' scales the distances by range first "
            "(density falls with range on a spinning Lidar). Run it early: on an already clean cloud the "
            "global statistics tighten and sparse real objects (tree crowns) start to go.")
    params = [P("k", "Neighbours k", "int", 12, 3, 200), P("alpha", "SD multiplier", "float", 2.5, 0.1, 20, 0.1),
              P("adaptive", "Range adaptive", "bool", True)]

    def run(self, ctx, sel):
        tr, acc = ctx.neighbours()
        k = int(self.v("k"))
        md = sp.mean_knn_distance(tr, ctx.xyz(sel), k)          # in blocks: bounded memory
        if self.v("adaptive"):
            r = np.maximum(ctx.ps.rng[sel] * 0.01, 1.0)
            md = md / r
        mu, sd = md.mean(), md.std()
        return _res(reject=sel[md > mu + self.v("alpha") * sd])


class RadiusOutlier(Filter):
    key, name, group, speed = "ror", "Radius outlier (ROR)", "Outliers", "+"
    help = "Rejects points with fewer than N neighbours within radius R (3-D)."
    params = [P("r", "Radius", "float", 0.3, 0.01, 10, 0.01, " m"), P("nmin", "Min. neighbours", "int", 3, 1, 500)]

    def run(self, ctx, sel):
        tr, acc = ctx.neighbours()
        few = sp.fewer_than(tr, ctx.xyz(sel), self.v("r"), int(self.v("nmin")))   # nearest-neighbour test
        return _res(reject=sel[few])


class Weather(Filter):
    key, name, group, speed = "weather", "Snow / rain / dust (DROR, LIOR, DIOR)", "Outliers", "+"
    ref = ("Charron, Phillips & Waslander 2018, CRV, pp. 254-261 (DROR); Park, Park & Kim 2020, IEEE "
           "Access 8:160202, doi:10.1109/ACCESS.2020.3020266 (LIOR); Roriz et al. 2022, IEEE Sensors J. "
           "22(2):1621, doi:10.1109/JSEN.2021.3133873 (DIOR)")
    help = ("DROR: radius outlier removal with a radius that grows with range, SR = max(SRmin, "
            "beta*r*alpha), alpha = azimuth step (0.18 deg on the XT-32 at 10 Hz). LIOR/DIOR first keep "
            "every bright return (intensity above the threshold, optionally range-normalised) and run "
            "ROR/DROR only on the dim candidates, which protects signs, markings and thin structures.")
    params = [P("mode", "Method", "choice", "DIOR (intensity + DROR)",
                choices=["DROR", "LIOR (intensity + ROR)", "DIOR (intensity + DROR)"]),
              P("beta", "Beta (radius multiplier)", "float", 3.0, 0.5, 20, 0.1),
              P("alpha", "Azimuth step alpha", "float", 0.18, 0.01, 2, 0.01, " deg"),
              P("srmin", "Min. search radius", "float", 0.04, 0.005, 2, 0.005, " m"),
              P("kmin", "Min. neighbours", "int", 3, 1, 50),
              P("ror_r", "ROR radius (LIOR)", "float", 0.2, 0.01, 5, 0.01, " m"),
              P("ithr", "Intensity threshold", "int", 18, 0, 255),
              P("inorm", "Range-normalise intensity", "bool", False),
              P("max_range", "Only within range", "float", 40.0, 1, 500, 1, " m",
                help="Weather returns are near the sensor; points beyond are kept")]

    def run(self, ctx, sel):
        r = ctx.ps.rng[sel] * 0.01
        cand = r <= self.v("max_range")
        mode = self.v("mode")
        if not mode.startswith("DROR"):
            I = ctx.ps.i[sel].astype(float)
            if self.v("inorm"):
                I = I * (np.maximum(r, 0.5) / 10.0) ** 2
            cand &= I < self.v("ithr")
        ci = np.flatnonzero(cand)
        if len(ci) == 0:
            return _res()
        tr, acc = ctx.neighbours()
        q = ctx.xyz(sel[ci])
        if mode.startswith("LIOR"):
            rad = np.full(len(ci), self.v("ror_r"))
        else:
            rad = np.maximum(self.v("srmin"), self.v("beta") * r[ci] * math.radians(self.v("alpha")))
        few = sp.fewer_than(tr, q, rad, int(self.v("kmin")))      # nearest-neighbour test, any density
        return _res(reject=sel[ci[few]])


class IsolatedCluster(Filter):
    key, name, group, speed = "isolated", "Isolated cluster", "Outliers", "++"
    ref = "Connected components on occupied voxels; cf. DBSCAN, Ester et al. 1996, KDD-96"
    help = ("Groups points that lie within the point interval of each other (voxel connectivity). Rejects "
            "clusters smaller than the minimum, or every cluster except the largest. Z scale < 1 makes the "
            "filter less sensitive vertically.")
    params = [P("interval", "Point interval", "float", 0.5, 0.02, 20, 0.05, " m"),
              P("min_pts", "Reject clusters smaller than (points)", "int", 10, 1, 10_000_000),
              P("mode", "Mode", "choice", "Small clusters", choices=["Small clusters", "All but the largest"]),
              P("zscale", "Z scale", "float", 1.0, 0.05, 10, 0.05)]

    def run(self, ctx, sel):
        acc = np.flatnonzero(ctx.accepted)
        p = ctx.xyz(acc)
        p[:, 2] *= self.v("zscale")
        comp, sizes = sp.voxel_components(p, self.v("interval"))
        pos = np.full(len(ctx.x), -1)
        pos[acc] = comp
        c = pos[sel]
        if self.v("mode") == "Small clusters":
            bad = (c >= 0) & (sizes[np.maximum(c, 0)] < self.v("min_pts"))
        else:
            bad = (c >= 0) & (c != np.argmax(sizes))
        return _res(reject=sel[bad])


class SlopeSpike(Filter):
    key, name, group, speed = "slope", "Slope / spike filter", "Outliers", "-"
    ref = "Vosselman 2000, IAPRS 33(B3/2):935-942 (slope based filtering)"
    help = ("2.5-D spike filter: mean slope from a point to its k nearest neighbours (horizontal "
            "distance); reject when steeper than the limit and the point stands out above or below "
            "all of them. For surfaces (ground, road); restrict with 'Only classes'.")
    params = [P("k", "Neighbours", "int", 8, 3, 64), P("slope", "Max. mean slope", "float", 1.0, 0.05, 50, 0.05),
              P("dir", "Direction", "choice", "Both", choices=["Both", "Spikes up", "Spikes down"]),
              P("dh", "Min. height difference", "float", 0.05, 0, 10, 0.01, " m")]

    def run(self, ctx, sel):
        acc = np.flatnonzero(ctx.accepted & _class_mask(ctx, self))
        if len(acc) < 4:
            return _res()
        tr = sp.tree(np.column_stack([ctx.x[acc], ctx.y[acc]]))
        k = int(self.v("k"))
        d, i = tr.query(np.column_stack([ctx.x[sel], ctx.y[sel]]), k=k + 1, workers=-1)
        d, i = d[:, 1:], i[:, 1:]
        nz = ctx.z[acc][i]
        dzv = ctx.z[sel][:, None] - nz
        slope = np.abs(dzv) / np.maximum(d, 1e-3)
        ms = slope.mean(1)
        up = (dzv > self.v("dh")).all(1)
        dn = (dzv < -self.v("dh")).all(1)
        dirv = self.v("dir")
        stand = up | dn if dirv == "Both" else up if dirv == "Spikes up" else dn
        return _res(reject=sel[(ms > self.v("slope")) & stand])


class SurfaceFit(Filter):
    key, name, group, speed = "surface", "Surface fit (robust local quadric)", "Outliers", "-"
    ref = "Robust moving least squares, cf. Fleishman, Cohen-Or & Silva 2005, ACM TOG 24(3):544"
    help = ("Fits a local quadric z = f(x, y) through each "
            "point's k nearest neighbours (point excluded, Huber-reweighted twice) and rejects the point "
            "when it lies further than the gate from it. Run after the large spikes are gone.")
    params = [P("k", "Neighbours", "int", 16, 8, 100), P("gate", "Reject gate", "float", 0.08, 0.005, 5, 0.005, " m"),
              P("order", "Surface", "choice", "Quadric", choices=["Plane", "Quadric"])]

    def run(self, ctx, sel):
        acc = np.flatnonzero(ctx.accepted & _class_mask(ctx, self))
        if len(acc) < 10:
            return _res()
        tr = sp.tree(np.column_stack([ctx.x[acc], ctx.y[acc]]))
        k = int(self.v("k"))
        out = np.zeros(len(sel), bool)
        for s in range(0, len(sel), 200000):
            ss = sel[s:s + 200000]
            d, i = tr.query(np.column_stack([ctx.x[ss], ctx.y[ss]]), k=k + 1, workers=-1)
            i = i[:, 1:]
            gx = ctx.x[acc][i] - ctx.x[ss][:, None]
            gy = ctx.y[acc][i] - ctx.y[ss][:, None]
            gz = ctx.z[acc][i]
            if self.v("order") == "Plane":
                A = np.stack([np.ones_like(gx), gx, gy], -1)
            else:
                A = np.stack([np.ones_like(gx), gx, gy, gx * gx, gx * gy, gy * gy], -1)
            w = np.ones_like(gx)
            for _ in range(3):
                Aw = A * w[..., None]
                N = np.einsum("nki,nkj->nij", Aw, A) + np.eye(A.shape[-1]) * 1e-6
                b = np.einsum("nki,nk->ni", Aw, gz)
                c = np.linalg.solve(N, b[..., None])[..., 0]
                res = gz - np.einsum("nki,ni->nk", A, c)
                sc = 1.4826 * np.median(np.abs(res), 1, keepdims=True) + 1e-4
                u = np.abs(res) / (1.5 * sc)
                w = np.where(u <= 1, 1.0, 1.0 / u)
            out[s:s + len(ss)] = np.abs(ctx.z[ss] - c[:, 0]) > self.v("gate")
        return _res(reject=sel[out])


# =============================================================================
# 5. Geometry / structure
# =============================================================================
class VerticalPlane(Filter):
    key, name, group, speed = "vplane", "Vertical plane (walls)", "Structure", "-"
    ref = "RANSAC, Fischler & Bolles 1981; efficient variant Schnabel et al. 2007, CGF 26(2):214, doi:10.1111/j.1467-8659.2007.01016.x"
    help = ("Detects vertical planes (facades, walls, quay walls) by RANSAC in the working area. Points "
            "inside the plane can be classified (e.g. 6 building); points behind it, seen from the "
            "Lidar, are usually reflections through glass or interior returns and can be rejected.")
    params = [P("thick", "Thickness", "float", 0.10, 0.01, 2, 0.01, " m"),
              P("tilt", "Max. tilt from vertical", "float", 5.0, 0, 30, 0.5, " deg"),
              P("min_pts", "Min. points per plane", "int", 2000, 50, 10_000_000),
              P("min_h", "Min. wall height", "float", 1.0, 0.2, 100, 0.1, " m"),
              P("planes", "Max. planes", "int", 6, 1, 50),
              P("inside", "Inside the plane", "choice", "Classify", choices=["Nothing", "Classify", "Reject"]),
              P("cls", "Class for inside", "int", 6, 0, 255),
              P("behind", "Behind the plane", "choice", "Reject", choices=["Nothing", "Reject", "Classify"]),
              P("cls_b", "Class for behind", "int", 7, 0, 255),
              P("behind_gap", "Behind: further than", "float", 0.3, 0.01, 10, 0.01, " m")]
    needs_tracks = True

    def run(self, ctx, sel):
        rng = np.random.default_rng(1)
        pts = ctx.xyz(sel)
        free = np.ones(len(sel), bool)
        planes = []
        thick = self.v("thick")
        tilt_c = math.sin(math.radians(self.v("tilt")))
        # vertical structure candidates: points with near-horizontal normals help, use all
        for _ in range(int(self.v("planes"))):
            idx = np.flatnonzero(free)
            if len(idx) < self.v("min_pts"):
                break
            best = (0, None)
            sample = idx if len(idx) < 300000 else rng.choice(idx, 300000, replace=False)
            P2 = pts[sample, :2]
            if engine.available() and P2.dtype == np.float32:
                # the same 300 random draws, scored together by the engine on every core
                cand = []
                for _it in range(300):
                    a, b = rng.choice(len(sample), 2, replace=False)
                    d = P2[b] - P2[a]
                    L = np.hypot(*d)
                    if L >= 0.5:
                        cand.append((a, np.array([-d[1], d[0]]) / L))
                if cand:
                    cnts = engine.line_counts(P2, P2[[a for a, _n in cand]], np.array([n for _a, n in cand]),
                                              thick / 2)
                    for (a, nrm), cnt in zip(cand, cnts):
                        if cnt > best[0]:
                            best = (int(cnt), (nrm, float(P2[a] @ nrm)))
            else:
                for _it in range(300):
                    a, b = rng.choice(len(sample), 2, replace=False)
                    d = P2[b] - P2[a]
                    L = np.hypot(*d)
                    if L < 0.5:
                        continue
                    nrm = np.array([-d[1], d[0]]) / L
                    dist = np.abs((P2 - P2[a]) @ nrm)
                    cnt = int((dist < thick / 2).sum())
                    if cnt > best[0]:
                        best = (cnt, (nrm, float(P2[a] @ nrm)))
            if best[1] is None:
                break
            nrm, off = best[1]
            dist = pts[:, :2] @ nrm - off
            inl = free & (np.abs(dist) < thick / 2)
            if inl.sum() < self.v("min_pts"):
                break
            # refine with PCA on inliers (allow small tilt)
            Q = pts[inl]
            c = Q.mean(0)
            _, _, vt = np.linalg.svd(Q - c, full_matrices=False)
            n3 = vt[2]
            if abs(n3[2]) > tilt_c:
                free &= ~inl
                continue
            dist3 = (pts - c) @ n3
            inl = free & (np.abs(dist3) < thick / 2)
            zr = pts[inl, 2]
            if len(zr) == 0 or np.percentile(zr, 98) - np.percentile(zr, 2) < self.v("min_h"):
                free &= ~inl
                continue
            planes.append((c, n3, inl.copy()))
            free &= ~inl
        r = _res()
        ins, beh = [], []
        if planes and self.v("behind") != "Nothing":
            E, N, Z, H = ctx.sensor(sel) if ctx.tracks else (None,) * 4
        for c, n3, inl in planes:
            ins.append(np.flatnonzero(inl))
            if self.v("behind") != "Nothing" and E is not None:
                # signed distance of the sensor and the point; behind = opposite sides, beyond the gap,
                # and within the plane's along-wall extent
                ds = (np.column_stack([E, N, Z]) - c) @ n3
                dp = (pts - c) @ n3
                along = np.cross([0, 0, 1], n3)
                along /= np.linalg.norm(along) + 1e-12
                ua = (pts - c) @ along
                ui = ua[inl]
                zin = pts[inl, 2]
                ext = (ua >= ui.min()) & (ua <= ui.max()) & (pts[:, 2] >= zin.min()) & (pts[:, 2] <= zin.max())
                bh = np.isfinite(ds) & (np.sign(ds) != np.sign(dp)) & (np.abs(dp) > self.v("behind_gap")) & ext
                beh.append(np.flatnonzero(bh))
        ctx.messages.append("Vertical plane: %d plane(s) found" % len(planes))
        rej, cl, cv = [], [], []
        if ins and self.v("inside") == "Reject":
            rej.append(np.concatenate(ins))
        elif ins and self.v("inside") == "Classify":
            cl.append(np.concatenate(ins))
            cv.append(np.full(len(cl[-1]), self.v("cls")))
        if beh:
            b = np.concatenate(beh)
            if self.v("behind") == "Reject":
                rej.append(b)
            elif self.v("behind") == "Classify":
                cl.append(b)
                cv.append(np.full(len(b), self.v("cls_b")))
        if rej:
            r["reject"] = sel[np.unique(np.concatenate(rej))]
        if cl:
            r["cls"] = (sel[np.concatenate(cl)], np.concatenate(cv))
        return r


class GeometricFeatures(Filter):
    key, name, group, speed = "eigen", "Geometric features (eigenvalues)", "Structure", "-"
    ref = ("Demantke et al. 2011, ISPRS Archives XXXVIII-5/W12:97, doi:10.5194/isprsarchives-XXXVIII-5-W12-97-2011; "
           "Weinmann et al. 2015, ISPRS J. 105:286, doi:10.1016/j.isprsjprs.2015.01.016")
    help = ("Local covariance of the k nearest neighbours gives linearity, planarity and scattering. "
            "Scattered points are noise or vegetation; linear points are wires, poles, edges; planar "
            "vertical points are walls; planar horizontal points are ground or roofs.")
    params = [P("target", "Select", "choice", "Scattered",
                choices=["Scattered", "Linear", "Planar - vertical", "Planar - horizontal"]),
              P("k", "Neighbours k", "int", 16, 6, 100),
              P("thr", "Feature threshold", "float", 0.5, 0.0, 1.0, 0.01),
              P("vert", "Verticality threshold", "float", 0.85, 0.0, 1.0, 0.01)] + ACTION("Classify", 1)

    def run(self, ctx, sel):
        tr, acc = ctx.neighbours()
        k = int(self.v("k"))
        _, i = tr.query(ctx.xyz(sel), k=k, workers=-1)
        f = sp.eigen_features(ctx.xyz(acc), i)
        t = self.v("target")
        thr = self.v("thr")
        if t == "Scattered":
            m = f["scattering"] > thr
        elif t == "Linear":
            m = f["linearity"] > thr
        elif t == "Planar - vertical":
            m = (f["planarity"] > thr) & (f["verticality"] > self.v("vert"))
        else:
            m = (f["planarity"] > thr) & (f["nz"] > self.v("vert"))
        return _action(ctx, sel[m], self.v("action"), self.v("cls"))


# =============================================================================
# 6. Ground classification
# =============================================================================
def _min_grid(x, y, z, cell, pit=0.3, rank=3):
    """Robust minimum-elevation grid for the ground filters.

    Each cell takes its ``rank``-th lowest point (or the highest when it has
    fewer), so isolated multipath 'low points' below the road do not define
    the surface; remaining pits deeper than ``pit`` below the median of their
    neighbourhood (5x5, twice, then 3x3) are lifted to that median."""
    ix, iy, x0, y0 = sp.grid_index(x, y, cell)
    nx, ny = int(ix.max()) + 1, int(iy.max()) + 1
    cid = iy * nx + ix
    g = np.full(ny * nx, np.nan)
    if rank <= 1:
        g[:] = np.inf
        np.minimum.at(g, cid, z)
        g[np.isinf(g)] = np.nan
    else:
        o = np.lexsort((z, cid))
        cs = cid[o]
        first = np.r_[0, np.flatnonzero(np.diff(cs)) + 1]
        cnt = np.diff(np.r_[first, len(cs)])
        pick = first + np.minimum(rank, cnt) - 1
        g[cs[first]] = z[o][pick]
    g = g.reshape(ny, nx)
    if pit and pit > 0 and np.isfinite(g).sum() > 9:
        from scipy import ndimage
        for size in (5, 5, 3):
            f = sp.fill_nan_nearest(g)
            med = ndimage.median_filter(f, size=size)
            pits = np.isfinite(g) & (g < med - pit)
            if not pits.any():
                break
            g[pits] = med[pits]
    return g, ix, iy, x0, y0


def _ground_result(ctx, flt, sel, is_ground):
    r = _res()
    gidx = sel[is_ground]
    ngidx = sel[~is_ground]
    cl = [gidx]
    cv = [np.full(len(gidx), 2)]
    if flt.v("nonground") >= 0:
        # only re-label points currently labelled ground or unclassified
        m = np.isin(ctx.cls[ngidx], (0, 1, 2))
        cl.append(ngidx[m])
        cv.append(np.full(m.sum(), flt.v("nonground")))
    r["cls"] = (np.concatenate(cl), np.concatenate(cv))
    if flt.v("reject_ng"):
        r["reject"] = ngidx
    return r


GROUND_OUT = [P("nonground", "Class for non-ground (-1 = keep)", "int", 1, -1, 255),
              P("reject_ng", "Reject non-ground", "bool", False)]


class ClothFilter(Filter):
    key, name, group, mod, speed = "csf", "Inverted cloth (CSF ground)", "Ground", "C", "+"
    ref = "Zhang W. et al. 2016, Remote Sensing 8(6):501, doi:10.3390/rs8060501"
    help = ("Turns the cloud upside down and drops a simulated cloth on it; points within the clip "
            "distance of the settled cloth are ground. Rigidness: 3 flat, 2 relief, 1 steep slope.")
    params = [P("res", "Cloth resolution", "float", 0.5, 0.05, 10, 0.05, " m"),
              P("rigid", "Scene (rigidness)", "choice", "3 - Flat", choices=["1 - Steep slope", "2 - Relief",
                                                                             "3 - Flat", "4 - Super flat"]),
              P("iters", "Max. iterations", "int", 500, 50, 2000),
              P("hcc", "Clip distance", "float", 0.25, 0.01, 5, 0.01, " m"),
              P("smooth", "Slope smoothing", "bool", True)] + GROUND_OUT

    def run(self, ctx, sel):
        res = self.v("res")
        x, y, z = ctx.x[sel], ctx.y[sel], ctx.z[sel]
        g, ix, iy, x0, y0 = _min_grid(x, y, z, res)
        # inverted: cloth falls onto -z; work with h = -min z per cell
        surf = -g
        filled = sp.fill_nan_nearest(surf)
        valid = ~np.isnan(surf)
        top = np.nanmax(filled) + 1.0
        cloth = np.full_like(filled, top)
        prev = cloth.copy()
        movable = np.ones_like(filled, bool)
        rig = int(self.v("rigid")[0])
        grav = 0.2 * 0.65 ** 2               # CSF: gravity 0.2, time step 0.65
        for it in range(int(self.v("iters"))):
            disp = (cloth - prev) * 0.99 - grav
            prev = cloth.copy()
            cloth = np.where(movable, cloth + disp, cloth)
            # collision
            hit = cloth <= filled
            cloth = np.where(hit, filled, cloth)
            movable &= ~hit
            # internal springs (rigidness = number of relaxations, as CSF)
            for _ in range(rig):
                avg = cloth.copy()
                avg[1:-1, 1:-1] = 0.25 * (cloth[:-2, 1:-1] + cloth[2:, 1:-1] + cloth[1:-1, :-2] + cloth[1:-1, 2:])
                cloth = np.where(movable, cloth + 0.5 * (avg - cloth), cloth)
            if it > 20 and np.abs(cloth - prev).max() < 0.005:
                break
        if self.v("smooth"):
            from scipy import ndimage
            # slope post-processing: movable cells next to fixed cells snap to the surface when close
            sm = ndimage.uniform_filter(np.where(valid, filled, cloth), 3)
            near = movable & (np.abs(cloth - filled) < 0.3)
            cloth = np.where(near, np.minimum(cloth, sm), cloth)
        ground_surface = -cloth                       # back to upright heights
        gx = (x - x0) / res - 0.5
        gy = (y - y0) / res - 0.5
        zs = _bilinear(ground_surface, gx, gy)
        is_ground = np.abs(z - zs) <= self.v("hcc")
        return _ground_result(ctx, self, sel, is_ground)


def _bilinear(G, gx, gy):
    ny, nx = G.shape
    gx = np.clip(gx, 0, nx - 1.0001)
    gy = np.clip(gy, 0, ny - 1.0001)
    i0 = np.floor(gx).astype(int)
    j0 = np.floor(gy).astype(int)
    i1 = np.minimum(i0 + 1, nx - 1)
    j1 = np.minimum(j0 + 1, ny - 1)
    fx = gx - i0
    fy = gy - j0
    return (G[j0, i0] * (1 - fx) * (1 - fy) + G[j0, i1] * fx * (1 - fy) + G[j1, i0] * (1 - fx) * fy +
            G[j1, i1] * fx * fy)


class SMRF(Filter):
    key, name, group, mod, speed = "smrf", "Simple morphological filter (SMRF)", "Ground", "C", "+"
    ref = "Pingel, Clarke & McBride 2013, ISPRS J. 77:21-30, doi:10.1016/j.isprsjprs.2012.12.002"
    help = ("Progressive morphological opening of a minimum surface with a growing disk; then an "
            "interpolated DEM and an elevation threshold that grows with the local slope. Few "
            "parameters, robust in towns.")
    params = [P("cell", "Cell size", "float", 0.5, 0.05, 10, 0.05, " m"),
              P("slope", "Slope (rise/run)", "float", 0.15, 0.01, 2, 0.01),
              P("window", "Max. window", "float", 18.0, 1, 100, 1, " m"),
              P("thr", "Elevation threshold", "float", 0.30, 0.01, 5, 0.01, " m"),
              P("scalar", "Elevation scalar", "float", 1.25, 0, 10, 0.05)] + GROUND_OUT

    def run(self, ctx, sel):
        c = self.v("cell")
        x, y, z = ctx.x[sel], ctx.y[sel], ctx.z[sel]
        g, ix, iy, x0, y0 = _min_grid(x, y, z, c)
        zmin = sp.fill_nan_nearest(g)
        obj = np.zeros_like(zmin, bool)
        lastsurf = zmin.copy()
        wmax = int(math.ceil(self.v("window") / c))
        for r in range(1, wmax + 1):
            thr = self.v("slope") * r * c
            opened = sp.disk_opening(zmin, r)        # = grey_opening(footprint=disk(r)), 25x faster
            obj |= (lastsurf - opened) > thr
            lastsurf = opened
        ground_cells = np.where(obj | np.isnan(g), np.nan, zmin)
        dem = _interp_grid(ground_cells)
        gy_, gx_ = np.gradient(dem, c)
        slope = np.hypot(gx_, gy_)
        gx = (x - x0) / c - 0.5
        gy = (y - y0) / c - 0.5
        zs = _bilinear(dem, gx, gy)
        sl = _bilinear(slope, gx, gy)
        is_ground = (z - zs) <= self.v("thr") + self.v("scalar") * sl
        is_ground &= (zs - z) <= self.v("thr") + self.v("scalar") * sl
        return _ground_result(ctx, self, sel, is_ground)


def _interp_grid(G):
    """Fill NaN cells of a DEM: linear inside the convex hull, nearest outside."""
    m = ~np.isnan(G)
    if m.sum() < 4:
        return sp.fill_nan_nearest(G)
    try:
        from scipy.interpolate import griddata
        yy, xx = np.nonzero(m)
        qy, qx = np.nonzero(~m)
        if len(qy):
            vals = griddata(np.column_stack([xx, yy]), G[m], np.column_stack([qx, qy]), method="linear")
            H = G.copy()
            H[qy, qx] = vals
            return sp.fill_nan_nearest(H)
        return G
    except Exception:
        return sp.fill_nan_nearest(G)


class PMF(Filter):
    key, name, group, mod, speed = "pmf", "Progressive morphological filter (PMF)", "Ground", "C", "+"
    ref = "Zhang K. et al. 2003, IEEE TGRS 41(4):872-882, doi:10.1109/TGRS.2003.810682"
    help = ("Openings of a minimum-elevation grid with a growing square window; cells rising more than "
            "dh_k = s*(w_k - w_k-1)*c + dh0 (capped at dh_max) above the opened surface are non-ground.")
    params = [P("cell", "Cell size", "float", 0.5, 0.05, 10, 0.05, " m"),
              P("slope", "Slope s", "float", 1.0, 0.01, 10, 0.05),
              P("wmax", "Max. window", "float", 20.0, 1, 200, 1, " m"),
              P("dh0", "Initial height threshold", "float", 0.15, 0.01, 5, 0.01, " m"),
              P("dhmax", "Max. height threshold", "float", 2.5, 0.1, 50, 0.1, " m"),
              P("growth", "Window growth", "choice", "Exponential", choices=["Exponential", "Linear"]),
              P("thr", "Final ground tolerance", "float", 0.25, 0.01, 5, 0.01, " m")] + GROUND_OUT

    def run(self, ctx, sel):
        from scipy import ndimage
        c = self.v("cell")
        x, y, z = ctx.x[sel], ctx.y[sel], ctx.z[sel]
        g, ix, iy, x0, y0 = _min_grid(x, y, z, c)
        surf = sp.fill_nan_nearest(g)
        obj = np.zeros_like(surf, bool)
        wmax = self.v("wmax") / c
        ws = []
        k = 0
        while True:
            w = (2 * (2 ** k) + 1) if self.v("growth") == "Exponential" else (2 * (k + 1) + 1)
            if w > wmax:
                break
            ws.append(w)
            k += 1
        prev_w = 1
        for w in ws:
            opened = ndimage.grey_opening(surf, size=(w, w))
            dh = min(self.v("slope") * (w - prev_w) * c + self.v("dh0"), self.v("dhmax"))
            obj |= (surf - opened) > dh
            surf = opened
            prev_w = w
        ground_cells = np.where(obj | np.isnan(g), np.nan, g)
        dem = _interp_grid(ground_cells)
        zs = _bilinear(dem, (x - x0) / c - 0.5, (y - y0) / c - 0.5)
        is_ground = np.abs(z - zs) <= self.v("thr")
        return _ground_result(ctx, self, sel, is_ground)


class HeightAboveGround(Filter):
    key, name, group, mod = "hag", "Height above ground", "Ground", "C/R"
    ref = "ASPRS LAS 1.4 classes; DEM from the ground class"
    help = ("Builds a DEM from the ground class (2) and acts on each point's height above it: reject "
            "low points below the ground (multipath, 'low noise'), reject high noise, or classify "
            "vegetation low / medium / high (3 / 4 / 5) among the unclassified points.")
    params = [P("cell", "DEM cell", "float", 0.5, 0.05, 10, 0.05, " m"),
              P("mode", "Mode", "choice", "Reject below ground",
                choices=["Reject below ground", "Reject above", "Classify vegetation", "Classify band"]),
              P("below", "Below ground by more than", "float", 0.25, 0.01, 50, 0.01, " m"),
              P("above", "Above ground by more than", "float", 40.0, 0.5, 500, 0.5, " m"),
              P("low", "Low vegetation up to", "float", 0.5, 0.05, 10, 0.05, " m"),
              P("med", "Medium vegetation up to", "float", 2.0, 0.1, 50, 0.1, " m"),
              P("b0", "Band from", "float", 0.0, -50, 500, 0.1, " m"),
              P("b1", "Band to", "float", 2.0, -50, 500, 0.1, " m"),
              P("bcls", "Band class", "int", 3, 0, 255)]

    def run(self, ctx, sel):
        gm = ctx.accepted & (ctx.cls == 2)
        if gm.sum() < 10:
            ctx.messages.append("Height above ground: no ground class yet - run a ground filter first")
            return _res()
        c = self.v("cell")
        gi = np.flatnonzero(gm)
        ix, iy, x0, y0 = sp.grid_index(ctx.x[gi], ctx.y[gi], c)
        nx, ny = ix.max() + 1, iy.max() + 1
        S = np.zeros(ny * nx)
        C = np.zeros(ny * nx)
        np.add.at(S, iy * nx + ix, ctx.z[gi])
        np.add.at(C, iy * nx + ix, 1)
        with np.errstate(invalid="ignore", divide="ignore"):
            G = (S / C).reshape(ny, nx)
        dem = _interp_grid(G)
        h = ctx.z[sel] - _bilinear(dem, (ctx.x[sel] - x0) / c - 0.5, (ctx.y[sel] - y0) / c - 0.5)
        m = self.v("mode")
        if m == "Reject below ground":
            return _res(reject=sel[h < -self.v("below")])
        if m == "Reject above":
            return _res(reject=sel[h > self.v("above")])
        if m == "Classify band":
            b = (h >= self.v("b0")) & (h <= self.v("b1"))
            return _res(cls=(sel[b], int(self.v("bcls"))))
        un = np.isin(ctx.cls[sel], (0, 1, 3, 4, 5))
        cl = np.full(len(sel), -1)
        cl[(h > 0.05) & (h <= self.v("low"))] = 3
        cl[(h > self.v("low")) & (h <= self.v("med"))] = 4
        cl[h > self.v("med")] = 5
        ok = un & (cl > 0)
        return _res(cls=(sel[ok], cl[ok]))


# =============================================================================
# 7. Multi-pass
# =============================================================================
class DynamicObjects(Filter):
    key, name, group, speed = "dynamic", "Moving objects / ghosts (multi-pass)", "Multi-pass", "-"
    ref = ("Schauer & Nuechter 2018, 'The Peopleremover', IEEE RA-L, doi:10.1109/LRA.2018.2801797; "
           "cf. Kim & Kim 2020 (Removert), IROS, doi:10.1109/IROS45743.2020.9340856")
    help = ("Traverses a voxel grid along the laser rays of each pass. A point is a moving object (car, "
            "pedestrian, cyclist) when its voxel was seen as free space by another pass, which did not "
            "hit anything there. Needs two or more lines over the same place.")
    params = [P("voxel", "Voxel size", "float", 0.25, 0.05, 2, 0.05, " m"),
              P("min_free", "Min. free-space rays", "int", 2, 1, 100),
              P("ray_frac", "Ray sampling (1 in N points)", "int", 3, 1, 50),
              P("max_range", "Max. ray length", "float", 40.0, 2, 200, 1, " m"),
              P("margin", "Stop before the return", "float", 0.6, 0.1, 5, 0.05, " m"),
              P("protect", "Protect voxels next to a pass's own returns", "int", 1, 0, 1)] + ACTION("Reject", 64)
    needs_tracks = True

    def run(self, ctx, sel):
        lines = np.unique(ctx.ps.line[ctx.accepted])
        if len(lines) < 2:
            ctx.messages.append("Moving objects: needs two or more lines in the area")
            return _res()
        if not ctx.tracks:
            ctx.messages.append("Moving objects: no sensor tracks")
            return _res()
        vs = self.v("voxel")
        acc = np.flatnonzero(ctx.accepted)
        pts = ctx.xyz(acc)
        lo = pts.min(0) - vs
        hi = pts.max(0) + vs
        dims = np.ceil((hi - lo) / vs).astype(np.int64) + 1

        def vkey(p):
            v = np.floor((p - lo) / vs).astype(np.int64)
            ok = np.all((v >= 0) & (v < dims), axis=1)
            return v[:, 0] * dims[1] * dims[2] + v[:, 1] * dims[2] + v[:, 2], ok

        lid_bits = 8
        occ_k, ok = vkey(pts)
        occ_l = ctx.ps.line[acc][ok].astype(np.int64)
        occ_v = np.unique((occ_k[ok] << lid_bits) | occ_l)
        if self.v("protect") > 0:
            # a pass protects the voxels around its own returns (semi-transparent canopy, grazing rays)
            offs = [(dx * dims[1] + dy) * dims[2] + dz for dx in (-1, 0, 1) for dy in (-1, 0, 1) for dz in (-1, 0, 1)]
            base = occ_v >> lid_bits
            ll = occ_v & ((1 << lid_bits) - 1)
            occ = np.unique(np.concatenate([((base + o) << lid_bits) | ll for o in offs]))
        else:
            occ = occ_v
        # rays
        rays = acc[:: int(self.v("ray_frac"))]
        E, N, Z, _H = ctx.sensor(rays)
        good = np.isfinite(E)
        rays, E, N, Z = rays[good], E[good], N[good], Z[good]
        o = np.column_stack([E, N, Z])
        p = ctx.xyz(rays)
        d = p - o
        L = np.linalg.norm(d, axis=1)
        u = d / np.maximum(L, 1e-6)[:, None]
        Lmax = np.minimum(L - self.v("margin"), self.v("max_range"))
        step = vs * 0.5
        keys_parts, cnt_parts = [], []
        chunk = max(1, int(3_000_000 / max(1.0, Lmax.max() / step)))
        rl = ctx.ps.line[rays].astype(np.int64)
        if engine.available() and len(rays):
            # the C engine marches the rays on every core and counts in a hash table (same keys and counts)
            k_, c_ = engine.ray_count(o, u, Lmax, rl, step, lo, vs, dims, lid_bits)
            keys_parts, cnt_parts = [k_], [c_]
            rays_py = range(0)
        else:
            rays_py = range(0, len(rays), chunk)
        for s in rays_py:
            e = slice(s, s + chunk)
            ns = np.ceil(np.maximum(Lmax[e], 0) / step).astype(np.int64)
            tot = int(ns.sum())
            if tot == 0:
                continue
            owner = np.repeat(np.arange(len(ns)), ns)
            k = np.arange(tot) - np.repeat(np.cumsum(ns) - ns, ns)
            dist = Lmax[e][owner] - k * step
            q = o[e][owner] + u[e][owner] * dist[:, None]
            kk, okk = vkey(q)
            fk = (kk[okk] << lid_bits) | rl[e][owner][okk]
            ray = owner[okk]
            # one count per (voxel, line, ray): a straight ray visits each voxel in one run of consecutive
            # samples, so dropping repeats of the previous sample of the same ray gives the same set as a sort
            # (np.unique) of every sample, in one pass
            if len(fk):
                keep = np.ones(len(fk), bool)
                keep[1:] = (fk[1:] != fk[:-1]) | (ray[1:] != ray[:-1])
                fk = fk[keep]
            uk, uc = np.unique(fk, return_counts=True)
            keys_parts.append(uk)
            cnt_parts.append(uc)
            if ctx.cancel():
                return _res()
        if not keys_parts:
            return _res()
        allk = np.concatenate(keys_parts)
        allc = np.concatenate(cnt_parts)
        free, inv = np.unique(allk, return_inverse=True)
        fcnt = np.bincount(inv.ravel(), allc)
        valid = (fcnt >= self.v("min_free")) & ~np.isin(free, occ)
        free = free[valid]
        fv = free >> lid_bits
        fl = free & ((1 << lid_bits) - 1)
        order = np.argsort(fv, kind="stable")
        fv_s, fl_s = fv[order], fl[order]
        sk, sok = vkey(ctx.xyz(sel))
        sl = ctx.ps.line[sel].astype(np.int64)
        start = np.searchsorted(fv_s, sk)
        end = np.searchsorted(fv_s, sk, side="right")
        cnt = np.where(sok, end - start, 0)
        dyn = cnt >= 2
        one = np.flatnonzero(cnt == 1)
        dyn[one] = fl_s[start[one]] != sl[one]
        ctx.messages.append("Moving objects: %d points in voxels seen through by another pass" % dyn.sum())
        return _action(ctx, sel[dyn], self.v("action"), self.v("cls"))


# =============================================================================
REGISTRY = {}
for _c in (ResetModifications, ClipHeight, ClipIntensity, ClipRange, ClipChannelReturn, ClipTime, VehicleBox,
           ClipReference, Confidence95, LowHitCount, ClipMeanHeight, LonelyCells, VoxelReducer, ScanReducer,
           AreaOutlier, RadiusOutlier, Weather, IsolatedCluster, SlopeSpike, SurfaceFit, VerticalPlane,
           GeometricFeatures, ClothFilter, SMRF, PMF, HeightAboveGround, DynamicObjects, EditClassification,
           ShiftHeight):
    REGISTRY[_c.key] = _c
from .features import FEATURE_FILTERS  # noqa: E402  (features.py uses the classes above)
for _c in FEATURE_FILTERS:
    REGISTRY[_c.key] = _c
GROUPS_ORDER = ["Edit", "Clip", "Cell statistics", "Reduce", "Outliers", "Structure", "Ground", "Multi-pass",
                "Height", "Features"]


def make(key, **values):
    return REGISTRY[key](**values)


def _class_mask(ctx, flt):
    cl = sp.parse_set(flt.values.get("classes", ""))
    if not cl:
        return np.ones(len(ctx.x), bool)
    return np.isin(ctx.cls, list(cl))


def _condition(ctx, flt, include_rejected=False):
    m = _class_mask(ctx, flt)
    ln = sp.parse_set(flt.values.get("lines", ""), offset=1)
    if ln:
        m &= np.isin(ctx.ps.line, list(ln))
    if not include_rejected:
        m &= ctx.accepted
    return m


# =============================================================================
class FilterGroup:
    def __init__(self, name="New group", filters=None, mode="Combined"):
        self.name = name
        self.filters = filters or []
        self.mode = mode           # Combined | Individually (per line)
        self.user = ""

    def to_dict(self):
        return {"name": self.name, "mode": self.mode, "user": self.user,
                "filters": [f.to_dict() for f in self.filters]}

    @classmethod
    def from_dict(cls, d):
        g = cls(d.get("name", "Group"), mode=d.get("mode", "Combined"))
        g.user = d.get("user", "")
        for fd in d.get("filters", []):
            if fd.get("key") in REGISTRY:
                f = make(fd["key"], **fd.get("values", {}))
                f.enabled = fd.get("enabled", True)
                g.filters.append(f)
        return g

    def run(self, ctx, progress=None):
        """Run every enabled filter in order on ctx (modified in place).
        Returns per-point change masks."""
        n = len(ctx.x)
        ctx.group_rejected = np.zeros(n, bool)
        ctx.group_modified = np.zeros(n, bool)
        start_flag = ctx.flag.copy()
        timings = []
        import time
        active = [f for f in self.filters if f.enabled]
        for k, f in enumerate(active):
            if ctx.cancel():
                break
            t0 = time.time()
            if self.mode == "Individually" and not isinstance(f, (ResetModifications, EditClassification)):
                for lid in np.unique(ctx.ps.line):
                    sub_scope = ctx.scope & (ctx.ps.line == lid)
                    self._apply_one(ctx, f, sub_scope)
            else:
                self._apply_one(ctx, f, ctx.scope)
            timings.append((f.name, time.time() - t0))
            if progress:
                progress((k + 1) / max(1, len(active)), f.name)
        ctx.group_rejected = ((ctx.flag & REJECTED) != 0) & ((start_flag & REJECTED) == 0)
        ctx.timings = timings
        return ctx

    @staticmethod
    def _apply_one(ctx, f, scope):
        old_scope = ctx.scope
        ctx.scope = scope
        try:
            sel = np.flatnonzero(scope & _condition(ctx, f))
            if len(sel) == 0 and not isinstance(f, (ResetModifications, EditClassification)):
                return
            r = f.run(ctx, sel)
        finally:
            ctx.scope = old_scope
        apply_result(ctx, r)


def apply_result(ctx, r):
    if r.get("accept") is not None and len(r["accept"]):
        a = np.asarray(r["accept"])
        ctx.flag[a] &= ~np.uint8(REJECTED)
        ctx.group_modified[a] = True
    if r.get("reject") is not None and len(r["reject"]):
        j = np.asarray(r["reject"])
        j = j[(ctx.flag[j] & REJECTED) == 0]
        ctx.flag[j] |= np.uint8(REJECTED | REJ_AUTO)
        ctx.group_modified[j] = True
    if r.get("cls") is not None:
        idx, val = r["cls"]
        if len(idx):
            ctx.cls[np.asarray(idx)] = val
            ctx.group_modified[np.asarray(idx)] = True
    if r.get("dz") is not None:
        idx, val = r["dz"]
        if len(idx):
            ctx.dz[np.asarray(idx)] += val
            ctx.z[np.asarray(idx)] += val
            ctx.group_modified[np.asarray(idx)] = True


# =============================================================================
# default groups
# =============================================================================
def default_groups():
    G = []
    g = FilterGroup("Mobile mapping - standard clean")
    g.filters = [make("reset"), make("vehicle_box"), make("isolated", interval=0.5, min_pts=8),
                 make("sor", k=12, alpha=3.0), make("weather", mode="DIOR (intensity + DROR)"),
                 make("smrf"), make("hag", mode="Reject below ground", below=0.25)]
    G.append(g)
    g = FilterGroup("Snow and rain")
    g.filters = [make("reset"), make("weather", mode="DIOR (intensity + DROR)", ithr=20),
                 make("isolated", interval=0.4, min_pts=5), make("low_hit", cell=0.4, nmin=3)]
    G.append(g)
    g = FilterGroup("Ground and vegetation classes")
    g.filters = [make("csf", res=0.5, hcc=0.2), make("hag", mode="Classify vegetation"),
                 make("vplane", inside="Classify", behind="Nothing")]
    G.append(g)
    g = FilterGroup("Moving objects (multi-pass)")
    g.filters = [make("dynamic"), make("isolated", min_pts=20)]
    G.append(g)
    g = FilterGroup("Feature extraction (road and street furniture)")
    g.filters = [make("smrf"), make("f_markings"), make("f_manholes"), make("f_curbs"), make("f_signs"),
                 make("f_poles"), make("f_wires"), make("f_guardrails")]
    G.append(g)
    g = FilterGroup("Road surface QA")
    g.filters = [make("surface", classes="2", gate=0.05), make("conf95", classes="2", limit=0.04)]
    G.append(g)
    return G
