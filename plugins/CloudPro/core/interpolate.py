"""Interpolation: fill holes in the grid with virtual points (as BeamworX AutoClean's Interpolation).

The project's accepted points are gridded (mean height per cell). Empty cells are filled by one of the methods
below and each filled cell gets 1, 4, 16 or 64 virtual points spread evenly over it. The virtual points go into
one line of their own ("Interpolated points", kind "interpolated"), so they can be shown, hidden, edited,
exported or deleted like any other line, and the grid keeps updating. They survive Update georeferencing (they
are put back after the new points).

Methods (AutoClean manual, Interpolation):
  hole_idw    Hole - inverse distance: for an empty cell, the nearest filled cell in each of the 8 directions
              (N, NE, E, SE, S, SW, W, NW) within the search range; filled when 6 of the 8 are found; the height
              is their inverse-distance mean. On the whole project only cells surrounded by data are filled.
  hole_tri    Hole - triangulation (straight line): each hole (empty cells joined together, not open to the
              outside) is filled from a triangle mesh of the filled cell centres around it (its bounding box
              blown up 4 cells): heights on straight lines between the surrounding cells.
  area_tri    Area - triangulation (straight line): every empty cell of the chosen area is one hole; triangles
              longer than the search range are not used (that is the largest gap bridged).
  area_extra  Area - extrapolation: as hole_idw but 2 of the 8 directions are enough (edges of the survey).
The area methods need an area (the Inspection Area), as in AutoClean.
"""
import json
import os
from dataclasses import asdict, dataclass

import numpy as np

from . import store as S
from dcs_core.files import replace as _replace   # retried while Windows holds the target open (audit 23 Nov 2026)

METHODS = [("hole_idw", "Hole - inverse distance"), ("hole_tri", "Hole - triangulation (straight line)"),
           ("area_tri", "Area - triangulation (straight line)"), ("area_extra", "Area - extrapolation (inverse distance)")]
AREA_ONLY = ("area_tri", "area_extra")
POINTS_PER_CELL = (1, 4, 16, 64)
CELLS = (0.5, 1.0, 2.0, 2.5, 5.0, 10.0)
LINE_NAME = "Interpolated points"
KIND = "interpolated"
NEED = {"hole_idw": 6, "area_extra": 2}
DIRS = [(0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1), (-1, 0), (-1, 1)]       # (dx, dy) cells
MAX_CELLS = 40_000_000


class InterpolationError(ValueError):
    pass


@dataclass
class Settings:
    method: str = "hole_idw"
    cell: float = 1.0                  # grid cell (m)
    points_per_cell: int = 4           # 1 / 4 / 16 / 64 virtual points per filled cell
    search: int = 10                   # search range (cells); for area_tri the longest triangle edge
    classes: tuple = ()                # heights from these classes only (empty = every accepted point)
    new_class: int = 2                 # class of the virtual points
    append: bool = False               # keep the virtual points made before

    def check(self, area):
        if self.method not in dict(METHODS):
            raise InterpolationError("unknown method %s" % self.method)
        if self.method in AREA_ONLY and area is None:
            raise InterpolationError("%s needs an area: choose the Inspection Area" % dict(METHODS)[self.method])
        if self.points_per_cell not in POINTS_PER_CELL:
            raise InterpolationError("virtual points per cell: 1, 4, 16 or 64")
        if self.cell <= 0 or self.search < 1:
            raise InterpolationError("cell size and search range must be positive")


# ----------------------------------------------------------------------------- the interpolated line
def line_id(project):
    """The id of the interpolated points' line, or None."""
    for ln in project.lines:
        if ln.get("kind") == KIND:
            return ln["id"]
    return None


def count(project):
    """Virtual points in the project."""
    lid = line_id(project)
    if lid is None:
        return 0
    n = 0
    for k in project.store.keys():
        a = project.store.tile_array(k)
        n += int((a["line"] == lid).sum())
    return n


