"""MCAP container reader for .clraw files: DCS Core's (dcs_core.mcap)."""
from dcs_core.mcap import MAGIC, McapFile, iter_messages  # noqa: F401

__all__ = ["MAGIC", "McapFile", "iter_messages"]
