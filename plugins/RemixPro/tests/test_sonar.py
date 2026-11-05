"""SonarView recordings and TidalCal projects to .clraw (kind "sonar").

Simulated two-head Omniscan 3D recording laid out as on the boat (SonarView session devices 1 and 2 with their
mounts, NTP UTC in the sonar packets, MAVLink JSON from mavlink2rest, packets 3104 / 3010 / 504 / 505 / 2198 per
head), a Reach LLH log, a RINEX 3 observation file and a TidalCal project folder. With REMIXPRO_SVLZ=<a real .svlz>
(or tests/data/*.svlz) the real file is converted and checked lossless too."""
import glob
import gzip
import hashlib
import json
import os
import shutil
import struct
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))

from dcs_core import clraw, mcap, ping                      # noqa: E402
from RemixPro.core import make, sonar, sources, tidalcal   # noqa: E402

T0 = 1790000000.0           # 2026-09-21
REAL = next((p for p in [os.environ.get("REMIXPRO_SVLZ")] + glob.glob(os.path.join(HERE, "data", "*.svl*"))
             + ["/home/claude/samples/737_Public.svlz"] if p and os.path.isfile(p)), None)


def mav(msg, seq=0):
    return ping.build(150, json.dumps({"header": {"system_id": 1, "component_id": 1, "sequence": seq},
                                       "message": msg}).encode())


def point_set(dev, n, t, k):
    head = struct.pack("<IfhHIQIBBBB3f9I", k, 1500.0, n, 0, 0, int(t * 1000), 5000 + k * 100, 1, dev - 1, 0, 0,
                       0.5, 0.2, 0.1, *([0] * 9))
    pts = b"".join(struct.pack("<fffB3x", -0.5 + i / n, 0.01 + i * 1e-4, 0.3 + i / 100, 0) for i in range(n))
    return ping.build(3104, head + pts, src=dev)


def end_ping(dev, t, k):
    return ping.build(3010, struct.pack("<I2f3fI2f4f f iHHHBBIQ", 0, 0.5, 20.0, 0.01, -0.02, 0.9998, k, 12.5, -1000,
                                        0, 0, 0, 0, 10.0, 3, 40, 1000, 4, dev - 1, 0, 5000 + k * 100,
                                        int(t * 1000)), src=dev)


def attitude(dev, t, k):
    return ping.build(504, struct.pack("<6fQIB", 0.0175, -0.0349, 0.9992, 0, 0, 0, int(t * 1000), 5000 + k * 10,
                                       dev - 1), src=dev)


def make_svlz(path, seconds=6.0, heads=(1, 2), gz=True, cut=0):
    sess = {"session_id": 1, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(T0 - 1)),
            "session_devices": [{"url": "tcp://192.168.2.%d:62312" % (20 + d), "device_id": d, "product_id": "os3d45016",
                                 "nickname": "3005%d" % d, "status": "CONNECTED",
                                 "options": {"mount_roll_deg": 0, "mount_pitch_deg": 0,
                                             "mount_yaw_deg": 90 if d == 1 else -90, "mount_x": -0.122,
                                             "mount_y": 0.056 if d == 1 else -0.056, "mount_z": 0.564}}
                                for d in heads]}
    out = [ping.build(10, json.dumps(sess).encode())]
    k = 0
    for i in range(int(seconds * 10)):
        t = T0 + i * 0.1
        boot = 100000 + int(i * 100)
        if i % 10 == 0:
            out.append(mav({"type": "SYSTEM_TIME", "time_unix_usec": int(t * 1e6), "time_boot_ms": boot}))
            out.append(mav({"type": "GPS_RAW_INT", "fix_type": 6, "time_usec": int(t * 1e6), "cog": 9000}))
        out.append(mav({"type": "GLOBAL_POSITION_INT", "time_boot_ms": boot, "lat": 459636000 + i, "lon": -666431000,
                        "alt": 1200, "relative_alt": 0, "vx": 100, "vy": 0, "vz": 0, "hdg": 9000}))
        out.append(mav({"type": "ATTITUDE", "time_boot_ms": boot, "roll": 0.01, "pitch": -0.02, "yaw": 1.57}))
        for d in heads:
            for j in range(10):     # 100 Hz motion: packet 505 (layout unpublished, carried as is)
                out.append(ping.build(505, struct.pack("<Q", int((t + j / 100) * 1000)) + bytes([d]) * 40, src=d))
            out.append(attitude(d, t, k))
            out.append(point_set(d, 50, t + 0.01 * d, k))
            out.append(end_ping(d, t + 0.01 * d, k))
            out.append(ping.build(2198, struct.pack("<IIIIIHHHBB6f", k, 0, 20000, 5000 + k, 10, 3, 4, 15000, d, 0,
                                                    1e-4, 1, 0, -60, 0, 0) + struct.pack("<4H", 1, 2, 3, 4), src=d))
        k += 1
    raw = b"".join(out)
    if cut:
        raw = raw[:-cut]
    with (gzip.open(path, "wb") if gz else open(path, "wb")) as f:
        f.write(raw)
    return raw