def _remove(project, lid):
    """Take every point of line lid out of the store (tiles rewritten); -> keys changed. Windows refuses to replace
    a file while a memory map of it is open, so every map of the tile is dropped first and the replace is retried
    for a few seconds (another reader finishing)."""
    import gc
    from dcs_core import files as dfiles
    st = project.store
    st.flush()
    changed = []
    for k in st.keys():
        a = st.tile_array(k)
        m = a["line"] == lid
        if not m.any():
            del a, m
            continue
        keep = np.array(a[~m])
        del a, m                                # no reference to the map may stay
        with st.lock:
            st._close_mm(k)
        gc.collect()
        p = st.tile_path(k)
        keep.tofile(p + ".tmp")
        try:
            dfiles.replace(p + ".tmp", p)
        except PermissionError:                 # still held by another reader after the retries
            os.remove(p + ".tmp")
            raise InterpolationError("the tile %s is in use by another task; try again in a moment" % k)
        st.counts[k] = len(keep)
        changed.append(k)
    st.save_meta()
    return changed


def delete(project):
    """Delete the virtual points (Delete interpolation); the line stays, empty, for the next interpolation.
    -> (points removed, tiles changed)."""
    lid = line_id(project)
    if lid is None:
        return 0, []
    n = count(project)
    keys = _remove(project, lid)
    project.lines[lid]["points"] = 0
    project.lines[lid]["interpolation"] = None
    _save_file(project, None)
    return n, keys


# ----------------------------------------------------------------------------- the grid
def _cell_grid(project, s, area):
    """Mean height per cell of the accepted points (classes per s.classes; the virtual points are left out unless
    appending) over the area (box dict) or the whole project -> (x0, y0, nx, ny, Z with nan, inside mask)."""
    st = project.store
    c = float(s.cell)
    if area is not None:
        from .pointset import rect_corners
        cor = rect_corners(area["cx"], area["cy"], area["length"], area["width"], area["bearing"])
        x0, y0 = np.floor(cor.min(0) / c) * c
        x1, y1 = np.ceil(cor.max(0) / c) * c
    else:
        ext = st.extent()
        if ext is None:
            raise InterpolationError("the project has no points")
        x0, y0 = np.floor(np.array(ext[:2]) / c) * c
        x1, y1 = np.ceil(np.array(ext[2:]) / c) * c
    pad = (s.search + 4) * c            # neighbours outside the area count for its edge cells
    x0, y0, x1, y1 = x0 - pad, y0 - pad, x1 + pad, y1 + pad
    nx, ny = int(round((x1 - x0) / c)), int(round((y1 - y0) / c))
    if nx * ny > MAX_CELLS:
        raise InterpolationError("the grid would have %s cells: use a larger cell or the Inspection Area"
                                 % "{:,}".format(nx * ny))
    zs = np.zeros(nx * ny)
    cnt = np.zeros(nx * ny)
    skip = None if s.append else line_id(project)
    cls = np.array(sorted(set(s.classes)), dtype=np.uint8) if s.classes else None
    for k in st.tiles_in_bbox(x0, y0, x1, y1):
        a = st.tile_array(k)
        if len(a) == 0:
            continue
        m = (a["flag"] & S.REJECTED) == 0
        if skip is not None:
            m &= a["line"] != skip
        if cls is not None:
            m &= np.isin(a["cls"], cls)
        if not m.any():
            continue
        x = a["x"][m] * 1e-3
        y = a["y"][m] * 1e-3
        ix = np.floor((x - x0) / c).astype(np.int64)
        iy = np.floor((y - y0) / c).astype(np.int64)
        ok = (ix >= 0) & (ix < nx) & (iy >= 0) & (iy < ny)
        cell = iy[ok] * nx + ix[ok]
        zs += np.bincount(cell, a["z"][m][ok] * 1e-3, minlength=nx * ny)
        cnt += np.bincount(cell, minlength=nx * ny)
    with np.errstate(invalid="ignore", divide="ignore"):
        Z = (zs / cnt).reshape(ny, nx)
    Z[cnt.reshape(ny, nx) == 0] = np.nan
    gx = x0 + (np.arange(nx) + 0.5) * c
    gy = y0 + (np.arange(ny) + 0.5) * c
    if area is not None:
        from .pointset import to_box_frame
        X, Y = np.meshgrid(gx, gy)
        u, v = to_box_frame(X, Y, area["cx"], area["cy"], area["bearing"])
        inside = (np.abs(u) <= area["length"] / 2) & (np.abs(v) <= area["width"] / 2)
    else:
        inside = np.ones((ny, nx), bool)
        inside[:s.search + 4, :] = inside[-(s.search + 4):, :] = False
        inside[:, :s.search + 4] = inside[:, -(s.search + 4):] = False
    return x0, y0, nx, ny, Z, inside


