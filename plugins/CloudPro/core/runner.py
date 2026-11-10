"""Run a filter group over a scope larger than the Inspection Area (entire
project, selected lines, a polygon), tile by tile with a buffer so the
neighbourhood filters see across tile edges; only the tile's own points are
edited."""
import math
import os
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import numpy as np

from .filters import Ctx
from .pointset import PointSet
from .store import parse_key

# Work units (CloudPro 1.9.9): a tile is split (quad tree) until a unit holds at most UNIT_POINTS of its own
# points; the buffer around a unit shrinks (not below MIN_BUFFER) where the points are so dense that the unit
# with its buffer would hold more than BOX_POINTS. Units run in parallel threads (numpy, scipy and the engine
# release the interpreter lock) while their estimated memory (BYTES_PER_POINT per loaded point, measured
# 180-335 B for the default groups) fits in half the free memory.
UNIT_POINTS = 2_000_000
BOX_POINTS = 6_000_000
MIN_BUFFER = 3.0
BYTES_PER_POINT = 400
MIN_SIDE_TILES = 16                      # smallest unit: tile / 16


class Unit:
    __slots__ = ("x0", "y0", "x1", "y1", "n", "buffer", "est")

    def __init__(self, x0, y0, x1, y1, n, buffer):
        self.x0, self.y0, self.x1, self.y1, self.n = x0, y0, x1, y1, int(n)
        side = max(x1 - x0, y1 - y0)
        dens = n / max(1e-6, (x1 - x0) * (y1 - y0))
        b = buffer
        if dens > 0 and dens * (side + 2 * b) ** 2 > BOX_POINTS:
            b = max(MIN_BUFFER, (math.sqrt(BOX_POINTS / dens) - side) / 2)
        self.buffer = min(buffer, b)
        self.est = int(dens * (x1 - x0 + 2 * self.buffer) * (y1 - y0 + 2 * self.buffer)) if dens > 0 else 0

    def box(self):
        return dict(cx=(self.x0 + self.x1) / 2, cy=(self.y0 + self.y1) / 2, length=self.x1 - self.x0 + 2 * self.buffer,
                    width=self.y1 - self.y0 + 2 * self.buffer, bearing=0.0)


def plan_units(store, keys, buffer, unit_points=None):
    """[Unit] covering the tiles, dense tiles split by their real point distribution (largest first)."""
    unit_points = unit_points or UNIT_POINTS
    T = store.tile
    out = []
    for k in keys:
        n = int(store.counts.get(k, 0))
        if n == 0:
            continue
        i, j = parse_key(k)
        x0, y0 = i * T, j * T
        if n <= unit_points:
            out.append(Unit(x0, y0, x0 + T, y0 + T, n, buffer))
            continue
        g = MIN_SIDE_TILES
        a = store.tile_array(k)
        cx = np.clip(((np.asarray(a["x"]) * 1e-3 - x0) * g / T).astype(np.int64), 0, g - 1)
        cy = np.clip(((np.asarray(a["y"]) * 1e-3 - y0) * g / T).astype(np.int64), 0, g - 1)
        H = np.bincount(cx * g + cy, minlength=g * g).reshape(g, g)
        S = np.zeros((g + 1, g + 1), np.int64)
        S[1:, 1:] = H.cumsum(0).cumsum(1)

        def count(a0, b0, a1, b1):
            return int(S[a1, b1] - S[a0, b1] - S[a1, b0] + S[a0, b0])

        def split(a0, b0, a1, b1):
            c = count(a0, b0, a1, b1)
            if c == 0:
                return
            if c <= unit_points or a1 - a0 <= 1:
                out.append(Unit(x0 + a0 * T / g, y0 + b0 * T / g, x0 + a1 * T / g, y0 + b1 * T / g, c, buffer))
                return
            am, bm = (a0 + a1) // 2, (b0 + b1) // 2
            for q in ((a0, b0, am, bm), (am, b0, a1, bm), (a0, bm, am, b1), (am, bm, a1, b1)):
                split(*q)
        split(0, 0, g, g)
    out.sort(key=lambda u: -u.est)
    return out


def _free_memory():
    try:
        from .perflog import _ram_gb
        _tot, av = _ram_gb()
        if av:
            return av * 2 ** 30
    except Exception:                           # noqa: BLE001
        pass
    return 4 * 2 ** 30


