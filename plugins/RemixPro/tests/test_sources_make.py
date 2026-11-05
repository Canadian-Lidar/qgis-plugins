"""Finding recordings in every form the mapper hands them over, and making the outputs.

On a simulated mapper 2.10 project (sim210.py, written with the mapper's own .clraw writer):
  - the project folder, a USB copy (hidden .mapper-mirror.json, *.partial, no .pcap, a line still being
    copied), a line download and a project download (renamed safety copies), single files;
  - outputs made once, not again while the inputs are unchanged, again when they change; Cancel leaves
    nothing behind; the inputs are never touched;
  - the end-to-end checks the mapper ran on its exports (kit tests/e2e_from_mapper_2.10.py): one IMR record
    per IMU sample, IMR time tags always increase, IMR Z reads +1 g, the IMR header lever arm equals
    (1, -495, 500, 1200) for lever (0.5, -0.495, -1.2), the $qimu sample count matches.
"""
import glob
import hashlib
import json
import os
import shutil
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", ".."))

import sim210                                                              # noqa: E402
from RemixPro.core import inertial, make, sources                          # noqa: E402
from RemixPro.core.sources import BEAMWORX, IE, LINE, LOG, NAVIGATION, QINERTIA   # noqa: E402


def tree_hash(folder):
    h = {}
    for root, _d, files in os.walk(folder):
        for f in files:
            p = os.path.join(root, f)
            with open(p, "rb") as fh:
                h[os.path.relpath(p, folder)] = hashlib.sha256(fh.read()).hexdigest()
    return h


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.sim = sim210.make_project(os.path.join(cls.tmp, "unit"))
        cls.project = cls.sim["project"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)


