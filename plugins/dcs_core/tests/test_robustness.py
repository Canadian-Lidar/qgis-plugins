"""DCS Core 1.8.0: locks, project writes, staleness of survey lines, damaged .clraw, profiles, decoders (no Qt).

Run from the plugins folder:   python3 -m pytest dcs_core/tests/test_robustness.py   (needs zstandard)
"""
import json
import multiprocessing as mp
import os
import struct
import sys
import threading

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dcs_core import anpp, clraw, files, handover, las, locks, mcap, profiles, project, timeutil  # noqa: E402

pytest.importorskip("zstandard")
import test_readers as R  # noqa: E402


# ----------------------------------------------------------------------------- locks and project writes
def _writer(path, key, n, q):
    p = project.DcsProject.open(path)
    ok = 0
    for i in range(n):
        p.modify(lambda d, i=i: d["stages"].__setitem__("%s_%d" % (key, i), 1))
        ok += 1
    q.put(ok)


def test_concurrent_project_writes_lose_nothing(tmp_path):
    p = project.DcsProject.create(str(tmp_path), "R")
    q = mp.get_context("spawn").Queue()
    ps = [mp.get_context("spawn").Process(target=_writer, args=(p.path, k, 40, q)) for k in "ABC"]
    for x in ps:
        x.start()
    for x in ps:
        x.join(120)
    assert sum(q.get(timeout=5) for _ in ps) == 120
    assert len(project.DcsProject.open(p.path).data["stages"]) == 120


def test_threads_of_one_process_are_kept_apart(tmp_path):
    p = project.DcsProject.create(str(tmp_path), "T")

    def work(k):
        q = project.DcsProject.open(p.path)
        for i in range(30):
            q.modify(lambda d, i=i: d["stages"].__setitem__("%s_%d" % (k, i), 1))
    ts = [threading.Thread(target=work, args=(k,)) for k in "ABCD"]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(project.DcsProject.open(p.path).data["stages"]) == 120


def test_lock_is_exclusive_reentrant_and_takes_over_dead_locks(tmp_path):
    d = str(tmp_path)
    a = locks.Lock(d, "x", "A").acquire()
    b = locks.Lock(d, "x", "B").acquire()               # same process: nested
    assert a.mine and not b.mine
    b.release()
    assert os.path.exists(a.path)
    a.release()
    assert not os.path.exists(a.path)
    # a lock of a process that is gone (same host) is taken over
    with open(os.path.join(d, ".lock"), "w") as f:
        json.dump({"stage": "x", "plugin": "old", "host": __import__("socket").gethostname(), "pid": 999999999,
                   "time": __import__("time").time()}, f)
    c = locks.Lock(d, "x", "C").acquire()
    assert c.mine and c.taken_over["plugin"] == "old"
    # release removes only our own lock
    with open(c.path, "w") as f:
        json.dump({"token": "someone-else", "host": "other", "pid": 1, "time": __import__("time").time()}, f)
    c.release()
    assert os.path.exists(c.path)
    with pytest.raises(locks.Locked):                   # another host's fresh lock is live
        locks.Lock(d, "x", "D", wait=0.2).acquire()


def test_failed_run_start_releases_the_stage_lock(tmp_path, monkeypatch):
    p = project.DcsProject.create(str(tmp_path), "S")

    def boom(*a, **k):
        raise locks.Locked({"stage": "project file"})
    monkeypatch.setattr(p, "update_stage", boom)
    with pytest.raises(locks.Locked):
        with p.run("pospro", "PosPro"):
            pass
    assert not os.path.exists(os.path.join(p.folder, "01_PosPro", ".lock"))


def test_damaged_project_file_is_never_overwritten_with_memory(tmp_path):
    p = project.DcsProject.create(str(tmp_path), "B")       # p's memory: no shared facts
    other = project.DcsProject.open(p.path)
    other.set_shared("crs", {"h": 1}, "PosPro")
    other.set_shared("vehicle", {"type": "van"}, "PosPro")  # .bak now holds crs
    with open(p.path, "w") as f:
        f.write("{ damaged")
    p.set_shared("antennas", [1, 2], "AlignPro")
    d = project.DcsProject.open(p.path).data["shared"]
    assert "antennas" in d and "crs" in d                  # rebuilt from the .bak, not from p's memory
    assert "crs" in files.read_json(p.path + ".bak")["shared"]     # the good .bak was kept


