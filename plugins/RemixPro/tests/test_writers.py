"""The writers taken over from the mapper, with the mapper's own tests (handover kit tests/test_bwx.py,
test_clraw.py, test_convert_from_mapper.py), ported to RemixPro's modules.

The "same as NavAQ" test needs Sean's 2024 NavAQ recording 0100_20240815_135643_NoLine.0001.bwxraw (107 MB,
not in the repository): REMIXPRO_NAVAQ=<path>, or put it in tests/data/. Skipped without it.
"""
import json
import os
import shutil
import struct
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", ".."))

import mapper_clraw as mclraw                                          # noqa: E402
from dcs_core import anpp                                              # noqa: E402
from RemixPro.core import beamworx, bwxraw, inertial as convert, surveyconfig   # noqa: E402

NAVAQ_SAMPLE = next((p for p in (os.environ.get("REMIXPRO_NAVAQ"),
                                 os.path.join(HERE, "data", "0100_20240815_135643_NoLine.0001.bwxraw"),
                                 "/home/claude/navaq_sample/MM-FosterThurston/0100_20240815_135643_NoLine.0001.bwxraw")
                     if p and os.path.isfile(p)), "")
XML = open(surveyconfig.TEMPLATE, "rb").read()
NS = 1000000000
UT = 1791415292          # 2026-10-07


def hesai_packet(seq, t=1791684000.0):
    import hashlib
    p = bytearray(hashlib.shake_256(b"%d" % seq).digest(1080) if seq % 2 == 0 else bytes(1080))
    p[0], p[1] = 0xEE, 0xFF
    p[9] = 4
    g = time.gmtime(int(t))
    struct.pack_into("<6BI", p, 1052 + 13, g.tm_year - 1900, g.tm_mon, g.tm_mday, g.tm_hour, g.tm_min, g.tm_sec,
                     int((t % 1) * 1e6))
    struct.pack_into("<I", p, 1076, seq)
    return bytes(p)


def certus_state(t):
    body = bytearray(100)
    struct.pack_into("<II", body, 4, int(t), int((t % 1) * 1e6))
    return anpp.build(20, bytes(body))


def state_packet(ut=UT, us=0, fs=0x022F, lat=0.7931, lon=-1.1512, h=50.0):
    body = struct.pack("<HHIIdddffffffffffffffff", 0, fs, ut, us, lat, lon, h,
                       1.0, 2.0, 0.1, 0, 0, 0, 1.0, 0.01, -0.02, 0.5, 0, 0, 0, 1.5, 1.6, 2.0)
    return anpp.build(20, body)


def raw_packet(ax=0.1, ay=-0.2, az=-9.81, gx=0.001, gy=0.002, gz=-0.003, temp=30.0):
    return anpp.build(28, struct.pack("<12f", ax, ay, az, gx, gy, gz, 0, 0, 0, temp, 0, 0))


def obs_packet(ut=UT, ns=190000000, rx=0, pn=1, tot=1,
               sats=((1, 8, [(1, 0x39, 113584732.815, 21614456.178, 439.8, 28.0)]),)):
    body = struct.pack("<IIiBBBB", ut, ns, -1000, rx, pn, tot, len(sats))
    for sysid, prn, freqs in sats:
        body += struct.pack("<BBBHB", sysid, prn, 30, 120, len(freqs))
        for f in freqs:
            body += struct.pack("<BBddff", *f)
    return anpp.build(60, body)


def record(folder, t0, seconds, split_bytes=None):
    """A recording like the mapper's live sink makes: 3 s of navigation, start event, points + navigation,
    stop event, 3 s navigation."""
    r = mclraw.Recorder(folder, 5, {"line": 5, "vehicle": "Van"},
                        [("beamworx_survey_config.xml", "application/xml", XML)],
                        split_bytes=split_bytes, name_ns=int(t0 * NS))
    ev = lambda t, e: r.put(mclraw.CH_EVENTS, int(t * NS), int(t * NS), json.dumps({"event": e, "t": t}).encode())  # noqa: E731
    for i in range(30):
        t = t0 - 3 + i * 0.1
        r.put(mclraw.CH_ANPP, int(t * NS), int(t * NS), certus_state(t))
    ev(t0, "clraw_start")
    seq = 0
    n = int(seconds * 1000)
    for i in range(n):
        t = t0 + i * 0.001
        r.put(mclraw.CH_POINTS, int(t * NS), mclraw.sensor_time_ns(mclraw.CH_POINTS, hesai_packet(seq, t), 0),
              hesai_packet(seq, t))
        seq += 1
        if i % 100 == 0:
            r.put(mclraw.CH_ANPP, int(t * NS), int(t * NS), certus_state(t))
    t_end = t0 + seconds
    ev(t_end, "clraw_stop")
    r.hold_split = True
    for i in range(30):
        t = t_end + i * 0.1
        r.put(mclraw.CH_ANPP, int(t * NS), int(t * NS), certus_state(t))
    return r.close(), n


