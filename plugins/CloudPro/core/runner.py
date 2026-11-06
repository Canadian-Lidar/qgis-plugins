"""Run a filter group over a scope larger than the Inspection Area (entire
project, selected lines, a polygon), tile by tile with a buffer so the
neighbourhood filters see across tile edges; only the tile's own points are
edited."""
import numpy as np

from .filters import Ctx
from .pointset import PointSet
from .store import parse_key


def run_group_tiled(project, group, edits, buffer=10.0, lines=None, polygon=None, progress=None,
                    cancel=None, undoable_limit=20_000_000):
    """Every tile is computed on the points as they were before the run (its buffer included), and the
    changes are written after the last tile, so the result does not depend on the order of the tiles.
    The grids of the changed tiles are rebuilt here (in the worker), not by the window."""
    store = project.store
    tracks = project.tracks()
    keys = store.keys()
    if polygon is not None:
        px, py = polygon[:, 0], polygon[:, 1]
        keys = [k for k in keys if _tile_hits_bbox(store, k, px.min(), py.min(), px.max(), py.max())]
    total = 0
    acts = []
    T = store.tile
    for n, k in enumerate(keys):
        if cancel and cancel():
            break
        i, j = parse_key(k)
        cx, cy = (i + 0.5) * T, (j + 0.5) * T
        box = dict(cx=cx, cy=cy, length=T + 2 * buffer, width=T + 2 * buffer, bearing=0.0)
        ps = PointSet.from_store(store, box)
        if len(ps) == 0:
            continue
        core = (ps.x >= i * T) & (ps.x < (i + 1) * T) & (ps.y >= j * T) & (ps.y < (j + 1) * T)
        if lines:
            core &= np.isin(ps.line, list(lines))
        if polygon is not None:
            core &= points_in_polygon(ps.x, ps.y, polygon)
        if not core.any():
            continue
        ctx = Ctx(ps, tracks=tracks, scope_mask=core, cancel=cancel, origin=store.origin)
        group.run(ctx)
        act = edits.apply_ctx(ctx, "Filter '%s'" % group.name, ps=ps, scope=core, write=False)
        if act is not None:
            total += act.size
            acts.append(act)
        if progress:
            progress(0.9 * (n + 1) / max(1, len(keys)), "%d points changed" % total)
    # write (the cores of the tiles never overlap)
    touched = set()
    for a in acts:
        store.write_codes(a.codes, flag=a.new["flag"], cls=a.new["cls"], dz=a.new["dz"], z=a.new["z"])
    store.sync()
    touched |= set(getattr(store, "dirty_tiles", set()))
    for m, k in enumerate(sorted(touched)):
        store.build_grid(k)
        if progress and m % 16 == 0:
            progress(0.9 + 0.1 * (m + 1) / max(1, len(touched)), "updating the project grids")
    store.dirty_tiles = set()
    # merge into one undo step when small enough
    if acts:
        from .edits import Action
        if total <= undoable_limit:
            fields_old, fields_new = list(acts[0].old), list(acts[0].new)
            codes = np.concatenate([a.codes for a in acts])
            old = {f: np.concatenate([a.old[f] for a in acts]) for f in fields_old}
            new = {f: np.concatenate([a.new[f] for a in acts]) for f in fields_new}
            acts.clear()
            merged = Action("Filter '%s' (%s)" % (group.name, "selection" if polygon is not None or lines else
                                                   "entire project"), codes, old, new)
            edits.commit(merged, True)
        else:
            acts.clear()
            edits.commit(Action("Filter '%s' (entire project, %d points)" % (group.name, total),
                                np.zeros(0, np.int64), {}, {}), False)
    return total


def _tile_hits_bbox(store, key, x0, y0, x1, y1):
    i, j = parse_key(key)
    T = store.tile
    return not ((i + 1) * T < x0 or i * T > x1 or (j + 1) * T < y0 or j * T > y1)


def points_in_polygon(x, y, poly):
    """Even-odd rule, vectorised over points (poly: (m, 2))."""
    inside = np.zeros(len(x), bool)
    px, py = poly[:, 0], poly[:, 1]
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi, xj, yj = px[i], py[i], px[j], py[j]
        cond = ((yi > y) != (yj > y))
        with np.errstate(divide="ignore", invalid="ignore"):
            xint = (xj - xi) * (y - yi) / (yj - yi) + xi
        inside ^= cond & (x < xint)
        j = i
    return inside