# ----------------------------------------------------------------------------- the methods
def _idw8(Z, todo, R, need):
    """Inverse distance from the nearest filled cell in each of 8 directions within R cells -> heights (nan where
    fewer than `need` directions found)."""
    ny, nx = Z.shape
    filled = np.isfinite(Z)
    found = np.zeros(Z.shape, np.int8)
    num = np.zeros(Z.shape)
    den = np.zeros(Z.shape)
    for dx, dy in DIRS:
        got = np.zeros(Z.shape, bool)
        for k in range(1, R + 1):
            sx, sy = dx * k, dy * k
            # cell (i, j) looks at (i + sy, j + sx)
            ys = slice(max(0, -sy), ny - max(0, sy))
            xs = slice(max(0, -sx), nx - max(0, sx))
            yd = slice(max(0, sy), ny - max(0, -sy))
            xd = slice(max(0, sx), nx - max(0, -sx))
            hit = ~got[ys, xs] & filled[yd, xd] & todo[ys, xs]
            if not hit.any():
                continue
            d = k * (1.4142135623730951 if dx and dy else 1.0)
            num[ys, xs] += np.where(hit, Z[yd, xd] / d, 0.0)
            den[ys, xs] += np.where(hit, 1.0 / d, 0.0)
            found[ys, xs] += hit
            got[ys, xs] |= hit
    with np.errstate(invalid="ignore", divide="ignore"):
        out = num / den
    out[(found < need) | ~todo] = np.nan
    return out


def _tri_fill(gx, gy, Z, src, dst, max_edge=None):
    """Heights at the dst cells from a triangle mesh of the filled src cells (straight lines); triangles with an
    edge longer than max_edge (m) are not used."""
    from scipy.spatial import Delaunay
    out = np.full(len(dst[0]), np.nan)
    if len(src[0]) < 3 or not len(dst[0]):
        return out
    P = np.column_stack([gx[src[1]], gy[src[0]]])
    zv = Z[src]
    try:
        tri = Delaunay(P)
    except Exception:       # noqa: BLE001 - all in a line: nothing to triangulate
        return out
    Q = np.column_stack([gx[dst[1]], gy[dst[0]]])
    simp = tri.find_simplex(Q)
    ok = simp >= 0
    if max_edge is not None and ok.any():
        V = P[tri.simplices]                                   # (m, 3, 2)
        e = np.max(np.linalg.norm(V - np.roll(V, 1, axis=1), axis=2), axis=1)
        ok &= e[np.maximum(simp, 0)] <= max_edge
    if not ok.any():
        return out
    T = tri.transform[simp[ok]]
    b = np.einsum("ijk,ik->ij", T[:, :2], Q[ok] - T[:, 2])
    bc = np.column_stack([b, 1 - b.sum(1)])
    out[ok] = (zv[tri.simplices[simp[ok]]] * bc).sum(1)
    return out


