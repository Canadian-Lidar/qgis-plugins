"""MapPro 2.11 day folders (DCS file structure of 17 Nov 2026): RemixPro finds each line's .clraw, its Certus logs
and the day's ephemeris from the day's session.json, the navigation recordings from their index files, and makes
the files from them.

    <Project>/<YYYYMMDD>/session.json, events.json
    <Project>/<YYYYMMDD>/CLRAW/YYYYMMDD_001_001.clraw (split), YYYYMMDD_002_001.clraw
    <Project>/<YYYYMMDD>/IMU/ins_YYYYMMDD_0001.anpp (line 1), _0002 (line 2), ephemeris_cache.anpp
    <Project>/<YYYYMMDD>/NAV/nav_YYYYMMDD_0001.anpp / .clraw, .index_nav_<start>.json
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", ".."))

import sim210                                              # noqa: E402
from sim210 import NS, T_LINE1, certus_stream, clraw, ephemeris_packet, hardware, hesai_gps, hesai_packet  # noqa: E402
from dcs_core import clraw as dclraw                       # noqa: E402
from RemixPro.core import make, sources                    # noqa: E402


def write_day_line(ddir, project, n, t0, seconds, ins_name, split_bytes=None):
    day = os.path.basename(ddir)
    lid = "%s_%03d" % (day, n)
    cid = "%s_%s" % (project, lid)
    meta = {"id": cid, "line_id": lid, "name": project, "kind": "survey", "project": {"id": project, "name": project},
            "line": n, "line_digits": 3, "file_prefix": lid, "layout": "2.11", "day": day,
            "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0)), "started_unix": t0,
            "settings": {"ins_to_primary_antenna_m": sim210.PRIMARY, "ins_to_secondary_antenna_m": sim210.SECONDARY},
            "session": 1}
    md = {"format": "clraw", "format_version": "1", "mapper_version": "2.11.0", "capture": cid,
          "capture_name": project, "project": project, "project_id": project, "line": n, "line_digits": 3,
          "day": day}
    atts = [("beamworx_survey_config.xml", "application/xml", sim210.survey_xml("CLM Van 01")),
            ("hardware.json", "application/json", json.dumps(hardware(), indent=1).encode()),
            ("session.json", "application/json", json.dumps(meta).encode())]
    cdir = os.path.join(ddir, "CLRAW")
    rec = clraw.Recorder(cdir, n, md, atts, split_bytes=split_bytes, digits=3, name_ns=int(t0 * NS), prefix=lid)
    stream = certus_stream(t0 - 3.0, t0 + seconds + 3.0)
    with open(os.path.join(ddir, "IMU", ins_name), "wb") as f:
        f.write(b"".join(p for _t, p in stream))
    msgs = [(t, clraw.CH_ANPP, p) for t, p in stream]
    for i in range(int(seconds * 600)):
        t = t0 + i / 600.0
        msgs.append((t + 1e-5, clraw.CH_POINTS, hesai_packet(i, t)))
    for s in range(int(seconds) + 1):
        msgs.append((t0 + s + 2e-5, clraw.CH_STATUS, hesai_gps(t0 + s)))
    msgs.sort(key=lambda m: m[0])
    for t, ch, data in msgs:
        lt = int(round(t * NS))
        rec.put(ch, lt, clraw.sensor_time_ns(ch, data, lt), data)
    names = rec.close()
    out = []
    for k, name in enumerate(names):                   # MapPro 2.11 numbers the files of a line with 3 digits
        new = "%s_%03d.clraw" % (lid, k + 1)
        os.rename(os.path.join(cdir, name), os.path.join(cdir, new))
        out.append(new)
    meta["ins_stats"] = [{"files": [ins_name], "closed_cleanly": True}]
    meta["continuity"] = {"verdict": "Complete", "level": "ok"}
    return lid, meta, out


def make_day_project(root, name="Deck_Test"):
    pdir = os.path.join(root, "MP_PROJECTS", name)
    day = time.strftime("%Y%m%d", time.gmtime(T_LINE1))
    ddir = os.path.join(pdir, day)
    for sub in ("NAV", "LIDAR", "IMU", "CLRAW", "BASE", "GNSS", "EXPORT"):
        os.makedirs(os.path.join(ddir, sub))
    with open(os.path.join(pdir, "project.json"), "w") as f:
        json.dump({"id": name, "name": name}, f)
    with open(os.path.join(ddir, "IMU", "ephemeris_cache.anpp"), "wb") as f:
        f.write(b"".join(ephemeris_packet(prn, T_LINE1 - 3600) for prn in (1, 3, 8, 14, 22)))
    lines = {}
    for n, t0, secs, sb in ((1, T_LINE1, 10.0, 3 << 20), (2, T_LINE1 + 60, 5.0, None)):
        lid, meta, files = write_day_line(ddir, name, n, t0, secs, "ins_%s_%04d.anpp" % (day, n), sb)
        lines[lid] = meta
    # the navigation recording: NAV/nav_YYYYMMDD_0001.anpp + .clraw, its index
    t0, t1 = T_LINE1 - 20, T_LINE1 + 75
    stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime(t0))
    cap = "%s_NAV_%s" % (name, stamp)
    stream = certus_stream(t0, t1, invalid_first=40, rate=100)
    nav = os.path.join(ddir, "NAV")
    with open(os.path.join(nav, "nav_%s_0001.anpp" % day), "wb") as f:
        f.write(b"".join(p for _t, p in stream))
    with open(os.path.join(nav, ".index_nav_%s.json" % stamp), "w") as f:
        json.dump({"files": [{"name": "nav_%s_0001.anpp" % day, "ended": "closed"}]}, f)
    md = {"format": "clraw", "format_version": "1", "mapper_version": "2.11.0", "kind": "navigation", "capture": cap,
          "capture_name": name, "project": name, "project_id": name, "line": 0, "line_digits": 3}
    rec = clraw.Recorder(nav, 0, md, [("hardware.json", "application/json",
                                       json.dumps(hardware(line=False)).encode())], prefix="nav_" + day)
    for t, p in stream:
        lt = int(round(t * NS))
        rec.put(clraw.CH_ANPP, lt, clraw.sensor_time_ns(clraw.CH_ANPP, p, lt), p)
    rec.close()
    record = {"schema": "mapper.day/1", "date": day, "project": {"id": name, "name": name},
              "sessions": [{"n": 1, "settings": {"ins_to_primary_antenna_m": sim210.PRIMARY}, "lines": sorted(lines)}],
              "lines": lines,
              "navigation": [{"capture": cap, "started_unix": t0, "index": ".index_nav_%s.json" % stamp}]}
    with open(os.path.join(ddir, "session.json"), "w") as f:
        json.dump(record, f)
    open(os.path.join(ddir, "events.json"), "w").close()
    return pdir, ddir, cap


class TestDay211(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.pdir, self.ddir, self.nav = make_day_project(self.tmp)
        self.day = os.path.basename(self.ddir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_found_with_their_logs(self):
        items = sources.scan([self.pdir])
        by = {i.name: i for i in items}
        l1, l2 = "Deck_Test_%s_001" % self.day, "Deck_Test_%s_002" % self.day
        self.assertEqual(sorted(by), sorted([l1, l2, self.nav]))          # no stray Certus logs
        self.assertEqual(by[l1].kind, sources.LINE)
        self.assertGreater(len(by[l1].clraw), 1)                          # split
        self.assertEqual([os.path.basename(p) for p in by[l1].anpp], ["ins_%s_0001.anpp" % self.day])
        self.assertEqual([os.path.basename(p) for p in by[l2].anpp], ["ins_%s_0002.anpp" % self.day])
        self.assertEqual(os.path.basename(by[l1].ephemeris), "ephemeris_cache.anpp")
        self.assertTrue(by[l1].complete, by[l1].notes)
        self.assertEqual(by[l1].lever_primary, sim210.PRIMARY)
        self.assertEqual(by[self.nav].kind, sources.NAVIGATION)
        self.assertEqual([os.path.basename(p) for p in by[self.nav].anpp], ["nav_%s_0001.anpp" % self.day])
        self.assertEqual(by[l1].outputs, [sources.BEAMWORX, sources.IE, sources.QINERTIA])
        # DCS Core groups the lines by what the files record
        files = dclraw.find_clraw([self.ddir])
        self.assertEqual(len(dclraw.group_recordings(dclraw.split_navigation(files)[0])), 2)

    def test_without_the_clraw_the_logs_are_still_the_line(self):
        for n in os.listdir(os.path.join(self.ddir, "CLRAW")):
            if n.startswith(self.day + "_002"):
                os.remove(os.path.join(self.ddir, "CLRAW", n))
        by = {i.name: i for i in sources.scan([self.pdir])}
        it = by["Deck_Test_%s_002" % self.day]
        self.assertEqual(it.kind, sources.LOG)
        self.assertEqual([os.path.basename(p) for p in it.anpp], ["ins_%s_0002.anpp" % self.day])
        self.assertEqual(it.lever_primary, sim210.PRIMARY)

    def test_makes_inertial_files(self):
        by = {i.name: i for i in sources.scan([self.pdir])}
        it = by["Deck_Test_%s_001" % self.day]
        out = os.path.join(self.tmp, "out")
        r = make.run(it, [sources.IE, sources.QINERTIA], out)
        self.assertEqual(r[sources.IE]["state"], "made")
        self.assertTrue(any(f.endswith(".imr") for f in r[sources.IE]["files"]))
        self.assertTrue(any(f.endswith(".nav") for f in r[sources.IE]["files"]))


if __name__ == "__main__":
    unittest.main()
