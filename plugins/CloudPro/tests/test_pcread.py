"""CloudPro 2.16.0: every point-cloud format read back to the points written (positions to 1 mm, intensity,
colour, class, time, parts, CRS).

    python3 CloudPro/tests/test_pcread.py      (from the plugins folder)
"""
import math
import os
import shutil
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)

import pcfiles as PF                                   # noqa: E402
from CloudPro.core import pcread as R                  # noqa: E402


def read_all(path, size=1700):
    src = R.open_source(path)
    bs = list(src.blocks(size))
    keys = set().union(*[b.keys() for b in bs])
    out = {}
    for k in keys:
        out[k] = np.concatenate([b[k] for b in bs])
    return src, out


def close(a, b, tol=0.0011):
    return np.max(np.abs(np.asarray(a) - np.asarray(b))) <= tol


def test_las_laz():
    tmp = tempfile.mkdtemp()
    try:
        c = PF.cloud()
        p = os.path.join(tmp, "a.las")
        PF.write_las_legacy(p, c)
        s, b = read_all(p)
        assert s.crs == "EPSG:2953" and s.has_rgb and s.has_time and s.count == len(c["x"]), (s.crs, s.describe())
        assert close(b["x"], c["x"]) and close(b["y"], c["y"]) and close(b["z"], c["z"])
        assert np.array_equal(b["i"], c["i"]) and np.array_equal(b["rgb"], c["rgb"])
        assert np.array_equal(b["cls"], c["cls"]) and np.allclose(b["t"], c["t"], atol=1e-6)
        assert set(np.unique(b["part"])) == {1, 2} and np.all(b["ret"] == 2)
        print("LAS 1.2 format 3:", s.describe())
        for laz in (False, True):
            p = os.path.join(tmp, "b.la%s" % ("z" if laz else "s"))
            PF.write_las14(p, c, wkt='PROJCS["NAD83(CSRS) / New Brunswick Stereographic"]', laz=laz)
            s, b = read_all(p)
            assert s.crs.startswith("PROJCS") and s.count == len(c["x"])
            assert close(b["x"], c["x"]) and np.array_equal(b["rgb"], c["rgb"]) and np.array_equal(b["i"], c["i"])
            assert np.array_equal(b["withheld"], c["cls"] == 7) and np.all(b["ret"] == 2)
            print(("LAZ" if laz else "LAS 1.4") + " format 7:", s.describe())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_text_formats():
    tmp = tempfile.mkdtemp()
    try:
        c = PF.cloud(3000)
        p = os.path.join(tmp, "h.txt")
        PF.write_text(p, c, cols=("x", "y", "z", "i", "r", "g", "b", "class", "time"))
        s, b = read_all(p, 777)
        assert s.cols[:9] == ["x", "y", "z", "i", "r", "g", "b", "cls", "t"], s.cols
        assert close(b["x"], c["x"]) and np.array_equal(b["rgb"], c["rgb"]) and np.array_equal(b["cls"], c["cls"])
        assert np.array_equal(b["i"], c["i"]) and np.allclose(b["t"], c["t"], atol=1e-5)
        p = os.path.join(tmp, "n.xyz")
        PF.write_text(p, c, header=False)                        # x y z i r g b by count and range
        s, b = read_all(p)
        assert s.cols == ["x", "y", "z", "i", "r", "g", "b"] and np.array_equal(b["rgb"], c["rgb"])
        p = os.path.join(tmp, "c.csv")
        PF.write_text(p, c, header=False, sep=",", cols=("x", "y", "z"))
        s, b = read_all(p)
        assert not s.has_rgb and close(b["z"], c["z"])
        p = os.path.join(tmp, "s.pts")
        h = len(c["x"]) // 2
        PF.write_pts(p, [{k: v[:h] for k, v in c.items()}, {k: v[h:] for k, v in c.items()}])
        s, b = read_all(p, 500)
        assert s.count == len(c["x"]) and len(s.parts) == 2
        assert close(b["x"], c["x"]) and np.array_equal(b["rgb"], c["rgb"]) and np.array_equal(b["i"], c["i"])
        assert np.array_equal(b["part"], np.r_[np.zeros(h), np.ones(len(c["x"]) - h)])
        print("text: XYZ / TXT with names / CSV / PTS (2 scans) read")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_ply_pcd():
    tmp = tempfile.mkdtemp()
    try:
        c = PF.cloud(2500)
        for binary in (True, False):
            p = os.path.join(tmp, "m%d.ply" % binary)
            PF.write_ply(p, c, binary)
            s, b = read_all(p, 600)
            assert s.has_rgb and s.has_intensity and np.array_equal(b["rgb"], c["rgb"])
            assert close(b["x"], c["x"]) and close(b["z"], c["z"], 0.002) and np.array_equal(b["i"], c["i"])
        p = os.path.join(tmp, "p.pcd")
        PF.write_pcd(p, c)
        s, b = read_all(p)
        assert np.array_equal(b["rgb"], c["rgb"]) and close(b["x"], c["x"] - 2_490_000, 0.004)
        print("PLY (binary, ASCII), PCD read")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_e57():
    tmp = tempfile.mkdtemp()
    try:
        r = np.random.default_rng(4)
        scans = []
        want = []
        for k in range(2):
            n = 6000 + 1500 * k
            x, y, z = r.uniform(-30, 30, n), r.uniform(-30, 30, n), r.uniform(-3, 8, n)
            inv = r.random(n) < 0.03
            a = math.radians(30 * (k + 1))
            rot = (math.cos(a / 2), 0.0, 0.0, math.sin(a / 2))         # turned about z
            tr = (100.0 + 5 * k, 200.0, 10.0)
            sc = {"name": "Station %d" % (k + 1), "x": x, "y": y, "z": z, "i": r.integers(0, 1001, n),
                  "rgb": r.integers(0, 256, (n, 3)), "invalid": inv, "rotation": rot, "translation": tr}
            scans.append(sc)
            ca, sa = math.cos(a), math.sin(a)
            xr = np.round(x / 1e-4) * 1e-4
            P = np.column_stack([ca * xr - sa * y + tr[0], sa * xr + ca * y + tr[1], z.astype(np.float32) + tr[2]])
            want.append((P[~inv], sc["i"][~inv], sc["rgb"][~inv], np.full((~inv).sum(), k)))
        p = os.path.join(tmp, "s.e57")
        PF.write_e57(p, scans)
        s, b = read_all(p, 2000)
        P = np.concatenate([w[0] for w in want])
        assert s.crs == "EPSG:2953" and s.parts == {0: "Station 1", 1: "Station 2"}, (s.crs, s.parts)
        assert len(b["x"]) == len(P), (len(b["x"]), len(P))
        assert close(b["x"], P[:, 0], 2e-4) and close(b["y"], P[:, 1], 1e-6) and close(b["z"], P[:, 2], 1e-4)
        assert np.array_equal(b["rgb"], np.concatenate([w[2] for w in want]))
        ii = np.concatenate([w[1] for w in want])
        assert np.max(np.abs(b["i"].astype(int) - np.round(ii / 1000 * 255))) <= 1
        assert np.array_equal(b["part"], np.concatenate([w[3] for w in want]))
        print("E57: 2 scans with poses, %d points (invalid left out), bit-packed across packets and pages" % len(P))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_errors():
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "x.las")
        with open(p, "wb") as f:
            f.write(b"not a las file at all" * 20)
        for path in (p, os.path.join(tmp, "y.dwg")):
            try:
                R.open_source(path)
                raise AssertionError("read %s" % path)
            except R.PointCloudError as e:
                assert os.path.basename(path) in str(e), e
        print("damaged / unknown files: PointCloudError naming the file")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_las_laz()
    test_text_formats()
    test_ply_pcd()
    test_e57()
    test_errors()
    print("test_pcread: OK")
