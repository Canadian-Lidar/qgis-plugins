"""Side scan (2.10.0) on a simulated pass: a boat running north at 2 m/s, 10 m above a flat seabed, a port and a
starboard head, and a 1 m high box on the port side 20 m out with its acoustic shadow behind it.
- the gain levelling flattens the range trend;
- the mosaic puts the target where it is (to a cell) and closes the gaps between pings;
- a contact boxed round the target gets its position, length and a height from its shadow;
- XTF: written and read back by an independent reader (pyxtf, when installed): channels, samples, navigation.

    python3 CloudPro/tests/test_sidescan.py      (from the plugins folder)
"""
import math
import os
import shutil
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from CloudPro.core import sidescan as SS                        # noqa: E402

ALT = 10.0
NP, NS = 600, 1000
R0, R1 = 5.0, 60.0
TARGET = (20.0, 50.0, 1.0, 2.0)          # ground range, northing, height, length along track


class _Store:
    origin = np.array([500000.0, 5000000.0, 0.0])


class FakeProject:
    def __init__(self, d):
        self.work = d
        self.root = d
        self.crs = "EPSG:32620"
        self.store = _Store()
        self.data = {}
        self.lines = [{"id": 0, "name": "pass 1 port", "kind": "sonar", "head": 1, "pass": 1, "files": ["a"]},
                      {"id": 1, "name": "pass 1 starboard", "kind": "sonar", "head": 2, "pass": 1, "files": ["a"]}]


def simulate(p):
    rng = np.random.default_rng(3)
    r = R0 + (np.arange(NS) + 0.5) * (R1 - R0) / NS
    t = 1.79e9 + np.arange(NP) * 0.25
    north = np.arange(NP) * 0.5
    knots = np.unique(np.r_[np.arange(0, NS, SS.KNOT), NS - 1])
    for lid, side in ((0, 1.0), (1, -1.0)):
        gr = np.sqrt(np.maximum(r * r - ALT * ALT, 0))
        valid = r > ALT
        db = 30.0 - 25.0 * np.log10(r)[None, :] + rng.normal(0, 2.0, (NP, NS))    # range loss + speckle
        db[:, ~valid] = rng.normal(-30, 1, (NP, (~valid).sum()))
        if side > 0:                                                                 # the target and its shadow
            g0, n0, h, ln = TARGET
            on = (np.abs(north - n0) <= ln / 2)[:, None]
            r_t0 = math.hypot(g0, ALT)
            r_t1 = math.hypot(g0 + 0.5, ALT - h)                  # far top edge
            shadow_end = math.hypot((g0 + 0.5) * ALT / (ALT - h), ALT)   # where the shadow line meets the bed
            hit = (r >= r_t0) & (r <= r_t1 + 0.3)
            sh = (r > r_t1 + 0.3) & (r <= shadow_end)
            db = np.where(on & hit[None, :], db + 25.0, db)
            db = np.where(on & sh[None, :], -45.0 + rng.normal(0, 1, (NP, NS)), db)
        E = np.full((NP, len(knots)), np.nan, np.float32)
        N = np.repeat(north[:, None], len(knots), 1).astype(np.float32)
        Z = np.full((NP, len(knots)), -ALT, np.float32)
        gk = gr[knots]
        okk = valid[knots]
        E[:, okk] = -side * gk[okk]                            # port = west of a boat heading north
        N[:, ~okk] = np.nan
        ang = np.where(okk, np.degrees(np.arccos(np.clip(ALT / r[knots], -1, 1))), np.nan)
        ang = np.repeat(ang[None, :], NP, 0).astype(np.float16)
        nav = np.column_stack([np.full(NP, 45.1) + north / 111111.0, np.full(NP, -63.0), np.zeros(NP),
                               np.zeros(NP), np.zeros(NP)])
        np.savez(SS.line_file(p, lid), version=SS.VERSION, georef=SS._georef_stamp(p), t=t,
                 ping=np.arange(NP) + 100, db=db.astype(np.float16), r=r.astype(np.float32), knots=knots,
                 E=E, N=N, Z=Z, ang=ang, alt=np.full(NP, ALT, np.float32), side=side, head=lid + 1,
                 raw_lo=np.zeros(NP), raw_hi=np.zeros(NP), hE=np.zeros(NP, np.float32),
                 hN=north.astype(np.float32), hZ=np.zeros(NP, np.float32), nav=nav, sos=1500.0)
    return r, north


