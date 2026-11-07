"""In-memory working set of points (the Inspection Area) with a link back to
the tile records they came from, so edits go straight to disk."""
import numpy as np

from .store import REC, REJECTED

FIELDS = ("t", "i", "ret", "ch", "cls", "flag", "line", "rng", "az", "dz")


def rect_corners(cx, cy, length, width, bearing_deg):
    """Corners of a rotated rectangle. bearing = direction of the long axis,
    degrees clockwise from grid north."""
    b = np.radians(bearing_deg)
    u = np.array([np.sin(b), np.cos(b)])         # along
    v = np.array([np.cos(b), -np.sin(b)])        # across (to the right)
    c = np.array([cx, cy])
    hl, hw = length / 2.0, width / 2.0
    return np.array([c - u * hl - v * hw, c + u * hl - v * hw, c + u * hl + v * hw, c - u * hl + v * hw])


def to_box_frame(x, y, cx, cy, bearing_deg):
    """Coordinates along (u) and across (v, + to the right) a box axis."""
    b = np.radians(bearing_deg)
    su, cu = np.sin(b), np.cos(b)
    dx = x - cx
    dy = y - cy
    return dx * su + dy * cu, dx * cu - dy * su


class PointSet:
    def __init__(self, n=0):
        self.x = np.zeros(n, np.float32)
        self.y = np.zeros(n, np.float32)
        self.z = np.zeros(n, np.float32)
        for f in FIELDS:
            setattr(self, f, np.zeros(n, REC[f]))
        self.code = np.zeros(n, np.int64)          # key_id << 40 | record index
        self.box = None

    def __len__(self):
        return len(self.x)

    @property
    def rejected(self):
        return (self.flag & REJECTED) != 0

    @property
    def accepted(self):
        return (self.flag & REJECTED) == 0

    @classmethod
    def from_store(cls, store, box, zrange=None, max_points=None):
        """Load the points inside a rotated box dict(cx, cy, length, width, bearing)."""
        cx, cy, L, W, b = box["cx"], box["cy"], box["length"], box["width"], box["bearing"]
        cor = rect_corners(cx, cy, L, W, b)
        keys = store.tiles_in_bbox(cor[:, 0].min(), cor[:, 1].min(), cor[:, 0].max(), cor[:, 1].max())
        parts = []
        codes = []
        for k in keys:
            a = store.tile_array(k)
            if len(a) == 0:
                continue
            x = a["x"] * 1e-3
            y = a["y"] * 1e-3
            u, v = to_box_frame(x, y, cx, cy, b)
            m = (np.abs(u) <= L / 2) & (np.abs(v) <= W / 2)
            if zrange is not None:
                z = a["z"] * 1e-3
                m &= (z >= zrange[0]) & (z <= zrange[1])
            idx = np.flatnonzero(m)
            if len(idx) == 0:
                continue
            parts.append(np.asarray(a[idx]))
            codes.append((np.int64(store.key_id(k)) << 40) | idx.astype(np.int64))
        ps = cls(0)
        ps.box = dict(box)
        if not parts:
            return ps
        r = np.concatenate(parts)
        code = np.concatenate(codes)
        if max_points and len(r) > max_points:
            sel = np.sort(np.random.default_rng(0).choice(len(r), max_points, replace=False))
            r, code = r[sel], code[sel]
        # single precision: the coordinates are millimetres from the project origin (a few km at most), so
        # float32 keeps them to about 0.1 mm while halving the memory of the three largest arrays
        k = np.float32(1e-3)
        ps.x = r["x"].astype(np.float32) * k
        ps.y = r["y"].astype(np.float32) * k
        ps.z = r["z"].astype(np.float32) * k
        for f in FIELDS:
            setattr(ps, f, np.ascontiguousarray(r[f]))
        ps.code = code
        return ps

    def subset(self, idx):
        s = PointSet(0)
        s.x, s.y, s.z = self.x[idx], self.y[idx], self.z[idx]
        for f in FIELDS:
            setattr(s, f, getattr(self, f)[idx])
        s.code = self.code[idx]
        s.box = self.box
        return s

    def find(self, codes):
        """Positions of the given record codes in this set (-1 where absent)."""
        if len(self.code) == 0:
            return np.full(len(codes), -1)
        order = np.argsort(self.code, kind="stable")
        sc = self.code[order]
        pos = np.searchsorted(sc, codes)
        pos = np.clip(pos, 0, len(sc) - 1)
        hit = sc[pos] == codes
        out = np.full(len(codes), -1, np.int64)
        out[hit] = order[pos[hit]]
        return out

    def xyz(self, idx=None):
        if idx is None:
            return np.column_stack([self.x, self.y, self.z])
        return np.column_stack([self.x[idx], self.y[idx], self.z[idx]])