def run_group_tiled(project, group, edits, buffer=10.0, lines=None, polygon=None, progress=None,
                    cancel=None, undoable_limit=20_000_000, workers=None, memory=None, stats=None):
    """Every unit is computed on the points as they were before the run (its buffer included), and the
    changes are written after the last unit, so the result does not depend on the order or the number of threads.
    Cancelling writes nothing (raises importer.Cancelled). The grids of the changed tiles are rebuilt here (in the
    worker), not by the window. stats (dict) receives units, workers, reduced buffers, times."""
    from .importer import Cancelled
    from . import engine
    store = project.store
    tracks = project.tracks()
    keys = store.keys()
    if polygon is not None:
        px, py = polygon[:, 0], polygon[:, 1]
        keys = [k for k in keys if _tile_hits_bbox(store, k, px.min(), py.min(), px.max(), py.max())]
    cancel = cancel or (lambda: False)
    t_start = time.time()
    if progress:
        progress(0.0, "planning the work")
    units = plan_units(store, keys, buffer)
    total_pts = max(1, sum(u.n for u in units))
    budget = memory if memory is not None else 0.5 * _free_memory()
    nw = workers or max(1, engine.threads() if engine.available() else (os.cpu_count() or 2) - 1)
    nw = max(1, min(nw, len(units) or 1))
    lock = threading.Lock()
    state = {"done_pts": 0, "part": {}, "changed": 0, "units": 0, "last": 0.0}

    def report(force=False):
        if not progress:
            return
        now = time.time()
        with lock:
            if not force and now - state["last"] < 0.5:
                return
            state["last"] = now
            f = (state["done_pts"] + sum(state["part"].values())) / total_pts
            running = len(state["part"])
            msg = "%d of %d parts, %s of %s points, %d running, %s changed" % (
                state["units"], len(units), _m(state["done_pts"] + sum(state["part"].values())), _m(total_pts),
                running, "{:,}".format(state["changed"]))
        el = now - t_start
        if f > 0.02 and el > 5:
            msg += ", about %s left" % _dur(el * (1 - f) / f)
        progress(0.9 * min(1.0, f), msg)

    def work(u, uid):
        if cancel():
            return None, 0
        ps = PointSet.from_store(store, u.box())
        if len(ps) == 0:
            return None, 0
        core = (ps.x >= u.x0) & (ps.x < u.x1) & (ps.y >= u.y0) & (ps.y < u.y1)
        if lines:
            core &= np.isin(ps.line, list(lines))
        if polygon is not None:
            core &= points_in_polygon(ps.x, ps.y, polygon)
        if not core.any():
            return None, len(ps)
        ctx = Ctx(ps, tracks=tracks, scope_mask=core, cancel=cancel, origin=store.origin)

        def fprog(fr, _name):
            with lock:
                state["part"][uid] = u.n * fr
            report()
        group.run(ctx, progress=fprog)
        if cancel():
            return None, len(ps)
        act = edits.apply_ctx(ctx, "Filter '%s'" % group.name, ps=ps, scope=core, write=False)
        return act, len(ps)

    acts = []
    loaded = 0
    peak_running = 0
    with ThreadPoolExecutor(nw, thread_name_prefix="CloudPro filter") as ex:
        pending = {}
        inflight = 0
        queue = list(enumerate(units))
        while queue or pending:
            if cancel():
                break
            while queue and len(pending) < nw:
                uid, u = queue[0]
                need = u.est * BYTES_PER_POINT
                if pending and inflight + need > budget:
                    break                                   # wait for memory
                queue.pop(0)
                with lock:
                    state["part"][uid] = 0.0
                fut = ex.submit(work, u, uid)
                pending[fut] = (uid, u, need)
                inflight += need
            peak_running = max(peak_running, len(pending))
            done, _ = wait(list(pending), timeout=1.0, return_when=FIRST_COMPLETED)
            for fut in done:
                uid, u, need = pending.pop(fut)
                inflight -= need
                act, nl = fut.result()                      # a filter error stops the run (nothing written)
                loaded += nl
                with lock:
                    state["part"].pop(uid, None)
                    state["done_pts"] += u.n
                    state["units"] += 1
                    if act is not None:
                        state["changed"] += act.size
                if act is not None:
                    acts.append(act)
            report(force=bool(done))
        if cancel():
            for fut in pending:
                fut.cancel()
    if cancel():
        raise Cancelled("cancelled: nothing was written")
    t_compute = time.time() - t_start
    total = sum(a.size for a in acts)
    # write (the cores of the units never overlap)
    if progress:
        progress(0.9, "writing %s changed points" % "{:,}".format(total))
    for a in acts:
        store.write_codes(a.codes, flag=a.new["flag"], cls=a.new["cls"], dz=a.new["dz"], z=a.new["z"])
    store.sync()
    touched = sorted(set(getattr(store, "dirty_tiles", set())))
    with ThreadPoolExecutor(nw, thread_name_prefix="CloudPro grids") as ex:
        for m, _ in enumerate(ex.map(store.build_grid, touched)):
            if progress and m % 8 == 0:
                progress(0.9 + 0.1 * (m + 1) / max(1, len(touched)), "updating the project grids")
    store.dirty_tiles = set()
    if stats is not None:
        stats.update(units=len(units), workers=nw, peak_running=peak_running,
                     reduced_buffer=sum(1 for u in units if u.buffer < buffer), points=total_pts, loaded=loaded,
                     compute_s=t_compute, total_s=time.time() - t_start, memory_gb=budget / 2 ** 30)
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


def _m(n):
    return "%.1f M" % (n / 1e6) if n >= 1e6 else "{:,}".format(int(n))


def _dur(s):
    s = int(s)
    return "%d min %02d s" % (s // 60, s % 60) if s >= 60 else "%d s" % s


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
