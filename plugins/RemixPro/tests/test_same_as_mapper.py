"""RemixPro makes the same files the mapper made, byte for byte.

Runs the mapper's own conversion code (the handover kit, remixpro-handover-kit.zip: mapper_converters/) and
RemixPro on the same simulated mapper 2.10 recordings and compares every output file. The only differences
allowed are the ones RemixPro makes on purpose: the program name in the IMR header and the RINEX
"PGM / RUN BY / DATE" line (RemixPro instead of mapper, and the date the file was made), and the random file
ID in each .bwxraw header (fixed to the same value here).

Needs the kit unzipped: REMIXPRO_KIT=<folder holding mapper_converters/> (default: ../../../../kit/remixpro-kit
or /home/claude/kit/remixpro-kit). Skipped when it is not there.

    python -m unittest tests.test_same_as_mapper      (from plugins/RemixPro/..)
"""
import importlib
import itertools
import os
import shutil
import sys
import tempfile
import types
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", ".."))

import sim210                                                  # noqa: E402
from RemixPro.core import beamworx, inertial                   # noqa: E402

KIT = next((p for p in (os.environ.get("REMIXPRO_KIT"), os.path.join(HERE, "..", "..", "..", "..", "kit",
                                                                       "remixpro-kit"),
                        "/home/claude/kit/remixpro-kit") if p and os.path.isdir(os.path.join(p, "mapper_converters"))),
           None)


def load_kit(tmp):
    """The kit's mapper_converters as an importable package called mapper_kit (it imports .. for __version__)."""
    pkg = os.path.join(tmp, "mapper_kit")
    shutil.copytree(os.path.join(KIT, "mapper_converters"), pkg)
    with open(os.path.join(pkg, "__init__.py"), "w") as f:
        f.write('__version__ = "2.10.0"\n')
    with open(os.path.join(pkg, "export", "__init__.py"), "w") as f:
        f.write("")
    sys.path.insert(0, tmp)
    for m in [m for m in sys.modules if m.startswith("mapper_kit")]:
        del sys.modules[m]
    return (importlib.import_module("mapper_kit.clraw"), importlib.import_module("mapper_kit.export.convert"))


def fixed_ids():
    c = itertools.count(1)
    return lambda: types.SimpleNamespace(bytes=(b"%016d" % next(c)))


def imr_masked(b):
    return b[:101] + b"\x00" * 32 + b[133:]       # program name (32 bytes at 101) differs on purpose


def rinex_masked(text):
    return [l for l in text.splitlines() if "PGM / RUN BY / DATE" not in l]


