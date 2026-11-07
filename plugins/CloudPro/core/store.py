"""On-disk tiled point store of a CloudPro project.

Points are kept in square tiles (default 50 m) as flat files of packed
records, so any area loads in milliseconds without reading the whole survey,
and edits (status, class, height shift) are written straight back through a
memory map, so an edit is on disk the moment it is made.

Record (33 bytes, little endian):
    x, y, z  int32   millimetres from the project origin
    t        float64 UTC Unix seconds (firing time)
    i        uint8   intensity
    ret      uint8   return: 0 last, 1 strongest/second, 2 single
    ch       uint8   laser channel 0-31
    cls      uint8   ASPRS classification
    flag     uint8   status bits (FLAG_*)
    line     uint16  line index in the project
    rng      uint16  range, centimetres
    az       uint16  azimuth, 0.01 deg
    dz       int16   applied height shift, millimetres (original z = z - dz)
"""
import json
import os
import threading

import numpy as np

REC = np.dtype([("x", "<i4"), ("y", "<i4"), ("z", "<i4"), ("t", "<f8"), ("i", "u1"), ("ret", "u1"),
                ("ch", "u1"), ("cls", "u1"), ("flag", "u1"), ("line", "<u2"), ("rng", "<u2"), ("az", "<u2"),
                ("dz", "<i2")])

# status bits: the reason bits are kept when a point is re-accepted
REJ_MANUAL = 0x01
REJ_AUTO = 0x02
REJ_ORIG = 0x04
ZMOD = 0x10
CLSMOD = 0x20
REJECTED = 0x80
REASONS = REJ_MANUAL | REJ_AUTO | REJ_ORIG

GRID_CELL = 0.5          # overview grid cell (m)


def tile_key(ix, iy):
    return "%d_%d" % (ix, iy)


def parse_key(k):
    a, b = k.split("_")
    return int(a), int(b)