class TestBwxWriter(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.bwxraw")
            xml = b"<?xml version=\"1.0\"?><BeamworX><Format>SurveyConfig</Format></BeamworX>"
            run_in = [(2, bwxraw.anpp_code(20), 100.0 + i * 0.1, i * 0.1, certus_state(100.0 + i * 0.1))
                      for i in range(5)]
            w = bwxraw.Writer(path, xml, "Van", "Hesai Laser Scanner", 7, 1, 100.5, run_in=run_in)
            for i in range(30):
                w.raw(3, bwxraw.hesai_code(1080), 101.0 + i * 0.5, 1.0 + i, hesai_packet(i))
            w.end_marker(116.0)
            w.close()
            f = open(path, "rb").read()
            h = bwxraw.read_header(f)
            self.assertEqual((h["records"], h["line"], h["number"], h["vehicle"]), (35, 7, 1, "Van"))
            self.assertEqual(bwxraw.read_config(f), xml)
            types = [t for _, _, t, _ in bwxraw.read_records(f)]
            self.assertEqual(types[:3], [4, 5, 6])
            self.assertEqual(types[8:10], [7, 8])
            self.assertIn(6, types[10:])
            self.assertEqual(types[-1], 7)
            pk = list(bwxraw.raw_packets(f))
            self.assertEqual([p[4] for p in pk[5:]], [hesai_packet(i) for i in range(30)])
            self.assertFalse(os.path.exists(path + ".partial"))

    def test_recorder_split_run_in_out(self):
        with tempfile.TemporaryDirectory() as d:
            r = bwxraw.Recorder(d, b"<BeamworX/>", "Van", "Hesai Laser Scanner", 3, split_seconds=10)
            t = 1000.0
            for i in range(250):
                r.put(2, bwxraw.anpp_code(20), t + i * 0.1, certus_state(t + i * 0.1))
                r.put(3, bwxraw.hesai_code(1080), t + i * 0.1 + 0.01, hesai_packet(i))
            r.stop(t + 25.0)
            for i in range(250, 300):
                r.put(2, bwxraw.anpp_code(20), t + i * 0.1, certus_state(t + i * 0.1))
                r.put(3, bwxraw.hesai_code(1080), t + i * 0.1 + 0.01, hesai_packet(i))
            names = r.close()
            self.assertEqual([n[-11:] for n in names], ["0001.bwxraw", "0002.bwxraw", "0003.bwxraw"])
            seqs = []
            for n in names:
                f = open(os.path.join(d, n), "rb").read()
                pk = list(bwxraw.raw_packets(f))
                seqs += [struct.unpack_from("<I", p[4], 1076)[0] for p in pk if p[0] == 3]
                self.assertGreater(len([p for p in pk if p[0] == 2]), 30)       # run-in and run-out
            self.assertEqual(seqs, list(range(250)))

    @unittest.skipUnless(NAVAQ_SAMPLE, "NavAQ sample not on this computer (set REMIXPRO_NAVAQ)")
    def test_same_as_navaq(self):
        """Re-wrapping NavAQ's own packets gives NavAQ's records byte for byte."""
        f = open(NAVAQ_SAMPLE, "rb").read()
        hdr, xml = bwxraw.read_header(f), bwxraw.read_config(f)
        offs = [(o, t) for o, L, t, _ in bwxraw.read_records(f)]
        recs = list(bwxraw.raw_packets(f))
        o3 = [o for o, t in offs if t == 3]
        t7 = [o for o, t in offs if t == 7]
        run_in = [r for o, r in zip(o3, recs) if o < t7[0]]
        body = [r for o, r in zip(o3, recs) if t7[0] < o < t7[1]]
        run_out = [r for o, r in zip(o3, recs) if o > t7[1]]
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "x.bwxraw")
            w = bwxraw.Writer(out, xml, hdr["vehicle"], hdr["sensor"], 100, 1,
                              struct.unpack_from("<d", f, 0x10C)[0], run_in=run_in)
            for r in body:
                w.raw(*r)
            w.end_marker(body[-1][2])
            for r in run_out:
                w.raw(*r)
            w.close()
            g = open(out, "rb").read()
        a3 = [f[o:o + L] for o, L, t, _ in bwxraw.read_records(f) if t == 3]
        b3 = [g[o:o + L] for o, L, t, _ in bwxraw.read_records(g) if t == 3]
        self.assertEqual(a3, b3)
        self.assertEqual(bwxraw.read_config(g), xml)
        hg = bwxraw.read_header(g)
        self.assertEqual((hg["records"], hg["first"], hg["last"]), (hdr["records"], hdr["first"], hdr["last"]))


