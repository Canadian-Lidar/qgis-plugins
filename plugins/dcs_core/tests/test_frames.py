"""dcs_core.frames: the canonical frame and each device adapter, and that every plugin uses the same ones."""
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from dcs_core import frames as F       # noqa: E402

RNG = np.random.default_rng(7)
ANGLES = [(0, 0, 0), (90, 45, 0), (180, 44, 180), (-3.2, 1.7, 12.5)] + \
    [tuple(RNG.uniform([-180, -80, -180], [180, 80, 180])) for _ in range(20)]


def test_hesai_zero_and_inverse():
    # all zero: the Hesai rotation axis (sensor z) is forward, azimuth 0 (sensor y) points up (connector down)
    M = F.hesai_mount_matrix(0, 0, 0)
    assert np.allclose(M @ [0, 0, 1], [1, 0, 0])
    assert np.allclose(M @ [0, 1, 0], [0, 0, -1])
    for a in ANGLES:
        M = F.hesai_mount_matrix(*a)
        assert np.allclose(M @ M.T, np.eye(3))
        assert np.allclose(F.hesai_mount_matrix(*F.hesai_mount_angles(M)), M, atol=1e-9)


def test_every_plugin_uses_the_same_hesai_convention():
    from AlignPro.core import geo
    from CloudPro.core import georef
    from StripPro.core import job
    from RemixPro.core import surveyconfig
    for a in ANGLES:
        M = F.hesai_mount_matrix(*a)
        assert np.allclose(geo.lidar_matrix(*a), M)
        assert np.allclose(georef.lidar_matrix(*a), M)
        assert np.allclose(job.lidar_matrix(*a), M)
        assert np.allclose(np.array(surveyconfig.lidar_matrix_from_navaq(*a)), M, atol=1e-12)


def test_sonarview_mount_is_frd():
    # Cerulean: +X forward, +Y right (starboard), +Z down = canonical
    assert F.sonarview_mount_to_frd([-0.122, 0.056, 0.564]) == [-0.122, 0.056, 0.564]
    assert F.sonarview_mount_to_frd([0, 0, 0]) is None
    assert F.sonarview_mount_to_frd(None) is None
    r = F.adapter_record(*F.SONARVIEW_MOUNT, raw=[1, 2, 3])
    assert r == {"adapter": "cerulean_sonarview_mount", "version": 1, "raw": [1, 2, 3]}


def test_cerulean_device_frame():
    # positive beam angle looks to starboard: device y is port, so the ray has negative y; in FRD positive y
    v = F.cerulean_beam(np.array([math.radians(30)]), np.array([10.0]))[0]
    assert v[1] < 0 and v[2] < 0
    frd = F.CERULEAN_TO_FRD @ v
    assert frd[1] > 0 and frd[2] > 0
    assert abs(np.linalg.norm(v) - 10.0) < 1e-9
    # 504: the up vector of a head rolled 10 deg starboard down, pitched 5 deg bow up
    r, p = math.radians(10), math.radians(5)
    up = np.array([-math.sin(p), math.cos(p) * math.sin(r), math.cos(p) * math.cos(r)])
    roll, pitch = F.cerulean_attitude(up)
    assert abs(roll - 10) < 1e-9 and abs(pitch - 5) < 1e-9
    assert F.cerulean_looks(np.radians([-30, -20, -25])) == "port"
    assert F.cerulean_looks(np.radians([20, 35])) == "starboard"


def test_beam_centre_side_and_tilt():
    import math
    # Lumston device 1: 504 up (0.018, 0.637, 0.771) -> looks port, 39.6 deg from vertical
    looks, tilt = F.cerulean_beam_centre([[0.018, 0.637, 0.771]])
    assert looks == "port" and abs(tilt - 39.56) < 0.05
    assert F.cerulean_beam_centre([[0.0, -0.5, 0.866]])[0] == "starboard"
    # steep mounting (29 deg): the beam centre still gives the side, where the beam angles alone may read "down"
    r = math.radians(29)
    assert F.cerulean_beam_centre([[0, math.sin(r), math.cos(r)]]) == ("port", F.cerulean_beam_centre(
        [[0, math.sin(r), math.cos(r)]])[1]) and abs(F.cerulean_beam_centre([[0, math.sin(r), math.cos(r)]])[1] - 29) < 1e-6
    assert F.cerulean_looks([math.radians(a) for a in (-20, 15, -3, 10, -8)]) == "down"
    assert F.cerulean_beam_centre([[0.02, 0.0, 1.0]])[0] == "down"
    assert F.cerulean_beam_centre([]) == (None, None)


def test_shared_strip():
    from dcs_core import marine
    w, meet = marine.shared_strip(1.6, 6.8, 0.152, 13.0)
    assert abs(meet - 0.152 / (math.tan(math.radians(1.6)) + math.tan(math.radians(6.8)))) < 1e-9
    assert 1.5 < w < 2.0
    assert marine.shared_strip(-4.0, 1.0, 0.152, 13.0) == (0.0, None)


def test_export_frames():
    assert F.frd_to_inertial_explorer([1.0, 2.0, 3.0]) == [2.0, 1.0, -3.0]
    assert F.frd_to_qinertia([1, 2, 3]) == [1.0, 2.0, 3.0]
    from RemixPro.core import notes
    assert notes.lever_ie([0.5, -0.2, 0.3]) == [-0.2, 0.5, -0.3]


def test_body_to_ned():
    C = F.body_to_ned(0, 0, 90)                     # heading east: forward -> east
    assert np.allclose(C @ [1, 0, 0], [0, 1, 0])
    C = F.body_to_ned(10, 0, 0)                     # roll + starboard down: starboard axis gets +down
    assert (C @ [0, 1, 0])[2] > 0
