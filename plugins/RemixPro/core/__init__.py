"""RemixPro core: no Qt and no QGIS, so it also runs from the command line (cli.py) and in the tests.

  sources    find what can be converted in a folder or a list of files (lines, navigation recordings, logs)
  beamworx   BeamworX .bwxraw (+ survey configuration .xml) from a line's .clraw files
  inertial   Inertial Explorer IMR + RINEX and Qinertia $qimu + RINEX from the Certus stream
  notes      the setup notes written beside those files
  make       run the conversions: once, again only when the inputs changed, never touching the inputs
  bwxraw, surveyconfig   the writers, taken unchanged from the mapper (2.10.0, 4 Nov 2026)
"""
NAME = "RemixPro"
__version__ = "1.2.1"


def software():
    """What the outputs name as the program that made them (IMR header, RINEX PGM line)."""
    return "%s %s" % (NAME, __version__)
