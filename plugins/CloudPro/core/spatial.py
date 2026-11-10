"""Spatial helpers shared by the filters: grids, voxels, neighbour queries,
connected components and batched local geometry."""
import math

import numpy as np

try:
    from scipy.spatial import cKDTree
    HAVE_SCIPY = True
except Exception:          # pragma: no cover
    cKDTree = None
    HAVE_SCIPY = False


def need_scipy():
    if not HAVE_SCIPY:
        raise RuntimeError("this filter needs SciPy (scipy.spatial); install it in the QGIS Python")


def grid_index(x, y, cell, x0=None, y0=None):
    """Integer cell coordinates and a compact cell id per point."""
    x0 = np.floor(x.min() / cell) * cell if x0 is None else x0
    y0 = np.floor(y.min() / cell) * cell if y0 is None else y0
    ix = np.floor((x - x0) / cell).astype(np.int64)
    iy = np.floor((y - y0) / cell).astype(np.int64)
    return ix, iy, x0, y0


def compact_ids(*cols):
    """Unique id per distinct tuple of integer columns -> (ids, n_unique)."""
    if len(cols) == 1:
        key = cols[0]
    else:
        key = np.zeros(len(cols[0]), np.int64)
        for c in cols:
            c = c - c.min()
            key = key * (int(c.max()) + 1) + c
    u, inv = np.unique(key, return_inverse=True)
    return inv.ravel(), len(u)


def group_stats(ids, n, v):
    cnt = np.bincount(ids, minlength=n).astype(float)
    s = np.bincount(ids, v, minlength=n)
    s2 = np.bincount(ids, v * v, minlength=n)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = s / cnt
        var = np.maximum(s2 / cnt - mean * mean, 0)
    return cnt, mean, np.sqrt(var)


def group_median(ids, n, v):
    o = np.lexsort((v, ids))
    ids_s = ids[o]
    v_s = v[o]
    cnt = np.bincount(ids_s, minlength=n)
    start = np.r_[0, np.cumsum(cnt)[:-1]]
    med = np.full(n, np.nan)
    ok = cnt > 0
    lo = start + (cnt - 1) // 2
    hi = start + cnt // 2
    med[ok] = 0.5 * (v_s[lo[ok]] + v_s[hi[ok]])
    return med


def tree(xyz):
    need_scipy()
    return cKDTree(xyz, leafsize=32, balanced_tree=False, compact_nodes=False)


CHUNK = 2_000_000          # queries per block: bounded memory however many points


def fewer_than(tr, q, r, kmin):
    """For each query point: fewer than kmin other points within r (r scalar or per point)?

    The same answer as query_ball_point(q, r, return_length=True) - 1 < kmin, but from the kmin + 1 nearest
    points: the cost no longer grows with the density (counting every neighbour within r took thousands of
    neighbours per point on a dense facade). Queried in blocks, so memory stays bounded."""
    q = np.asarray(q, np.float64)
    r = np.broadcast_to(np.asarray(r, np.float64), (len(q),))
    out = np.zeros(len(q), bool)
    k = int(kmin) + 1
    todo = np.arange(len(q))
    # exact shortcut: two points in one cube of side a are within a * sqrt(3); a query point whose cube (side
    # min(r) / sqrt(3)) holds k tree points (itself included when it is in the tree) has kmin others within r
    rmin = float(r.min()) if len(r) else 0.0
    if len(q) > 50_000 and rmin > 0:
        dense = _dense_cells(tr.data, q, rmin / math.sqrt(3.0) * (1 - 1e-9), k)
        todo = np.flatnonzero(~dense)
    for s in range(0, len(todo), CHUNK):
        ii = todo[s:s + CHUNK]
        d, _ = tr.query(q[ii], k=k, workers=-1)
        d = d.reshape(len(d), -1)
        out[ii] = d[:, k - 1] > r[ii]
    return out


def _dense_cells(data, q, a, need):
    """For each query point: does its cube of side a (grid anchored at the data's minimum) hold at least
    `need` data points? (The neighbour count it stands in for includes the query point itself when it is one
    of the data points, exactly as the count within r does.)"""
    lo = data.min(0)
    cd = np.floor((data - lo) / a).astype(np.int64)
    cq = np.floor((q - lo) / a).astype(np.int64)
    dims = np.maximum(cd.max(0), cq.max(0)) + 1
    if float(dims[0]) * dims[1] * dims[2] > 9e18:
        return np.zeros(len(q), bool)
    kd = (cd[:, 0] * dims[1] + cd[:, 1]) * dims[2] + cd[:, 2]
    kq = (cq[:, 0] * dims[1] + cq[:, 1]) * dims[2] + cq[:, 2]
    u, c = np.unique(kd, return_counts=True)
    pos = np.clip(np.searchsorted(u, kq), 0, len(u) - 1)
    return (u[pos] == kq) & (c[pos] >= need)


def mean_knn_distance(tr, q, k):
    """Mean distance to the k nearest other points (the first neighbour is the point itself), in blocks."""
    q = np.asarray(q, np.float64)
    out = np.empty(len(q), np.float64)
    for s in range(0, len(q), CHUNK):
        d, _ = tr.query(q[s:s + CHUNK], k=k + 1, workers=-1)
        out[s:s + CHUNK] = d[:, 1:].mean(1)
    return out


def knn(tr, q, k):
    d, i = tr.query(q, k=k, workers=-1)
    return d, i


