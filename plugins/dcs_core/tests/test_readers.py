"""Headless tests of DCS Core's shared readers (no QGIS needed).

Run from the folder that holds dcs_core/:   python3 -m pytest dcs_core/tests/test_readers.py
Needs numpy and the zstandard package (pip install zstandard).
"""
import json
import os
import struct
import sys
import zlib

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from dcs_core import anpp, clraw, hesai, las, mcap, sbet, zstd  # noqa: E402

zstandard = pytest.importorskip("zstandard")
T0 = 1792508400.0          # 2026-10-20 15:00 UTC


# ----------------------------------------------------------------------------- a tiny .clraw by hand
def _s(x):
    b = x.encode()
    return struct.pack("<I", len(b)) + b


def _map(d):
    body = b"".join(_s(k) + _s(v) for k, v in d.items())
    return struct.pack("<I", len(body)) + body


def _rec(op, body):
    return struct.pack("<BQ", op, len(body)) + body


def _channel(cid, topic):
    return _rec(0x04, struct.pack("<HH", cid, cid) + _s(topic) + _s("x") + _map({}))


def _attachment(name, data):
    body = struct.pack("<QQ", 0, 0) + _s(name) + _s("text/plain") + struct.pack("<Q", len(data)) + data
    return _rec(0x09, body + struct.pack("<I", zlib.crc32(body) & 0xFFFFFFFF))


def _message(cid, seq, t_ns, data):
    return _rec(0x05, struct.pack("<HIQQ", cid, seq, t_ns, t_ns) + data)


def _chunk(records, t0, t1):
    comp = zstandard.ZstdCompressor().compress(records)
    body = (struct.pack("<QQQI", t0, t1, len(records), zlib.crc32(records) & 0xFFFFFFFF) + _s("zstd")
            + struct.pack("<Q", len(comp)) + comp)
    return _rec(0x06, body)


def hesai_packet(t, az_deg, dist_raw, dual=False, inten=50):
    """One 1080-byte XT-32 packet: every channel of every block at dist_raw (4 mm units)."""
    p = bytearray(1080)
    p[6], p[7], p[9], p[10] = 32, 8, 4, 2 if dual else 1
    p[1062] = 0x39 if dual else 0x37
    for b in range(8):
        o = 12 + 130 * b
        struct.pack_into("<H", p, o, int(round(az_deg * 100)) % 36000)
        for k in range(32):
            struct.pack_into("<HB", p, o + 2 + 4 * k, dist_raw + (b % 2 if dual else 0), inten)
    import datetime as dt
    d = dt.datetime.fromtimestamp(int(t), dt.timezone.utc)
    p[1065:1071] = bytes([d.year - 1900, d.month, d.day, d.hour, d.minute, d.second])
    struct.pack_into("<I", p, 1071, int(round((t - int(t)) * 1e6)))
    return bytes(p)


def anpp20(t, lat=0.8, lon=-1.15, h=20.0):
    fs = (7 << 4) | 0b1010                  # filter status: RTK fixed, navigation and time initialised
    pl = struct.pack("<HHII3d3f3ff3f3f3f", 0, fs, int(t), int(round((t - int(t)) * 1e6)), lat, lon, h,
                     *([0.0] * 3), *([0.0] * 3), 9.8, 0.01, 0.02, 1.5, *([0.0] * 3), 0.02, 0.02, 0.03)
    hdr = bytes([0, 20, len(pl), 0, 0])
    lrc = (-(hdr[1] + hdr[2] + hdr[3] + hdr[4])) & 0xFF
    return bytes([lrc]) + hdr[1:] + pl


CAL = "Laser id,Elevation,Azimuth\n" + "\n".join("%d,%.3f,%.3f" % (i + 1, 15 - i, 0.5) for i in range(32))


