"""NAD83(CSRS) epoch moves with NRCan's velocity grid (needs pyproj; skipped without it)."""
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from dcs_core import epochs  # noqa: E402

pyproj = pytest.importorskip("pyproj")


def test_parse_and_defaults():
    assert epochs.parse("1997.0") == 1997.0 and epochs.parse("2010") == 2010.0
    assert abs(epochs.parse("1997-01-01") - 1997.0) < 1e-9
    assert abs(epochs.parse("26:307:43200") - 2026.8397) < 1e-3          # CSRS-PPP YY:DDD:SSSSS
    assert epochs.parse("CURR") is None and epochs.parse("") is None and epochs.parse("observed") is None
    assert epochs.default_epoch("EPSG:2953") == 1997.0                   # New Brunswick Stereographic
    assert epochs.default_epoch("EPSG:2961") == 2010.0                   # UTM 20N: federal
    assert epochs.is_nad83csrs("NAD83(CSRS)v8") and not epochs.is_nad83csrs("ITRF2020")


def test_fredericton_2026_to_1997():
    x, y, z = epochs.propagate([-66.6447], [45.9583], [21.0], 2026.84, 1997.0)
    dn = (y[0] - 45.9583) * 111132
    de = (x[0] + 66.6447) * 111320 * math.cos(math.radians(45.96))
    du = z[0] - 21.0
    # NRCan NAD83v70VG: a few mm/yr in New Brunswick; ~30 years
    assert 0.03 < math.hypot(dn, de) < 0.15 and abs(du) < 0.05, (dn, de, du)
    back = epochs.propagate(x, y, z, 1997.0, 2026.84)
    assert abs(back[0][0] + 66.6447) < 1e-9 and abs(back[1][0] - 45.9583) < 1e-9 and abs(back[2][0] - 21.0) < 1e-4
    same = epochs.propagate([-66.6447], [45.9583], [21.0], 2010.0, 2010.0)
    assert same[1][0] == 45.9583


def test_outside_canada_refused():
    with pytest.raises(epochs.EpochError):
        epochs.propagate([10.0], [50.0], [0.0], 2026.0, 1997.0)