def voxel_components(xyz, size, connectivity=26):
    """Connected components of occupied voxels.
    Returns (component id per point, points per component)."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    v = np.floor(xyz / size).astype(np.int64)
    v -= v.min(0)
    dims = v.max(0) + 3
    key = (v[:, 0] + 1) * dims[1] * dims[2] + (v[:, 1] + 1) * dims[2] + (v[:, 2] + 1)
    uk, inv = np.unique(key, return_inverse=True)
    inv = inv.ravel()
    nv = len(uk)
    offs = []
    rng = (-1, 0, 1)
    for dx in rng:
        for dy in rng:
            for dz in rng:
                if (dx, dy, dz) <= (0, 0, 0):
                    continue
                if connectivity == 6 and abs(dx) + abs(dy) + abs(dz) != 1:
                    continue
                offs.append(dx * dims[1] * dims[2] + dy * dims[2] + dz)
    rows, cols = [], []
    ar = np.arange(nv)
    for o in offs:
        q = uk + o
        p = np.searchsorted(uk, q)
        p = np.clip(p, 0, nv - 1)
        hit = uk[p] == q
        rows.append(ar[hit])
        cols.append(p[hit])
    rows = np.concatenate(rows) if rows else np.zeros(0, np.int64)
    cols = np.concatenate(cols) if cols else np.zeros(0, np.int64)
    g = coo_matrix((np.ones(len(rows), np.int8), (rows, cols)), shape=(nv, nv))
    ncomp, lab = connected_components(g, directed=False)
    comp = lab[inv]
    sizes = np.bincount(comp, minlength=ncomp)
    return comp, sizes


def local_cov(xyz, nbr_idx):
    """Covariance matrices (n,3,3) of neighbourhoods given index array (n,k)."""
    P = xyz[nbr_idx]                                  # (n,k,3)
    c = P.mean(1, keepdims=True)
    Q = P - c
    return np.einsum("nki,nkj->nij", Q, Q) / nbr_idx.shape[1]


def eigen_features(xyz, nbr_idx, chunk=200000):
    """Demantke/Weinmann dimensionality features from k neighbours.
    Returns dict linearity, planarity, scattering, verticality, normal_z."""
    n = len(nbr_idx)
    out = {k: np.empty(n, np.float32) for k in ("linearity", "planarity", "scattering", "verticality", "nz",
                                                 "curvature")}
    for s in range(0, n, chunk):
        C = local_cov(xyz, nbr_idx[s:s + chunk])
        w, V = np.linalg.eigh(C)                       # ascending
        l3, l2, l1 = np.maximum(w[:, 0], 1e-12), np.maximum(w[:, 1], 1e-12), np.maximum(w[:, 2], 1e-12)
        s1, s2, s3 = np.sqrt(l1), np.sqrt(l2), np.sqrt(l3)
        out["linearity"][s:s + chunk] = (s1 - s2) / s1
        out["planarity"][s:s + chunk] = (s2 - s3) / s1
        out["scattering"][s:s + chunk] = s3 / s1
        nz = np.abs(V[:, 2, 0])                        # normal = smallest eigenvector
        out["nz"][s:s + chunk] = nz
        out["verticality"][s:s + chunk] = 1 - nz
        out["curvature"][s:s + chunk] = l3 / (l1 + l2 + l3)
    return out


def fill_nan_nearest(g):
    """Fill NaN cells of a 2-D grid with the nearest valid value."""
    from scipy import ndimage
    m = np.isnan(g)
    if not m.any():
        return g
    if m.all():
        return np.zeros_like(g)
    idx = ndimage.distance_transform_edt(m, return_distances=False, return_indices=True)
    return g[tuple(idx)]


def disk(r):
    r = int(max(1, r))
    y, x = np.ogrid[-r:r + 1, -r:r + 1]
    return (x * x + y * y) <= r * r


def _disk_rows(r):
    """Half-widths of the rows of disk(r): row dy spans |dx| <= w[dy + r]."""
    r = int(max(1, r))
    dy = np.arange(-r, r + 1)
    return dy, np.floor(np.sqrt(np.maximum(r * r - dy * dy, 0)) + 1e-9).astype(int)


def _disk_morph(G, r, op):
    """Grey erosion (op 'min') or dilation ('max') of G by disk(r), identical to scipy.ndimage
    grey_erosion / grey_dilation with footprint=disk(r) and mode 'reflect': the disk is cut into rows, each
    row a 1-D running min / max (whose cost does not grow with its length), and the rows are combined with
    shifts. O(r) passes instead of O(r^2) comparisons per cell."""
    from scipy import ndimage
    r = int(max(1, r))
    f1 = ndimage.minimum_filter1d if op == "min" else ndimage.maximum_filter1d
    comb = np.minimum if op == "min" else np.maximum
    P = np.pad(G, r, mode="symmetric")                  # scipy 'reflect' == numpy 'symmetric'
    ny, nx = G.shape
    dy, w = _disk_rows(r)
    out = None
    rows = {}
    for d, half in zip(dy, w):
        R = rows.get(half)
        if R is None:
            R = rows[half] = f1(P, size=2 * half + 1, axis=1, mode="nearest")
        part = R[r + d:r + d + ny, r:r + nx]
        out = part.copy() if out is None else comb(out, part, out=out)
    return out


def disk_opening(G, r):
    """scipy.ndimage.grey_opening(G, footprint=disk(r)), much faster for large r."""
    return _disk_morph(_disk_morph(G, r, "min"), r, "max")


def parse_set(text, offset=0):
    """'1,2,4-6' -> set of ints (minus offset)."""
    out = set()
    for part in str(text).replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part[1:]:
            a, b = part.split("-", 1) if not part.startswith("-") else (part, part)
            try:
                for v in range(int(a), int(b) + 1):
                    out.add(v - offset)
            except ValueError:
                pass
        else:
            try:
                out.add(int(part) - offset)
            except ValueError:
                pass
    return out