def make_clraw(path, channels_in_chunk=False, fmt="1"):
    chans = b"".join(_channel(c, t) for t, c in clraw.TOPICS.items())
    out = mcap.MAGIC + _rec(0x01, _s("clraw") + _s("mapper"))
    out += _rec(0x0C, _s("clraw") + _map({"format_version": fmt, "line": "7", "file_number": "1", "capture": "C1"}))
    out += _attachment("hardware.json", json.dumps({"ins_to_lidar_m": [0.1, 0.2, -0.3]}).encode())
    if not channels_in_chunk:
        out += chans
    recs = chans if channels_in_chunk else b""
    seq = 0
    for i in range(10):
        t = T0 + i * 0.01
        seq += 1
        recs += _message(1, seq, int(t * 1e9), hesai_packet(t, i * 3.6, 2500, dual=(i % 2 == 1)))
        recs += _message(3, seq, int(t * 1e9), anpp20(t))
    recs += _message(4, 1, int(T0 * 1e9), json.dumps({"event": "clraw_start", "t": T0}).encode())
    recs += _message(4, 2, int((T0 + 0.1) * 1e9), json.dumps({"event": "mark", "t": T0 + 0.05}).encode())
    out += _chunk(recs, int(T0 * 1e9), int((T0 + 0.1) * 1e9))
    out += _attachment("lidar_calibration.csv", CAL.encode())        # arrives between chunks
    out += _chunk(_message(5, 1, int((T0 + 0.2) * 1e9), b'{"t": 1}'), int((T0 + 0.2) * 1e9), int((T0 + 0.2) * 1e9))
    out += _rec(0x0F, struct.pack("<I", 0)) + _rec(0x02, struct.pack("<QQI", 0, 0, 0)) + mcap.MAGIC
    with open(path, "wb") as f:
        f.write(out)
    return path


# ----------------------------------------------------------------------------- tests
@pytest.mark.parametrize("inside", [False, True])
def test_clraw_file_and_recording(tmp_path, inside):
    p = make_clraw(str(tmp_path / "007_20261020_150000_0001.clraw"), channels_in_chunk=inside)
    cf = clraw.ClrawFile(p)
    assert cf.line == 7 and cf.file_number == 1 and cf.hardware["ins_to_lidar_m"][2] == -0.3
    assert cf.channels[1] == "/lidar/points"
    cal = cf.calibration()
    assert len(cal) == 32 and cal[0] == (15.0, 0.5)
    n = {}
    for ch, _lt, _pt, _d in cf.iter_messages():
        n[ch] = n.get(ch, 0) + 1
    assert n == {clraw.CH_POINTS: 10, clraw.CH_ANPP: 10, clraw.CH_EVENTS: 2, clraw.CH_QUALITY: 1}
    assert [e["event"] for e in cf.events()] == ["clraw_start", "mark"]
    assert cf.bad_chunks == 0
    rec = clraw.Recording([p])
    assert rec.calib_source == "file" and rec.calib.shape == (32, 2)
    assert rec.recording_window()[0] == T0 and len(rec.marks()) == 1
    P = np.concatenate([rec.read_lidar_chunk({}, fi, ci) for fi, ci in rec.chunk_list()])
    assert P.shape == (10, 1080)
    st, t = rec.nav_states()
    assert len(t) == 10 and abs(st["h"][0] - 20.0) < 1e-9
    nav = anpp.NavCollector()
    for ch, _lt, pt, d in cf.iter_messages({clraw.CH_ANPP}):
        nav.add(d, pt)
    a = nav.arrays()
    assert len(a["t"]) == 10 and a["fix"][0] == 7 and a["nav_init"][0] == 1 and np.allclose(a["t"], t)


def test_lines_and_groups(tmp_path):
    a = make_clraw(str(tmp_path / "007_20261020_150000_0001.clraw"))
    b = make_clraw(str(tmp_path / "007_20261020_150100_0002.clraw"))
    assert clraw.find_clraw([str(tmp_path)]) == [a, b]
    assert len(clraw.group_lines([a, b])) == 1
    assert list(clraw.group_recordings([a, b]).values()) == [[a, b]]


def test_newer_format_is_refused(tmp_path):
    p = make_clraw(str(tmp_path / "x.clraw"), fmt="2")
    with pytest.raises(clraw.ClrawError):
        clraw.ClrawFile(p)
    with pytest.raises(ValueError):
        clraw.Recording([p])


def test_missing_decoder_is_an_error_not_damage(tmp_path, monkeypatch):
    p = make_clraw(str(tmp_path / "x.clraw"))
    cf = clraw.ClrawFile(p)

    def none():
        raise zstd.ZstdUnavailable("no decoder")
    monkeypatch.setattr(zstd, "decoder", none)
    with pytest.raises(ImportError):
        list(cf.iter_chunks())


