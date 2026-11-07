"""Import options for big jobs (CloudPro 1.8.0):

- the vehicle speed along the trajectory: zero while parked, the drive speed while moving
- "Skip points while stopped": packets fired while the vehicle stands still are left out before decoding;
  a threshold above every speed of the drive leaves no points and counts the skipped packets
- binning into tiles in the worker threads gives the same store as before (same points per tile)

    python3 -m CloudPro.tests.test_import_speed          (from the plugins folder)
"""
import os
import sys

PLUGINS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PLUGINS)

import numpy as np                                                  # noqa: E402

from CloudPro.tests import _job                                     # noqa: E402


def test_speed():
    from CloudPro.core.georef import GeorefContext
    from CloudPro.core.trajectory import Trajectory
    from CloudPro.core.geodesy import Projector
    t = np.arange(0, 60, 0.05)
    # 10 m/s north for 20 s, parked for 20 s, 10 m/s again
    d = np.where(t < 20, 10 * t, np.where(t < 40, 200.0, 200 + 10 * (t - 40)))
    lat = np.radians(45.9) + d / 6367000.0
    tr = Trajectory(t + 1.7e9, lat, np.full(len(t), np.radians(-66.6)), np.full(len(t), 20.0), np.zeros(len(t)),
                    np.zeros(len(t)), np.zeros(len(t)), source="test")
    ctx = GeorefContext(tr, Projector("EPSG:2953"), (0, 45, 180), (0, 0, 0))
    v = ctx.speed_at(np.array([10.0, 30.0, 50.0]) + 1.7e9)
    assert abs(v[0] - 10) < 0.3 and v[1] < 0.05 and abs(v[2] - 10) < 0.3, v
    print("speed along the trajectory: moving %.2f, parked %.3f, moving %.2f m/s: OK" % tuple(v))


def test_skip_stopped_and_binning():
    from CloudPro.core import chain, importer as IM
    dcs, p = _job.fresh_job("speed")
    IM.import_chain(p, chain.resolve(dcs), IM.ImportOptions())
    n_all = p.store.total
    per_tile = dict(p.store.counts)
    p.store.close()
    dcs2, p2 = _job.fresh_job("speed2")
    IM.import_chain(p2, chain.resolve(dcs2), IM.ImportOptions(min_speed=500.0))     # faster than any drive
    assert p2.store.total == 0, p2.store.total
    st = [l["stats"] for l in p2.lines]
    assert all(s["stopped_packets"] > 0 and s["stopped_packets"] == s["packets"] for s in st), st
    assert all(l["min_speed"] == 500.0 for l in p2.lines)
    p2.store.close()
    dcs3, p3 = _job.fresh_job("speed3")
    IM.import_chain(p3, chain.resolve(dcs3), IM.ImportOptions(min_speed=0.3))       # the drive never stops
    assert p3.store.total == n_all and dict(p3.store.counts) == per_tile
    print("skip stopped: all %d packets skipped above the drive speed; at 0.3 m/s the same %d points in the "
          "same tiles as before: OK" % (sum(s["packets"] for s in st), n_all))


if __name__ == "__main__":
    test_speed()
    test_skip_stopped_and_binning()
    print("test_import_speed: OK")