def make_llh(path, t0, t1):
    with open(path, "w") as f:
        t = t0
        while t <= t1:
            f.write("%s.%03d   45.963600000  -66.643100000    12.000   1  12   0.01   0.01   0.02   0.00   0.00   0.00"
                    "   1.0    0.0\n" % (time.strftime("%Y/%m/%d %H:%M:%S", time.gmtime(int(t))), int(round(t % 1 * 1000))))
            t += 0.2


def make_rinex(path, t0, t1):
    head = "     3.03           OBSERVATION DATA    M                   RINEX VERSION / TYPE\n" \
           "                                                            END OF HEADER\n"
    lines = [head]
    t = t0 - 3600
    while t <= t1 + 3600:
        g = time.gmtime(int(t + 18))
        lines.append("> %04d %02d %02d %02d %02d %10.7f  0  1\nG01  21614456.178\n" % (g.tm_year, g.tm_mon, g.tm_mday,
                                                                                    g.tm_hour, g.tm_min, g.tm_sec))
        t += 60
    with open(path, "w") as f:
        f.write("".join(lines))


def topics_of(files):
    out = {}
    for f in files:
        m = mcap.McapFile(f).scan()
        with open(f, "rb") as fh:
            for i in range(len(m.chunk_pos)):
                for ch, lt, pt, p in mcap.iter_messages(m.read_chunk(fh, i)):
                    out.setdefault(m.channels[ch]["topic"], []).append((lt, pt, bytes(p)))
    return out


class TestPing(unittest.TestCase):
    def test_framing_and_decoders(self):
        p = point_set(2, 3, T0, 7)
        mid, src, dst, pl = ping.split(p)
        self.assertEqual((mid, src), (3104, 2))
        d = ping.os3d_point_set(pl)
        self.assertEqual((d["ping_number"], d["num_points"], d["utc_msec"], d["device_number"]), (7, 3, int(T0 * 1000), 1))
        self.assertAlmostEqual(d["points"][1][0], -0.5 + 1 / 3, places=5)
        e = ping.end_ping_info(ping.split(end_ping(1, T0, 3))[3])
        self.assertEqual((e["ping_number"], e["utc_msec"], e["n_range_bins"]), (3, int(T0 * 1000), 1000))
        a = ping.attitude_report(ping.split(attitude(1, T0, 0))[3])
        self.assertAlmostEqual(a["pitch_deg"], -1.0, places=1)
        self.assertAlmostEqual(a["roll_deg"], -2.0, places=1)
        self.assertEqual(ping.packet_utc_ms(504, ping.split(attitude(1, T0, 0))[3]), int(T0 * 1000))
        with self.assertRaises(ping.PingError):
            ping.split(p[:-1] + bytes([p[-1] ^ 1]))

    def test_reader_skips_junk_and_keeps_a_cut_file(self):
        with tempfile.TemporaryDirectory() as d:
            raw = make_svlz(os.path.join(d, "a.svlz"), 1.0)
            with open(os.path.join(d, "b.svlog"), "wb") as f:
                cut = 8 + struct.unpack_from("<H", raw, 2)[0] + 2       # after the first packet
                f.write(raw[:cut] + b"junk!" + raw[cut:-7])
            r = ping.Reader(os.path.join(d, "b.svlog"))
            n = sum(1 for _ in r)
            self.assertGreaterEqual(r.skipped_bytes, 5)      # the junk, and the cut final packet's bytes
            self.assertTrue(r.truncated)
            self.assertEqual(n, sum(1 for _ in ping.Reader(os.path.join(d, "a.svlz"))) - 1)