class TestSources(Base):
    def test_project_folder(self):
        items = sources.scan([self.project])
        kinds = [(i.kind, i.name) for i in items]
        self.assertEqual([k for k, _n in kinds], [LINE, NAVIGATION, LINE, LINE])          # recording order
        self.assertEqual(kinds[0][1], "20261104_123012_old")                                # older capture in lines/
        l1 = items[2]
        self.assertEqual(l1.name, os.path.basename(self.sim["lines"][0][0]))
        self.assertEqual(len(l1.clraw), 3)                                                  # one line, three files
        self.assertEqual(l1.line, 1)
        self.assertEqual(l1.outputs, [BEAMWORX, IE, QINERTIA])
        self.assertEqual([os.path.basename(p) for p in l1.anpp], ["ins_0001.anpp"])
        self.assertTrue(l1.ephemeris.endswith("ephemeris_cache.anpp"))
        self.assertEqual(l1.lever_primary, sim210.PRIMARY)
        self.assertEqual(l1.lever_secondary, sim210.SECONDARY)
        self.assertEqual(l1.project, "Deck_Test")
        nav = items[1]
        self.assertEqual(nav.outputs, [IE, QINERTIA])                                     # never BeamworX
        self.assertTrue(nav.ephemeris.endswith("_ephemeris.anpp"))
        self.assertEqual(nav.lever_primary, sim210.PRIMARY)
        # any folder inside the project finds the same lines
        self.assertEqual(len(sources.scan([os.path.join(self.project, "lines")])), 3)
        self.assertEqual([i.kind for i in sources.scan([os.path.join(self.project, "nav")])], [NAVIGATION])
        self.assertEqual(sources.default_output_root([os.path.join(self.project, "lines")]),
                         os.path.join(self.project, "Processing", "RemixPro"))

    def test_usb_copy(self):
        usb = os.path.join(self.tmp, "usb", "Mapper captures")
        dst = os.path.join(usb, "Deck_Test")
        shutil.copytree(self.project, dst, ignore=shutil.ignore_patterns("Processing"))
        for root, _d, _f in os.walk(dst):
            with open(os.path.join(root, ".mapper-mirror.json"), "w") as f:
                json.dump({}, f)
        l2 = os.path.join(dst, "lines", os.path.basename(self.sim["lines"][1][0]))
        os.remove(os.path.join(l2, "continuity.json"))
        with open(os.path.join(l2, "clraw", os.path.basename(self.sim["lines"][1][1][0]) + ".partial"), "wb") as f:
            f.write(b"\x89MCAP0\r\n")
        items = sources.scan([os.path.join(self.tmp, "usb")])
        self.assertEqual(len(items), 4)
        by = {i.name: i for i in items}
        it = by[os.path.basename(l2)]
        self.assertFalse(it.complete)
        self.assertTrue(any("continuity.json" in n for n in it.notes))
        self.assertTrue(by[os.path.basename(self.sim["lines"][0][0])].complete)
        self.assertFalse(any(p.endswith(".partial") for i in items for p in i.clraw))

    def test_line_download_renamed_safety_copies(self):
        sdir = self.sim["lines"][0][0]
        lid = os.path.basename(sdir)
        dl = os.path.join(self.tmp, "dl", lid)
        shutil.copytree(sdir, dl)
        os.rename(os.path.join(dl, "ins", "ins_0001.anpp"), os.path.join(dl, "ins", lid + "_ins_0001.anpp"))
        os.rename(os.path.join(dl, "ins", "ephemeris_cache.anpp"), os.path.join(dl, "ins", lid + "_ephemeris.anpp"))
        items = sources.scan([os.path.join(self.tmp, "dl")])
        self.assertEqual(len(items), 1)
        self.assertEqual([os.path.basename(p) for p in items[0].anpp], [lid + "_ins_0001.anpp"])
        self.assertTrue(items[0].ephemeris.endswith(lid + "_ephemeris.anpp"))
        self.assertEqual(sources.default_output_root([os.path.join(self.tmp, "dl")]),
                         os.path.join(self.tmp, "dl", "RemixPro"))

    def test_project_download(self):
        dl = os.path.join(self.tmp, "pdl")
        shutil.copytree(self.project, os.path.join(dl, "Deck_Test"), ignore=shutil.ignore_patterns("Processing"))
        self.assertEqual(len(sources.scan([dl])), 4)

    def test_single_files(self):
        sdir, files = self.sim["lines"][0]
        items = sources.scan([files[1]])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, LINE)
        self.assertEqual(len(items[0].clraw), 1)
        nav = glob.glob(os.path.join(self.project, "nav", "*_0001.anpp"))[0]
        d = os.path.join(self.tmp, "single")
        os.makedirs(d)
        shutil.copy(nav, d)
        items = sources.scan([os.path.join(d, os.path.basename(nav))])
        self.assertEqual([(i.kind, i.outputs) for i in items], [(NAVIGATION, [IE, QINERTIA])])
        self.assertIsNone(items[0].lever_primary)
        self.assertTrue(any("ephemeris" in n for n in items[0].notes))
        # a capture from before 2.8: only the Certus log
        old = os.path.join(self.tmp, "old27", "20250601_101010_x")
        os.makedirs(os.path.join(old, "ins"))
        shutil.copy(os.path.join(sdir, "ins", "ins_0001.anpp"), os.path.join(old, "ins"))
        with open(os.path.join(old, "session.json"), "w") as f:
            json.dump({"settings": {"ins_to_primary_antenna_m": [0.1, 0.2, 0.3]}}, f)
        items = sources.scan([os.path.join(self.tmp, "old27")])
        self.assertEqual([(i.kind, i.name, i.lever_primary) for i in items], [(LOG, "20250601_101010_x", [0.1, 0.2, 0.3])])