def test_sidescan():
    d = tempfile.mkdtemp()
    try:
        p = FakeProject(d)
        r, north = simulate(p)
        assert SS.processed(p, 0) and SS.processed(p, 1)
        assert len(SS.passes(p)) == 1
        L = SS.LineSS(p, 1)
        lv = L.levelled()
        with np.errstate(invalid="ignore"):
            import warnings
            warnings.simplefilter("ignore", RuntimeWarning)
            prof = np.nanmean(lv, 0)
        ok = np.isfinite(prof)
        trend = np.polyfit(r[ok], prof[ok], 1)[0]
        assert abs(trend) < 0.02, trend                                   # was -25 log10(r): flattened
        # mosaic: the target (bright) at E = -20.25, N = 50
        G, x0, y1, cell = SS.mosaic(p, [0, 1], cell=0.25)
        assert np.isfinite(G).mean() > 0.6
        iy, ix = np.unravel_index(np.nanargmax(np.where(np.isfinite(G), G, -1e9)), G.shape)
        e, n = x0 + (ix + 0.5) * cell, y1 - (iy + 0.5) * cell
        assert abs(e - (-TARGET[0] - 0.25)) < 1.0 and abs(n - TARGET[1]) < 1.5, (e, n)
        # gaps: no empty rows inside the swath at 0.25 m cells with 0.5 m between pings
        row = G[:, ix]
        inside = np.flatnonzero(np.isfinite(row))
        assert np.isfinite(row[inside[0]:inside[-1] + 1]).mean() > 0.99
        G0, *_ = SS.mosaic(p, [0, 1], cell=0.25, fill=0)
        assert np.isfinite(G0[:, ix][inside[0]:inside[-1] + 1]).mean() < 0.8      # without filling: lines
        # contact: box round the target in the port waterfall
        Lp = SS.LineSS(p, 0)
        i0, i1 = int(np.searchsorted(north, 49.0)), int(np.searchsorted(north, 51.0))
        s0 = int(np.searchsorted(r, math.hypot(TARGET[0], ALT))) - 1
        s1 = int(np.searchsorted(r, math.hypot(TARGET[0] + 0.5, ALT - TARGET[2]))) + 3
        c = SS.add_contact(p, 0, i0, i1, s0, s1)
        assert abs(c["e"] - p.store.origin[0] + 20.3) < 0.6 and abs(c["n"] - p.store.origin[1] - 50.0) < 0.6, c
        assert 1.5 <= c["length_m"] <= 2.5, c
        assert 0.7 <= c["height_m"] <= 1.4, c                              # 1 m box, from its shadow
        assert SS.find_shadow(Lp, 10, 40, s1) is None                      # no target there: no shadow
        SS.export_contacts_csv(p, os.path.join(d, "c.csv"))
        assert open(os.path.join(d, "c.csv")).read().count("\n") == 2
        # palettes, data limit (the simulated echoes fall to the end: no limit), ground range
        for k, _n, _st in SS.PALETTES:
            lut = SS.palette_lut(k)
            assert lut.shape == (256, 3) and lut.dtype == np.uint8
        assert tuple(SS.palette_lut("grey")[255]) == (255, 255, 255)
        assert (Lp.coverage() == NS).all() and Lp.coverage_m() is None
        g = Lp.ground_ranges(300)
        ok = np.isfinite(g)
        assert np.allclose(g[ok], np.sqrt(np.maximum(r[ok] ** 2 - ALT ** 2, 0)), atol=0.1)
        # past the echoes: the noise floor -> a limit
        Lp.db[:, 600:] = np.random.default_rng(1).normal(-60, 2.0, (NP, NS - 600)).astype(np.float32)
        Lp._avg = Lp._cov = None
        assert abs(float(np.median(Lp.coverage())) - 600) < 30, np.median(Lp.coverage())
        assert np.isnan(Lp.trimmed(Lp.levelled())[:, 650:]).all()
        # concise names: P<pass> Port / Stbd, passes in time order over recordings, typed names kept
        from CloudPro.core import sonar as SN
        p.lines = [
            {"id": 0, "kind": "sonar", "name": "2026-10-07-17-19 pass 2 port (30053)", "nickname": "30053", "head": 1,
             "pass": 2, "files": ["b"], "window": [20.0, 30.0]},
            {"id": 1, "kind": "sonar", "name": "2026-10-07-17-19 pass 2 starboard (30052)", "nickname": "30052",
             "head": 2, "pass": 2, "files": ["b"], "window": [20.0, 30.0]},
            {"id": 2, "kind": "sonar", "name": "My line", "looks": "port", "head": 1, "pass": 1, "files": ["a"],
             "window": [0.0, 10.0]},
            {"id": 3, "kind": "sonar", "name": "x pass 1 starboard (30052)", "nickname": "30052", "head": 2,
             "pass": 1, "files": ["b"], "window": [11.0, 19.0]}]
        assert SN.concise_names(p) == 3
        assert [ln["name"] for ln in p.lines] == ["P3 Port", "P3 Stbd", "My line", "P2 Stbd"], p.lines
        assert [x[1] for x in SS.passes(p)] == ["P1 · L3 Port", "P2 · L4 Stbd", "P3 · L1 Port · L2 Stbd"]
        # XTF
        fp = os.path.join(d, "pass1.xtf")
        p.lines = FakeProject(d).lines
        nx = SS.write_xtf(p, [0, 1], fp)
        assert nx == NP and os.path.getsize(fp) > NP * 2 * NS * 2
        try:
            import pyxtf
        except ImportError:
            pyxtf = None
        if pyxtf is not None:
            fh, packets = pyxtf.xtf_read(fp)
            son = packets[pyxtf.XTFHeaderType.sonar]
            assert fh.NumberOfSonarChannels == 2 and len(son) == NP
            q = son[300]
            assert q.PingNumber == 400 and abs(q.SensorYcoordinate - (45.1 + north[300] / 111111.0)) < 1e-9
            pad = len(q.data[0]) - NS
            got = SS.XTF_DB0 + q.data[0][pad:].astype(float) / SS.XTF_DB_SCALE
            want = Lp.db[300]
            assert np.allclose(got[want > SS.XTF_DB0], want[want > SS.XTF_DB0], atol=0.01)
            assert abs(q.ping_chan_headers[0].SlantRange - R1) < 0.1
            xtf = "XTF read back by pyxtf"
        else:
            xtf = "XTF written (pyxtf not installed: not read back)"
        print("side scan: levelled, mosaic target at E %.2f N %.2f, contact %.1f m long %.2f m high; %s"
              % (e, n, c["length_m"], c["height_m"], xtf))
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    test_sidescan()
    print("test_sidescan: OK")
