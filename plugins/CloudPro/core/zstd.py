"""Zstandard decompression: DCS Core's (dcs_core.zstd), which also holds the bundled zstandard package."""
from dcs_core.zstd import ZstdUnavailable, decompress  # noqa: F401
from dcs_core.zstd import decoder as backend  # noqa: F401 - (decompress, name), as CloudPro 1.2 had it

__all__ = ["ZstdUnavailable", "backend", "decompress"]
