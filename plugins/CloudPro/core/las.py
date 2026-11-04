"""LAS 1.4 (point data record format 6) for CloudPro deliverables: DCS Core's writer (dcs_core.las),
with rejected points flagged withheld."""
from dcs_core import las as _las
from dcs_core.las import GPS_UNIX, LEAP, PDRF6, read_las, utc_to_adjusted_gps  # noqa: F401

from .store import REJECTED

__all__ = ["GPS_UNIX", "LEAP", "PDRF6", "LasWriter", "read_las", "utc_to_adjusted_gps"]


class LasWriter(_las.LasWriter):
    def __init__(self, path, offset, scale=0.001, wkt="", system="CloudPro", software="CloudPro (QGIS)"):
        super().__init__(path, offset, scale, wkt, system, software)

    def write(self, E, N, Z, t, intensity, ret, cls, channel, line, flag):
        super().write(E, N, Z, t, intensity, ret, cls, channel, line, withheld=(flag & REJECTED) != 0)
