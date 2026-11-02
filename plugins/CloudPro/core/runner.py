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
        act = edits.apply_ctx(ctx, "Filter '%s'" % group.name, ps=ps, scope=core)
        if act is not None:
            total += act.size
            acts.append(act)
        if progress:
            progress((n + 1) / max(1, len(keys)), "%d points changed" % total)
    store.sync()
    # merge into one undo step when small enough
    if acts:
        if total <= undoable_limit:
            from .edits import Action
            merged = Action("Filter '%s' (%s)" % (group.name, "selection" if polygon is not None or lines else
                                                   "entire project"),
                            np.concatenate([a.codes for a in acts]),
                            {f: np.concatenate([a.old[f] for a in acts]) for f in acts[0].old},
                            {f: np.concatenate([a.new[f] for a in acts]) for f in acts[0].new})
            edits.commit(merged, True)
        else:
            from .edits import Action
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
