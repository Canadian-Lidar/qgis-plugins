"""AlignPro's safe docking now lives in DCS Core (dcs_core.window), shared by every DCS plugin:
the same functions and names, kept here for older code and AlignPro's tests."""
from dcs_core.window import (ALL_AREAS, DOCK_CHOICES, NO_AREAS, WINDOWED, alive, area_of,  # noqa: F401
                             dock_to, float_panel, float_window, lock, make_panel, make_window, release,
                             toggle_maximised)

__all__ = ["ALL_AREAS", "DOCK_CHOICES", "NO_AREAS", "WINDOWED", "alive", "area_of", "dock_to", "float_panel",
           "float_window", "lock", "make_panel", "make_window", "release", "toggle_maximised"]
