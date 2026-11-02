"""The DCS look now lives in DCS Core (dcs_core/theme.py), shared by PosPro,
AlignPro and CloudPro. This module is that same module, so existing
`from . import theme` code keeps working and a fix there reaches every
plugin."""
import sys

from dcs_core import theme as _shared

sys.modules[__name__] = _shared