class TestMake(Base):
    def test_make_once_then_current_then_again(self):
        before = tree_hash(self.project)
        out = os.path.join(self.tmp, "out1")
        items = sources.scan([self.project])
        for it in items:
            r = make.run(it, it.outputs, out)
            self.assertTrue(all(v["state"] == "made" for v in r.values()))
            for kind in it.outputs:
                self.assertEqual(make.state(out, it, kind), "current")
        self.assertEqual(tree_hash(self.project), before)                      # inputs untouched
        r = make.run(items[2], items[2].outputs, out)
        self.assertTrue(all(v["state"] == "current" for v in r.values()))
        # the line's Certus log changes (copied again in full): IE and Qinertia are made again, BeamworX not
        log = items[2].anpp[0]
        save = log + ".save"
        shutil.copy(log, save)
        try:
            with open(log, "ab") as f:
                f.write(b"\x00" * 10)
            self.assertEqual(make.state(out, items[2], IE), "out of date")
            r = make.run(items[2], items[2].outputs, out)
            self.assertEqual({k: v["state"] for k, v in r.items()}, {BEAMWORX: "current", IE: "made", QINERTIA: "made"})
        finally:
            os.replace(save, log)
        self.assertFalse(glob.glob(os.path.join(out, "*", "*.making")) + glob.glob(os.path.join(out, "*", "*.old")))

    def test_cancel_leaves_nothing(self):
        out = os.path.join(self.tmp, "out2")
        it = sources.scan([self.sim["lines"][0][0]])[0]
        calls = {"n": 0}

        def cancelled():
            calls["n"] += 1
            return calls["n"] > 1
        with self.assertRaises(make.Cancelled):
            make.run(it, it.outputs, out, progress=lambda f: None, cancelled=cancelled)
        left = [os.path.relpath(os.path.join(r, f), out) for r, _d, fs in os.walk(out) for f in fs] if os.path.isdir(out) else []
        self.assertEqual(left, [])

    def test_never_writes_into_the_recording(self):
        it = sources.scan([self.sim["lines"][1][0]])[0]
        with self.assertRaises(ValueError):
            make.run(it, [IE], os.path.join(self.sim["lines"][1][0], "ins"))

    def test_e2e_checks(self):
        """The checks the mapper's end-to-end test ran on its exports."""
        out = os.path.join(self.tmp, "out3")
        it = sources.scan([self.sim["lines"][0][0]])[0]
        it.lever_primary = [0.5, -0.495, -1.2]
        r = make.run(it, [IE, QINERTIA], out)
        imr = open(os.path.join(r[IE]["folder"], it.name + ".imr"), "rb").read()
        nrec = (len(imr) - 512) // 32
        s = json.load(open(os.path.join(r[IE]["folder"], make.NOTE)))["summary"]
        self.assertEqual(nrec, s["imu"]["samples"])                                     # one record per sample
        ts = [struct.unpack_from("<d", imr, 512 + 32 * k)[0] for k in range(nrec)]
        self.assertTrue(all(b > a for a, b in zip(ts, ts[1:])))                         # always increasing
        az = sum(struct.unpack_from("<i", imr, 512 + 32 * k + 28)[0] for k in range(0, nrec, 50)) / len(range(0, nrec, 50)) * 1e-6
        self.assertAlmostEqual(az, 9.81, delta=0.05)                                    # Z up reads +1 g
        hdr = inertial.IMR_HEADER.unpack(imr[:512])
        self.assertEqual(hdr[15:19], (1, -495, 500, 1200))                              # lever arm in IE axes, mm
        q = open(os.path.join(r[QINERTIA]["folder"], it.name + "_imu.qimu")).read().splitlines()
        self.assertEqual(q[0], "$qimu")
        self.assertEqual(len(q) - 5, nrec)                                              # same samples
        note = open(os.path.join(r[IE]["folder"], "README_INERTIAL_EXPLORER.txt")).read()
        self.assertIn("-0.495, +0.500, +1.200", note)                                   # IE axes
        self.assertIn("+0.500, -0.495, -1.200", note)                                   # vehicle axes
        self.assertIn("RemixPro", note)
        self.assertTrue(os.path.isfile(os.path.join(r[IE]["folder"], it.name + "_rx2.obs")))
        nav = open(os.path.join(r[IE]["folder"], it.name + ".nav")).read()
        self.assertGreaterEqual(nav.count("\nG"), 5)                                    # cache + live ephemerides
        qn = open(os.path.join(r[QINERTIA]["folder"], "README_QINERTIA.txt")).read()
        self.assertIn("secondary antenna  +0.082, -0.493, +0.112", qn)


if __name__ == "__main__":
    unittest.main()