class TestClrawToBwxraw(unittest.TestCase):
    t0 = 1791684014.0

    def test_split_and_bwxraw(self):
        with tempfile.TemporaryDirectory() as d:
            files, n = record(d, self.t0, 10.0, split_bytes=3 << 20)
            self.assertGreaterEqual(len(files), 2)
            r = beamworx.to_bwxraw([os.path.join(d, f) for f in files], os.path.join(d, "bwx"))
            made = r["files"]
            self.assertEqual([os.path.basename(m) for m in made], [f[:-6] + ".bwxraw" for f in files])
            self.assertEqual(r["config"], "recorded")
            pts = 0
            for m in made:
                data = open(m, "rb").read()
                h = bwxraw.read_header(data)
                rp = list(bwxraw.raw_packets(data))
                self.assertEqual(h["records"], len(rp))
                self.assertEqual(bwxraw.read_config(data), XML)
                self.assertTrue(os.path.exists(m[:-7] + ".xml"))
                pts += sum(1 for x in rp if x[0] == bwxraw.HESAI_DEVICE)
                nav = [x[2] for x in rp if x[0] == bwxraw.CERTUS_DEVICE]
                hes = [x[2] for x in rp if x[0] == bwxraw.HESAI_DEVICE]
                self.assertLess(min(nav), min(hes) - 2.5)            # run-in
                self.assertGreater(max(nav), max(hes) + 2.5)         # run-out
                self.assertIn(7, [t for _, _, t, _ in bwxraw.read_records(data)])
            self.assertEqual(pts, n)

    def test_bwxraw_despite_a_damaged_chunk(self):
        with tempfile.TemporaryDirectory() as d:
            files, n = record(d, self.t0, 4.0)
            p = os.path.join(d, files[0])
            offs = []
            mclraw.scan(p, on_record=lambda op, off, body: offs.append(off) if op == mclraw.OP_CHUNK else None)
            with open(p, "r+b") as f:
                f.seek(offs[2] + 300)
                f.write(b"\x55" * 32)
            r = beamworx.to_bwxraw([p], os.path.join(d, "b"))
            self.assertEqual(r["damaged_chunks"], 1)
            pts = sum(1 for x in bwxraw.raw_packets(open(r["files"][0], "rb").read()) if x[0] == bwxraw.HESAI_DEVICE)
            self.assertTrue(n // 2 < pts < n)

    def test_config_built_from_hardware_json(self):
        with tempfile.TemporaryDirectory() as d:
            hw = {"ins_to_primary_antenna_m": [0.082, 0.495, 0.112], "ins_to_secondary_antenna_m": [0.082, -0.493, 0.112],
                  "ins_to_lidar_m": [0.1, 0.2, -0.3], "lidar_mount_navaq_deg": {"roll": 90, "pitch": 45, "heading": 0}}
            r = mclraw.Recorder(d, 1, {"line": 1, "vehicle": "Van"},
                                [("hardware.json", "application/json", json.dumps(hw).encode())], name_ns=int(self.t0 * NS))
            for i in range(200):
                t = self.t0 + i * 0.01
                r.put(mclraw.CH_POINTS, int(t * NS), int(t * NS), hesai_packet(i, t))
                r.put(mclraw.CH_ANPP, int(t * NS), int(t * NS), certus_state(t))
            files = r.close()
            out = beamworx.to_bwxraw([os.path.join(d, f) for f in files], os.path.join(d, "b"))
            self.assertEqual(out["config"], "built from hardware.json")
            xml = bwxraw.read_config(open(out["files"][0], "rb").read()).decode()
            self.assertIn('<Location Forward="0.082" Starboard="0.495" Down="0.112"/>', xml)
            self.assertIn('<Location Forward="0.100" Starboard="0.200" Down="-0.300"/>', xml)


class TestConvert(unittest.TestCase):
    def make_log(self, d):
        path = os.path.join(d, "ins_0001.anpp")
        with open(path, "wb") as f:
            f.write(anpp.build(3, struct.pack("<IIIIII", 2020, 26, 1100, 1, 2, 3)))
            for i in range(10):                       # before GNSS time: stamped 2013, time bit clear
                f.write(state_packet(ut=1357110419, us=i * 5000, fs=0x0007) + raw_packet())
            for i in range(400):                      # 2 s of valid data at 200 Hz
                us = (i % 200) * 5000
                f.write(state_packet(ut=UT + i // 200, us=us) + raw_packet(az=-9.81 - i * 1e-4))
                if i % 20 == 0:
                    f.write(obs_packet(ut=UT + i // 200, ns=us * 1000, pn=1, tot=2))
                    f.write(obs_packet(ut=UT + i // 200, ns=us * 1000, pn=2, tot=2,
                                       sats=((4, 5, [(1, 0x39, 140358487.9, 26709332.3, -1278.6, 31.0),
                                                     (9, 0x38, 0.0, 26709325.5, -980.3, 26.0)]),)))
        return path

    def test_outputs(self):
        with tempfile.TemporaryDirectory() as d:
            path = self.make_log(d)
            s = convert.convert([path], os.path.join(d, "out"), "t", lever_arm_vehicle_m=[1.0, 2.0, 3.0])
            self.assertEqual(s["imu"]["samples"], 400)
            self.assertEqual(s["imu"]["dropped_before_time_valid"], 10)
            self.assertEqual(s["imu"]["rate_hz"], 200.0)
            self.assertEqual(s["imu"]["gaps_over_2_samples"], 0)
            raw = open(os.path.join(d, "out", "t.imr"), "rb").read()
            self.assertEqual(len(raw), 512 + 400 * 32)
            hdr = convert.IMR_HEADER.unpack(raw[:512])
            self.assertEqual(hdr[0], b"$IMURAW\x00")
            self.assertEqual((hdr[3], hdr[4], hdr[5]), (0, 0, 200.0))
            self.assertEqual(hdr[8], 2)
            self.assertEqual(hdr[15:19], (1, 2000, 1000, -3000))   # lever arm in IE axes, mm
            t0, gx, gy, gz, ax, ay, az = convert.IMR_RECORD.unpack(raw[512:544])
            gps0 = UT - 315964800 + 18
            self.assertAlmostEqual(t0, gps0 % 604800, places=6)
            self.assertAlmostEqual(ax * hdr[7], -0.2, places=5)
            self.assertAlmostEqual(ay * hdr[7], 0.1, places=5)
            self.assertAlmostEqual(az * hdr[7], 9.81, places=4)
            self.assertAlmostEqual(gx * hdr[6], 0.002 * 57.29578, places=4)
            self.assertAlmostEqual(gz * hdr[6], 0.003 * 57.29578, places=4)
            self.assertAlmostEqual(convert.IMR_RECORD.unpack(raw[-32:])[0] - t0, 399 * 0.005, places=6)
            lines = open(os.path.join(d, "out", "t_imu.qimu")).read().splitlines()
            self.assertEqual(lines[:3], ["$qimu", "$version:2", "$timeSource:gps"])
            first = lines[5].split(";")
            self.assertEqual(first[0], "%d.000000" % gps0)
            self.assertAlmostEqual(float(first[3]), -9.81, places=4)
            self.assertEqual(len(lines), 5 + 400)
            raw_lines = open(os.path.join(d, "out", "t.obs")).read().splitlines()
            end = [i for i, x in enumerate(raw_lines) if "END OF HEADER" in x][0]
            self.assertTrue(all(len(x) == 80 for x in raw_lines[:end + 1]))
            obs = [x.rstrip() for x in raw_lines]
            self.assertTrue(obs[0].startswith("     3.04           OBSERVATION DATA    M"))
            self.assertIn("G    4 C1C L1C D1C S1C                                      SYS / # / OBS TYPES", obs)
            self.assertIn("E    8 C1C L1C D1C S1C C7Q L7Q D7Q S7Q                      SYS / # / OBS TYPES", obs)
            ep = [x for x in obs if x.startswith(">")]
            self.assertEqual(len(ep), 20)
            self.assertEqual(ep[0], "> 2026 10 07 23 21 50.0000000  0  2")   # UTC + 18 s
            body = obs[obs.index(ep[0]) + 1]
            self.assertEqual(body[:3], "G08")
            self.assertEqual(body[3:17].strip(), "21614456.178")
            self.assertEqual(body[19:33].strip(), "113584732.815")
            e05 = obs[obs.index(ep[0]) + 2]
            self.assertEqual(e05[3 + 64 + 16:3 + 64 + 32].strip(), "")      # invalid phase left blank
            self.assertEqual(s["rinex_obs"][0]["interval_s"], 0.1)

    def test_packet_split_across_two_files_is_not_lost(self):
        with tempfile.TemporaryDirectory() as d:
            whole = open(self.make_log(d), "rb").read()
            cut = len(whole) // 2 + 7
            a, b = os.path.join(d, "a_0001.anpp"), os.path.join(d, "a_0002.anpp")
            open(a, "wb").write(whole[:cut])
            open(b, "wb").write(whole[cut:])
            s = convert.convert([a, b], os.path.join(d, "out2"), "t")
            self.assertEqual(s["imu"]["samples"], 400)
            self.assertEqual(s["imu"]["gaps_over_2_samples"], 0)

    def test_time_does_not_go_backwards(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.anpp")
            with open(path, "wb") as f:
                for us in (0, 5000, 5000, 0, 10000):
                    f.write(state_packet(us=us) + raw_packet())
            s = convert.convert([path], d, "t", want_rinex=False, want_track=False)
            self.assertEqual(s["imu"]["samples"], 3)
            self.assertEqual(s["imu"]["dropped_out_of_order"], 2)

    def test_rinex_with_a_signal_it_does_not_know(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ins_0001.anpp")
            with open(path, "wb") as f:
                for i in range(40):
                    f.write(state_packet(ut=UT, us=i * 5000) + raw_packet())
                    if i % 20 == 0:
                        f.write(obs_packet(ut=UT, ns=i * 5000000, sats=(
                            (1, 8, [(1, 0x39, 113584732.815, 21614456.178, 439.8, 28.0)]),
                            (3, 12, [(4, 0x39, 113584732.815, 21614456.178, 439.8, 28.0)]),)))
            s = convert.convert([path], os.path.join(d, "out"), "t")
            body = open(os.path.join(d, "out", "t.obs")).read().split("END OF HEADER")[1]
            self.assertIn("G08", body)
            self.assertNotIn("C12", body)
            self.assertEqual(s["rinex_obs"][0]["epochs"], 2)
            self.assertEqual(s["rinex_obs"][0]["unmapped_signal_records"], 2)

    def test_cancel_closes_everything(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "big.anpp")
            with open(path, "wb") as f:
                for i in range(60000):
                    f.write(state_packet(ut=UT + i // 200, us=(i % 200) * 5000) + raw_packet())
            out = os.path.join(d, "out")
            with self.assertRaises(convert.Cancelled):
                convert.convert([path], out, "t", cancelled=lambda: True)
            shutil.rmtree(out)          # nothing left open (Windows could not remove an open file)
            self.assertFalse(os.path.exists(out))


class TestSurveyConfig(unittest.TestCase):
    def test_navaq_lidar_convention(self):
        fitted = [[0.7109, -0.005, 0.7033], [-0.0028, -1.0, -0.0044], [0.7033, 0.0012, -0.7109]]
        m = surveyconfig.lidar_matrix_from_navaq(90, 45, 0)
        self.assertLess(max(abs(m[i][j] - fitted[i][j]) for i in range(3) for j in range(3)), 0.006)
        for ang in ((90, 45, 0), (10, -20, 30), (-90, 45, 180), (45, 10, 270), (0, 0, 0)):
            back = surveyconfig.navaq_from_lidar_matrix(surveyconfig.lidar_matrix_from_navaq(*ang))
            for a, b in zip(ang, back):
                self.assertAlmostEqual(((a - b + 180) % 360) - 180, 0.0, places=6)

    def test_build_changes_only_our_values(self):
        s = surveyconfig.build({"primary_antenna": [0.082, 0.495, 0.112], "secondary_antenna": [0.082, -0.493, 0.112],
                                "lidar_offset": [0.1, 0.2, -0.3], "lidar_mount": {"roll": 90, "pitch": 45, "heading": 0},
                                "lidar_ip": "10.1.0.201"}, vehicle="CLM <01>").decode()
        self.assertIn('Name="Primary GPS">', s)
        self.assertIn('<Location Forward="0.082" Starboard="0.495" Down="0.112"/>', s)
        self.assertIn("CLM &lt;01&gt;", s)


if __name__ == "__main__":
    unittest.main()