class TestSonarClraw(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.src = os.path.join(cls.tmp, "rec", "2026-09-21_survey.svlz")
        os.makedirs(os.path.dirname(cls.src))
        cls.raw = make_svlz(cls.src)
        make_llh(os.path.join(cls.tmp, "rec", "reach.LLH"), T0 - 600, T0 + 600)
        make_rinex(os.path.join(cls.tmp, "rec", "reach.26O"), T0, T0 + 6)
        with open(os.path.join(cls.tmp, "rec", "sonar_offsets.json"), "w") as f:
            json.dump({"1": [-0.40, 0.31, 0.65], "port": [-0.40, -0.31, 0.65]}, f)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_found_and_converted_lossless(self):
        items = sources.scan([os.path.join(self.tmp, "rec")])
        self.assertEqual([(i.kind, i.name, i.outputs) for i in items],
                         [(sources.SONAR, "2026-09-21_survey", [sources.CLRAW])])
        it = items[0]
        self.assertEqual([os.path.basename(p) for p in it.rinex + it.llh], ["reach.26O", "reach.LLH"])
        out = os.path.join(self.tmp, "out")
        with open(self.src, "rb") as f:
            before = hashlib.sha256(f.read()).hexdigest()
        r = make.run(it, [sources.CLRAW], out)
        with open(self.src, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), before)                  # input untouched
        files = [os.path.join(r[sources.CLRAW]["folder"], f) for f in r[sources.CLRAW]["files"]]
        cf = clraw.ClrawFile(files[0])
        self.assertEqual(cf.metadata["kind"], "sonar")
        self.assertEqual(cf.metadata["time_basis"], "sonar UTC (NTP)")
        self.assertEqual(clraw.sonar_topics(cf), {1: "/sonar/1/ping", 2: "/sonar/2/ping"})
        hw = cf.attachment_json("hardware.json")
        s = {x["device_id"]: x for x in hw["sonars"]}
        self.assertEqual(s[1]["acoustic_centre_m"], [-0.40, 0.31, 0.65])            # by device id
        self.assertEqual(s[2]["acoustic_centre_m"], [-0.40, -0.31, 0.65])           # by side (mount_y < 0)
        self.assertEqual(s[2]["mount_deg_sonarview"]["yaw"], -90)
        self.assertEqual(s[1]["packet_ids"]["505"], 600)
        self.assertEqual(hw["reference_point"][:12], "GNSS antenna")
        tops = topics_of(files)
        # lossless: every sonar packet back in order, byte for byte, on its own head's channel
        orig = {1: [], 2: []}
        for mid, src, dst, pl, pk in ping.Reader(self.src):
            if src in orig:
                orig[src].append(pk)
        for d in (1, 2):
            self.assertEqual([p for _l, _t, p in tops["/sonar/%d/ping" % d]], orig[d])
        self.assertEqual(len(tops["/vehicle/mavlink"]), 6 * 10 * 2 + 6 * 2)
        # times: sonar packets carry their own UTC; nothing decreases
        lt = [l for t in tops.values() for l, _p, _b in t]
        pts = [(l, t) for l, t, b in tops["/sonar/1/ping"] if b[4:6] == struct.pack("<H", 3104)]
        self.assertAlmostEqual(pts[0][1] / 1e9, T0 + 0.01, places=3)
        self.assertTrue(all(abs(a - b) < 1 for a, b in pts))
        for t in ("/sonar/1/ping", "/vehicle/mavlink"):
            seq = [l for l, _p, _b in tops[t]]
            self.assertEqual(seq, sorted(seq))
        # GNSS side files: LLH lines within the recording +- 60 s; RINEX cut to +- 10 min
        llh = tops["/gnss/llh"]
        self.assertTrue(T0 - 61 <= llh[0][0] / 1e9 and llh[-1][0] / 1e9 <= T0 + 67)
        rin = cf.attachments["gnss/reach.26O"].decode()
        self.assertTrue(10 <= rin.count("\n>") <= 25, rin.count("\n>"))
        self.assertIn("2026-09-21_survey.svlz", [x["file"] for x in json.loads(cf.attachments["sources.json"])])
        ev = [json.loads(b)["event"] for _l, _t, b in tops["/capture/events"]]
        self.assertEqual(ev, ["clraw_start", "clraw_stop"])
        # made once
        self.assertEqual(make.run(it, [sources.CLRAW], out)[sources.CLRAW]["state"], "current")
        # the Lidar plugins never take it as a survey line or a navigation recording
        self.assertEqual(clraw.split_navigation(files), ([], []))
        self.assertEqual(clraw.group_recordings(files), {})
        self.assertEqual(len(clraw.group_recordings(files, sonar=True)), 1)

    def test_tidalcal_project(self):
        p = os.path.join(self.tmp, "TC")
        for sub in tidalcal.FOLDERS:
            os.makedirs(os.path.join(p, sub), exist_ok=True)
        shutil.copy(self.src, os.path.join(p, "01_Raw_Sonar"))
        shutil.copy(os.path.join(self.tmp, "rec", "reach.LLH"), os.path.join(p, "02_GNSS_Rover_Base"))
        with open(os.path.join(p, "project.tidalcal"), "w") as f:
            json.dump({"format": "clam-tidalcal-project", "version": 1, "name": "Harbour",
                       "dataset_paths": ["C:\\\\Users\\\\x\\\\TC\\\\01_Raw_Sonar\\\\2026-09-21_survey.svlz"]}, f)
        with open(os.path.join(p, "project_settings.json"), "w") as f:
            json.dump({"schema_version": 1, "sonar_latency_s": 0.12, "devices": [
                {"device_id": 1, "nickname": "30051", "forward_m": -0.122, "starboard_m": 0.056, "down_m": 0.564,
                 "roll_deg": 0, "pitch_deg": 0, "yaw_deg": 90},
                {"device_id": 2, "nickname": "30052", "forward_m": -0.122, "starboard_m": -0.056, "down_m": 0.564,
                 "roll_deg": 0, "pitch_deg": 0, "yaw_deg": -90}], "qc_geometry": {"1": {"roll_deg": 1.5}}}, f)
        items = sources.scan([p])
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual((it.project, it.offsets_source), ("Harbour", "TidalCal vessel table"))
        self.assertEqual([os.path.basename(x) for x in it.llh], ["reach.LLH"])
        r = make.run(it, [sources.CLRAW], os.path.join(self.tmp, "out_tc"))
        cf = clraw.ClrawFile(os.path.join(r[sources.CLRAW]["folder"], r[sources.CLRAW]["files"][0]))
        hw = cf.attachment_json("hardware.json")
        self.assertEqual(hw["tidalcal"]["sonar_latency_s"], 0.12)
        self.assertEqual(hw["sonars"][0]["acoustic_centre_m"], [-0.122, 0.056, 0.564])
        self.assertIn("tidalcal/project_settings.json", cf.attachments)
        self.assertEqual(cf.metadata["project"], "Harbour")

    def test_cancel_leaves_nothing(self):
        it = sources.scan([self.src])[0]
        out = os.path.join(self.tmp, "out_cancel")
        with self.assertRaises(make.Cancelled):
            make.run(it, [sources.CLRAW], out, cancelled=lambda: True)
        self.assertFalse(os.path.exists(out) and any(fs for _r, _d, fs in os.walk(out)))


@unittest.skipUnless(REAL, "no real SonarView recording here (REMIXPRO_SVLZ)")
class TestRealRecording(unittest.TestCase):
    def test_real_lossless(self):
        with tempfile.TemporaryDirectory() as d:
            r = sonar.to_clraw(REAL, d)
            h1 = hashlib.sha256()
            for mid, src, dst, pl, pk in ping.Reader(REAL):
                h1.update(bytes(pl).rstrip(b"\x00") if mid in (10, 12, 150) else pk)
            h2 = hashlib.sha256()
            for f in r["files"]:
                m = mcap.McapFile(f).scan()
                with open(f, "rb") as fh:
                    for i in range(len(m.chunk_pos)):
                        for ch, lt, pt, p in mcap.iter_messages(m.read_chunk(fh, i)):
                            if m.channels[ch]["topic"] not in ("/capture/events", "/gnss/llh", "/gnss/ppk"):
                                h2.update(bytes(p))
            self.assertEqual(h1.hexdigest(), h2.hexdigest())
            self.assertEqual(r["skipped_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