def fill(Z, inside, gx, gy, s):
    """The empty cells of the area filled by the method -> new heights (nan where not filled)."""
    todo = ~np.isfinite(Z) & inside
    R = int(s.search)
    if s.method in NEED:
        return _idw8(Z, todo, R, NEED[s.method])
    out = np.full(Z.shape, np.nan)
    if s.method == "area_tri":
        src = np.nonzero(np.isfinite(Z))
        dst = np.nonzero(todo)
        out[dst] = _tri_fill(gx, gy, Z, src, dst, max_edge=R * float(gx[1] - gx[0]) * 1.0001 if len(gx) > 1 else None)
        return out
    # hole_tri: holes = 8-connected empty cells not open to the outside of the area
    from scipy import ndimage
    empty = ~np.isfinite(Z)
    lab, n = ndimage.label(empty & inside, structure=np.ones((3, 3), int))
    if not n:
        return out
    # open to the outside: touches an empty cell outside the area or the grid edge
    outside_empty = empty & ~inside
    near_out = ndimage.binary_dilation(outside_empty, structure=np.ones((3, 3), bool))
    edge = np.zeros(Z.shape, bool)
    edge[0, :] = edge[-1, :] = edge[:, 0] = edge[:, -1] = True
    open_lab = np.unique(lab[(near_out | edge) & (lab > 0)])
    sl = ndimage.find_objects(lab)
    ny, nx = Z.shape
    for h in range(1, n + 1):
        if h in open_lab or sl[h - 1] is None:
            continue
        ys, xs = sl[h - 1]
        y0, y1 = max(0, ys.start - 4), min(ny, ys.stop + 4)
        x0, x1 = max(0, xs.start - 4), min(nx, xs.stop + 4)
        sub = Z[y0:y1, x0:x1]
        src = np.nonzero(np.isfinite(sub))
        dst = np.nonzero(lab[y0:y1, x0:x1] == h)
        v = _tri_fill(gx[x0:x1], gy[y0:y1], sub, src, dst)
        out[y0:y1, x0:x1][dst] = v
    return out


def virtual_points(x0, y0, c, Zall, new, ppc):
    """ppc points per filled cell spread evenly over it, their heights on the completed grid (bilinear between
    cell centres where the neighbours have heights, else the cell's) -> (x, y, z)."""
    iy, ix = np.nonzero(np.isfinite(new))
    if not len(iy):
        return np.zeros(0), np.zeros(0), np.zeros(0)
    n = int(round(ppc ** 0.5))
    f = (np.arange(n) + 0.5) / n                       # sub-cell centres, 0..1 across the cell
    fx, fy = np.meshgrid(f, f)
    fx, fy = fx.ravel(), fy.ravel()
    X = x0 + (ix[:, None] + fx[None, :]) * c
    Y = y0 + (iy[:, None] + fy[None, :]) * c
    zc = new[iy, ix]
    Zf = np.where(np.isfinite(Zall), Zall, np.nan)
    ny, nx = Zf.shape
    # bilinear on cell centres: position in "centre index" units
    u = (X - x0) / c - 0.5
    v = (Y - y0) / c - 0.5
    i0 = np.clip(np.floor(u).astype(np.int64), 0, nx - 2)
    j0 = np.clip(np.floor(v).astype(np.int64), 0, ny - 2)
    tu, tv = u - i0, v - j0
    z00, z10 = Zf[j0, i0], Zf[j0, i0 + 1]
    z01, z11 = Zf[j0 + 1, i0], Zf[j0 + 1, i0 + 1]
    z = (z00 * (1 - tu) * (1 - tv) + z10 * tu * (1 - tv) + z01 * (1 - tu) * tv + z11 * tu * tv)
    z = np.where(np.isfinite(z), z, zc[:, None])
    return X.ravel(), Y.ravel(), z.ravel()


