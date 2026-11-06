"""Shared set-up for the CloudPro tests: the simulated mapper 2.10 job (guides/pospro/simjob.py: two survey lines
and a navigation recording, written with the mapper's own .clraw writer), copied fresh for each test, and a DCS
processing project on it.

    python3 -m CloudPro.tests.test_rebuild_safety          (from the plugins folder)
"""
import os
import shutil
import subprocess
import sys
import tempfile

PLUGINS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PLUGINS not in sys.path:
    sys.path.insert(0, PLUGINS)

CACHE = os.environ.get("CLOUDPRO_SIMJOB") or os.path.join(tempfile.gettempdir(), "cloudpro_test_simjob")


def simjob():
    """Folder holding Shirley_Ave/ (made once, then reused)."""
    if not os.path.exists(os.path.join(CACHE, "Shirley_Ave", "project.json")):
        shutil.rmtree(CACHE, ignore_errors=True)
        os.makedirs(CACHE)
        subprocess.check_call([sys.executable, os.path.join(PLUGINS, "guides", "pospro", "simjob.py"), CACHE],
                              stdout=subprocess.DEVNULL)
    return CACHE


def fresh_job(tag):
    """A private copy of the job -> (DcsProject, CloudPro Project)."""
    from dcs_core.project import DcsProject
    from CloudPro.core.project import Project
    dst = os.path.join(tempfile.mkdtemp(prefix="cloudpro_%s_" % tag), "Shirley_Ave")
    shutil.copytree(os.path.join(simjob(), "Shirley_Ave"), dst, ignore=shutil.ignore_patterns("Processing"))
    dcs = DcsProject.create(dst, "T", plugin="test")
    return dcs, Project.from_dcs(dcs)


def publish_sbet(dcs, traj, frame=None, epoch=None):
    """Publish a dcs.trajectory/1 handover (as PosPro would) from a CloudPro Trajectory."""
    from dcs_core import handover, sbet
    od = handover.out_dir(dcs, "pospro")
    sp = os.path.join(od, "trajectory_sbet.out")
    ws = sbet.write(sp, traj.t, traj.lat, traj.lon, traj.h, traj.roll, traj.pitch, traj.heading)
    week = int(round((ws - 315964800.0) / 604800))
    return handover.publish(dcs, "pospro", {"sbet": sp}, {
        "time": {"week": week, "leap_seconds": 18},
        "frame": {"name": frame or "NAD83(CSRS)", "epoch": 2010.0 if epoch is None else epoch},
        "quality": {"mode": "test"}}, "PosPro", "test")


def realtime_track(files):
    """The Certus real-time track of every line, joined (a stand-in for a PosPro trajectory)."""
    import numpy as np
    from CloudPro.core import chain, importer
    from CloudPro.core.trajectory import Trajectory
    parts = [importer.load_trajectory(importer.clraw.Recording(p)) for p in chain.group(files).values()]
    cat = lambda f: np.concatenate([getattr(t, f) for t in parts])       # noqa: E731
    return Trajectory(cat("t"), cat("lat"), cat("lon"), cat("h"), cat("roll"), cat("pitch"), cat("heading"),
                      source="test")


def counts(project):
    """(points, rejected, classified != 1) in the current store."""
    st = project.store
    n = r = c = 0
    for k in st.keys():
        a = st.tile_array(k)
        n += len(a)
        r += int(((a["flag"] & 0x80) != 0).sum())
        c += int((a["cls"] != 1).sum())
    return n, r, c
