"""StripPro core: no Qt and no QGIS (tests and the command line use it too).

  frames   geodetic <-> one ENU frame for the job; vehicle attitude in it
  strip    the solve: a correction per line, linear between knots, from point-to-plane observations
  job      read PosPro / AlignPro / the survey lines, sample, solve, write the corrected trajectory (handover)
"""
NAME = "StripPro"
__version__ = "1.0.0"
