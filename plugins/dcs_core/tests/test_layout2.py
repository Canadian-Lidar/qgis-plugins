"""DCS Core 1.18.0: the file structure of 17 Nov 2026 (layout 2) beside layout 1 projects. No Qt.

    python3 -m pytest dcs_core/tests/test_layout2.py   (from plugins/)
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dcs_core import clraw, handover  # noqa: E402
from dcs_core.project import (STAGES, DcsProject, in_processing, is_processing_dir, prune_walk)  # noqa: E402


def _job(tmp):
    job = os.path.join(tmp, "MP_PROJECTS", "Greenstone")
    for day in ("20261014", "20261015"):
        for sub in ("NAV", "LIDAR", "IMU", "CLRAW", "BASE", "GNSS", "EXPORT"):
            os.makedirs(os.path.join(job, day, sub))
    with open(os.path.join(job, "project.json"), "w") as f:
        json.dump({"id": "Greenstone"}, f)
    return job


def test_create_layout2():
    with tempfile.TemporaryDirectory() as tmp:
        job = _job(tmp)
        p = DcsProject.create(job, "2026-10-14_1")
        assert p.path == os.path.join(job, "2026-10-14_1.dcsproj")
        assert p.layout == 2 and p.job_folder == job
        assert p.work_root == os.path.join(job, "2026-10-14_1")
        assert p.qgis_project_path() == os.path.join(job, "2026-10-14_1.qgz")
        assert p.stage_dir("pospro", "out") == os.path.join(job, "2026-10-14_1", "01_PosPro", "OUT")
        assert p.stage_dir("cloudpro", "work/store") == os.path.join(job, "2026-10-14_1", "03_CloudPro", "WORK", "store")
        assert p.stage_folder("strippro") == "04_StripPro"
        assert p.remix_dir() == os.path.join(job, "2026-10-14_1", "00_RemixPro")
        assert p.export_dir("x", create=False) == os.path.join(job, "2026-10-14_1", "EXPORT", "x")
        assert [s[0] for s in STAGES] == ["pospro", "alignpro", "cloudpro", "strippro"]
        # open by the file, the job folder, the processing folder, a file deep in a stage
        assert DcsProject.open(p.path).name == p.name
        assert DcsProject.open(p.work_root).path == p.path
        f = os.path.join(p.stage_dir("alignpro", "out"), "x.txt")
        open(f, "w").close()
        assert DcsProject.find_for(f).path == p.path
        # a second processing project in the same job folder: each file finds its own
        q = DcsProject.create(job, "2026-10-14_2")
        g = os.path.join(q.stage_dir("pospro", "work"), "y.txt")
        open(g, "w").close()
        assert DcsProject.find_for(g).path == q.path
        assert DcsProject.find_for(f).path == p.path
        assert DcsProject.open(q.work_root).path == q.path
        # the processing folders are never raw data
        assert is_processing_dir(job, "2026-10-14_1") and not is_processing_dir(job, "20261014")
        assert in_processing(f) and not in_processing(os.path.join(job, "20261014", "CLRAW", "a.clraw"))
        dirs = ["20261014", "2026-10-14_1", "2026-10-14_2", ".git"]
        prune_walk(job, dirs)
        assert dirs == ["20261014"]
        # a name that is already a folder of the job is refused
        try:
            DcsProject.create(job, "20261014")
            raise AssertionError("expected a refusal")
        except Exception as e:      # noqa: BLE001
            assert "choose another name" in str(e)


def test_publish_into_upper_out():
    with tempfile.TemporaryDirectory() as tmp:
        job = _job(tmp)
        p = DcsProject.create(job, "J")
        d = p.stage_dir("pospro", "out")
        data = os.path.join(d, "t.sbet")
        with open(data, "wb") as fh:
            fh.write(b"\0" * 136)
        handover.publish(p, "pospro", {"sbet": data}, {"x": 1}, "PosPro", "2.10.0", "test")
        assert os.path.isfile(os.path.join(job, "J", "01_PosPro", "OUT", "trajectory.json"))
        assert p.state("pospro")[0] == "done"


def test_layout1_unchanged():
    with tempfile.TemporaryDirectory() as tmp:
        job = os.path.join(tmp, "job")
        os.makedirs(job)
        folder = os.path.join(job, "Processing", "Old")
        os.makedirs(folder)
        path = os.path.join(folder, "Old.dcsproj")
        with open(path, "w") as f:
            json.dump({"schema": "dcs.project/1", "name": "Old", "raw_root": "../..", "captures": [], "shared": {},
                       "shared_meta": {}, "stages": {}}, f)
        p = DcsProject.open(path)
        assert p.layout == 1 and p.work_root == folder and p.job_folder == job
        assert p.stage_dir("pospro", "out", create=False) == os.path.join(folder, "01_PosPro", "out")
        assert p.stage_folder("strippro") == "02_StripPro"
        assert p.remix_dir() == os.path.join(folder, "RemixPro")
        assert p.export_dir() is None
        assert DcsProject.find_for(os.path.join(folder, "01_PosPro")).path == path


def test_day_file_names():
    assert clraw._line_from_name("20261014_012_003.clraw") == 12
    info = clraw.file_info("/nowhere/20261014_012_003.clraw")
    assert info["line"] == 12 and info["kind"] == "line" and info["capture"] == "20261014_012"
    assert clraw.file_info("/nowhere/nav_20261014_0002.clraw")["kind"] == clraw.NAVIGATION
    # the older names still read
    assert clraw._line_from_name("Greenstone_L007_20261014_120000_0001.clraw") == 7


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
