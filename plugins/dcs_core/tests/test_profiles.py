"""System profiles (no QGIS): save / load / sections kept / match by serial / differences / export-import."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from dcs_core import profiles  # noqa: E402


def _fresh():
    d = tempfile.mkdtemp()
    profiles.set_folder(d)
    return d


def test_identity_from_capture_files():
    hw = {"certus_reported": {"device": {"serial": "00ABCDEF0000000100000002", "device_id": 22}}}
    ses = {"unit": {"name": "mapper-1", "serial": "NC100XTLM0006", "model": "mapper"}}
    assert profiles.identity(hw, ses) == {"unit_serial": "NC100XTLM0006", "unit_name": "mapper-1",
                                          "certus_serial": "00ABCDEF0000000100000002"}
    assert profiles.identity({}, {}) == {}
    assert profiles.identity({"certus_reported": {}}, None) == {}


def test_save_sections_and_match():
    _fresh()
    ident = {"unit_serial": "NC100XTLM0006", "certus_serial": "C1"}
    p = profiles.new("Van 1", ident)
    profiles.update_sections(p, {"antennas": {"gnss1_m": [0.082, 0.495, 0.112], "gnss2_m": [0.082, -0.493, 0.112]}},
                             by="PosPro 2.0.2")
    profiles.save(p)
    # AlignPro adds the Lidar section: the antennas stay
    q = profiles.load("Van 1")
    profiles.update_sections(q, {"lidar": {"lever_m": [-0.3, 0.0, -0.4], "mount_deg": [-90.0, 45.0, 180.0],
                                           "latency_ms": 0.0}}, by="AlignPro 1.6.0")
    profiles.save(q)
    r = profiles.load("Van 1")
    assert set(r["sections"]) == {"antennas", "lidar"}
    assert r["sections"]["antennas"]["by"] == "PosPro 2.0.2" and r["sections"]["lidar"]["by"] == "AlignPro 1.6.0"
    assert len(profiles.list_profiles()) == 1
    # match: unit serial first, then Certus serial; nothing for another system
    assert profiles.match({"unit_serial": "NC100XTLM0006"})["name"] == "Van 1"
    assert profiles.match({"certus_serial": "C1"})["name"] == "Van 1"
    assert profiles.match({"unit_serial": "OTHER", "certus_serial": "C9"}) is None
    assert profiles.match({}) is None


def test_differences_use_display_precision():
    _fresh()
    p = profiles.new("V", {})
    profiles.update_sections(p, {"lidar": {"lever_m": [-0.3, 0.0, -0.4], "mount_deg": [-90.0, 45.0, 180.0],
                                           "latency_ms": 0.0}})
    same = {"lidar": {"lever_m": [-0.30001, 0.0, -0.4], "mount_deg": [-90.0, 45.00001, 180.0], "latency_ms": 0.0}}
    assert profiles.differences(p, same) == []
    other = {"lidar": {"lever_m": [-0.3, 0.0, -0.4], "mount_deg": [90.0, 45.0, 0.0], "latency_ms": 0.0}}
    d = profiles.differences(p, other)
    assert [x[1] for x in d] == ["mount_deg"]
    assert profiles.differences(p, {"antennas": {"gnss1_m": [1, 2, 3]}}) == []   # section not in the profile


def test_export_import_and_names():
    d = _fresh()
    p = profiles.new("Van 1 / roof", {"unit_serial": "U1"})
    profiles.update_sections(p, {"imu": {"preset": "certus_evo"}})
    path = profiles.save(p)
    assert os.path.dirname(path) == d and "/" not in os.path.basename(path)[:-5]
    out = os.path.join(tempfile.mkdtemp(), "x.json")
    profiles.export("Van 1 / roof", out)
    assert json.load(open(out))["schema"] == profiles.SCHEMA
    q = profiles.import_file(out)                    # same name: kept, imported as "(2)"
    assert q["name"] == "Van 1 / roof (2)"
    assert len(profiles.list_profiles()) == 2
    profiles.import_file(out, replace=True)
    assert len(profiles.list_profiles()) == 2
    assert profiles.delete("Van 1 / roof (2)") and len(profiles.list_profiles()) == 1
    bad = os.path.join(tempfile.mkdtemp(), "bad.json")
    json.dump({"name": "x"}, open(bad, "w"))
    try:
        profiles.import_file(bad)
        raise AssertionError("a non-profile file was imported")
    except ValueError:
        pass


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(name, "ok")
