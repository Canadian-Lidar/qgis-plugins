"""Recording holes count only while the survey lines were recorded (PosPro 2.10.0): the system off between lines is
reported as information, not as holes."""
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from dcs_core import sbet as dsbet  # noqa: E402
from PosPro.core import pipeline  # noqa: E402

WEEK = 2440                # 2026-10
T0 = 3 * 86400 + 12 * 3600.0        # Wednesday 12:00 GPS (11:59:42 UTC)


def write_sbet(path, pieces, rate=200.0):
    t = np.concatenate([np.arange(a, b, 1.0 / rate) for a, b in pieces])
    a = np.zeros(len(t), dsbet.SBET)
    a["t"] = t
    a.tofile(path)


class TestRecordingHoles(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.sb = os.path.join(self.d, "trajectory_sbet.out")

    def test_off_between_lines_is_not_a_hole(self):
        # line 1 12:00-12:10, system off 12:10-13:40, line 2 13:40-13:50
        write_sbet(self.sb, [(T0, T0 + 600), (T0 + 6000, T0 + 6600)])
        lines = [(T0 + 30, T0 + 570), (T0 + 6030, T0 + 6570)]
        whole = pipeline.recording_holes(self.sb)
        self.assertTrue(dsbet.holes_matter(whole))                 # the whole-span view calls it a hole
        rh = pipeline.recording_holes(self.sb, lines, WEEK, 18)
        self.assertEqual(rh["scope"], "lines")
        self.assertEqual(rh["count"], 0)
        self.assertFalse(dsbet.holes_matter(rh))
        self.assertEqual(len(rh["off"]), 1)
        self.assertEqual(rh["off_utc"], ["12:09-13:39"])          # UTC (18 s behind GPS)

    def test_a_hole_inside_a_line_counts(self):
        write_sbet(self.sb, [(T0, T0 + 300), (T0 + 305, T0 + 600)])
        rh = pipeline.recording_holes(self.sb, [(T0 + 30, T0 + 570)], WEEK, 18)
        self.assertEqual(rh["count"], 1)
        self.assertAlmostEqual(rh["longest_s"], 5.0, delta=0.01)
        self.assertTrue(dsbet.holes_matter(rh))
        self.assertEqual(rh["off"], [])

    def test_without_lines_the_whole_trajectory(self):
        write_sbet(self.sb, [(T0, T0 + 300), (T0 + 305, T0 + 600)])
        rh = pipeline.recording_holes(self.sb, [])
        self.assertEqual(rh["scope"], "trajectory")
        self.assertEqual(rh["count"], 1)


if __name__ == "__main__":
    unittest.main()
