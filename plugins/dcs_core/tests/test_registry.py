"""registry.focus (DCS Core 1.12.0, API 13): opens the plugin on the project, then shows the target there; a plugin
registered without a focus function is only opened; a focus function's error is logged, never raised.

    python3 dcs_core/tests/test_registry.py      (from the plugins folder)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from dcs_core import registry  # noqa: E402

got = []
registry.register("pospro", lambda p: got.append(("open", p)), lambda t: got.append(("focus", t)))
assert registry.focus("pospro", "/job/a.dcsproj", {"kind": "cloud_area", "id": "C3"})
assert got == [("open", "/job/a.dcsproj"), ("focus", {"kind": "cloud_area", "id": "C3"})], got

del got[:]
registry.register("pospro", lambda p: got.append(("open", p)))          # re-registered without focus
assert registry.focus("pospro", "/job/a.dcsproj", {"kind": "cloud_area"}) and got == [("open", "/job/a.dcsproj")]


def boom(t):
    raise RuntimeError("focus failed")


registry.register("alignpro", lambda p: None, boom)
assert registry.focus("alignpro", "/job/a.dcsproj", {"kind": "cloud_pair", "lines": ["Line 4", "Line 5"]})
registry.unregister("alignpro")
registry.unregister("pospro")
assert not registry.focus("alignpro", "/job/a.dcsproj", {}) and not registry.open("pospro", "/x")
print("OK")