# ----------------------------------------------------------------------------- run
def run(project, s, area=None, progress=None, cancel=None):
    """Interpolate and add the virtual points -> dict(points, cells, filled_cells, keys, line)."""
    s.check(area)
    prog = progress or (lambda f, m=None: None)
    prog(0.05, "gridding the points")
    if not s.append:
        lid = line_id(project)
        if lid is not None:
            _remove(project, lid)
    x0, y0, nx, ny, Z, inside = _cell_grid(project, s, area)
    if cancel and cancel():
        raise InterpolationCancelled()
    c = float(s.cell)
    gx = x0 + (np.arange(nx) + 0.5) * c
    gy = y0 + (np.arange(ny) + 0.5) * c
    prog(0.3, "filling the empty cells")
    new = fill(Z, inside, gx, gy, s)
    if cancel and cancel():
        raise InterpolationCancelled()
    Zall = np.where(np.isfinite(Z), Z, new)
    prog(0.6, "making the virtual points")
    x, y, z = virtual_points(x0, y0, c, Zall, new, s.points_per_cell)
    lid = line_id(project)
    info = {"name": LINE_NAME, "kind": KIND, "files": [], "interpolation": asdict(s)}
    if lid is None:
        lid = project.add_line(dict(info, colour="#ff9f1c"))
    else:
        project.lines[lid].update(info)
    keys = _add(project, lid, x, y, z, s.new_class)
    total = count(project)
    project.lines[lid]["points"] = total
    _save_file(project, lid)
    filled = int(np.isfinite(new).sum())
    prog(1.0, None)
    return {"points": int(len(x)), "total": total, "filled_cells": filled,
            "empty_cells": int((~np.isfinite(Z) & inside).sum()), "keys": keys, "line": lid}


class InterpolationCancelled(Exception):
    pass


def _add(project, lid, x, y, z, cls, flag=None, cls_arr=None, dz=None):
    """Append virtual points (local metres) to the store -> tile keys written."""
    rec = np.zeros(len(x), S.REC)
    rec["x"] = np.round(np.asarray(x) * 1000).astype(np.int32)
    rec["y"] = np.round(np.asarray(y) * 1000).astype(np.int32)
    rec["z"] = np.round(np.asarray(z) * 1000).astype(np.int32)
    rec["cls"] = cls if cls_arr is None else cls_arr
    rec["line"] = lid
    rec["ret"] = 2
    if flag is not None:
        rec["flag"] = flag
    if dz is not None:
        rec["dz"] = dz
    parts = project.store.bin(rec)
    project.store.append_binned(parts)
    project.store.flush()
    project.store.save_meta()
    return [k for k, _ in parts]


# ----------------------------------------------------------------------------- kept through Update georeferencing
def _file(project):
    return os.path.join(project.work, "interpolated.npz")


def _save_file(project, lid):
    """The virtual points (world coordinates, flags, classes) beside the store, so Update georeferencing can put
    them back (its rebuild makes a new store)."""
    f = _file(project)
    if lid is None:
        if os.path.exists(f):
            os.remove(f)
        return
    snap = snapshot(project)
    if snap is None:
        if os.path.exists(f):
            os.remove(f)
        return
    np.savez(f + ".tmp.npz", **snap)
    _replace(f + ".tmp.npz", f)


def snapshot(project):
    """The current virtual points in world coordinates (dict of arrays + settings) or None."""
    lid = line_id(project)
    if lid is None:
        return None
    o = np.asarray(project.store.origin if project.store.origin is not None else [0.0, 0.0, 0.0], float)
    parts = []
    for k in project.store.keys():
        a = project.store.tile_array(k)
        m = a["line"] == lid
        if m.any():
            parts.append(np.array(a[m]))
    if not parts:
        return None
    a = np.concatenate(parts)
    return {"X": a["x"] * 1e-3 + o[0], "Y": a["y"] * 1e-3 + o[1], "Z": a["z"] * 1e-3 + o[2], "flag": a["flag"],
            "cls": a["cls"], "dz": a["dz"],
            "settings": np.array(json.dumps(project.lines[lid].get("interpolation") or {}))}


def restore(project, snap):
    """Put the virtual points back after a rebuild (new store, new line ids) -> points restored."""
    if snap is None:
        return 0
    o = np.asarray(project.store.origin if project.store.origin is not None else [0.0, 0.0, 0.0], float)
    try:
        settings = json.loads(str(snap["settings"]))
    except (ValueError, KeyError):
        settings = {}
    lid = project.add_line({"name": LINE_NAME, "kind": KIND, "files": [], "interpolation": settings,
                            "colour": "#ff9f1c"})
    _add(project, lid, snap["X"] - o[0], snap["Y"] - o[1], snap["Z"] - o[2], 0, flag=snap["flag"],
         cls_arr=snap["cls"], dz=snap["dz"])
    project.lines[lid]["points"] = int(len(snap["X"]))
    return int(len(snap["X"]))
