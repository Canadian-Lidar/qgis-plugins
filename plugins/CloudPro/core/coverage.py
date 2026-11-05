"""Point density and coverage of a project, from the stored 0.5 m project grids (no points loaded).

Used by the Plan view's Density and Coverage grids and by `cloudpro info`: is the area covered densely
enough, or does it need another line? Counts are the kept points (rejected ones are left out)."""
import numpy as np

from .store import GRID_CELL

DEFAULT_TARGET = 100.0                   # pts/m2


def coverage_summary(store, cell, target):
    """Area with points (m2), the share of it at or above the target density, and the median density
    (pts/m2), over the whole project at the display cell. Kept points only (rejected ones left out)."""
    f = max(1, int(round(cell / GRID_CELL)))
    a = cell * cell
    dens = []
    for k in store.keys():
        g = store.load_grid(k)
        if "count" not in g:
            continue
        c = g["count"].astype(float)
        if f > 1:
            n = c.shape[0] // f
            c = c[:n * f, :n * f].reshape(n, f, n, f).sum(axis=(1, 3))
        d = c[c > 0] / a
        if len(d):
            dens.append(d)
    if not dens:
        return {"area_m2": 0.0, "ok_share": 0.0, "median": 0.0, "cells": 0}
    d = np.concatenate(dens)
    return {"area_m2": float(len(d) * a), "ok_share": float((d >= target).mean()), "median": float(np.median(d)),
            "cells": int(len(d))}
