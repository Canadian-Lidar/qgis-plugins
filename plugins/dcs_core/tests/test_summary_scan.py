"""DCS Core 1.19.4 (audit 23 Nov 2026): a finished .clraw is opened from its summary (chunk / attachment / metadata
indexes at the end), attachments are read only when asked for; unfinished files are read front to back as before.
Same chunks, times, channels, metadata, attachments and chunk data either way; a damaged chunk still fails its CRC.

    python3 -m pytest dcs_core/tests/test_summary_scan.py      (from the plugins folder)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "RemixPro", "tests"))
import mapper_clraw as W  # noqa: E402  (the mapper's own .clraw writer)
from dcs_core import mcap  # noqa: E402

NS = 1_000_000_000


def write(path, n=4000):
    big = os.urandom(300_000)
    w = W.Writer(path, {"format": "clraw", "format_version": "1", "line": 3},
                 [("hardware.json", "application/json", b'{"a": 1}'), ("big.bin", "application/octet-stream", big)])
    t0 = 1_790_000_000 * NS
    for i in range(n):
        w.add(2, t0 + i * 10_000_000, t0 + i * 10_000_000, os.urandom(600))
        if i % 500 == 499:
            w.flush_chunk(sync=False)
    w.close()
    return big


def full(path):
    m = mcap.McapFile(path)
    m._scan_summary = lambda: False
    return m.scan()


def test_summary_equals_full_scan(tmp_path):
    p = str(tmp_path / "a.clraw")
    big = write(p)
    a = mcap.McapFile(p)
    assert a._scan_summary()                       # a finished file has the summary
    b = full(p)
    assert [c[:4] + c[5:] for c in a.chunk_pos] == [c[:4] + c[5:] for c in b.chunk_pos] and len(a.chunk_pos) > 3
    assert a.channels == b.channels and a.metadata == b.metadata and a.profile == b.profile
    assert sorted(a.attachments.keys()) == ["big.bin", "hardware.json"]
    assert dict.get(a.attachments, "big.bin") is None          # not read yet
    assert a.attachments["big.bin"] == big and dict(a.attachments.items()) == dict(b.attachments.items())
    with open(p, "rb") as f:
        assert all(a.read_chunk(f, i) == b.read_chunk(f, i) for i in range(len(a.chunk_pos)))
    assert a.bad_chunks == 0


def test_unfinished_and_damaged(tmp_path):
    p = str(tmp_path / "a.clraw")
    write(p)
    with open(p, "rb") as f:
        data = f.read()
    cut = str(tmp_path / "cut.clraw")
    with open(cut, "wb") as f:
        f.write(data[:len(data) * 2 // 3])
    m = mcap.McapFile(cut).scan()                  # no footer: front to back, up to the cut
    assert m.truncated and m.chunk_pos and m.attachments["hardware.json"] == b'{"a": 1}'
    a = mcap.McapFile(p).scan()
    i = len(a.chunk_pos) // 2
    off = a.chunk_pos[i][0] + a.chunk_pos[i][1] // 2
    bad = bytearray(data)
    bad[off] ^= 0xFF
    q = str(tmp_path / "bad.clraw")
    with open(q, "wb") as f:
        f.write(bad)
    m = mcap.McapFile(q).scan()
    with open(q, "rb") as f:
        assert m.read_chunk(f, i) is None and m.bad_chunks == 1