def test_safe_name_reserved():
    assert project.safe_name("CON") == "_CON" and project.safe_name("nul.txt") == "_nul.txt"


# ----------------------------------------------------------------------------- fingerprints
def test_fingerprint_small_files_hashed_whole_and_old_ones_accepted(tmp_path):
    f = str(tmp_path / "a.bin")
    data = bytearray(os.urandom(1_500_000))
    open(f, "wb").write(data)
    old = files._fingerprint_v1(f, len(data))
    new = files.fingerprint(f)
    assert new.startswith("fp2:") and files.fingerprint_matches(f, old) and files.fingerprint_matches(f, new)
    data[1_200_000] ^= 0xFF
    open(f, "wb").write(data)
    assert not files.fingerprint_matches(f, new)
    big = str(tmp_path / "b.bin")
    open(big, "wb").write(os.urandom(3 * files.MB))
    assert files.fingerprint(big).startswith("fp:")       # unchanged for big files


# ----------------------------------------------------------------------------- survey lines in built_from
def test_survey_lines_make_alignpro_stale_and_old_projects_do_not(tmp_path):
    pj, lines, old, nav = R.make_210(str(tmp_path))
    dcs = project.DcsProject.create(pj, "P")
    dcs.set_capture_files([nav], "PosPro")
    od = handover.out_dir(dcs, "pospro")
    sb = os.path.join(od, "trajectory_sbet.out")
    open(sb, "wb").write(b"x" * 136)
    t = handover.publish(dcs, "pospro", {"sbet": sb}, {}, "PosPro")
    rep = os.path.join(handover.out_dir(dcs, "alignpro"), "alignment_report.txt")
    open(rep, "w").write("r")
    doc = handover.publish(dcs, "alignpro", {"report": rep}, {}, "AlignPro", used={"pospro": t["id"]},
                           inputs=lines)
    assert set(doc["built_from"]["inputs"]) == {files.portable(f, dcs.job_folder) for f in lines}
    assert doc["built_from"]["stages"] == {"pospro": t["id"]}
    assert dcs.state("alignpro") == ("done", [])
    # a new survey line appears
    R.make_clraw(os.path.join(os.path.dirname(lines[0]), "Deck_Test_L001_20261104_124512_0003.clraw"),
                 meta={"line": 1, "capture": "Deck_Test_L001_20261104_124512", "file_number": 3})
    dcs._survey_cache = None
    st, why = dcs.state("alignpro")
    assert st == "stale" and any("survey line added" in w for w in why)
    # a used survey line changes
    with open(lines[0], "ab") as f:
        f.write(b"\0")
    dcs._survey_cache = None
    st, why = dcs.state("alignpro")
    assert any("survey line changed" in w for w in why)
    # an older project (built_from without inputs / survey) is not made stale by this
    def strip(d):
        bf = d["stages"]["alignpro"]["built_from"]
        for k in ("inputs", "inputs_stat", "survey"):
            bf.pop(k, None)
    dcs.modify(strip)
    assert dcs.state("alignpro") == ("done", [])


def test_used_ids_are_recorded(tmp_path):
    dcs = project.DcsProject.create(str(tmp_path), "U")
    dcs.update_stage("pospro", status="done", handover_id="sha256:new", handover="x")
    assert dcs.built_from_now("alignpro")["stages"] == {"pospro": "sha256:new"}
    assert dcs.built_from_now("alignpro", used={"pospro": "sha256:old"})["stages"] == {"pospro": "sha256:old"}


# ----------------------------------------------------------------------------- damaged .clraw
def test_truncated_and_corrupt_clraw_never_raise_unexpectedly(tmp_path):
    p = R.make_clraw(str(tmp_path / "007_20261020_150000_0001.clraw"))
    body = open(p, "rb").read()
    q = str(tmp_path / "t_0001.clraw")
    for n in range(8, len(body), 7):
        open(q, "wb").write(body[:n])
        clraw._meta_cache.clear()
        clraw.file_info(q)
        try:
            cf = clraw.ClrawFile(q)
            list(cf.iter_chunks())
        except clraw.ClrawError:
            pass
    open(q, "wb").write(mcap.MAGIC + struct.pack("<BQ", 0x03, 1 << 62) + b"x" * 20)    # absurd record length
    clraw._meta_cache.clear()
    assert clraw.file_info(q)["kind"] == "line"
    m = mcap.McapFile(q).scan()
    assert m.truncated


