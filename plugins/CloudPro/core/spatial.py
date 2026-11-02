"""Spatial helpers shared by the filters: grids, voxels, neighbour queries,
connected components and batched local geometry."""
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
