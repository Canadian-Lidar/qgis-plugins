"""Zstandard decompression: DCS Core's (dcs_core.zstd)."""
from dcs_core.zstd import ZstdUnavailable, backend, decompress  # noqa: F401

__all__ = ["ZstdUnavailable", "backend", "decompress"]