class Store:
    def __init__(self, root, origin=None, tile=50.0, crs=""):
        self.root = root
        self.tdir = os.path.join(root, "tiles")
        self.gdir = os.path.join(root, "grids")
        os.makedirs(self.tdir, exist_ok=True)
        os.makedirs(self.gdir, exist_ok=True)
        self.meta_path = os.path.join(root, "store.json")
        self.lock = threading.RLock()
        self._mm = {}
        self._buf = {}
        self._bufn = 0
        if os.path.exists(self.meta_path):
            with open(self.meta_path) as f:
                m = json.load(f)
            self.origin = None if m.get("origin") is None else np.array(m["origin"], float)
            self.tile = float(m["tile"])
            self.crs = m.get("crs", crs)
            self.counts = {k: int(v) for k, v in m.get("counts", {}).items()}
            self.key_ids = {k: int(v) for k, v in m.get("key_ids", {}).items()}
        else:
            self.key_ids = {}
            self.origin = None if origin is None else np.array(origin, float)
            self.tile = float(tile)
            self.crs = crs
            self.counts = {}

    # -- consistency ---------------------------------------------------------
    def check(self, repair=True):
        """Tile files against the index (store.json), quickly (file sizes only). A crash or a full disk while
        points were being written can leave a part record at the end of a tile, a tile the index does not
        count yet, or a count for a file that is gone. With repair: part records are cut off, counts follow
        the files, orphan tiles are taken in, and the grids of changed tiles are rebuilt when next needed.
        Returns a list of findings in words (empty: all consistent)."""
        out = []
        size = REC.itemsize
        changed = False
        with self.lock:
            on_disk = {}
            for fn in os.listdir(self.tdir):
                if fn.startswith("t_") and fn.endswith(".cpt"):
                    on_disk[fn[2:-4]] = os.path.getsize(os.path.join(self.tdir, fn))
            for key in sorted(set(self.counts) | set(on_disk)):
                n_idx = self.counts.get(key, 0)
                if key not in on_disk:
                    if n_idx:
                        out.append("tile %s: file missing, %d points lost (Update re-creates them)" % (key, n_idx))
                        if repair:
                            self.counts[key] = 0
                            changed = True
                    continue
                b = on_disk[key]
                n_file, rest = divmod(b, size)
                if rest:
                    out.append("tile %s: a part point (%d bytes) at the end, cut off" % (key, rest))
                    if repair:
                        self._close_mm(key)
                        with open(self.tile_path(key), "r+b") as f:
                            f.truncate(n_file * size)
                        changed = True
                if n_file != n_idx:
                    out.append("tile %s: %d points in the file, %d in the index%s" % (
                        key, n_file, n_idx, "" if key in self.counts else " (tile not indexed yet)"))
                    if repair:
                        self.counts[key] = n_file
                        self.key_id(key)
                        changed = True
                if repair and (rest or n_file != n_idx):
                    gp = self.grid_path(key)
                    if os.path.exists(gp):
                        os.remove(gp)
            if repair and changed:
                self.save_meta()
        return out

    # -- metadata ----------------------------------------------------------
    def save_meta(self):
        with self.lock:
            m = {"origin": None if self.origin is None else [float(v) for v in self.origin],
                 "tile": self.tile, "crs": self.crs, "counts": self.counts, "key_ids": self.key_ids,
                 "record": REC.descr.__repr__()}
            tmp = self.meta_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(m, f)
            os.replace(tmp, self.meta_path)

    @property
    def total(self):
        return int(sum(self.counts.values()))

    def key_id(self, key):
        with self.lock:
            k = self.key_ids.get(key)
            if k is None:
                k = self.key_ids[key] = len(self.key_ids)
            return k

    def key_of(self, kid):
        if not hasattr(self, "_rev") or len(self._rev) != len(self.key_ids):
            self._rev = {v: k for k, v in self.key_ids.items()}
        return self._rev[int(kid)]

    def write_codes(self, codes, **fields):
        """Write field values for records given by code (key_id << 40 | index)."""
        if len(codes) == 0:
            return
        kid = codes >> 40
        idx = codes & ((1 << 40) - 1)
        order = np.argsort(kid, kind="stable")
        kid_s = kid[order]
        cuts = np.flatnonzero(np.diff(kid_s)) + 1
        for s_, e_ in zip(np.r_[0, cuts], np.r_[cuts, len(kid_s)]):
            sel = order[s_:e_]
            key = self.key_of(kid_s[s_])
            vals = {n: (v[sel] if np.ndim(v) else v) for n, v in fields.items()}
            self.write_fields(key, idx[sel], **vals)
        self.dirty_tiles = getattr(self, "dirty_tiles", set()) | {self.key_of(k) for k in np.unique(kid)}

    def tile_path(self, key):
        return os.path.join(self.tdir, "t_%s.cpt" % key)

    def grid_path(self, key):
        return os.path.join(self.gdir, "g_%s.npz" % key)

    def keys(self):
        return [k for k, v in self.counts.items() if v > 0]

    def extent(self):
        ks = self.keys()
        if not ks:
            return None
        ij = np.array([parse_key(k) for k in ks])
        return (ij[:, 0].min() * self.tile, ij[:, 1].min() * self.tile,
                (ij[:, 0].max() + 1) * self.tile, (ij[:, 1].max() + 1) * self.tile)

    # -- writing (import) ----------------------------------------------------
    def bin(self, rec):
        """Split records into per-tile blocks -> [(key, records)]. Pure (no lock, no I/O): the importer's
        worker threads do this, so the main thread only queues blocks."""
        if len(rec) == 0:
            return []
        ts = self.tile * 1000.0
        ix = np.floor(rec["x"] / ts).astype(np.int64)
        iy = np.floor(rec["y"] / ts).astype(np.int64)
        code = (ix + 1_000_000) * 2_000_003 + (iy + 1_000_000)
        order = np.argsort(code, kind="stable")
        code = code[order]
        rec = rec[order]
        cuts = np.flatnonzero(np.diff(code)) + 1
        starts = np.r_[0, cuts]
        ends = np.r_[cuts, len(rec)]
        return [(tile_key(int(ix[order[a]]), int(iy[order[a]])), rec[a:b]) for a, b in zip(starts, ends)]

    def append_binned(self, parts):
        """Queue per-tile blocks from bin(); written by flush() every 4 M points."""
        with self.lock:
            for key, a in parts:
                self._buf.setdefault(key, []).append(a)
                self._bufn += len(a)
            if self._bufn > 4_000_000:
                self.flush()

    def append(self, rec):
        """Append records (REC array, coordinates already relative to origin)."""
        self.append_binned(self.bin(rec))

    def flush(self):
        with self.lock:
            for key, parts in self._buf.items():
                a = np.concatenate(parts) if len(parts) > 1 else parts[0]
                self._close_mm(key)
                with open(self.tile_path(key), "ab") as f:
                    a.tofile(f)
                self.counts[key] = self.counts.get(key, 0) + len(a)
                self.key_id(key)
            self._buf = {}
            self._bufn = 0

    # -- reading / editing --------------------------------------------------------
    def _close_mm(self, key):
        mm = self._mm.pop(key, None)
        if mm is not None:
            try:
                mm.flush()
            except Exception:
                pass
            del mm

    def tile_array(self, key, writable=False):
        """Memory map of one tile (cached)."""
        with self.lock:
            mm = self._mm.get(key)
            if mm is not None and (not writable or mm.mode == "r+"):
                return mm
            if mm is not None:
                self._close_mm(key)
            p = self.tile_path(key)
            if not os.path.exists(p) or os.path.getsize(p) < REC.itemsize:
                return np.zeros(0, REC)
            mm = np.memmap(p, REC, mode="r+" if writable else "r")
            if len(self._mm) > 256:
                for k in list(self._mm)[:64]:
                    self._close_mm(k)
            self._mm[key] = mm
            return mm

    def tiles_in_bbox(self, x0, y0, x1, y1):
        """Tile keys (with data) intersecting a bbox in local metres."""
        t = self.tile
        i0, i1 = int(np.floor(x0 / t)), int(np.floor(x1 / t))
        j0, j1 = int(np.floor(y0 / t)), int(np.floor(y1 / t))
        out = []
        if (i1 - i0 + 1) * (j1 - j0 + 1) > len(self.counts):
            for k in self.keys():
                i, j = parse_key(k)
                if i0 <= i <= i1 and j0 <= j <= j1:
                    out.append(k)
            return out
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                k = tile_key(i, j)
                if self.counts.get(k, 0) > 0:
                    out.append(k)
        return out

    def write_fields(self, key, idx, **fields):
        """Write field values back into one tile at record indices idx."""
        with self.lock:
            mm = self.tile_array(key, writable=True)
            for name, val in fields.items():
                mm[name][idx] = val

    def sync(self):
        with self.lock:
            for mm in self._mm.values():
                try:
                    if mm.mode == "r+":
                        mm.flush()
                except Exception:
                    pass

    def close(self):
        with self.lock:
            for k in list(self._mm):
                self._close_mm(k)

    # -- overview grids -------------------------------------------------------------
    def build_grid(self, key):
        a = self.tile_array(key)
        i, j = parse_key(key)
        n = int(round(self.tile / GRID_CELL))
        out = {"n": n}
        if len(a) == 0:
            np.savez(self.grid_path(key), **out)
            return
        x = a["x"] * 1e-3 - i * self.tile
        y = a["y"] * 1e-3 - j * self.tile
        z = a["z"] * 1e-3
        cx = np.clip((x / GRID_CELL).astype(np.int64), 0, n - 1)
        cy = np.clip((y / GRID_CELL).astype(np.int64), 0, n - 1)
        cell = cy * n + cx
        acc = (a["flag"] & REJECTED) == 0
        N = n * n
        cnt = np.bincount(cell[acc], minlength=N)
        rej = np.bincount(cell[~acc], minlength=N)
        zs = np.bincount(cell[acc], z[acc], minlength=N)
        ins = np.bincount(cell[acc], a["i"][acc].astype(float), minlength=N)
        zmax = np.full(N, -np.inf)
        zmin = np.full(N, np.inf)
        np.maximum.at(zmax, cell[acc], z[acc])
        np.minimum.at(zmin, cell[acc], z[acc])
        with np.errstate(invalid="ignore", divide="ignore"):
            zmean = zs / cnt
            imean = ins / cnt
        out.update(count=cnt.reshape(n, n).astype(np.uint32), rej=rej.reshape(n, n).astype(np.uint32),
                   zmean=zmean.reshape(n, n).astype(np.float32), zmin=zmin.reshape(n, n).astype(np.float32),
                   zmax=zmax.reshape(n, n).astype(np.float32), imean=imean.reshape(n, n).astype(np.float32))
        np.savez(self.grid_path(key), **out)

    def load_grid(self, key):
        p = self.grid_path(key)
        if not os.path.exists(p):
            self.build_grid(key)
        try:
            with np.load(p) as d:
                return {k: d[k] for k in d.files}
        except Exception:
            self.build_grid(key)
            with np.load(p) as d:
                return {k: d[k] for k in d.files}
