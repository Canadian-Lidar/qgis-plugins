"""Exports split by chainage: sections cut square to a line (a survey line's trajectory, the centerline computed
from the drive lines, or a QGIS line layer), with a width across the line, numbered and named by chainage.

    QT_QPA_PLATFORM=offscreen python3 CloudPro/tests/test_sections.py      (from the plugins folder)
"""
import os
import sys
import tempfile

import numpy as np

PLUGINS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(PLUGINS, "guides"))
sys.path.insert(0, PLUGINS)

from CloudPro.core import sections as SC                          # noqa: E402

# chainage text
assert [SC.chainage_text(v) for v in (0, 100, 1250, 1250.5, 999.96, -40)] == \
    ["0+000", "0+100", "1+250", "1+250.5", "1+000", "-0+040"], [SC.chainage_text(v) for v in (0, 100, 1250, 1250.5,
                                                                                             999.96, -40)]
# stationing on an L-shaped line: 0..100 east, then 100 north
al = SC.Alignment([[0, 0], [100, 0], [100, 100]])
s, o = al.station(np.array([50.0, 103.0, 98.0, -5.0, 120.0, 100.0]), np.array([2.0, 40.0, 60.0, -1.0, 210.0, -3.0]))
assert np.allclose(s, [50, 140, 160, -5, 310, 100]) and np.allclose(o[:2], [2, -3]) and np.isclose(o[2], 2), (s, o)
sec = SC.Sections(al, 30, 1200, width=10)
assert sec.count == 7 and sec.name("S", ".laz", 0) == "S_001_1+200-1+230.laz" and \
    sec.name("S", ".laz", 6, 3) == "S_L004_007_1+380-1+400.laz"
assert list(sec.index(np.array([50.0, 50.0, -9.0]), np.array([4.0, 6.0, 0.0]))) == [1, -1, 0]
print("chainage, stationing (ends extended, offsets left +), section names: OK")

from CloudPro.tests import _job                                    # noqa: E402
from CloudPro.core import chain, importer as IM, export as EX     # noqa: E402
dcs, p = _job.fresh_job("sec")
IM.import_chain(p, chain.resolve(dcs), IM.ImportOptions())
out = tempfile.mkdtemp()
full = EX.export_las(p, os.path.join(out, "all.las"))

from CloudPro.core import laz as LZ                               # noqa: E402
import struct                                                     # noqa: E402


def read(path):
    """E, N of a LAS / LAZ file CloudPro wrote."""
    with open(path, "rb") as f:
        h = f.read(375)
        off, nvlr = struct.unpack_from("<II", h, 96)
        fmt, = struct.unpack_from("<B", h, 104)
        n, = struct.unpack_from("<Q", h, 247)
        sc = struct.unpack_from("<3d", h, 131)
        of = struct.unpack_from("<3d", h, 155)
        f.seek(375)
        rec = None
        for _ in range(nvlr):
            _u, rid, ln = struct.unpack("<2x16sHH32x", f.read(54))
            data = f.read(ln)
            if rid == 22204:
                rec = data
        f.seek(off)
        if fmt & 0x80:
            buf = np.zeros(n * 30, np.uint8)
            LZ.module().LasZipDecompressor(f, rec).decompress_many(buf)
        else:
            buf = np.frombuffer(f.read(n * 30), np.uint8)
    xy = buf.reshape(-1, 30)[:, :8].copy().view("<i4")
    return xy[:, 0] * sc[0] + of[0], xy[:, 1] * sc[1] + of[1]


o = p.store.origin
for along in ("line", "centerline"):
    a = SC.track_alignment(p, p.lines[0]["id"]) if along == "line" else SC.centerline_from_lines(p)
    for width in (0.0, 20.0):
        sec = SC.Sections(a, 50.0, 0.0, width)
        d = os.path.join(out, "%s_%d" % (along, width))
        os.makedirs(d)
        n = EX.export_las(p, os.path.join(d, "S.laz"), tile=sec)
        files = p.last_export_files
        tot = 0
        for f in files:
            k = int(os.path.basename(f).split("_")[1]) - 1
            assert os.path.basename(f) == sec.name("S", ".laz", k)
            E, N = read(f)
            s, off = a.station(E - o[0], N - o[1])
            lo, hi = k * 50.0, (k + 1) * 50.0
            first, last = k == 0, k == sec.count - 1
            inside = ((s >= lo - 1e-3) | first) & ((s < hi + 1e-3) | last)
            assert inside.all(), (f, s.min(), s.max())
            if width:
                assert (np.abs(off) <= width / 2 + 1e-3).all()
            tot += len(E)
        if not width:
            assert tot == n == full, (tot, n, full)
        else:
            assert 0 < n < full
        print("%s (%.0f m): %d sections of 50 m%s, %s points, every point in its section: OK" % (
            a.name, a.length, len(files), ", 20 m wide" if width else " (all points)", "{:,}".format(n)))
    if along == "line":
        n2 = EX.export_xyz(p, os.path.join(out, "S.csv"), tile=SC.Sections(a.reversed(), 100.0, 2000.0))
        assert n2 == full and all(os.path.basename(f).startswith("S_001_2+000-2+100") or "_001_" not in f
                                  for f in p.last_export_files)
        print("XYZ, chainage from the other end starting at 2+000: %d files: OK" % len(p.last_export_files))

# the centerline lies between the drive lines where they overlap (or on the only line)
c = SC.centerline_from_lines(p)
print("centerline from the drive lines: %.0f m" % c.length)

# the dialog: Split into › Sections along a line shows the section settings and example names
import harness                                                    # noqa: E402
app, iface = harness.start(PLUGINS, tempfile.mkdtemp())
from CloudPro.ui.dialogs import ExportDialog                     # noqa: E402
dlg = ExportDialog(None, p.lines, os.path.join(out, "Shirley.las"), None, None, "laz", [("Design CL", "abc")],
                   {"on": True, "along": "centerline", "length": 250, "start": 1000, "width": 40})
dlg._kind()
assert dlg.sections_on() and dlg.along.currentData() == "centerline" and dlg.along.findData("layer:abc") > 0
assert not dlg.sec_len.isHidden() and dlg.sec_name.text().startswith("Shirley_001_1+000-1+250.laz"), \
    dlg.sec_name.text()
assert dlg.section_settings() == {"on": True, "along": "centerline", "length": 250, "start": 1000, "reverse": False,
                                  "width": 40}
dlg.tile.setCurrentIndex(0)
assert dlg.sec_len.isHidden() and not dlg.sections_on()
print("dialog: Split into › Sections along a line, settings remembered, example names: OK")
p.store.close()
print("test_sections: OK")