def test_hesai_geometry_and_forms():
    pk = np.frombuffer(hesai_packet(T0, 90.0, 2500) + hesai_packet(T0 + 0.01, 90.0, 2500, dual=True),
                       np.uint8).reshape(2, 1080)
    cal = np.array([(0.0, 0.0)] * 32)
    d = hesai.decode_packets(pk, cal, returns="both")
    assert np.allclose(d["range"], 10.0, atol=0.0041)             # 2500 x 4 mm; strongest 1 unit more
    assert np.allclose(d["x"][d["ret"] == 2], 10.0, atol=1e-9)  # azimuth 90 deg, elevation 0 -> +x
    assert set(np.unique(d["ret"])) == {0, 1, 2}
    t1 = hesai.packet_times(pk)
    assert abs(t1[0] - T0) < 1e-6
    a = hesai.decode(pk, t1, cal, "strongest", 1.0, 80.0)
    c = hesai.decode_hesai(pk, cal, 1.0, 80.0, "strongest", False)
    assert np.array_equal(a["t"], c["t"]) and np.allclose(a["p"][:, 0], c["x"])
    assert len(a["t"]) == 8 * 32 + 4 * 32                       # single: 8 blocks; dual: the 4 strongest
    dd = hesai.decode_hesai(pk, cal, returns="both", drop_duplicates=True)
    assert len(dd["t"]) == 8 * 32 + 4 * 32                      # dual pairs 1 apart count as duplicates
    assert hesai.header_info(pk)["dist_unit_mm"] == 4


def test_sbet_week_rollover_and_smrmsg(tmp_path):
    ws = sbet.week_start_for(T0)
    t = ws + sbet.WEEK - 18 - 5 + np.arange(0, 10, 0.5)        # crosses into the next GPS week
    n = len(t)
    p = str(tmp_path / "job_sbet_20261020.out")
    sbet.write(p, t, np.full(n, 0.8), np.full(n, -1.1), np.full(n, 20.0), np.zeros(n), np.zeros(n),
               np.linspace(0, 1, n), start=ws)
    raw = np.fromfile(p, sbet.SBET)
    raw["t"] = np.where(raw["t"] >= sbet.WEEK, raw["t"] - sbet.WEEK, raw["t"])   # as a receiver writes it
    raw.tofile(p)
    tt, a, w = sbet.read(p, start=ws)
    assert w == ws and np.allclose(tt, t)
    tt2, _a, _w = sbet.read(p, near_unix=float(t[0]) + 2 * 86400)
    assert np.allclose(tt2, t)
    m = np.zeros(n, sbet.SMRMSG)
    m["t"], m["sd_n"] = raw["t"], 0.02
    m.tofile(str(tmp_path / "job_smrmsg_20261020.out"))
    sp = sbet.smrmsg_path(p)
    ts, sm = sbet.read_smrmsg(sp, ws)
    assert np.allclose(ts, t) and np.allclose(sm["sd_n"], 0.02)


def test_las_round_trip(tmp_path):
    p = str(tmp_path / "x.las")
    n = 100
    E, N, Z = np.linspace(1e5, 1e5 + 10, n), np.linspace(5e6, 5e6 + 10, n), np.linspace(10, 20, n)
    with las.LasWriter(p, (1e5, 5e6, 0), wkt="PROJCS[\"x\"]") as w:
        w.write(E, N, Z, np.full(n, T0), np.full(n, 10), np.zeros(n, int), np.full(n, 2, np.uint8),
                np.zeros(n, np.uint8), np.zeros(n, int), withheld=np.arange(n) % 2 == 0)
    r = las.read_las(p)
    assert np.allclose(r["x"], E, atol=1e-3) and int((r["raw"]["flags"] == 4).sum()) == 50
    las.write_las(str(tmp_path / "y.las"), E, N, Z, np.zeros(n), np.zeros(n), np.zeros(n), epsg=2953)
    assert os.path.getsize(str(tmp_path / "y.las")) == 227 + 54 + 32 + 28 * n     # header, GeoKey VLR, points
