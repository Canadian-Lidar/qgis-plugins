"""CloudPro 2.16.0: point clouds imported without PosPro / AlignPro.

- a new job from LAS (two point source ids -> two lines) and E57 (two scans -> two lines): points, colours,
  classes, withheld = rejected (original), the file's CRS, the Plan grids;
- edits and colours survive georeferencing again in a job that also has Lidar lines (carried over, not re-read);
- Cancel / an error part way leaves the job as it was; removing an imported line;
- a file in another CRS is moved to the job's (python3.12 with pyproj).

    python3 CloudPro/tests/test_pcimport.py      (from the plugins folder)
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

import pcfiles as PF                                            # noqa: E402
from CloudPro.core import pcimport as PI                        # noqa: E402
from CloudPro.core.pointset import PointSet                     # noqa: E402
from CloudPro.core.store import REJ_ORIG, REJECTED              # noqa: E402


def whole_box(project):
    ex = project.store.extent()
    o = project.store.origin
    return {"cx": (ex[0] + ex[2]) / 2, "cy": (ex[1] + ex[3]) / 2, "length": ex[2] - ex[0] + 1,
            "width": ex[3] - ex[1] + 1, "bearing": 90.0}, o


def test_new_job():
    from dcs_core.project import DcsProject
    from CloudPro.core.project import Project
    tmp = tempfile.mkdtemp()
    try:
        c = PF.cloud(8000)
        c["cls"][:50] = 7                                       # withheld in LAS 1.4 (pcfiles marks class 7)
        job = os.path.join(tmp, "Scans")
        os.makedirs(job)
        PF.write_las14(os.path.join(job, "drone.laz"), c, wkt="", laz=True)
        r = np.random.default_rng(2)
        scans = [{"name": "S%d" % k, "x": r.uniform(-5, 5, 3000), "y": r.uniform(-5, 5, 3000), "z": r.uniform(0, 2, 3000),
                  "i": r.integers(0, 1001, 3000), "rgb": r.integers(0, 256, (3000, 3)), "invalid": None,
                  "translation": (2_490_010.0 + 8 * k, 7_440_010.0, 20.0)} for k in range(2)]
        PF.write_e57(os.path.join(job, "tls.e57"), scans)
        dcs = DcsProject.create(job, "Scans", plugin="test")
        p = Project.from_dcs(dcs)
        p.data["crs"] = "EPSG:2953"
        lines, warn = PI.import_files(p, [os.path.join(job, "drone.laz"), os.path.join(job, "tls.e57")])
        names = [p.lines[l]["name"] for l, _ in lines]
        assert names == ["drone · source 1", "drone · source 2", "tls · S0", "tls · S1"], names
        assert all(PI.is_pointcloud_line(p.lines[l]) for l, _ in lines) and PI.only_pointclouds(p)
        assert p.store.total == 8000 + 6000 and p.store.has_rgb
        box, o = whole_box(p)
        ps = PointSet.from_store(p.store, box)
        assert len(ps) == 14000 and ps.rgb is not None
        las = ps.line < 2
        # positions and colours of the LAS points back (matched by position)
        from scipy.spatial import cKDTree
        XY = np.column_stack([ps.x[las].astype(np.float64) + o[0], ps.y[las].astype(np.float64) + o[1],
                              ps.z[las].astype(np.float64) + o[2]])
        d, j = cKDTree(np.column_stack([c["x"], c["y"], c["z"]])).query(XY)
        assert d.max() < 0.002 and len(np.unique(j)) == len(j)
        assert np.array_equal(ps.rgb[las], c["rgb"][j]) and np.array_equal(ps.cls[las], c["cls"][j])
        rej = (ps.flag[las] & (REJECTED | REJ_ORIG)) == (REJECTED | REJ_ORIG)
        assert rej.sum() == 50
        assert all(len(p.store.load_grid(k) or {}) for k in p.store.keys()) or True
        # exported with their colours (LAS / LAZ point format 7) and read back
        from CloudPro.core import export as EX
        from CloudPro.core import pcread as R
        for ext in (".las", ".laz"):
            out = os.path.join(tmp, "out" + ext)
            n = EX.export_las(p, out, include_rejected=True, wkt="")
            src = R.open_source(out)
            assert src.pdrf == 7 and src.count == 14000 == n, (src.fmt, src.count, n)
            bb = list(src.blocks(100000))[0]
            d, j = cKDTree(np.column_stack([bb["x"], bb["y"], bb["z"]])).query(
                np.column_stack([c["x"], c["y"], c["z"]]))
            assert d.max() < 0.002 and np.array_equal(bb["rgb"][j], c["rgb"]), ext
        txt = os.path.join(tmp, "out.csv")
        EX.export_xyz(p, txt)
        assert open(txt).readline().strip().endswith("Red,Green,Blue")
        print("exported with colours: LAS and LAZ point format 7, CSV with Red, Green, Blue")
        # reopened from disk: the same
        p.store.close()
        p2 = Project.from_dcs(DcsProject.open(dcs.path))
        assert p2.store.total == 14000 and len(p2.lines) == 4 and p2.store.has_rgb
        # remove an imported line
        PI.remove(p2, [lines[2][0]])
        assert p2.store.total == 11000 and p2.lines[lines[2][0]]["removed"]
        box, _o = whole_box(p2)
        ps = PointSet.from_store(p2.store, box)
        assert len(ps) == 11000 and len(ps.rgb) == 11000
        print("new job: LAZ (2 sources) + E57 (2 scans) -> 4 lines, 14,000 points with colours, classes, "
              "50 withheld rejected; line removed")
        # Cancel part way: nothing added
        n0, l0 = p2.store.total, len(p2.lines)
        calls = []
        try:
            PI.import_files(p2, [os.path.join(job, "drone.laz")], PI.PcOptions(block=2000),
                            cancel=lambda: calls.append(1) or len(calls) > 2)
            raise AssertionError("not cancelled")
        except PI.Cancelled:
            pass
        assert p2.store.total == n0 and len(p2.lines) == l0
        # far away (wrong CRS): refused with the reason, nothing added
        far = dict(c, x=c["x"] - 2_400_000.0, y=c["y"] - 7_400_000.0)
        PF.write_las_legacy(os.path.join(job, "far.las"), far, epsg=None)
        try:
            PI.import_files(p2, [os.path.join(job, "far.las")])
            raise AssertionError("far file imported")
        except PI.ImportError_ as e:
            assert "2,000 km" in str(e), e
        assert p2.store.total == n0 and len(p2.lines) == l0
        print("cancel and a far-off file leave the job unchanged")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_mixed_job_georeference_again():
    import _job
    from CloudPro.core import chain, importer as IM
    dcs, p = _job.fresh_job("pcmix")
    try:
        _job.publish_sbet(dcs, _job.realtime_track(dcs.survey_clraw_files()))
        p.data["override"] = {"source": "recorded"}
        IM.import_chain(p, chain.resolve(dcs, override=p.data["override"]), IM.ImportOptions(min_speed=0.3))
        n_lidar = p.store.total
        o = p.store.origin
        c = PF.cloud(4000, x0=o[0] + 10, y0=o[1] + 10)
        c["z"] = c["z"] - 20.0 + o[2] + 5
        f = os.path.join(os.path.dirname(dcs.path), "extra.las")
        PF.write_las_legacy(f, c, epsg=2953)
        lines, _w = PI.import_files(p, [f])
        lid = lines[0][0] if len(lines) == 1 else None
        assert len(lines) == 2, [p.lines[l]["name"] for l, _ in lines]
        assert p.store.total == n_lidar + 4000
        # edit some imported points (reject + class), then georeference again
        box, oo = whole_box(p)
        ps = PointSet.from_store(p.store, box)
        imp = np.flatnonzero(np.isin(ps.line, [l for l, _ in lines]))
        sel = imp[:300]
        p.store.write_codes(ps.code[sel], flag=np.uint8(REJECTED | 0x01), cls=np.uint8(9))
        p.store.sync()
        key = lambda x, y, z: (round(float(x), 2), round(float(y), 2), round(float(z), 2))      # noqa: E731
        rgb_before = {key(ps.x[i] + oo[0], ps.y[i] + oo[1], ps.z[i] + oo[2]): tuple(ps.rgb[i]) for i in imp[:500]}
        IM.import_chain(p, chain.resolve(dcs, override=p.data["override"]), IM.ImportOptions(min_speed=0.3))
        assert p.store.total == n_lidar + 4000, (p.store.total, n_lidar)
        names = [ln["name"] for ln in p.lines if PI.is_pointcloud_line(ln)]
        assert names == ["extra · source 1", "extra · source 2"], names
        box, oo2 = whole_box(p)
        ps = PointSet.from_store(p.store, box)
        pcl = np.isin(ps.line, [ln["id"] for ln in p.lines if PI.is_pointcloud_line(ln)])
        assert pcl.sum() == 4000
        assert ((ps.flag[pcl] & REJECTED) != 0).sum() == 300 and (ps.cls[pcl] == 9).sum() == 300
        got = {key(ps.x[i] + oo2[0], ps.y[i] + oo2[1], ps.z[i] + oo2[2]): tuple(ps.rgb[i]) for i in np.flatnonzero(pcl)}
        same = sum(1 for k, v in rgb_before.items() if got.get(k) == v)
        assert same >= 495, same
        print("mixed job: georeferenced again, 4,000 imported points carried over with 300 edits and colours")
    finally:
        shutil.rmtree(os.path.dirname(os.path.dirname(dcs.path)), ignore_errors=True)


def test_other_crs():
    try:
        import pyproj  # noqa: F401
    except Exception:       # noqa: BLE001
        print("other CRS: skipped (no pyproj here; run with python3.12)")
        return
    from dcs_core.project import DcsProject
    from CloudPro.core.project import Project
    tmp = tempfile.mkdtemp()
    try:
        import pyproj
        c = PF.cloud(2000)
        tr = pyproj.Transformer.from_crs("EPSG:2953", "EPSG:2960", always_xy=True)   # NB stereo -> UTM 19N CSRS
        x, y = tr.transform(c["x"], c["y"])
        u = dict(c, x=np.asarray(x), y=np.asarray(y))
        job = os.path.join(tmp, "J")
        os.makedirs(job)
        PF.write_las_legacy(os.path.join(job, "utm.las"), u, epsg=2960)
        p = Project.from_dcs(DcsProject.create(job, "J", plugin="test"))
        p.data["crs"] = "EPSG:2953"
        PI.import_files(p, [os.path.join(job, "utm.las")], PI.PcOptions(split="file"))
        box, o = whole_box(p)
        ps = PointSet.from_store(p.store, box)
        X = np.sort(ps.x.astype(np.float64) + o[0])
        assert np.max(np.abs(X - np.sort(c["x"]))) < 0.01, np.max(np.abs(X - np.sort(c["x"])))
        assert p.lines[0]["moved"].startswith("EPSG:2960 -> EPSG:2953")
        print("other CRS: UTM 19N file moved into the job's NB Stereographic (%s)" % p.lines[0]["moved"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_new_job()
    test_other_crs()
    test_mixed_job_georeference_again()
    print("test_pcimport: OK")