def test_identity_of_files_reads_the_recording(tmp_path):
    p = R.make_clraw(str(tmp_path / "a_0001.clraw"))
    body = open(p, "rb").read()
    hw = json.dumps({"certus_reported": {"device": {"serial": "C123"}}}).encode()
    ses = json.dumps({"unit": {"serial": "NC100", "name": "mapper-1"}}).encode()
    old = R._attachment("hardware.json", json.dumps({"ins_to_lidar_m": [0.1, 0.2, -0.3]}).encode())
    assert body.count(old) == 1
    new = body.replace(old, R._attachment("hardware.json", hw) + R._attachment("session.json", ses))
    open(p, "wb").write(new)
    assert profiles.identity_of_files([p]) == {"unit_serial": "NC100", "unit_name": "mapper-1",
                                               "certus_serial": "C123"}


# ----------------------------------------------------------------------------- profiles
def test_profiles_merge_concurrent_sections_and_keep_names_apart(tmp_path):
    profiles.set_folder(str(tmp_path / "prof"))
    profiles.save_sections("Truck", {"antennas": {"gnss1_m": [1, 2, 3]}}, by="PosPro", system={"unit_serial": "U1"})
    profiles.save_sections("Truck", {"lidar": {"lever_m": [0, 0, 0]}}, by="AlignPro")
    p = profiles.load("Truck")
    assert sorted(p["sections"]) == ["antennas", "lidar"] and p["system"]["unit_serial"] == "U1"
    profiles.save(profiles.new("Van/1"))
    profiles.save(profiles.new("Van_1"))
    profiles.save(profiles.new("van_1"))
    assert sorted(x["name"] for x in profiles.list_profiles()) == ["Truck", "Van/1", "Van_1", "van_1"]

    def worker(k):
        for i in range(5):
            profiles.save_sections("Busy", {k: {"n": i}}, by=k)
    ts = [threading.Thread(target=worker, args=(k,)) for k in ("antennas", "rear_axle", "imu", "vehicle")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(profiles.load("Busy")["sections"]) == ["antennas", "imu", "rear_axle", "vehicle"]
    profiles.set_folder(None)


# ----------------------------------------------------------------------------- decoders, time, LAS
def test_nav_states_and_collector_drop_bad_crc():
    good = R.anpp20(R.T0)
    bad = bytearray(R.anpp20(R.T0 + 0.01))
    bad[40] ^= 0xFF                              # payload damaged, header intact
    stats = {}
    st, t = anpp.nav_states([good, bytes(bad)], stats)
    assert len(t) == 1 and stats["rejected"] == 1
    c = anpp.NavCollector()
    c.add(good, 0)
    c.add(bytes(bad), 0)
    assert len(c.s20) == 1 and c.rejected == 1


def test_timeutil_leap_at_utc():
    import datetime as dt
    utc = dt.datetime(2017, 1, 1, 0, 0, 5)
    assert timeutil.gps_to_utc(timeutil.utc_to_gps(utc)) == utc
    before = dt.datetime(2016, 12, 31, 23, 59, 50)
    assert timeutil.gps_to_utc(timeutil.utc_to_gps(before)) == before
    assert timeutil.leap_seconds(dt.datetime(1990, 6, 1)) == 6 and timeutil.LEAP == 18
    from dcs_core import sbet
    assert sbet.LEAP == las.LEAP == timeutil.LEAP


def test_las_flags_and_no_partial_file(tmp_path):
    p = str(tmp_path / "a.las")
    with las.LasWriter(p, (0, 0, 0)) as w:
        w.write(np.zeros(3), np.zeros(3), np.zeros(3), np.full(3, R.T0), np.zeros(3), np.zeros(3, int),
                np.zeros(3, np.uint8), np.zeros(3, np.uint8), np.zeros(3, int))
    assert struct.unpack_from("<H", open(p, "rb").read(8), 6)[0] == 0x11
    q = str(tmp_path / "b.las")
    with pytest.raises(RuntimeError):
        with las.LasWriter(q, (0, 0, 0)):
            raise RuntimeError("stop")
    assert not os.path.exists(q)
    r = str(tmp_path / "c.las")
    gt = las.utc_to_adjusted_gps(np.full(2, R.T0))
    las.write_las(r, np.zeros(2), np.zeros(2), np.zeros(2), np.zeros(2), np.zeros(2), gt)
    h = open(r, "rb").read(227)
    assert struct.unpack_from("<H", h, 6)[0] == 1 and struct.unpack_from("<HH", h, 90) == (293, 2026)