@unittest.skipUnless(KIT, "the RemixPro handover kit is not unzipped here (set REMIXPRO_KIT)")
class TestSameAsMapper(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.kit_clraw, cls.kit_convert = load_kit(cls.tmp)
        cls.sim = sim210.make_project(os.path.join(cls.tmp, "data"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _bwx_both(self, paths, name):
        a, b = os.path.join(self.tmp, name, "mapper"), os.path.join(self.tmp, name, "remixpro")
        real = uuid.uuid4
        try:
            uuid.uuid4 = fixed_ids()
            made_a = self.kit_clraw.to_bwxraw(paths, a)
            uuid.uuid4 = fixed_ids()
            made_b = beamworx.to_bwxraw(paths, b)["files"]
        finally:
            uuid.uuid4 = real
        self.assertEqual([os.path.basename(p) for p in made_a], [os.path.basename(p) for p in made_b])
        self.assertTrue(made_a)
        for pa, pb in zip(made_a, made_b):
            for ext in (".bwxraw", ".xml"):
                fa, fb = pa[:-7] + ext, pb[:-7] + ext
                with open(fa, "rb") as x, open(fb, "rb") as y:
                    self.assertEqual(x.read(), y.read(), os.path.basename(fb))
        return made_b

    def test_beamworx_lines(self):
        for i, (sdir, files) in enumerate(self.sim["lines"] + [self.sim["old"]]):
            made = self._bwx_both(files, "bwx%d" % i)
            self.assertEqual(len(made), len(files))

    def test_beamworx_damaged_chunk(self):
        sdir, files = self.sim["lines"][1]
        p = os.path.join(self.tmp, "damaged.clraw")
        shutil.copyfile(files[0], p)
        offs = []
        self.kit_clraw.scan(p, on_record=lambda op, off, body: offs.append(off) if op == 0x06 else None)
        with open(p, "r+b") as f:
            f.seek(offs[2] + 300)
            f.write(b"\x55" * 32)
        os.rename(p, os.path.join(self.tmp, "Deck_Test_L002_20261104_124612_0001.clraw"))
        self._bwx_both([os.path.join(self.tmp, "Deck_Test_L002_20261104_124612_0001.clraw")], "damaged")

    def _convert_both(self, name, anpp_paths, eph, lever, clraw_paths=None):
        a, b = os.path.join(self.tmp, name, "mapper"), os.path.join(self.tmp, name, "remixpro")
        sa = self.kit_convert.convert(anpp_paths, a, name, lever_arm_vehicle_m=lever,
                                      ephemeris_paths=[eph] if eph else [], want_track=False)
        if clraw_paths:
            sb = inertial.convert([], b, name, lever_arm_vehicle_m=lever, ephemeris_paths=[eph] if eph else [],
                                  want_track=False, clraw_paths=clraw_paths)
        else:
            sb = inertial.convert(anpp_paths, b, name, lever_arm_vehicle_m=lever,
                                  ephemeris_paths=[eph] if eph else [], want_track=False)
        self.assertEqual(sorted(sa["files"].values()), sorted(sb["files"].values()))
        for f in sa["files"].values():
            with open(os.path.join(a, f), "rb") as x, open(os.path.join(b, f), "rb") as y:
                da, db = x.read(), y.read()
            if f.endswith(".imr"):
                self.assertEqual(imr_masked(da), imr_masked(db), f)
                self.assertTrue(db[101:133].startswith(b"RemixPro "))
            elif f.endswith((".obs", ".nav")):
                self.assertEqual(rinex_masked(da.decode()), rinex_masked(db.decode()), f)
                self.assertIn("RemixPro", db.decode().splitlines()[1])
            else:
                self.assertEqual(da, db, f)
        for k in ("imu", "rinex_obs", "rinex_nav"):
            self.assertEqual(sa.get(k), sb.get(k), k)
        return sb

    def test_inertial_explorer_and_qinertia_from_the_log(self):
        for i, (sdir, _files) in enumerate(self.sim["lines"]):
            s = self._convert_both("line%d" % i, [os.path.join(sdir, "ins", "ins_0001.anpp")],
                                   os.path.join(sdir, "ins", "ephemeris_cache.anpp"), sim210.PRIMARY)
            self.assertGreater(s["imu"]["samples"], 1000)
            self.assertEqual(len(s["rinex_obs"]), 2)
        nd = os.path.join(self.sim["project"], "nav")
        pre = self.sim["nav"]
        s = self._convert_both("nav", [os.path.join(nd, pre + "_0001.anpp")], os.path.join(nd, pre + "_ephemeris.anpp"),
                               None)
        self.assertEqual(s["imu"]["dropped_before_time_valid"], 40)

    def test_from_the_clraw_channel(self):
        """No .anpp: the /ins/anpp channel of the .clraw gives what the mapper makes from the same packets."""
        sdir, files = self.sim["lines"][0]
        joined = os.path.join(self.tmp, "joined.anpp")
        with open(joined, "wb") as f:
            for p in sorted(files):
                for _ch, _seq, _lt, _pt, data in self.kit_clraw.messages(p, channels={3}):
                    f.write(data)
        s = self._convert_both("fromclraw", [joined], None, sim210.PRIMARY, clraw_paths=files)
        self.assertEqual(s["source_kind"], "clraw")


if __name__ == "__main__":
    unittest.main()
